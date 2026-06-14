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
    _is_path_safe,
    _is_protected_path,
    _safe_filename,
    _safe_relative_path,
)


class GitToolsMixin:
    async def ide_git_clone(
        self, event: AstrMessageEvent, repo_url: str, branch: str = ""
    ) -> str:
        """从 GitHub 拉取远程仓库到当前沙盒。
        当 AI 需要获取开源项目代码进行分析、修改或参考时使用此工具。
        注意：此功能需要管理员在插件配置中开启 ide_sandbox_allow_git_clone。
        Args:
            repo_url(string): GitHub 仓库地址，如 https://github.com/user/repo.git 或 https://github.com/user/repo
            branch(string, optional): 要克隆的分支名，留空则使用默认分支。
        Returns:
            克隆结果说明，包含仓库目录名。
        """
        if self.cover_only_mode:
            return "⛔ 仅翻唱模式已开启：禁止克隆仓库。"
        sandbox_id = self._get_sandbox_id(event)
        if not await self._check_permission(event):
            return "权限不足。"

        # 总开关检查
        if not self.allow_git_clone:
            return "⛔ GitHub 克隆功能已关闭（管理员可在插件配置中开启 ide_sandbox_allow_git_clone）。"
        if not self._can_use_command_tool(event):
            return "权限不足：GitHub 克隆仅限主人、沙盒管理员或 CMD 管理员使用。"

        # URL 安全校验：只允许 github.com
        url_lower = repo_url.lower().strip()
        if not url_lower.startswith("https://github.com/"):
            return "错误：仅支持克隆 GitHub 仓库（URL 必须以 https://github.com/ 开头）。"

        # 如果配置了 git 加速代理，自动替换 URL
        actual_repo_url = repo_url
        if self.git_mirror and url_lower.startswith("https://github.com/"):
            actual_repo_url = f"{self.git_mirror}{repo_url}"
            logger.info(f"[IdeSandbox] git 克隆已使用代理: {self.git_mirror}")

        # 提取仓库名
        repo_name = repo_url.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        safe_repo = _safe_filename(repo_name)
        if not safe_repo:
            return "错误：无法从 URL 中提取有效的仓库名称。"

        await self._broadcast(event, f"🤖 AI 正在克隆仓库 `{safe_repo}`（来自 {actual_repo_url}）...")

        cwd = self._get_group_sandbox(sandbox_id)
        target_dir = cwd / safe_repo

        # 如果已存在，拒绝覆盖
        if target_dir.exists():
            return f"错误：沙盒中已存在 `{safe_repo}` 目录。如需重新克隆，请先删除该目录。"

        # 构建 git clone 命令（使用 --depth 1 限制大小）
        cmd_parts = ["git", "clone", "--depth", "1"]
        if branch:
            cmd_parts.extend(["--branch", branch])
        cmd_parts.extend([actual_repo_url, str(target_dir)])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=self._build_run_env(),
            )

            # 实时读取 stderr 进度并广播到群聊
            last_broadcast_time = 0
            last_progress = ""
            err_lines = []

            async def read_stderr():
                nonlocal last_broadcast_time, last_progress
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    text = self._decode_process_output(line).rstrip()
                    if not text:
                        continue
                    err_lines.append(text)
                    # 解析进度信息
                    if "receiving objects" in text.lower() or "resolving deltas" in text.lower() or "remote:" in text.lower():
                        # 清理终端控制字符
                        clean = re.sub(r'\r|\x1b\[[0-9;]*m|\x1b\[[0-9;]*K', '', text)
                        if clean and clean != last_progress:
                            last_progress = clean
                            now = asyncio.get_event_loop().time()
                            if now - last_broadcast_time >= 3:  # 每 3 秒广播一次
                                await self._broadcast(event, f"🤖 AI 正在克隆中... {clean[:80]}")
                                last_broadcast_time = now

            # 启动后台读取 stderr
            stderr_task = asyncio.create_task(read_stderr())

            # 等待进程结束（带超时）
            try:
                await asyncio.wait_for(proc.wait(), timeout=120)
            except asyncio.TimeoutError:
                await self._kill_process_tree(proc)
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass
                if await asyncio.to_thread(target_dir.exists):
                    await self._rmtree_quiet(target_dir)
                return "⏱️ 克隆超时（限制 120 秒），已清理未完成的数据。"

            # 等待 stderr 读取完成
            await stderr_task

            # 读取 stdout
            out_bytes = await proc.stdout.read()
            out = self._decode_process_output(out_bytes)
            err = "\n".join(err_lines)

            if proc.returncode != 0:
                return f"克隆失败:\n```\n{err}\n```"

            # 检查仓库大小
            def _dir_size():
                total = 0
                for root, dirs, files in os.walk(target_dir):
                    for f in files:
                        fp = Path(root) / f
                        try:
                            total += fp.stat().st_size
                        except Exception:
                            pass
                return total

            total_size = await asyncio.to_thread(_dir_size)
            size_mb = total_size / (1024 * 1024)
            if size_mb > self.git_clone_limit_mb:
                # 超过限制则删除
                await self._rmtree_quiet(target_dir)
                return f"错误：克隆后的仓库大小为 {size_mb:.1f}MB，超过限制 {self.git_clone_limit_mb}MB，已自动删除。"

            self._record(sandbox_id, "git_clone", f"{repo_url} -> {safe_repo}")
            return (
                f"✅ 已克隆仓库 `{safe_repo}`。\n"
                f"📦 大小: {size_mb:.1f}MB\n"
                f"📁 路径: {safe_repo}/\n"
                f"可用 `ide_list_files` 查看目录结构。"
            )
        except Exception as e:
            if await asyncio.to_thread(target_dir.exists):
                await self._rmtree_quiet(target_dir)
            return f"克隆异常: {e}"

