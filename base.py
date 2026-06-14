from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import aiohttp

from astrbot.api import logger
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .security import (
    DEFAULT_EXECUTION_WHITELIST,
    _is_path_safe,
    _is_protected_path,
    _safe_filename,
    _safe_relative_path,
    _strip_current_sandbox_prefix,
)


@dataclasses.dataclass
class _BackgroundCommand:
    """后台命令执行状态。"""

    task_id: str
    description: str
    command: str
    proc: asyncio.subprocess.Process
    owner_id: str = ""
    sandbox_id: str = ""
    output_path: Path | None = None
    stdout_buffer: List[str] = dataclasses.field(default_factory=list)
    stderr_buffer: List[str] = dataclasses.field(default_factory=list)
    start_time: float = dataclasses.field(default_factory=lambda: asyncio.get_event_loop().time())
    status: str = "running"  # running / completed / failed / stopped
    returncode: int | None = None
    error_message: str = ""


class IdeSandboxCore(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        plugin_data_root = Path(__file__).resolve().parents[3] / "data" / "astrbot_plugin_ide_sandbox"
        # 沙盒根目录
        self.sandbox_root = plugin_data_root / "sandboxes"
        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        # 待办事项持久化目录
        self.todos_dir = plugin_data_root / "todos"
        self.todos_dir.mkdir(parents=True, exist_ok=True)
        # 后台命令输出日志目录
        self.background_log_dir = plugin_data_root / "background"
        self.background_log_dir.mkdir(parents=True, exist_ok=True)
        # WebUI 活动日志目录
        self.history_dir = plugin_data_root / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        # 主人 QQ（用于权限判断）
        self.master_qq = str(config.get("master_qq", "")).strip()
        self.global_admins = set()
        try:
            global_cfg = context.get_config()
            self.global_admins = {
                str(x).strip()
                for x in global_cfg.get("admins_id", [])
                if str(x).strip()
            }
        except Exception as e:
            logger.debug(f"[IdeSandbox] 读取 AstrBot 全局管理员失败: {e}")
        # 是否允许普通群员使用（默认 False，仅管理员/主人可用）
        self.allow_members = config.get("ide_sandbox_allow_members", False)
        # 沙盒管理员 QQ 列表（完整权限）
        admins_str = config.get("ide_sandbox_admins", "")
        self.admins = set()
        if admins_str and str(admins_str).strip():
            self.admins = {x.strip() for x in str(admins_str).split(",") if x.strip()}
        # 终端管理员 QQ 列表（文件操作最高权限）
        terminal_admins_str = config.get("ide_sandbox_terminal_admins", "")
        self.terminal_admins = set()
        if terminal_admins_str and str(terminal_admins_str).strip():
            self.terminal_admins = {x.strip() for x in str(terminal_admins_str).split(",") if x.strip()}
        # CMD 管理员 QQ 列表（命令执行最高权限，绕过白名单）
        cmd_admins_str = config.get("ide_sandbox_cmd_admins", "")
        self.cmd_admins = set()
        if cmd_admins_str and str(cmd_admins_str).strip():
            self.cmd_admins = {x.strip() for x in str(cmd_admins_str).split(",") if x.strip()}
        # 是否广播 AI 操作到群聊
        self.broadcast_actions = config.get("ide_sandbox_broadcast_actions", True)
        # 操作广播关闭时，文件写入提示阈值（KB）
        self.status_notice_threshold_kb = max(
            0, int(config.get("ide_sandbox_status_notice_threshold_kb", 100))
        )
        # LLM 长时间生成内容时的群聊心跳提示。放在插件内，避免修改 AstrBot 核心文件。
        self.llm_progress_notice = config.get("ide_sandbox_llm_progress_notice", True)
        self.llm_progress_heartbeat = config.get("ide_sandbox_llm_progress_heartbeat", True)
        self.llm_progress_heartbeat_delay = int(config.get("ide_sandbox_llm_progress_heartbeat_delay", 30))
        self.llm_progress_heartbeat_interval = int(config.get("ide_sandbox_llm_progress_heartbeat_interval", 60))
        self.llm_progress_show_elapsed = config.get("ide_sandbox_llm_progress_show_elapsed", True)
        self.suppress_none_response = config.get("ide_sandbox_suppress_none_response", True)
        self.llm_progress_auto_recall = config.get("ide_sandbox_llm_progress_auto_recall", False)
        self.llm_progress_auto_recall_delay = max(1, int(config.get("ide_sandbox_llm_progress_auto_recall_delay", 3)))
        # 是否允许拉取 GitHub 仓库
        self.allow_git_clone = config.get("ide_sandbox_allow_git_clone", False)
        # 是否允许执行命令
        self.allow_execution = config.get("ide_sandbox_allow_execution", False)
        # 是否启用插件 WebUI
        self.ide_sandbox_webui_enabled = config.get("ide_sandbox_webui_enabled", False)
        # 允许访问 WebUI 的 Dashboard 用户名白名单
        self.ide_sandbox_webui_allowed_users = str(config.get("ide_sandbox_webui_allowed_users", "") or "").strip()
        # 命令执行白名单（逗号分隔）
        whitelist_str = config.get(
            "ide_sandbox_execution_whitelist",
            ",".join(sorted(DEFAULT_EXECUTION_WHITELIST)),
        )
        self.execution_whitelist = set(DEFAULT_EXECUTION_WHITELIST)
        if whitelist_str and str(whitelist_str).strip():
            whitelist_items = {
                x.strip().lower()
                for x in str(whitelist_str).split(",")
                if x.strip()
            }
            if whitelist_items == {"*"}:
                self.execution_whitelist = set()
            else:
                self.execution_whitelist = whitelist_items
        # 是否允许运行测试
        self.allow_test = config.get("ide_sandbox_allow_test", False)
        # GitHub 克隆大小限制（MB）
        self.git_clone_limit_mb = config.get("ide_sandbox_git_clone_limit_mb", 100)
        # 群文件自动下载开关
        self.auto_download = config.get("ide_sandbox_auto_download", False)
        # 群文件自动下载关键字
        auto_download_keywords_str = config.get("ide_sandbox_auto_download_keywords", "")
        self.auto_download_keywords = set()
        if auto_download_keywords_str and str(auto_download_keywords_str).strip():
            self.auto_download_keywords = {x.strip().lower() for x in str(auto_download_keywords_str).split(",") if x.strip()}
        # pip 国内镜像源
        self.pip_mirror = str(config.get("ide_sandbox_pip_mirror", "https://pypi.tuna.tsinghua.edu.cn/simple")).strip()
        # GitHub 克隆加速代理前缀
        self.git_mirror = str(config.get("ide_sandbox_git_mirror", "")).strip()
        # Maven 仓库镜像
        self.maven_mirror = str(config.get("ide_sandbox_maven_mirror", "https://maven.aliyun.com/repository/public")).strip()
        # Gradle 仓库镜像
        self.gradle_mirror = str(config.get("ide_sandbox_gradle_mirror", "https://maven.aliyun.com/repository/public")).strip()
        
        # === 环境配置 ===
        self.custom_env = {}
        try:
            env_str = str(config.get("ide_sandbox_custom_env", "{}")).strip()
            if env_str:
                self.custom_env = json.loads(env_str)
        except Exception as e:
            logger.error(f"[IdeSandbox] 解析 ide_sandbox_custom_env 失败: {e}")
            
        custom_path_str = str(config.get("ide_sandbox_custom_path", "")).strip()
        self.custom_paths = []
        if custom_path_str:
            self.custom_paths = [x.strip() for x in custom_path_str.split(",") if x.strip()]
            
        # 文件大小限制（MB）
        self.max_file_size_mb = config.get("ide_sandbox_max_file_size_mb", 10)
        self.single_write_limit_kb = int(config.get("ide_sandbox_single_write_limit_kb", 256))
        self.single_write_limit_bytes = max(8, self.single_write_limit_kb) * 1024
        
        # 仅翻唱联动模式
        self.cover_only_mode = config.get("ide_sandbox_cover_only_mode", False)
        # 命令执行超时（秒）
        self.cmd_timeout = config.get("ide_sandbox_cmd_timeout", 30)
        # 单次命令最大输出长度
        self.max_output_len = config.get("ide_sandbox_max_output_len", 4000)
        # 沙盒管理员是否可绕过沙盒路径限制
        self.admins_can_bypass = config.get("ide_sandbox_admins_can_bypass", False)
        # 是否允许提权执行（UAC）
        self.allow_elevated = config.get("ide_sandbox_allow_elevated", False)
        
        # 执行历史记录（用于 LLM 上下文）
        self.history: dict[str, List[dict]] = {}  # group_id -> list of actions
        # 待办事项列表（每个群独立）
        self.todos: dict[str, List[dict]] = {}  # group_id -> list of todo items
        self._todo_id_counter: dict[str, int] = {}  # group_id -> next todo id
        # 最近文件变更摘要（每个沙盒独立）
        self.file_changes: dict[str, List[dict]] = {}
        # 后台广播任务。广播不能阻塞 LLM 工具本身，否则协议端回包慢时会卡住写入。
        self._broadcast_tasks: set[asyncio.Task] = set()
        self._background_tasks: set[asyncio.Task] = set()
        self._broadcast_locks: dict[str, asyncio.Lock] = {}
        self._llm_heartbeat_tasks: dict[str, asyncio.Task] = {}
        self._llm_recall_tasks: set[asyncio.Task] = set()
        # 后台命令执行（ide_execute run_in_background）
        self._background_commands: Dict[str, _BackgroundCommand] = {}
        logger.info(f"[IdeSandbox] 插件初始化完成: admins={self.admins}, terminal={self.terminal_admins}, cmd={self.cmd_admins}, allow_members={self.allow_members}")

    def _get_sandbox_id(self, event: AstrMessageEvent) -> str:
        """获取沙盒 ID（群聊为 group_id，私聊为 user_id）"""
        gid = event.get_group_id()
        if gid:
            return f"group_{gid}"
        return f"user_{event.get_sender_id()}"

    def _get_group_sandbox(self, sandbox_id: str) -> Path:
        """获取指定沙盒目录"""
        safe_id = re.sub(r'[^\w-]', '', str(sandbox_id))
        d = self.sandbox_root / safe_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _track_background_task(self, task: asyncio.Task):
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _rmtree_quiet(self, path: Path):
        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)

    def _delete_file_later(self, path: Path, delay: float = 3.0):
        async def _cleanup():
            try:
                await asyncio.sleep(delay)
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except Exception as e:
                logger.debug(f"[IdeSandbox] delayed cleanup skipped: {path}: {e}")

        self._track_background_task(asyncio.create_task(_cleanup()))

    def _ensure_java_configs(
        self,
        sandbox_dir: Path,
        *,
        need_maven: bool = False,
        need_gradle: bool = False,
    ):
        """按需在沙盒目录中生成 Java 构建工具镜像配置文件。"""
        # Maven settings.xml
        if need_maven and self.maven_mirror:
            maven_settings = sandbox_dir / "maven-settings.xml"
            if not maven_settings.exists():
                maven_settings.write_text(
                    f'<?xml version="1.0" encoding="UTF-8"?>\n'
                    f'<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"\n'
                    f'          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
                    f'          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0\n'
                    f'                              http://maven.apache.org/xsd/settings-1.0.0.xsd">\n'
                    f'  <mirrors>\n'
                    f'    <mirror>\n'
                    f'      <id>custom-mirror</id>\n'
                    f'      <name>Custom Mirror</name>\n'
                    f'      <url>{self.maven_mirror}</url>\n'
                    f'      <mirrorOf>central</mirrorOf>\n'
                    f'    </mirror>\n'
                    f'  </mirrors>\n'
                    f'</settings>\n',
                    encoding="utf-8"
                )
        # Gradle init.gradle
        if need_gradle and self.gradle_mirror:
            gradle_init = sandbox_dir / "gradle-init.gradle"
            if not gradle_init.exists():
                gradle_init.write_text(
                    f'allprojects {{\n'
                    f'    repositories {{\n'
                    f'        maven {{ url "{self.gradle_mirror}" }}\n'
                    f'        mavenCentral()\n'
                    f'    }}\n'
                    f'}}\n',
                    encoding="utf-8"
                )

    def _resolve(self, sandbox_id: str, filename: str, allow_bypass: bool = False) -> Optional[Path]:
        """解析文件路径。相对路径限制在沙盒内，超级管理员可传入绝对路径。"""
        base = self._get_group_sandbox(sandbox_id)
        raw_path = Path(filename)
        if raw_path.is_absolute():
            target = raw_path.resolve()
            if _is_path_safe(base, target):
                return target
            if not allow_bypass:
                return None
            # 即使是超级管理员，也禁止访问 BANNED_PATHS（防误操作）
            if _is_protected_path(target):
                return None
            return target

        safe_parts = _safe_relative_path(filename)
        if not safe_parts:
            return None
        safe_parts = _strip_current_sandbox_prefix(safe_parts, sandbox_id)
        if not safe_parts:
            return None
        target = (base.joinpath(*safe_parts)).resolve()
        if not _is_path_safe(base, target):
            return None
        return target

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        """判断是否为主人或 AstrBot 全局管理员。"""
        sender = str(event.get_sender_id()).strip()
        return bool(sender and (sender == self.master_qq or sender in self.global_admins))

    def _can_manage_admins(self, event: AstrMessageEvent) -> bool:
        """只有主人、全局管理员或已有沙盒管理员可管理沙盒管理员。"""
        sender = str(event.get_sender_id()).strip()
        return self._is_owner(event) or sender in self.admins

    def _display_path(self, sandbox_id: str, path: Path) -> str:
        """生成适合群聊展示的文件路径。"""
        sandbox = self._get_group_sandbox(sandbox_id)
        try:
            return str(path.resolve().relative_to(sandbox.resolve())).replace("\\", "/")
        except ValueError:
            return str(path)

    def _line_delta(self, old_text: str, new_text: str) -> tuple[int, int]:
        """按行数估算新增/删除量，匹配 Codex 风格摘要。"""
        old_lines = old_text.splitlines()
        new_lines = new_text.splitlines()
        return max(0, len(new_lines) - len(old_lines)), max(0, len(old_lines) - len(new_lines))

    async def _broadcast_file_change(
        self,
        event: AstrMessageEvent,
        sandbox_id: str,
        action: str,
        path: Path,
        added: int,
        removed: int,
        prefix: str = "",
    ):
        """广播并记录 Codex 风格文件变更摘要。"""
        display = self._display_path(sandbox_id, path)
        self.file_changes.setdefault(sandbox_id, [])
        self.file_changes[sandbox_id].append({
            "time": datetime.now().isoformat(),
            "action": action,
            "path": display,
            "added": added,
            "removed": removed,
        })
        self.file_changes[sandbox_id] = self.file_changes[sandbox_id][-100:]
        await self._broadcast(
            event,
            f"{prefix}{action} `{display}` +{added} -{removed}",
        )

    async def _check_permission(self, event: AstrMessageEvent) -> bool:
        """检查用户是否有基础权限使用沙盒工具"""
        if self.allow_members:
            return True
        sender = str(event.get_sender_id()).strip()
        if self._is_owner(event):
            return True
        if sender in self.admins or sender in self.terminal_admins or sender in self.cmd_admins:
            return True
        # 检查是否为群管理员
        try:
            info = await event.bot.call_action(
                "get_group_member_info",
                group_id=event.get_group_id(),
                user_id=int(sender),
                no_cache=True,
            )
            role = info.get("role", "")
            if role in ("admin", "owner"):
                return True
        except Exception as e:
            logger.debug(f"[IdeSandbox] 获取群成员信息失败: {e}")
        return False

    def _is_terminal_admin(self, event: AstrMessageEvent) -> bool:
        """判断当前用户是否为终端管理员（拥有文件操作最高权限）"""
        sender = str(event.get_sender_id()).strip()
        return self._is_owner(event) or sender in self.admins or sender in self.terminal_admins

    def _is_cmd_admin(self, event: AstrMessageEvent) -> bool:
        """判断当前用户是否为 CMD 管理员（拥有命令执行最高权限，绕过白名单）"""
        sender = str(event.get_sender_id()).strip()
        return self._is_owner(event) or sender in self.admins or sender in self.cmd_admins

    def _can_use_command_tool(self, event: AstrMessageEvent) -> bool:
        """Command-like tools require explicit trusted roles, even when members can use file tools."""
        sender = str(event.get_sender_id()).strip()
        return self._is_owner(event) or sender in self.admins or sender in self.cmd_admins

    def _can_manage_background_task(self, event: AstrMessageEvent, bg: _BackgroundCommand) -> bool:
        """Task owner and command-level admins may inspect/stop a background task."""
        sender = str(event.get_sender_id()).strip()
        return bool(sender and sender == bg.owner_id) or self._can_use_command_tool(event)

    def _can_use_elevated_command_tool(self, event: AstrMessageEvent) -> bool:
        """UAC elevation is reserved for the owner/global admins only."""
        return self._is_owner(event)

    def _is_super_admin(self, event: AstrMessageEvent) -> bool:
        """判断当前用户是否为超级管理员（可绕过沙盒路径限制，访问任意文件）
        主人 QQ 自动拥有此权限。如果开启了 admins_can_bypass 开关，沙盒管理员也拥有此权限。"""
        sender = str(event.get_sender_id()).strip()
        if self._is_owner(event):
            return True
        if self.admins_can_bypass and sender in self.admins:
            return True
        return False

    def _get_super_tag(self, event: AstrMessageEvent, target: str = "", always_bypass: bool = False) -> str:
        """获取超级管理员广播标识标签"""
        if not self._is_super_admin(event):
            return ""
        if always_bypass or (target and Path(target).is_absolute()):
            return "👑 [超级管理员·已绕过沙盒] "
        return "👑 [超级管理员] "

    def _is_empty_message(self, message) -> bool:
        """判断消息是否为空、None 或字符串 'None'，避免往群里发垃圾消息。"""
        if message is None:
            return True
        if not isinstance(message, str):
            message = str(message)
        return not message.strip() or message.strip() == "None"

    async def _send_broadcast_now(self, event: AstrMessageEvent, message: str) -> int | str | None:
        """实际发送广播。必须带超时，避免协议端卡住工具调用。
        返回 OneBot 消息 ID（若可用），用于后续撤回。"""
        if self._is_empty_message(message):
            logger.warning(f"[IdeSandbox] skip broadcast empty/None message")
            return None
        if isinstance(event, AiocqhttpMessageEvent):
            try:
                gid = event.get_group_id()
                # OneBot V11 标准消息段数组，避免纯字符串被协议端错误解析
                msg_segments = [{"type": "text", "data": {"text": message}}]
                if gid:
                    ret = await asyncio.wait_for(
                        event.bot.send_group_msg(
                            group_id=int(gid),
                            message=msg_segments,
                        ),
                        timeout=1.5,
                    )
                    return ret.get("message_id") if isinstance(ret, dict) else None
                uid = event.get_sender_id()
                if uid:
                    ret = await asyncio.wait_for(
                        event.bot.send_private_msg(
                            user_id=int(uid),
                            message=msg_segments,
                        ),
                        timeout=1.5,
                    )
                    return ret.get("message_id") if isinstance(ret, dict) else None
            except Exception as e:
                logger.warning(
                    f"[IdeSandbox] OneBot direct broadcast failed, fallback to event.send: {e}"
                )
        try:
            await asyncio.wait_for(event.send(event.plain_result(message)), timeout=1.5)
        except Exception as e:
            logger.warning(f"[IdeSandbox] broadcast failed: {e}")
        return None

    async def _recall_message(self, event: AstrMessageEvent, message_id: int | str):
        """撤回指定消息。忽略失败，避免影响主流程。"""
        if not message_id or not isinstance(event, AiocqhttpMessageEvent):
            return
        try:
            await asyncio.wait_for(
                event.bot.call_action("delete_msg", message_id=int(message_id)),
                timeout=3.0,
            )
            logger.debug(f"[IdeSandbox] recalled message {message_id}")
        except Exception as e:
            logger.debug(f"[IdeSandbox] recall message {message_id} failed: {e}")

    async def _broadcast(self, event: AstrMessageEvent, message: str):
        """将 AI 操作状态广播到群聊（如果配置开启）。调用方不等待发送完成。"""
        if not self.broadcast_actions:
            return
        if self._is_empty_message(message):
            return

        async def _ordered_send():
            sandbox_id = self._get_sandbox_id(event)
            lock = self._broadcast_locks.setdefault(sandbox_id, asyncio.Lock())
            async with lock:
                await self._send_broadcast_now(event, message)
                await asyncio.sleep(0.35)

        task = asyncio.create_task(_ordered_send())
        self._broadcast_tasks.add(task)
        task.add_done_callback(self._broadcast_tasks.discard)
        await asyncio.sleep(0)

    async def _status_notice(self, event: AstrMessageEvent, message: str):
        """发送轻量状态提示，不受操作广播开关影响。调用方不等待发送完成。
        若开启等待提示自动撤回，且消息为“等待 IDE 工具响应中...”类提示，则延迟撤回。"""
        if self._is_empty_message(message):
            return

        async def _ordered_send():
            sandbox_id = self._get_sandbox_id(event)
            lock = self._broadcast_locks.setdefault(sandbox_id, asyncio.Lock())
            async with lock:
                message_id = await self._send_broadcast_now(event, message)
                if (
                    message_id
                    and self.llm_progress_auto_recall
                    and "等待 IDE 工具响应中" in message
                ):
                    delay = max(1, int(self.llm_progress_auto_recall_delay))
                    recall_task = asyncio.create_task(
                        self._delayed_recall(event, message_id, delay)
                    )
                    self._llm_recall_tasks.add(recall_task)
                    recall_task.add_done_callback(self._llm_recall_tasks.discard)
                await asyncio.sleep(0.35)

        task = asyncio.create_task(_ordered_send())
        self._broadcast_tasks.add(task)
        task.add_done_callback(self._broadcast_tasks.discard)
        await asyncio.sleep(0)

    async def _delayed_recall(self, event: AstrMessageEvent, message_id: int | str, delay: int):
        """延迟一段时间后撤回消息。"""
        try:
            await asyncio.sleep(delay)
            await self._recall_message(event, message_id)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug(f"[IdeSandbox] delayed recall task error: {e}")

    async def _step_notice(self, event: AstrMessageEvent, message: str):
        """关键步骤提示，严格跟随“操作实时广播”开关。"""
        if not self.broadcast_actions:
            return
        await self._broadcast(event, message)

    async def _llm_heartbeat_loop(self, event: AstrMessageEvent, key: str):
        try:
            delay = max(5, int(self.llm_progress_heartbeat_delay))
            interval = max(10, int(self.llm_progress_heartbeat_interval))
            await asyncio.sleep(delay)
            elapsed = delay
            # 最大心跳时间 90 秒（LLM 通常 60 秒超时）
            while elapsed <= 90:
                # 如果 event 已被停止或取消，立即退出
                if event.is_stopped():
                    break
                if self.llm_progress_show_elapsed:
                    msg = f"等待 IDE 工具响应中（{elapsed} 秒）..."
                else:
                    msg = "等待 IDE 工具响应中..."
                await self._status_notice(event, msg)
                await asyncio.sleep(interval)
                elapsed += interval
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug(f"[IdeSandbox] LLM heartbeat stopped: {e}")
        finally:
            # 确保任务从字典中清理，无论正常结束还是异常
            self._llm_heartbeat_tasks.pop(key, None)

    def _cancel_llm_heartbeat(self, event: AstrMessageEvent):
        key = event.unified_msg_origin or self._get_sandbox_id(event)
        task = self._llm_heartbeat_tasks.pop(key, None)
        if task:
            task.cancel()

    def _is_ide_like_request(self, event: AstrMessageEvent) -> bool:
        text = ""
        try:
            text = event.get_message_str() or ""
        except Exception:
            text = getattr(event, "message_str", "") or ""
        if not text:
            try:
                text = event.get_message_outline() or ""
            except Exception:
                text = ""
        text = text.lower()
        keywords = (
            "写", "改", "脚本", "代码", "文件", "py", "python", "html", "js",
            "程序", "网页", "游戏", "运行", "执行", "测试", "打包", "上传",
            "显卡", "gpu", "wmic",
        )
        return any(keyword in text for keyword in keywords)


    def _build_run_env(self) -> dict:
        """Build a Windows-friendly subprocess env for sandbox commands."""
        run_env = os.environ.copy()
        run_env.setdefault("PYTHONUTF8", "1")
        run_env.setdefault("PYTHONIOENCODING", "utf-8")
        run_env.setdefault("NO_COLOR", "1")
        run_env.setdefault("TERM", "dumb")

        if self.custom_env:
            for k, v in self.custom_env.items():
                run_env[str(k)] = str(v)

        path_sep = ";" if os.name == "nt" else ":"
        path_parts: list[str] = []
        path_parts.extend(str(p).strip() for p in self.custom_paths if str(p).strip())

        if os.name == "nt":
            system_root = run_env.get("SystemRoot") or run_env.get("WINDIR") or r"C:\Windows"
            path_parts.extend(
                [
                    str(Path(system_root) / "System32"),
                    str(Path(system_root) / "System32" / "Wbem"),
                    str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0"),
                    str(Path(system_root)),
                ]
            )

        path_parts.extend(p for p in run_env.get("PATH", "").split(path_sep) if p)
        seen = set()
        deduped = []
        for part in path_parts:
            key = part.lower() if os.name == "nt" else part
            if key in seen:
                continue
            seen.add(key)
            deduped.append(part)
        run_env["PATH"] = path_sep.join(deduped)
        return run_env

    def _decode_process_output(self, data: bytes) -> str:
        if not data:
            return ""
        encodings = ["utf-8-sig", "utf-8"]
        if os.name == "nt":
            encodings.extend(["gb18030", "gbk", "cp936", "mbcs", "oem"])
        for encoding in encodings:
            try:
                return data.decode(encoding).strip()
            except (UnicodeDecodeError, LookupError):
                continue
        return data.decode("utf-8", errors="replace").strip()

    async def _download_to_path(self, url: str, target_path: Path) -> tuple[bool, str, int]:
        """流式下载文件到目标路径，并强制执行大小上限。"""
        max_bytes = self.max_file_size_mb * 1024 * 1024
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.with_name(f".{target_path.name}.download")
        total = 0
        timeout = aiohttp.ClientTimeout(total=max(30, self.cmd_timeout * 2))

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return False, f"HTTP {resp.status}", 0
                    content_length = resp.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > max_bytes:
                                return False, f"文件大小超过 {self.max_file_size_mb}MB 限制", 0
                        except ValueError:
                            pass
                    with tmp_path.open("wb") as f:
                        async for chunk in resp.content.iter_chunked(256 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > max_bytes:
                                tmp_path.unlink(missing_ok=True)
                                return False, f"文件大小超过 {self.max_file_size_mb}MB 限制", total
                            await asyncio.to_thread(f.write, chunk)
            await asyncio.to_thread(tmp_path.replace, target_path)
            return True, "", total
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            return False, str(e), total

    async def _write_bytes_with_progress(
        self,
        event: AstrMessageEvent,
        target_path: Path,
        data: bytes,
        display_name: str,
        prefix: str = "",
    ) -> int:
        """按块写入文件，并向群聊广播写入进度。
        同步 I/O 操作通过 asyncio.to_thread() 放到线程池执行，避免阻塞事件循环。"""
        total = len(data)
        await asyncio.to_thread(target_path.parent.mkdir, parents=True, exist_ok=True)

        if total == 0:
            await asyncio.to_thread(target_path.write_bytes, b"")
            await self._broadcast(event, f"{prefix}✍️ `{display_name}` 已写入 0B / 0B。")
            return 0

        logger.debug(f"[IdeSandbox] write_file_chunks start: {display_name}, total={total:,}B")
        threshold_bytes = self.status_notice_threshold_kb * 1024
        if total <= threshold_bytes:
            await asyncio.to_thread(target_path.write_bytes, data)
            logger.debug(f"[IdeSandbox] write_file_chunks done: {display_name}, written={total:,}B")
            return total

        # 操作广播关闭时，补一条轻量状态提示；开启时由 ide_write_file 的 _broadcast 负责，避免重复
        if not self.broadcast_actions:
            await self._status_notice(
                event,
                f"⏳ 正在写入 `{display_name}`（{total:,}B），请稍候nya～",
            )

        if total <= 64 * 1024:
            chunk_size = 4 * 1024
        elif total <= 1024 * 1024:
            chunk_size = 32 * 1024
        else:
            chunk_size = 256 * 1024
        report_step = max(chunk_size, total // 4)
        next_report = min(report_step, total)
        written = 0
        last_report_time = asyncio.get_event_loop().time()
        report_pause = 0.05 if total <= 128 * 1024 else 0.02

        # 在线程池中执行同步文件写入，避免阻塞事件循环
        def _sync_write():
            with target_path.open("wb") as f:
                f.write(data)
            return len(data)

        # 大文件直接一次性在线程池写入，避免 Python 层逐块切分的开销
        written = await asyncio.to_thread(_sync_write)

        # 写入完成后广播一次进度（简化逻辑，避免逐块广播带来的复杂度和开销）
        percent = 100.0
        await self._broadcast(
            event,
            f"{prefix}✍️ `{display_name}` 写入进度: {written:,}B / {total:,}B ({percent:.1f}%)",
        )

        logger.debug(f"[IdeSandbox] write_file_chunks done: {display_name}, written={written:,}B")
        return written

    async def _kill_process_tree(self, proc: asyncio.subprocess.Process):
        """尽量清理 shell 及其子进程，避免超时后残留后台进程。"""
        if proc.returncode is not None:
            return
        if sys.platform.startswith("win"):
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=5)
                return
            except Exception:
                pass
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass

    def _start_background_command(
        self,
        command: str,
        description: str,
        proc: asyncio.subprocess.Process,
        owner_id: str = "",
        sandbox_id: str = "",
    ) -> _BackgroundCommand:
        """注册并开始监控一个后台命令。"""
        task_id = uuid.uuid4().hex[:8]
        self.background_log_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.background_log_dir / f"{task_id}.log"
        output_path.write_text(
            f"[task_id] {task_id}\n[sandbox_id] {sandbox_id}\n[description] {description.strip()}\n[command] {command}\n\n",
            encoding="utf-8",
        )
        bg = _BackgroundCommand(
            task_id=task_id,
            description=description or command[:80],
            command=command,
            proc=proc,
            owner_id=owner_id,
            sandbox_id=sandbox_id,
            output_path=output_path,
        )
        self._background_commands[task_id] = bg

        async def _append_log(text: str):
            if not bg.output_path:
                return
            try:
                await asyncio.to_thread(
                    lambda: bg.output_path.open("a", encoding="utf-8", errors="replace").write(text)
                )
            except Exception:
                pass

        async def _read_stream(stream, buffer: list[str], prefix: str = ""):
            try:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace")
                    buffer.append(text)
                    await _append_log(f"{prefix}{text}")
                    # 限制缓冲区大小，避免内存无限增长
                    if len(buffer) > 5000:
                        buffer.pop(0)
            except Exception:
                pass

        async def _monitor():
            try:
                await proc.wait()
                bg.status = "completed" if proc.returncode == 0 else "failed"
                bg.returncode = proc.returncode
                await _append_log(f"\n[exit_code] {bg.returncode}\n[status] {bg.status}\n")
            except asyncio.CancelledError:
                bg.status = "stopped"
                bg.returncode = proc.returncode if proc.returncode is not None else -1
                await _append_log(f"\n[exit_code] {bg.returncode}\n[status] stopped\n")
                raise
            except Exception as e:
                bg.status = "failed"
                bg.error_message = str(e)
                await _append_log(f"\n[error] {bg.error_message}\n")

        stdout_task = asyncio.create_task(_read_stream(proc.stdout, bg.stdout_buffer))
        stderr_task = asyncio.create_task(_read_stream(proc.stderr, bg.stderr_buffer, "[stderr] "))
        monitor_task = asyncio.create_task(_monitor())
        self._background_tasks.add(stdout_task)
        self._background_tasks.add(stderr_task)
        self._background_tasks.add(monitor_task)
        stdout_task.add_done_callback(self._background_tasks.discard)
        stderr_task.add_done_callback(self._background_tasks.discard)
        monitor_task.add_done_callback(self._background_tasks.discard)
        return bg

    async def _stop_background_command(self, task_id: str) -> str:
        """停止并清理指定后台命令。"""
        bg = self._background_commands.get(task_id)
        if not bg:
            return f"错误：找不到后台任务 `{task_id}`。"
        if bg.status == "running" and bg.proc.returncode is None:
            await self._kill_process_tree(bg.proc)
            bg.status = "stopped"
            bg.returncode = bg.proc.returncode if bg.proc.returncode is not None else -1
            if bg.output_path:
                try:
                    await asyncio.to_thread(
                        lambda: bg.output_path.open("a", encoding="utf-8", errors="replace").write(
                            f"\n[status] stopped\n[exit_code] {bg.returncode}\n"
                        )
                    )
                except Exception:
                    pass
        return f"✅ 已停止后台任务 `{task_id}`（状态：{bg.status}，返回码：{bg.returncode}）。"

    def _format_background_output(self, bg: _BackgroundCommand, max_len: int = 4000) -> str:
        """格式化后台命令当前输出。"""
        lines = []
        lines.append(f"🆔 任务 ID: {bg.task_id}")
        lines.append(f"📋 描述: {bg.description}")
        lines.append(f"📝 命令: {bg.command[:200]}")
        lines.append(f"📊 状态: {bg.status}")
        if bg.returncode is not None:
            lines.append(f"🔢 返回码: {bg.returncode}")
        if bg.error_message:
            lines.append(f"❌ 错误: {bg.error_message}")
        output_path = getattr(bg, "output_path", None)
        preview = ""
        output_size = 0
        output_truncated = False
        if output_path:
            try:
                output_path = Path(output_path)
                output_size = output_path.stat().st_size if output_path.exists() else 0
                with output_path.open("rb") as f:
                    if output_size > max_len:
                        output_truncated = True
                        f.seek(max(0, output_size - max_len))
                    preview = f.read(max_len).decode("utf-8", errors="replace")
            except Exception:
                preview = ""
        if not preview:
            stdout = "".join(bg.stdout_buffer)
            stderr = "".join(bg.stderr_buffer)
            preview = (stdout + (f"\n[stderr]\n{stderr}" if stderr else ""))[-max_len:]
            output_truncated = len(stdout) + len(stderr) > max_len
        if output_path:
            lines.append(f"📄 完整日志: {Path(output_path).resolve()}")
            lines.append(f"📏 日志大小: {output_size:,}B")
            lines.append(f"✂️ 输出截断: {'是' if output_truncated else '否'}")
        if preview:
            if output_truncated and output_path:
                preview = f"[仅显示尾部预览，完整日志见 {Path(output_path).resolve()}]\n\n{preview}"
            lines.append(f"🟢 输出预览:\n```\n{preview[-max_len:]}\n```")
        if not preview and bg.status == "running":
            lines.append("⏳ 任务正在运行中，暂无可显示输出。")
        return "\n".join(lines)

    def _todo_file_path(self, sandbox_id: str) -> Path:
        """获取指定沙盒的待办事项持久化文件路径。"""
        safe_id = re.sub(r'[^\w-]', '', str(sandbox_id))
        return self.todos_dir / f"{safe_id}.json"

    def _history_file_path(self, sandbox_id: str) -> Path:
        """获取指定沙盒的活动日志文件路径。"""
        safe_id = re.sub(r'[^\w-]', '', str(sandbox_id))
        return self.history_dir / f"{safe_id}.jsonl"

    def _load_history_records(self, sandbox_id: str, limit: int = 100) -> List[dict]:
        """从磁盘加载最近的活动日志。"""
        path = self._history_file_path(sandbox_id)
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            logger.debug(f"[IdeSandbox] 读取历史记录失败 {sandbox_id}: {e}")
            return []
        records: List[dict] = []
        for line in lines[-max(limit * 2, limit):]:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records[-limit:]

    def _load_todos(self, sandbox_id: str):
        """从磁盘加载指定沙盒的待办事项。"""
        path = self._todo_file_path(sandbox_id)
        if not path.exists():
            self.todos[sandbox_id] = []
            self._todo_id_counter[sandbox_id] = 0
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.todos[sandbox_id] = data.get("todos", [])
            self._todo_id_counter[sandbox_id] = data.get("next_id", 0)
        except Exception as e:
            logger.warning(f"[IdeSandbox] 加载待办事项失败 {sandbox_id}: {e}")
            self.todos[sandbox_id] = []
            self._todo_id_counter[sandbox_id] = 0

    async def _save_todos(self, sandbox_id: str):
        """将指定沙盒的待办事项保存到磁盘。"""
        path = self._todo_file_path(sandbox_id)
        data = {
            "todos": self.todos.get(sandbox_id, []),
            "next_id": self._todo_id_counter.get(sandbox_id, 0),
        }

        def _write():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f".{path.name}.tmp")
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(path)

        try:
            await asyncio.to_thread(_write)
        except Exception as e:
            logger.warning(f"[IdeSandbox] 保存待办事项失败 {sandbox_id}: {e}")

    def _record(self, sandbox_id: str, action: str, detail: str):
        """记录操作历史"""
        self.history.setdefault(sandbox_id, [])
        record = {
            "time": datetime.now().isoformat(),
            "action": action,
            "detail": detail,
        }
        self.history[sandbox_id].append(record)
        # 只保留最近 100 条内存记录，磁盘日志由 WebUI 按需读取。
        self.history[sandbox_id] = self.history[sandbox_id][-100:]
        history_dir = getattr(self, "history_dir", None)
        if not history_dir:
            return
        try:
            Path(history_dir).mkdir(parents=True, exist_ok=True)
            with self._history_file_path(sandbox_id).open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[IdeSandbox] 写入历史记录失败 {sandbox_id}: {e}")

    # ========== 群文件自动下载 ==========
