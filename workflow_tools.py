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

MAX_PACK_ENTRIES = 3000


def _collect_zip_entries(
    target_dir: Path,
    *,
    max_total_bytes: int,
    max_entries: int = MAX_PACK_ENTRIES,
    per_file_limit_bytes: int = 50 * 1024 * 1024,
) -> tuple[list[tuple[Path, str, int]], list[str], int, str]:
    entries: list[tuple[Path, str, int]] = []
    skipped_files: list[str] = []
    total_size = 0
    for file_path in target_dir.rglob("*"):
        if file_path.is_symlink():
            skipped_files.append(f"{file_path.name} (符号链接)")
            continue
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() == ".zip":
            continue
        if len(entries) >= max_entries:
            return entries, skipped_files, total_size, f"超过打包文件数量限制（{max_entries} 个）"
        fsize = file_path.stat().st_size
        if fsize > per_file_limit_bytes:
            skipped_files.append(f"{file_path.name} ({fsize / 1024 / 1024:.1f}MB)")
            continue
        if total_size + fsize > max_total_bytes:
            return entries, skipped_files, total_size, "超过打包大小限制"
        arcname = str(file_path.relative_to(target_dir))
        entries.append((file_path, arcname, fsize))
        total_size += fsize
    return entries, skipped_files, total_size, ""


class WorkflowToolsMixin:
    async def ide_think(self, event: AstrMessageEvent, thought: str) -> str:
        """记录一段思考，不执行任何实际操作。

        使用场景：
        - 复杂任务开始前，先梳理思路、列出计划。
        - 记录关键决策依据，方便后续步骤回顾。
        - 在多个工具调用之间保持推理上下文。

        Tips:
        - 不要滥用本工具，只在需要复杂推理或长期规划时使用。
        - 思考内容会被记录到操作历史中，方便主人查看。

        Args:
            thought(string): 要记录的思考内容。

        Returns:
            确认信息。
        """
        sandbox_id = self._get_sandbox_id(event)
        self._record(sandbox_id, "think", thought[:200])
        return "💭 已记录思考。"


    async def ide_ask_user(self, event: AstrMessageEvent, question: str) -> str:
        """向用户提问并把问题发送到群聊。

        使用场景：
        - 需求不明确，需要用户澄清。
        - 多个可选方案，需要用户选择。
        - 需要用户确认某个操作（如删除文件、覆盖配置）。

        Tips:
        - 问题要简洁明确，避免一次性问太多问题。
        - 不要滥用本工具， trivial 决策应直接执行。

        Args:
            question(string): 要向用户提出的问题。

        Returns:
            确认信息，提示已发送问题。
        """
        sandbox_id = self._get_sandbox_id(event)
        if not question:
            return "错误：问题内容不能为空。"
        await self._broadcast(event, f"🙋 松子想问主人：{question}")
        self._record(sandbox_id, "ask_user", question[:200])
        return f"已向用户提问：{question}"


    async def ide_get_history(self, event: AstrMessageEvent) -> str:
        """获取当前沙盒的最近操作历史。
        当 AI 需要回顾之前做过哪些操作、避免重复时使用此工具。
        Returns:
            最近的操作记录列表。
        """
        if not await self._check_permission(event):
            return "权限不足。"
        sandbox_id = self._get_sandbox_id(event)
        records = self.history.get(sandbox_id, [])
        if not records:
            return "暂无操作历史。"
        lines = ["📜 最近操作历史:"]
        for r in records[-20:]:
            lines.append(f"  [{r['time'][:19]}] {r['action']}: {r['detail']}")
        return "\n".join(lines)


    async def ide_list_file_changes(self, event: AstrMessageEvent, limit: int = 20) -> str:
        """查看当前沙盒最近的文件变更摘要。
        当 AI 需要像 Codex 一样汇报修改了哪些文件、每个文件新增/删除多少行时使用。
        Args:
            limit(number, optional): 最多展示最近多少条变更，默认 20。
        Returns:
            文件变更摘要。
        """
        if not await self._check_permission(event):
            return "权限不足。"
        sandbox_id = self._get_sandbox_id(event)
        changes = self.file_changes.get(sandbox_id, [])
        if not changes:
            return "暂无文件变更记录。"
        limit = max(1, min(int(limit or 20), 100))
        recent = changes[-limit:]
        total_added = sum(c.get("added", 0) for c in recent)
        total_removed = sum(c.get("removed", 0) for c in recent)
        paths = {c.get("path", "") for c in recent}
        lines = [
            f"已编辑 {len(paths)} 个文件",
            f"+{total_added} -{total_removed}",
            "",
        ]
        for c in recent:
            action = c.get("action", "已编辑")
            path = c.get("path", "")
            added = c.get("added", 0)
            removed = c.get("removed", 0)
            lines.append(f"{action} {path} +{added} -{removed}")
        return "\n".join(lines)

    # ========== 待办事项（Todo List）==========

    def _get_next_todo_id(self, sandbox_id: str) -> int:
        """获取下一个待办事项 ID"""
        if sandbox_id not in self._todo_id_counter:
            self._load_todos(sandbox_id)
        current = self._todo_id_counter.get(sandbox_id, 0)
        current += 1
        self._todo_id_counter[sandbox_id] = current
        return current

    def _get_group_todos(self, sandbox_id: str) -> List[dict]:
        """获取指定沙盒的待办事项列表"""
        if sandbox_id not in self.todos:
            self._load_todos(sandbox_id)
        return self.todos.setdefault(sandbox_id, [])


    async def ide_add_todo(self, event: AstrMessageEvent, content: str) -> str:
        """添加一个待办事项到当前任务列表。
        当 AI 接到复杂任务、需要分步骤执行时，应该先创建待办事项来跟踪进度。
        完成后可以使用 ide_complete_todo 标记为已完成。
        Args:
            content(string): 待办事项的内容描述。
        Returns:
            添加结果，包含分配的待办 ID。
        """
        if not await self._check_permission(event):
            return "权限不足。"
        sandbox_id = self._get_sandbox_id(event)
        todo_id = self._get_next_todo_id(sandbox_id)
        todo = {
            "id": todo_id,
            "content": content,
            "title": content,
            "status": "pending",
            "completed": False,
            "created_at": datetime.now().isoformat()[:19],
        }
        self._get_group_todos(sandbox_id).append(todo)
        await self._save_todos(sandbox_id)
        await self._broadcast(event, f"📋 AI 添加了待办事项 #{todo_id}: {content[:40]}")
        return f"✅ 已添加待办事项 #{todo_id}: {content}"


    async def ide_list_todos(self, event: AstrMessageEvent) -> str:
        """列出当前沙盒的所有待办事项及完成进度。
        当 AI 需要汇报任务进度、查看还有哪些步骤未完成时使用此工具。
        Returns:
            格式化的待办事项列表，包含完成进度统计。
        """
        if not await self._check_permission(event):
            return "权限不足。"
        sandbox_id = self._get_sandbox_id(event)
        todos = self._get_group_todos(sandbox_id)
        if not todos:
            return "📋 当前没有待办事项。"

        total = len(todos)
        completed = sum(1 for t in todos if t.get("completed") or t.get("status") == "done")
        pending = total - completed

        lines = [
            f"📋 待办事项列表（{completed}/{total} 完成）:",
        ]
        if pending > 0:
            lines.append("\n⏳ 进行中:")
            for t in todos:
                if not (t.get("completed") or t.get("status") == "done"):
                    status = t.get("status", "pending")
                    marker = "进行中" if status == "in_progress" else "待处理"
                    lines.append(f"  #{t['id']}. [{marker}] {t.get('content') or t.get('title', '')}")
        if completed > 0:
            lines.append("\n✅ 已完成:")
            for t in todos:
                if t.get("completed") or t.get("status") == "done":
                    lines.append(f"  #{t['id']}. ~~{t.get('content') or t.get('title', '')}~~")
        return "\n".join(lines)


    async def ide_set_todo_list(self, event: AstrMessageEvent, todos: str = "") -> str:
        """Kimi 风格整表读取/设置待办事项。

        Args:
            todos(string): JSON 数组，元素为 {"title": "...", "status": "pending|in_progress|done"}。
                留空时只读取当前列表。
        """
        if not await self._check_permission(event):
            return "权限不足。"
        sandbox_id = self._get_sandbox_id(event)
        if not todos.strip():
            return await self.ide_list_todos(event)
        try:
            parsed = json.loads(todos)
        except json.JSONDecodeError as e:
            return f"错误：todos 不是合法 JSON: {e}"
        if not isinstance(parsed, list):
            return "错误：todos 必须是 JSON 数组。"

        normalized = []
        for idx, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                return f"错误：第 {idx} 项不是对象。"
            title = str(item.get("title") or item.get("content") or "").strip()
            status = str(item.get("status") or "pending").strip()
            if not title:
                return f"错误：第 {idx} 项缺少 title。"
            if status not in {"pending", "in_progress", "done"}:
                return f"错误：第 {idx} 项 status 必须是 pending、in_progress 或 done。"
            normalized.append({
                "id": idx,
                "content": title,
                "title": title,
                "status": status,
                "completed": status == "done",
                "created_at": datetime.now().isoformat()[:19],
                **({"completed_at": datetime.now().isoformat()[:19]} if status == "done" else {}),
            })

        self.todos[sandbox_id] = normalized
        self._todo_id_counter[sandbox_id] = len(normalized)
        await self._save_todos(sandbox_id)
        await self._broadcast(event, f"📋 AI 更新了待办列表（{len(normalized)} 项）")
        return "✅ Todo list updated:\n" + "\n".join(
            f"- [{item['status']}] {item['title']}" for item in normalized
        )


    async def ide_complete_todo(
        self, event: AstrMessageEvent, todo_id: int = 0, content_keyword: str = ""
    ) -> str:
        """将指定的待办事项标记为已完成。
        当 AI 完成了某个步骤后，使用此工具更新进度。
        可以通过 todo_id 或内容关键词来定位待办事项。
        Args:
            todo_id(number, optional): 要完成的待办事项 ID（优先使用）。
            content_keyword(string, optional): 如果不记得 ID，可以输入内容关键词来匹配。
        Returns:
            完成结果说明。
        """
        if not await self._check_permission(event):
            return "权限不足。"
        sandbox_id = self._get_sandbox_id(event)
        todos = self._get_group_todos(sandbox_id)
        if not todos:
            return "错误：当前没有待办事项。"

        target = None
        if todo_id > 0:
            for t in todos:
                if t["id"] == todo_id:
                    target = t
                    break
        elif content_keyword:
            keyword = content_keyword.lower()
            matches = [
                t for t in todos
                if not (t.get("completed") or t.get("status") == "done")
                and keyword in (t.get("content") or t.get("title", "")).lower()
            ]
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                ids = ", ".join(str(t["id"]) for t in matches)
                return f"错误：找到多个匹配的待办事项（ID: {ids}），请使用 todo_id 精确指定。"
        else:
            return "错误：请提供 todo_id 或 content_keyword 来指定要完成的待办事项。"

        if not target:
            return "错误：未找到指定的待办事项。"
        if target.get("completed") or target.get("status") == "done":
            return f"待办事项 #{target['id']} 已经标记为完成了。"

        target["completed"] = True
        target["status"] = "done"
        target["completed_at"] = datetime.now().isoformat()[:19]
        await self._save_todos(sandbox_id)
        title = target.get("content") or target.get("title", "")
        await self._broadcast(event, f"✅ AI 完成了待办事项 #{target['id']}: {title[:40]}")
        return f"✅ 待办事项 #{target['id']} 已标记为完成: {title}"


    async def ide_delete_todo(
        self, event: AstrMessageEvent, todo_id: int = 0, content_keyword: str = ""
    ) -> str:
        """删除指定的待办事项。
        当某个待办事项不再需要、或创建错误时使用此工具。
        Args:
            todo_id(number, optional): 要删除的待办事项 ID（优先使用）。
            content_keyword(string, optional): 内容关键词匹配。
        Returns:
            删除结果说明。
        """
        if not await self._check_permission(event):
            return "权限不足。"
        sandbox_id = self._get_sandbox_id(event)
        todos = self._get_group_todos(sandbox_id)
        if not todos:
            return "错误：当前没有待办事项。"

        target_idx = -1
        if todo_id > 0:
            for i, t in enumerate(todos):
                if t["id"] == todo_id:
                    target_idx = i
                    break
        elif content_keyword:
            keyword = content_keyword.lower()
            matches = [
                (i, t) for i, t in enumerate(todos)
                if keyword in (t.get("content") or t.get("title", "")).lower()
            ]
            if len(matches) == 1:
                target_idx = matches[0][0]
            elif len(matches) > 1:
                ids = ", ".join(str(t[1]["id"]) for t in matches)
                return f"错误：找到多个匹配的待办事项（ID: {ids}），请使用 todo_id 精确指定。"
        else:
            return "错误：请提供 todo_id 或 content_keyword 来指定要删除的待办事项。"

        if target_idx < 0:
            return "错误：未找到指定的待办事项。"

        removed = todos.pop(target_idx)
        await self._save_todos(sandbox_id)
        return f"🗑️ 已删除待办事项 #{removed['id']}: {removed.get('content') or removed.get('title', '')}"


    async def ide_pack_and_download(self, event: AstrMessageEvent, dir_name: str = "", zip_name: str = "sandbox_export.zip") -> str:
        """将沙盒中的指定目录打包为 ZIP 并直接发送给用户。
        当用户要求下载代码、获取生成的项目或文件时使用此工具。
        超级管理员可传入绝对路径打包沙盒外目录。
        Args:
            dir_name(string, optional): 要打包的目录名（不含路径），或超级管理员使用的绝对路径。留空则打包整个沙盒。
            zip_name(string, optional): 导出的压缩包文件名，默认 sandbox_export.zip。
        Returns:
            打包并发送的结果说明。
        """
        sandbox_id = self._get_sandbox_id(event)
        if not await self._check_permission(event):
            return "权限不足。"

        cwd = self._get_group_sandbox(sandbox_id)
        
        if dir_name:
            is_super = self._is_super_admin(event)
            target_dir = self._resolve(sandbox_id, dir_name, allow_bypass=is_super)
            if not target_dir or not await asyncio.to_thread(lambda: target_dir.exists() and target_dir.is_dir()):
                return f"错误：目录 `{dir_name}` 不存在或不合法。"
        else:
            target_dir = cwd

        if not zip_name.endswith(".zip"):
            zip_name += ".zip"
            
        safe_zip = _safe_filename(zip_name)
        if not safe_zip:
            return "错误：压缩包文件名不合法。"

        zip_path = cwd / safe_zip
        super_tag = self._get_super_tag(event, dir_name)
        await self._broadcast(event, f"{super_tag}🤖 AI 正在打包 `{dir_name or '根目录'}` 为 `{safe_zip}`...")

        # 尝试清理旧压缩包；如被占用（前次线程未结束）则换名，避免 PermissionError
        if await asyncio.to_thread(zip_path.exists):
            try:
                await asyncio.to_thread(zip_path.unlink)
            except PermissionError:
                safe_zip = f"{int(asyncio.get_event_loop().time())}_{safe_zip}"
                zip_path = cwd / safe_zip

        try:
            max_total_bytes = self.max_file_size_mb * 1024 * 1024

            def do_zip_pack():
                entries, skipped_files, total_size, truncated_reason = _collect_zip_entries(
                    target_dir,
                    max_total_bytes=max_total_bytes,
                    max_entries=MAX_PACK_ENTRIES,
                )
                if truncated_reason:
                    return total_size, len(entries), skipped_files, truncated_reason
                with zipfile.ZipFile(str(zip_path), 'w', zipfile.ZIP_STORED) as zf:
                    for file_path, arcname, _ in entries:
                        zf.write(str(file_path), arcname)
                return total_size, len(entries), skipped_files, ""

            total_size, file_count, skipped_files, truncated_reason = await asyncio.to_thread(do_zip_pack)
            if truncated_reason:
                await asyncio.to_thread(zip_path.unlink, missing_ok=True)
                return f"错误：{truncated_reason}，已停止打包（当前累计 {total_size:,}B，限制 {self.max_file_size_mb}MB）。"

            skip_msg = ""
            if skipped_files:
                skip_msg = f"\n⚠️ 已跳过 {len(skipped_files)} 个大文件：{', '.join(skipped_files[:3])}"
                if len(skipped_files) > 3:
                    skip_msg += f" 等"

            await event.send(event.chain_result([File(file=str(zip_path), name=safe_zip)]))
            self._record(sandbox_id, "pack_download", f"{dir_name} -> {safe_zip}")
            self._delete_file_later(zip_path, delay=3)
            return f"✅ 打包成功，文件 `{safe_zip}` ({total_size:,}B，{file_count} 个文件) 已发送。{skip_msg}"
        except asyncio.TimeoutError:
            return f"⏱️ 打包超时，目录可能过大。请指定更小的子目录，或清理沙盒后重试。"
        except Exception as e:
            return f"打包发送失败: {e}"


    async def ide_clear_todos(self, event: AstrMessageEvent, confirm: bool = False) -> str:
        """清空当前沙盒的所有待办事项。
        当任务全部完成、或需要重新开始时使用此工具。
        出于安全考虑，必须设置 confirm=true 才能执行。
        Args:
            confirm(bool): 必须设为 true 才会清空，防止误操作。
        Returns:
            清空结果说明。
        """
        if not await self._check_permission(event):
            return "权限不足。"
        sandbox_id = self._get_sandbox_id(event)
        if not confirm:
            return "⚠️ 请设置 confirm=true 以确认清空所有待办事项。"
        todos = self._get_group_todos(sandbox_id)
        count = len(todos)
        if count == 0:
            return "当前没有待办事项，无需清空。"
        self.todos[sandbox_id] = []
        self._todo_id_counter[sandbox_id] = 0
        await self._save_todos(sandbox_id)
        return f"🗑️ 已清空 {count} 个待办事项。"
