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
from astrbot.api.event import filter
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


class EventCommandMixin:
    @filter.on_waiting_llm_request()
    async def on_waiting_llm_request(self, event: AstrMessageEvent):
        if not self.llm_progress_notice and not self.llm_progress_heartbeat:
            return
        key = event.unified_msg_origin or self._get_sandbox_id(event)
        old_task = self._llm_heartbeat_tasks.pop(key, None)
        if old_task:
            old_task.cancel()
        if not self._is_ide_like_request(event):
            return
        if self.llm_progress_heartbeat:
            self._llm_heartbeat_tasks[key] = asyncio.create_task(
                self._llm_heartbeat_loop(event, key)
            )
        if self.llm_progress_notice:
            await self._status_notice(event, "等待 IDE 工具响应中...")


    @filter.on_agent_done()
    async def on_agent_done(self, event: AstrMessageEvent, *_):
        self._cancel_llm_heartbeat(event)


    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event: AstrMessageEvent, *_):
        self._cancel_llm_heartbeat(event)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response):
        if not getattr(self, "suppress_none_response", False):
            return
        if not self._is_ide_like_request(event):
            return
        text = (response.completion_text or "").strip()
        if text in ("None", "none", "NULL", "null") or not text:
            logger.debug("[IdeSandbox] suppress pure None/empty LLM response")
            response.result_chain = None
            response._completion_text = ""

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        if not getattr(self, "suppress_none_response", False):
            return
        if not self._is_ide_like_request(event):
            return
        result = event.get_result()
        if not result or not result.chain:
            return
        try:
            if hasattr(result, "get_plain_text"):
                text = result.get_plain_text().strip()
            else:
                text = " ".join(
                    getattr(comp, "text", "")
                    for comp in result.chain
                    if hasattr(comp, "text")
                ).strip()
        except Exception as e:
            logger.debug(f"[IdeSandbox] on_decorating_result get text failed: {e}")
            return
        if text in ("None", "none", "NULL", "null") or not text:
            logger.debug("[IdeSandbox] suppress pure None/empty outgoing message")
            event.clear_result()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_file_upload(self, event: AstrMessageEvent):
        """监听群文件上传事件，根据关键字自动下载到沙盒"""
        # 检查开关
        if not self.auto_download or not self.auto_download_keywords:
            return

        raw = getattr(event.message_obj, "raw_message", None)
        if not raw:
            return

        # 只处理 notice 类型的 group_upload 事件
        if raw.get("post_type") != "notice" or raw.get("notice_type") != "group_upload":
            return

        file_info = raw.get("file", {})
        file_name = file_info.get("name", "")
        if not file_name:
            return

        # 检查文件名是否包含关键字
        file_name_lower = file_name.lower()
        matched = any(kw in file_name_lower for kw in self.auto_download_keywords)
        if not matched:
            return

        # 权限检查：自动下载也需要权限
        if not await self._check_permission(event):
            return

        gid = event.get_group_id()
        if not gid:
            return

        sandbox_id = self._get_sandbox_id(event)

        try:
            if not isinstance(event, AiocqhttpMessageEvent):
                return

            file_size = file_info.get("size", 0)
            if file_size > self.max_file_size_mb * 1024 * 1024:
                logger.warning(f"[IdeSandbox] 自动下载文件 `{file_name}` 超过大小限制 ({file_size:,}B)，已跳过。")
                return

            file_id = file_info.get("id")
            busid = file_info.get("busid")
            if not file_id or busid is None:
                logger.warning(f"[IdeSandbox] 自动下载文件 `{file_name}` 缺少 file_id 或 busid，已跳过。")
                return

            # 获取下载链接
            url_info = await event.bot.call_action(
                "get_group_file_url",
                group_id=int(gid),
                file_id=file_id,
                busid=busid,
            )
            url = url_info.get("url")
            if not url:
                logger.warning(f"[IdeSandbox] 自动下载文件 `{file_name}` 无法获取下载链接。")
                return

            # 下载到沙盒
            target_path = self._resolve(sandbox_id, file_name)
            if not target_path:
                logger.warning(f"[IdeSandbox] 自动下载文件 `{file_name}` 文件名不合法。")
                return

            ok, reason, size = await self._download_to_path(url, target_path)
            if not ok:
                logger.warning(f"[IdeSandbox] 自动下载文件 `{file_name}` 失败: {reason}")
                return

            self._record(sandbox_id, "auto_download", file_name)
            logger.info(f"[IdeSandbox] 已自动下载群文件 `{file_name}` 到沙盒（{size:,}B）")

            # 广播到群聊
            if self.broadcast_actions:
                await event.send(event.plain_result(f"📥 检测到关键字文件 `{file_name}`，已自动下载到沙盒（{size:,}B）"))
        except Exception as e:
            logger.error(f"[IdeSandbox] 自动下载群文件失败: {e}")

    # ========== 普通命令（供用户手动触发）==========


    @filter.command("ide", alias={"ide帮助"})
    async def cmd_ide(self, event: AstrMessageEvent):
        """显示 AI IDE 帮助"""
        sandbox_id = self._get_sandbox_id(event)
        whitelist_str = ", ".join(sorted(self.execution_whitelist)) if self.execution_whitelist else "无限制（仅禁止危险命令）"
        admins_str = ", ".join(sorted(self.admins)) if self.admins else "未设置"
        terminal_admins_str = ", ".join(sorted(self.terminal_admins)) if self.terminal_admins else "未设置"
        cmd_admins_str = ", ".join(sorted(self.cmd_admins)) if self.cmd_admins else "未设置"
        auto_download_status = "关闭"
        if self.auto_download and self.auto_download_keywords:
            auto_download_status = f"开启（{', '.join(sorted(self.auto_download_keywords))}）"
        elif self.auto_download:
            auto_download_status = "开启但未配置关键词（不会触发）"
        risk_notes = []
        if self.allow_execution and not self.broadcast_actions:
            risk_notes.append("命令执行已开启但操作广播关闭")
        if self.allow_members and self.allow_execution:
            risk_notes.append("全员可用不会授予命令/测试/Git 权限")
        risk_text = "\n风险提示: " + "；".join(risk_notes) if risk_notes else ""
        text = (
            "🛠️ AI IDE 沙盒\n"
            "本插件为 LLM 提供文件操作、命令执行等工具。\n"
            "在对话中 @机器人 描述需求，AI 会自动调用工具完成。\n\n"
            "例如：\n"
            "@机器人 帮我写一个 Python 脚本，计算 1 到 100 的和，保存到 sum.py\n"
            "@机器人 查看沙盒里有哪些文件\n"
            "@机器人 拉取 https://github.com/xxx/yyy 仓库分析一下\n"
            "@机器人 把当前目录打包并发送给我\n\n"
            f"沙盒路径: {self._get_group_sandbox(sandbox_id)}\n"
            f"权限模式: {'全员可用' if self.allow_members else '仅管理员/主人'}\n"
            f"沙盒管理员: {admins_str}\n"
            f"终端管理员: {terminal_admins_str}\n"
            f"CMD 管理员: {cmd_admins_str}\n"
            f"命令执行: {'开启' if self.allow_execution else '关闭'}\n"
            f"命令白名单: {whitelist_str}\n"
            f"GitHub克隆: {'开启' if self.allow_git_clone else '关闭'}（限制{self.git_clone_limit_mb}MB）\n"
            f"自动下载: {auto_download_status}\n"
            f"操作广播: {'开启' if self.broadcast_actions else '关闭'}"
            f"{risk_text}"
        )
        yield event.plain_result(text)


    @filter.command("ide列表", alias={"沙盒列表"})
    async def cmd_list(self, event: AstrMessageEvent):
        """手动列出沙盒文件"""
        if not await self._check_permission(event):
            yield event.plain_result("权限不足：只有管理员或主人才可以查看沙盒文件。")
            return
        sandbox_id = self._get_sandbox_id(event)
        d = self._get_group_sandbox(sandbox_id)
        def _snapshot_files():
            rows = []
            for f in d.iterdir():
                if f.is_file():
                    rows.append((f.name, f.stat().st_size))
            return rows

        files = await asyncio.to_thread(_snapshot_files)
        if not files:
            yield event.plain_result("📂 沙盒为空。")
            return
        lines = [f"📂 沙盒文件（{len(files)} 个）:"]
        for n, sz in files:
            lines.append(f"  • {n} ({sz:,}B)")
        yield event.plain_result("\n".join(lines))


    @filter.command("ide清空", alias={"清空沙盒", "ide清空沙盒"})
    async def cmd_clear_sandbox(self, event: AstrMessageEvent, confirm: str = ""):
        """手动清空当前沙盒；需要显式传入“确认”才实际删除。"""
        confirmed = confirm.strip().lower() in {"确认", "true", "yes", "y", "1"}
        result = await self.ide_clear_sandbox(
            event,
            confirm=confirmed,
            dry_run=not confirmed,
        )
        yield event.plain_result(result)


    @filter.command("ide权限")
    async def cmd_check_permission(self, event: AstrMessageEvent):
        """查看当前沙盒权限状态 .ide权限"""
        sender = str(event.get_sender_id()).strip()
        lines = [
            "🔐 沙盒权限状态",
            f"你的QQ: {sender}",
            f"主人QQ: {self.master_qq or '未设置'}",
            f"沙盒管理员: {', '.join(sorted(self.admins)) or '无'}",
            f"终端管理员: {', '.join(sorted(self.terminal_admins)) or '无'}",
            f"CMD管理员: {', '.join(sorted(self.cmd_admins)) or '无'}",
            f"全员可用: {'是' if self.allow_members else '否'}",
            f"命令级权限: {'是' if self._can_use_command_tool(event) else '否'}",
            f"自动下载: {'开启' if self.auto_download else '关闭'}",
            f"自动下载关键词: {', '.join(sorted(self.auto_download_keywords)) or '未设置（不会触发）'}",
            f"操作广播: {'开启' if self.broadcast_actions else '关闭'}",
            "",
            f"你的权限: {'✅ 已通过' if await self._check_permission(event) else '❌ 被拒绝'}",
        ]
        yield event.plain_result("\n".join(lines))


    @filter.command("ide添加管理员")
    async def cmd_add_admin(self, event: AstrMessageEvent, qq: str = ""):
        """实时添加沙盒管理员 .ide添加管理员 123456789"""
        if not self._can_manage_admins(event):
            yield event.plain_result("❌ 只有主人、AstrBot 全局管理员或已有沙盒管理员才能添加其他管理员。")
            return
        qq = qq.strip()
        if not qq or not qq.isdigit():
            yield event.plain_result("❌ 请输入正确的QQ号。\n用法: .ide添加管理员 123456789")
            return
        if qq in self.admins:
            yield event.plain_result(f"⚠️ {qq} 已经是沙盒管理员了。")
            return
        self.admins.add(qq)
        yield event.plain_result(f"✅ 已将 {qq} 添加为沙盒管理员。\n（实时生效，重启后如需保留请同时写入插件配置）")


    @filter.command("ide删除管理员")
    async def cmd_remove_admin(self, event: AstrMessageEvent, qq: str = ""):
        """实时移除沙盒管理员 .ide删除管理员 123456789"""
        if not self._can_manage_admins(event):
            yield event.plain_result("❌ 只有主人、AstrBot 全局管理员或已有沙盒管理员才能删除其他管理员。")
            return
        qq = qq.strip()
        if not qq or not qq.isdigit():
            yield event.plain_result("❌ 请输入正确的QQ号。\n用法: .ide删除管理员 123456789")
            return
        if qq not in self.admins:
            yield event.plain_result(f"⚠️ {qq} 不在沙盒管理员列表中。")
            return
        self.admins.discard(qq)
        yield event.plain_result(f"✅ 已将 {qq} 从沙盒管理员中移除。")

    # ========== LLM 工具 ==========

