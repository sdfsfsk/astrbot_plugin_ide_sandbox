from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set

import aiohttp

from astrbot.api import logger
from astrbot.api.message_components import File
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .security import (
    DEFAULT_EXECUTION_WHITELIST,
    SEARCH_SKIP_DIRS,
    TEXT_EXTENSIONS,
    _is_command_safe,
    _is_elevated_command_allowed,
    _is_path_safe,
    _is_protected_path,
    _safe_filename,
    _safe_relative_path,
)


class CommandToolsMixin:
    async def _run_elevated(self, command: str, cwd: Path, env: dict, timeout: int) -> tuple[int, str, str]:
        """使用 PowerShell Start-Process -Verb runAs 以管理员权限执行命令。
        通过临时批处理文件和输出文件交换结果（因为提升后的进程无法直接管道通信）。
        返回 (returncode, stdout+stderr, error_message)
        """
        import uuid
        temp_dir = Path(tempfile.gettempdir())
        uid = uuid.uuid4().hex
        batch_file = temp_dir / f"elevated_{uid}.bat"
        output_file = temp_dir / f"elevated_{uid}.txt"
        flag_file = temp_dir / f"elevated_{uid}.done"

        # 构建批处理脚本（设置UTF-8，执行命令，写入退出码和完成标志）
        safe_cwd = str(cwd).replace('"', '"""')
        batch_content = (
            f'@echo off\n'
            f'chcp 65001 >nul 2>&1\n'
            f'cd /d "{safe_cwd}"\n'
            f'{command} > "{output_file}" 2>&1\n'
            f'echo __EXIT_CODE__=%ERRORLEVEL% >> "{output_file}"\n'
            f'echo done > "{flag_file}"\n'
        )
        batch_file.write_text(batch_content, encoding="utf-8")

        # 用 PowerShell 启动提升权限的 cmd（这会触发 UAC 弹窗）
        ps_cmd = f'Start-Process cmd -ArgumentList \'/c "{batch_file}"\' -Verb runAs -WindowStyle Hidden'
        try:
            proc = await asyncio.create_subprocess_shell(
                f'powershell -Command "{ps_cmd}"',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            for f in (batch_file, output_file, flag_file):
                f.unlink(missing_ok=True)
            return -1, "", "启动提权进程超时：PowerShell 未能及时唤起 UAC。"
        except Exception as e:
            for f in (batch_file, output_file, flag_file):
                f.unlink(missing_ok=True)
            return -1, "", f"启动提权进程失败: {e}"

        # 轮询等待完成标志文件（最多等待 timeout 秒）
        waited = 0.0
        poll_interval = 0.5
        while not flag_file.exists() and waited < timeout:
            await asyncio.sleep(poll_interval)
            waited += poll_interval

        if not flag_file.exists():
            for f in (batch_file, output_file, flag_file):
                f.unlink(missing_ok=True)
            return -1, "", f"提权执行超时（{timeout}秒），可能是 UAC 弹窗未响应或被拒绝"

        # 读取输出
        output = ""
        if output_file.exists():
            try:
                output = self._decode_process_output(output_file.read_bytes())
            except Exception:
                pass

        # 解析退出码
        exit_code = 0
        if "__EXIT_CODE__=" in output:
            for line in output.split("\n"):
                if line.startswith("__EXIT_CODE__="):
                    try:
                        exit_code = int(line.split("=", 1)[1].strip())
                    except Exception:
                        pass
                    break
            output = output.replace(f"__EXIT_CODE__={exit_code}\n", "").replace(f"__EXIT_CODE__={exit_code}", "")

        # 清理临时文件
        for f in (batch_file, output_file, flag_file):
            f.unlink(missing_ok=True)

        return exit_code, output.strip(), ""


    async def ide_execute(
        self,
        event: AstrMessageEvent,
        command: str,
        run_in_background: bool = False,
        description: str = "",
        dry_run: bool = False,
    ) -> str:
        """在沙盒环境中执行一条 shell 命令（如 dir, python, node, git 等）。

        使用场景：
        - 运行脚本、查看目录结构、安装依赖、测试代码。
        - 使用 `&&` 或 `;` 组合多个相关命令（管理员权限下可用 `&&`，普通用户受白名单限制）。
        - 使用 `|` 管道、`>` 重定向进行数据处理（需符合安全策略）。
        - 长耗时任务（如训练、编译、服务启动）可设置 run_in_background=true 后台运行。

        ⚠️ 安全规则：
        - 禁止执行删除、格式化、系统关机、修改系统配置等危险命令。
        - 普通用户只能执行白名单内的命令；CMD 管理员可绕过白名单但仍受基础黑名单保护。
        - 超级管理员拥有更高权限，但仍禁止访问系统关键目录和执行极端危险操作。
        - 所有命令在独立 shell 环境中执行，环境变量不会保留到下一次调用。
        - 如果命令需要 Windows UAC 提权（如 winget source 修改），请使用 ide_execute_elevated。

        Args:
            command(string): 要执行的命令字符串。
            run_in_background(bool, optional): 是否以后台任务运行，默认 False。
                设为 True 时不会等待命令结束，而是返回任务 ID，随后可用 ide_task_output 查询输出。
            description(string, optional): 后台任务描述，run_in_background=true 时建议填写。

        Returns:
            命令的标准输出、标准错误和返回码；
            或后台任务 ID（当 run_in_background=true 时）。
        """
        if self.cover_only_mode:
            return "⛔ 仅翻唱模式已开启：禁止执行任何命令。"
        sandbox_id = self._get_sandbox_id(event)
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以使用命令执行工具。"

        # 总开关检查
        if not self.allow_execution:
            return "⛔ 命令执行功能已关闭（管理员可在插件配置中开启 ide_sandbox_allow_execution）。"
        if not self._can_use_command_tool(event):
            return "权限不足：命令执行仅限主人、沙盒管理员或 CMD 管理员使用。"

        # 安全检查：CMD 管理员绕过白名单，但仍受基础黑名单保护
        is_cmd_admin = self._is_cmd_admin(event)
        whitelist = None if is_cmd_admin else self.execution_whitelist
        is_super = self._is_super_admin(event)
        safe, reason = _is_command_safe(command, whitelist, allow_and=is_super, unrestricted=is_super)
        if not safe:
            return f"⛔ 命令被拒绝: {reason}"
        if run_in_background and not description.strip():
            return "⛔ 后台任务必须提供 description，方便后续 ide_task_list / ide_task_output 识别。"

        cwd = self._get_group_sandbox(sandbox_id)

        # pip install 自动注入国内镜像
        actual_command = command
        if self.pip_mirror and command.lower().startswith("pip install"):
            lower_cmd = command.lower()
            if " -i " not in lower_cmd and " --index-url " not in lower_cmd and " --extra-index-url " not in lower_cmd:
                actual_command = f"{command} --index-url {self.pip_mirror}"
                logger.info(f"[IdeSandbox] pip 命令已注入镜像: {self.pip_mirror}")

        # Maven 自动使用国内镜像
        if self.maven_mirror and command.lower().startswith("mvn "):
            lower_cmd = command.lower()
            if " -s " not in lower_cmd and " --settings " not in lower_cmd:
                self._ensure_java_configs(cwd, need_maven=True)
                actual_command = f"{command} -s maven-settings.xml"
                logger.info(f"[IdeSandbox] mvn 命令已使用镜像配置: {self.maven_mirror}")

        # Gradle 自动使用国内镜像
        if self.gradle_mirror and command.lower().startswith("gradle "):
            lower_cmd = command.lower()
            if " --init-script " not in lower_cmd:
                self._ensure_java_configs(cwd, need_gradle=True)
                actual_command = f"{command} --init-script gradle-init.gradle"
                logger.info(f"[IdeSandbox] gradle 命令已使用镜像配置: {self.gradle_mirror}")

        if dry_run:
            return (
                "🔎 命令预览（未实际执行）：\n"
                f"工作目录: {cwd}\n"
                f"命令: `{actual_command}`\n"
                f"后台运行: {'是' if run_in_background else '否'}\n"
                f"描述: {description.strip() or '(无)'}"
            )

        cmd_display = actual_command[:80] + "..." if len(actual_command) > 80 else actual_command
        super_tag = self._get_super_tag(event, actual_command, always_bypass=True)
        await self._broadcast(event, f"{super_tag}🤖 AI 正在执行命令: `{cmd_display}`...")
        if is_cmd_admin:
            await self._broadcast(event, "👑 CMD 管理员模式：已绕过命令白名单限制")

        run_env = self._build_run_env()

        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                actual_command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=run_env,
            )

            if run_in_background:
                bg = self._start_background_command(
                    actual_command,
                    description.strip(),
                    proc,
                    owner_id=str(event.get_sender_id()).strip(),
                    sandbox_id=sandbox_id,
                )
                self._record(sandbox_id, "execute_bg", command[:100])
                return (
                    f"✅ 后台任务已启动\n"
                    f"🆔 任务 ID: `{bg.task_id}`\n"
                    f"📋 描述: {bg.description}\n"
                    f"📝 命令: `{bg.command[:200]}`\n"
                    f"📄 完整日志: {bg.output_path.resolve() if bg.output_path else '(无)'}\n"
                    f"💡 任务结束会保留日志。使用 ide_task_output(task_id='{bg.task_id}') 查询输出，"
                    f"使用 ide_task_stop(task_id='{bg.task_id}') 停止任务。"
                )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.cmd_timeout,
            )
            out = self._decode_process_output(stdout)
            err = self._decode_process_output(stderr)

            # 截断输出
            out = out[:self.max_output_len]
            err = err[:self.max_output_len]

            lines = []
            if out:
                lines.append(f"🟢 输出:\n```\n{out}\n```")
            if err:
                lines.append(f"🟡 错误:\n```\n{err}\n```")
            if not out and not err:
                lines.append("✅ 命令执行成功（无输出）。")
            lines.append(f"🔢 返回码: {proc.returncode}")

            self._record(sandbox_id, "execute", command[:100])
            await self._step_notice(event, f"✅ 命令执行完成，返回码 {proc.returncode}")
            return "\n".join(lines)
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                await self._kill_process_tree(proc)
            await self._step_notice(event, f"⏱️ 命令执行超时（{self.cmd_timeout} 秒）")
            return f"⏱️ 命令执行超时（限制 {self.cmd_timeout} 秒）。"
        except Exception as e:
            await self._step_notice(event, f"❌ 命令执行异常：{e}")
            return f"执行异常: {e}"


    async def ide_task_output(
        self,
        event: AstrMessageEvent,
        task_id: str,
        block: bool = False,
        timeout: int = 30,
    ) -> str:
        """查询后台命令任务的当前输出和状态。

        使用场景：
        - 使用 ide_execute(run_in_background=true) 启动长任务后，定期查询进度。
        - 任务结束后查询最终输出和返回码。

        Args:
            task_id(string): ide_execute 返回的任务 ID。

        Returns:
            任务状态、已收集的输出和错误信息。
        """
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以查看后台任务。"
        bg = self._background_commands.get(task_id)
        if not bg:
            return f"错误：找不到后台任务 `{task_id}`。"
        if not self._can_manage_background_task(event, bg):
            return "权限不足：只有任务创建者、主人、沙盒管理员或 CMD 管理员可以查看该后台任务。"
        retrieval_status = "not_ready" if bg.status == "running" else "success"
        if block and bg.status == "running":
            try:
                await asyncio.wait_for(bg.proc.wait(), timeout=max(0, int(timeout or 0)))
                if bg.proc.returncode is not None:
                    bg.returncode = bg.proc.returncode
                    bg.status = "completed" if bg.proc.returncode == 0 else "failed"
                retrieval_status = "success"
            except asyncio.TimeoutError:
                retrieval_status = "timeout"
        return f"检索状态: {retrieval_status}\n" + self._format_background_output(bg, max_len=self.max_output_len)


    async def ide_task_list(
        self,
        event: AstrMessageEvent,
        active_only: bool = True,
        limit: int = 20,
    ) -> str:
        """列出当前用户可见的后台任务。"""
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以查看后台任务。"
        limit = max(1, min(int(limit or 20), 100))
        rows = []
        for bg in reversed(list(self._background_commands.values())):
            if active_only and bg.status != "running":
                continue
            if not self._can_manage_background_task(event, bg):
                continue
            rows.append(bg)
            if len(rows) >= limit:
                break
        if not rows:
            return "暂无可见后台任务。"
        lines = [f"后台任务列表（{len(rows)} 个）:"]
        for bg in rows:
            lines.append(
                f"- `{bg.task_id}` [{bg.status}] {bg.description} "
                f"(owner={bg.owner_id or 'unknown'}, returncode={bg.returncode})"
            )
        return "\n".join(lines)


    async def ide_task_stop(
        self,
        event: AstrMessageEvent,
        task_id: str,
        reason: str = "Stopped by ide_task_stop",
    ) -> str:
        """停止一个正在运行的后台命令任务。

        使用场景：
        - 长任务不再需要时手动停止。
        - 任务运行异常时强制终止。

        Args:
            task_id(string): ide_execute 返回的任务 ID。

        Returns:
            停止结果。
        """
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以停止后台任务。"
        bg = self._background_commands.get(task_id)
        if not bg:
            return f"错误：找不到后台任务 `{task_id}`。"
        if not self._can_manage_background_task(event, bg):
            return "权限不足：只有任务创建者、主人、沙盒管理员或 CMD 管理员可以停止该后台任务。"
        if getattr(bg, "output_path", None):
            try:
                await asyncio.to_thread(
                    lambda: bg.output_path.open("a", encoding="utf-8", errors="replace").write(
                        f"\n[stop_reason] {reason.strip() or 'Stopped by ide_task_stop'}\n"
                    )
                )
            except Exception:
                pass
        return await self._stop_background_command(task_id)


    async def ide_execute_elevated(self, event: AstrMessageEvent, command: str) -> str:
        """以管理员权限（UAC 提权）执行一条命令。

        使用场景：
        - 仅用于主人处理极少数 Windows 维护操作，目前只允许 winget source 子命令。

        ⚠️ 使用前提：
        1. 必须在 Windows 环境下运行 AstrBot。
        2. AstrBot 有图形界面（远程桌面断开时 UAC 弹窗无法显示，会超时失败）。
        3. 插件配置中开启了 ide_sandbox_allow_elevated。
        4. 执行时会弹出 Windows UAC 对话框，需要有人在电脑前点击"是"。

        Args:
            command(string): 要执行的命令字符串。

        Returns:
            命令的标准输出和标准错误。
        """
        if self.cover_only_mode:
            return "⛔ 仅翻唱模式已开启：禁止执行任何命令。"
        sandbox_id = self._get_sandbox_id(event)
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以使用命令执行工具。"

        if not self.allow_execution:
            return "⛔ 命令执行功能已关闭。"

        if not self.allow_elevated:
            return (
                "⛔ 提权执行功能未开启。\n"
                "管理员需要在插件配置中开启 ide_sandbox_allow_elevated 才能使用此工具。\n"
                "或者主人可以直接右键 AstrBot 启动脚本 → 以管理员身份运行，这样 ide_execute 也能执行管理员命令。"
            )
        if not self._can_use_elevated_command_tool(event):
            return "权限不足：提权执行仅限主人或 AstrBot 全局管理员使用。"

        # 安全检查（与 ide_execute 相同）
        is_cmd_admin = self._is_cmd_admin(event)
        whitelist = None if is_cmd_admin else self.execution_whitelist
        is_super = self._is_super_admin(event)
        safe, reason = _is_command_safe(command, whitelist, allow_and=is_super, unrestricted=is_super)
        if not safe:
            return f"⛔ 命令被拒绝: {reason}"
        elevated_safe, elevated_reason = _is_elevated_command_allowed(command)
        if not elevated_safe:
            return f"⛔ 提权命令被拒绝: {elevated_reason}"

        cmd_display = command[:80] + "..." if len(command) > 80 else command
        await self._broadcast(event, f"🔒 {self._get_super_tag(event, command, always_bypass=True)}🤖 AI 正在以管理员权限执行: `{cmd_display}`...")
        await self._broadcast(event, "⚠️ 请留意 Windows UAC 弹窗，需要点击'是'才能继续nya～")
        if is_cmd_admin:
            await self._broadcast(event, "👑 CMD 管理员模式：已绕过命令白名单限制")

        cwd = self._get_group_sandbox(sandbox_id)

        run_env = self._build_run_env()

        try:
            returncode, output, error = await self._run_elevated(
                command, cwd, run_env, timeout=self.cmd_timeout
            )
            if error:
                return f"⛔ {error}"

            out = output[:self.max_output_len]
            lines = []
            if out:
                lines.append(f"🟢 输出:\n```\n{out}\n```")
            else:
                lines.append("✅ 提权命令执行成功（无输出）。")
            lines.append(f"🔢 返回码: {returncode}")

            self._record(sandbox_id, "execute_elevated", command[:100])
            return "\n".join(lines)
        except Exception as e:
            return f"提权执行异常: {e}"


    async def ide_run_test(
        self, event: AstrMessageEvent, test_path: str = "", test_framework: str = "pytest"
    ) -> str:
        """在沙盒中运行测试。
        当 AI 需要验证代码正确性、运行单元测试时使用此工具。
        支持 pytest 和 unittest 两种框架。
        注意：此功能需要管理员在插件配置中开启 ide_sandbox_allow_test。
        Args:
            test_path(string, optional): 测试文件或目录的路径（相对于沙盒根目录），留空则自动查找测试。
            test_framework(string, optional): 测试框架，可选 'pytest' 或 'unittest'，默认 'pytest'。
        Returns:
            测试结果摘要（通过/失败数量、错误信息）。
        """
        if self.cover_only_mode:
            return "⛔ 仅翻唱模式已开启：禁止运行测试。"
        gid = event.get_group_id()
        if not gid:
            return "错误：该功能仅支持群聊。"
        sandbox_id = self._get_sandbox_id(event)
        if not await self._check_permission(event):
            return "权限不足。"

        # 总开关检查
        if not self.allow_test:
            return "⛔ 测试运行功能已关闭（管理员可在插件配置中开启 ide_sandbox_allow_test）。"
        if not self._can_use_command_tool(event):
            return "权限不足：测试运行仅限主人、沙盒管理员或 CMD 管理员使用。"

        test_display = f"{test_framework} {test_path}" if test_path else f"{test_framework}（自动发现）"
        super_tag = self._get_super_tag(event, test_path, always_bypass=True)
        await self._broadcast(event, f"{super_tag}🤖 AI 正在运行测试（{test_display}）...")

        cwd = self._get_group_sandbox(sandbox_id)

        # 确定测试路径
        if test_path:
            raw_path = Path(test_path)
            if raw_path.is_absolute():
                if not self._is_super_admin(event):
                    return "错误：测试路径不合法（不允许访问沙盒外目录）。"
                target = raw_path.resolve()
                if _is_protected_path(target):
                    return "错误：禁止访问系统保护目录。"
                if not target.exists():
                    return f"错误：测试路径 `{test_path}` 不存在。"
                run_cwd = str(target) if target.is_dir() else str(target.parent)
                test_target = str(target)
            else:
                safe_parts = _safe_relative_path(test_path)
                if not safe_parts:
                    return "错误：测试路径不合法。"
                target = cwd.joinpath(*safe_parts).resolve()
                if not _is_path_safe(cwd, target):
                    return "错误：测试路径不合法（不允许访问沙盒外目录）。"
                if not target.exists():
                    return f"错误：测试路径 `{test_path}` 不存在。"
                run_cwd = str(target) if target.is_dir() else str(target.parent)
                test_target = str(target.relative_to(cwd)) if _is_path_safe(cwd, target) else test_path
        else:
            run_cwd = str(cwd)
            test_target = ""

        # 构建测试命令
        if test_framework.lower() == "pytest":
            cmd = f'pytest "{test_target}" -v --tb=short' if test_target else "pytest -v --tb=short"
        elif test_framework.lower() == "unittest":
            if test_target:
                # 将文件路径转为模块路径
                module_path = test_target.replace("/", ".").replace("\\", ".").replace(".py", "")
                cmd = f"python -m unittest {module_path} -v"
            else:
                cmd = "python -m unittest discover -v"
        else:
            return f"错误：不支持的测试框架 `{test_framework}`，仅支持 pytest 或 unittest。"

        # 命令安全检查：CMD 管理员绕过白名单
        is_cmd_admin = self._is_cmd_admin(event)
        whitelist = None if is_cmd_admin else self.execution_whitelist
        is_super = self._is_super_admin(event)
        safe, reason = _is_command_safe(cmd, whitelist, allow_and=is_super, unrestricted=is_super)
        if not safe:
            return f"⛔ 测试命令被拒绝: {reason}"
        if is_cmd_admin:
            await self._broadcast(event, "👑 CMD 管理员模式：已绕过命令白名单限制")

        run_env = self._build_run_env()

        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=run_cwd,
                env=run_env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.cmd_timeout,
            )
            out = self._decode_process_output(stdout)
            err = self._decode_process_output(stderr)

            out = out[:self.max_output_len]
            err = err[:self.max_output_len]

            lines = []
            if out:
                lines.append(f"🟢 输出:\n```\n{out}\n```")
            if err:
                lines.append(f"🟡 错误:\n```\n{err}\n```")
            if not out and not err:
                lines.append("✅ 测试运行成功（无输出）。")
            lines.append(f"🔢 返回码: {proc.returncode}")

            self._record(sandbox_id, "run_test", f"{test_framework} {test_path}")
            return "\n".join(lines)
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                await self._kill_process_tree(proc)
            return f"⏱️ 测试执行超时（限制 {self.cmd_timeout} 秒）。"
        except Exception as e:
            return f"测试异常: {e}"
