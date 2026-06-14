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


class GroupFileToolsMixin:
    async def ide_list_group_files(self, event: AstrMessageEvent) -> str:
        """列出当前群的群文件列表（根目录）。
        当 AI 需要查看群文件、决定将文件下载到沙盒或上传沙盒文件到群文件时使用此工具。
        Returns:
            群文件列表，包含文件名和大小。
        """
        gid = event.get_group_id()
        if not gid:
            return "错误：该功能仅支持群聊。"
        if not await self._check_permission(event):
            return "权限不足。"
        try:
            if not isinstance(event, AiocqhttpMessageEvent):
                return "错误：群文件操作仅支持 aiocqhttp (OneBot V11) 平台。"

            await self._broadcast(event, f"🤖 AI 正在读取群 {gid} 的文件列表...")
            result = await event.bot.call_action("get_group_root_files", group_id=int(gid))
            files = result.get("files", [])
            folders = result.get("folders", [])

            lines = [f"📂 群 {gid} 文件列表:"]
            if folders:
                lines.append("\n[文件夹]:")
                for f in folders:
                    lines.append(f"  📁 {f.get('folder_name', 'unknown')}")
            if files:
                lines.append("\n[文件]:")
                for f in files:
                    name = f.get("file_name", "unknown")
                    size = f.get("file_size", 0)
                    lines.append(f"  📄 {name} ({size:,}B)")
            if not folders and not files:
                lines.append("  （空）")

            self._record(self._get_sandbox_id(event), "list_group_files", f"{len(files)} 个文件, {len(folders)} 个文件夹")
            return "\n".join(lines)
        except Exception as e:
            return f"获取群文件列表失败: {e}"


    async def ide_download_group_file(
        self, event: AstrMessageEvent, filename: str
    ) -> str:
        """从群文件下载指定文件到当前群沙盒。
        当 AI 需要获取群文件并在沙盒中处理时使用此工具。
        Args:
            filename(string): 群文件中的文件名。
        Returns:
            下载结果说明。
        """
        gid = event.get_group_id()
        if not gid:
            return "错误：该功能仅支持群聊。"
        if not await self._check_permission(event):
            return "权限不足。"
        try:
            if not isinstance(event, AiocqhttpMessageEvent):
                return "错误：群文件操作仅支持 aiocqhttp (OneBot V11) 平台。"

            # 先获取文件列表找到 busid
            result = await event.bot.call_action("get_group_root_files", group_id=int(gid))
            files = result.get("files", [])
            target = None
            for f in files:
                if f.get("file_name") == filename:
                    target = f
                    break
            if not target:
                return f"错误：群文件中未找到 `{filename}`。"

            file_size = target.get("file_size", 0)
            super_tag = self._get_super_tag(event, filename)
            await self._broadcast(event, f"{super_tag}🤖 AI 正在从群文件下载 `{filename}`（{file_size:,}B）...")

            file_id = target.get("file_id")
            busid = target.get("busid")

            if file_size > self.max_file_size_mb * 1024 * 1024:
                return f"错误：文件大小 {file_size:,}B 超过 {self.max_file_size_mb}MB 限制。"

            # 获取下载链接
            url_info = await event.bot.call_action(
                "get_group_file_url",
                group_id=int(gid),
                file_id=file_id,
                busid=busid,
            )
            url = url_info.get("url")
            if not url:
                return "错误：无法获取文件下载链接。"

            # 下载文件
            sandbox_id = self._get_sandbox_id(event)
            is_super = self._is_super_admin(event)
            target_path = self._resolve(sandbox_id, filename, allow_bypass=is_super)
            if not target_path:
                return "错误：文件名不合法。"

            ok, reason, size = await self._download_to_path(url, target_path)
            if not ok:
                return f"下载失败: {reason}"

            self._record(sandbox_id, "download", filename)
            return f"✅ 已下载 `{filename}` 到沙盒（{size:,}B）。"
        except Exception as e:
            return f"下载失败: {e}"


    async def ide_upload_to_group(
        self, event: AstrMessageEvent, filename: str
    ) -> str:
        """将沙盒中的指定文件上传到群文件。
        当 AI 完成任务后需要把结果文件分享到群文件时使用此工具。
        Args:
            filename(string): 沙盒中要上传的文件名。
        Returns:
            上传结果说明。
        """
        gid = event.get_group_id()
        if not gid:
            return "错误：该功能仅支持群聊。"
        if not await self._check_permission(event):
            return "权限不足。"
        try:
            if not isinstance(event, AiocqhttpMessageEvent):
                return "错误：群文件操作仅支持 aiocqhttp (OneBot V11) 平台。"

            sandbox_id = self._get_sandbox_id(event)
            is_super = self._is_super_admin(event)
            path = self._resolve(sandbox_id, filename, allow_bypass=is_super)
            if not path or not path.exists() or not path.is_file():
                return f"错误：沙盒中不存在 `{filename}`。"

            size = path.stat().st_size
            if size > self.max_file_size_mb * 1024 * 1024:
                return f"错误：文件大小 {size:,}B 超过 {self.max_file_size_mb}MB 限制。"

            super_tag = self._get_super_tag(event, filename)
            await self._broadcast(event, f"{super_tag}🤖 AI 正在上传 `{filename}`（{size:,}B）到群文件...")

            await event.bot.call_action(
                "upload_group_file",
                group_id=int(gid),
                file=str(path),
                name=path.name,
            )
            self._record(sandbox_id, "upload", filename)
            return f"✅ 已上传 `{filename}` 到群文件（{size:,}B）。"
        except Exception as e:
            return f"上传失败: {e}"

