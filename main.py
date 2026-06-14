"""
AstrBot 群聊 AI IDE 沙盒插件
功能：
1. 为 LLM 提供文件操作工具（读/写/编辑/删除）
2. 为 LLM 提供命令执行工具（带安全沙盒）
3. 与群文件互通（下载群文件到沙盒、上传沙盒文件到群文件）
4. 每个群独立沙盒，路径隔离

使用方法：
- 在群聊中 @机器人 并说 "帮我创建一个 Python 脚本计算斐波那契数列"
- LLM 会自动调用工具在沙盒中创建文件、编辑内容
- 完成后可以自动上传到群文件

权限：默认仅群管理员和机器人主人可用，可在配置中调整。
"""

import os
import re
import json
import shutil
import asyncio
import tempfile
import zipfile
import sys
import fnmatch
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Set

import aiohttp

from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from .base import IdeSandboxCore
from .web_api import WebApiMixin
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.api.message_components import File

from .file_tools import FileToolsMixin
from .command_tools import CommandToolsMixin
from .events import EventCommandMixin
from .git_tools import GitToolsMixin
from .group_files import GroupFileToolsMixin
from .workflow_tools import WorkflowToolsMixin
from .tool_models import (
    IdeAddTodoArgs,
    IdeAppendToFileArgs,
    IdeAskUserArgs,
    IdeClearSandboxArgs,
    IdeClearTodosArgs,
    IdeCompleteTodoArgs,
    IdeDeleteFileArgs,
    IdeDeleteTodoArgs,
    IdeDownloadGroupFileArgs,
    IdeEditFileArgs,
    IdeExecuteArgs,
    IdeExecuteElevatedArgs,
    IdeFileInfoArgs,
    IdeGitCloneArgs,
    IdeGlobArgs,
    IdeListFileChangesArgs,
    IdeListTreeArgs,
    IdePackAndDownloadArgs,
    IdeReadFileArgs,
    IdeReadFileRangeArgs,
    IdeRunTestArgs,
    IdeSearchTextArgs,
    IdeTaskOutputArgs,
    IdeTaskListArgs,
    IdeTaskStopArgs,
    IdeThinkArgs,
    IdeSetTodoListArgs,
    IdeUploadToGroupArgs,
    IdeWriteFileArgs,
    validate_with,
)


def llm_tool_with_doc(name: str):
    """从 tool_docs/{name}.md 加载 docstring，再应用 @filter.llm_tool 注册工具。"""

    def decorator(func):
        doc_path = Path(__file__).parent / "tool_docs" / f"{name}.md"
        if doc_path.exists():
            func.__doc__ = doc_path.read_text(encoding="utf-8")
        return filter.llm_tool(name=name)(func)

    return decorator


@register("ide_sandbox", "matsuko", "IDE 管理", "1.5.1")
class IdeSandboxPlugin(
    WebApiMixin,
    IdeSandboxCore,
    EventCommandMixin,
    FileToolsMixin,
    CommandToolsMixin,
    GitToolsMixin,
    GroupFileToolsMixin,
    WorkflowToolsMixin,
):

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self._register_web_apis()

    # ========== 事件钩子 ==========
    # AstrBot 以插件主模块绑定 handler。mixin 里的装饰器会落在 events.py
    # 模块上，运行时会被插件白名单过滤掉，所以主类需要显式 wrapper。
    @filter.on_waiting_llm_request()
    async def on_waiting_llm_request(self, event: AstrMessageEvent):
        return await EventCommandMixin.on_waiting_llm_request(self, event)


    @filter.on_agent_done()
    async def on_agent_done(self, event: AstrMessageEvent, *_):
        return await EventCommandMixin.on_agent_done(self, event, *_)


    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event: AstrMessageEvent, *_):
        return await EventCommandMixin.on_using_llm_tool(self, event, *_)


    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response):
        return await EventCommandMixin.on_llm_response(self, event, response)


    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        return await EventCommandMixin.on_decorating_result(self, event)


    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_file_upload(self, event: AstrMessageEvent):
        return await EventCommandMixin.on_group_file_upload(self, event)


    # ========== 普通命令 ==========
    @filter.command("ide", alias={"ide帮助"})
    async def cmd_ide(self, event: AstrMessageEvent):
        """显示 IDE 管理帮助、权限状态和常用示例。"""
        async for item in EventCommandMixin.cmd_ide(self, event):
            yield item

    @filter.command("ide列表", alias={"沙盒列表"})
    async def cmd_list(self, event: AstrMessageEvent):
        """列出当前沙盒根目录下的文件。"""
        async for item in EventCommandMixin.cmd_list(self, event):
            yield item

    @filter.command("ide清空", alias={"清空沙盒", "ide清空沙盒"})
    async def cmd_clear_sandbox(self, event: AstrMessageEvent, confirm: str = ""):
        """预览或清空当前沙盒；实际删除需传入“确认”。"""
        async for item in EventCommandMixin.cmd_clear_sandbox(self, event, confirm):
            yield item

    @filter.command("ide权限")
    async def cmd_check_permission(self, event: AstrMessageEvent):
        """查看当前用户在 IDE 沙盒中的权限状态。"""
        async for item in EventCommandMixin.cmd_check_permission(self, event):
            yield item

    @filter.command("ide添加管理员")
    async def cmd_add_admin(self, event: AstrMessageEvent, qq: str = ""):
        """添加沙盒管理员 QQ 号。"""
        async for item in EventCommandMixin.cmd_add_admin(self, event, qq):
            yield item

    @filter.command("ide删除管理员")
    async def cmd_remove_admin(self, event: AstrMessageEvent, qq: str = ""):
        """移除沙盒管理员 QQ 号。"""
        async for item in EventCommandMixin.cmd_remove_admin(self, event, qq):
            yield item


    # ========== LLM 工具 ==========
    @llm_tool_with_doc("ide_list_files")
    async def ide_list_files(self, event: AstrMessageEvent) -> str:
        return await FileToolsMixin.ide_list_files(self, event)


    @llm_tool_with_doc("ide_list_tree")
    @validate_with(IdeListTreeArgs)
    async def ide_list_tree(
        self,
        event: AstrMessageEvent,
        root: str = "",
        max_depth: int = 3,
        max_entries: int = 200,
    ) -> str:
        return await FileToolsMixin.ide_list_tree(self, event, root, max_depth, max_entries)


    @llm_tool_with_doc("ide_glob")
    @validate_with(IdeGlobArgs)
    async def ide_glob(
        self,
        event: AstrMessageEvent,
        pattern: str,
        directory: str = "",
        include_dirs: bool = True,
        max_matches: int = 1000,
    ) -> str:
        return await FileToolsMixin.ide_glob(self, event, pattern, directory, include_dirs, max_matches)


    @llm_tool_with_doc("ide_file_info")
    @validate_with(IdeFileInfoArgs)
    async def ide_file_info(self, event: AstrMessageEvent, path_name: str) -> str:
        return await FileToolsMixin.ide_file_info(self, event, path_name)


    @llm_tool_with_doc("ide_read_file_range")
    @validate_with(IdeReadFileRangeArgs)
    async def ide_read_file_range(
        self,
        event: AstrMessageEvent,
        filename: str,
        start_line: int = 1,
        end_line: int = 120,
    ) -> str:
        return await FileToolsMixin.ide_read_file_range(self, event, filename, start_line, end_line)


    @llm_tool_with_doc("ide_search_text")
    @validate_with(IdeSearchTextArgs)
    async def ide_search_text(
        self,
        event: AstrMessageEvent,
        query: str,
        root: str = "",
        filename_pattern: str = "",
        regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 50,
        output_mode: str = "content",
        head_limit: int = 250,
        offset: int = 0,
        include_ignored: bool = False,
    ) -> str:
        return await FileToolsMixin.ide_search_text(
            self,
            event,
            query,
            root,
            filename_pattern,
            regex,
            case_sensitive,
            max_results,
            output_mode,
            head_limit,
            offset,
            include_ignored,
        )


    @llm_tool_with_doc("ide_read_file")
    @validate_with(IdeReadFileArgs)
    async def ide_read_file(
        self,
        event: AstrMessageEvent,
        filename: str,
        line_offset: int = 1,
        n_lines: int = 1000,
    ) -> str:
        return await FileToolsMixin.ide_read_file(self, event, filename, line_offset, n_lines)


    @llm_tool_with_doc("ide_write_file")
    @validate_with(IdeWriteFileArgs)
    async def ide_write_file(
        self, event: AstrMessageEvent, filename: str, content: str, dry_run: bool = False
    ) -> str:
        return await FileToolsMixin.ide_write_file(self, event, filename, content, dry_run)


    @llm_tool_with_doc("ide_append_to_file")
    @validate_with(IdeAppendToFileArgs)
    async def ide_append_to_file(
        self, event: AstrMessageEvent, filename: str, content: str
    ) -> str:
        return await FileToolsMixin.ide_append_to_file(self, event, filename, content)


    @llm_tool_with_doc("ide_edit_file")
    @validate_with(IdeEditFileArgs)
    async def ide_edit_file(
        self,
        event: AstrMessageEvent,
        filename: str,
        old_string: str = "",
        new_string: str = "",
        replace_all: bool = False,
        edits: str = "",
        dry_run: bool = False,
    ) -> str:
        return await FileToolsMixin.ide_edit_file(
            self,
            event,
            filename,
            old_string,
            new_string,
            replace_all,
            edits,
            dry_run,
        )


    @llm_tool_with_doc("ide_delete_file")
    @validate_with(IdeDeleteFileArgs)
    async def ide_delete_file(self, event: AstrMessageEvent, filename: str, dry_run: bool = False) -> str:
        return await FileToolsMixin.ide_delete_file(self, event, filename, dry_run)


    @llm_tool_with_doc("ide_clear_sandbox")
    @validate_with(IdeClearSandboxArgs)
    async def ide_clear_sandbox(
        self,
        event: AstrMessageEvent,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> str:
        return await FileToolsMixin.ide_clear_sandbox(self, event, confirm, dry_run)


    @llm_tool_with_doc("ide_execute")
    @validate_with(IdeExecuteArgs)
    async def ide_execute(
        self,
        event: AstrMessageEvent,
        command: str,
        run_in_background: bool = False,
        description: str = "",
        dry_run: bool = False,
    ) -> str:
        return await CommandToolsMixin.ide_execute(self, event, command, run_in_background, description, dry_run)


    @llm_tool_with_doc("ide_task_output")
    @validate_with(IdeTaskOutputArgs)
    async def ide_task_output(
        self,
        event: AstrMessageEvent,
        task_id: str,
        block: bool = False,
        timeout: int = 30,
    ) -> str:
        return await CommandToolsMixin.ide_task_output(self, event, task_id, block, timeout)


    @llm_tool_with_doc("ide_task_list")
    @validate_with(IdeTaskListArgs)
    async def ide_task_list(
        self,
        event: AstrMessageEvent,
        active_only: bool = True,
        limit: int = 20,
    ) -> str:
        return await CommandToolsMixin.ide_task_list(self, event, active_only, limit)


    @llm_tool_with_doc("ide_task_stop")
    @validate_with(IdeTaskStopArgs)
    async def ide_task_stop(
        self,
        event: AstrMessageEvent,
        task_id: str,
        reason: str = "Stopped by ide_task_stop",
    ) -> str:
        return await CommandToolsMixin.ide_task_stop(self, event, task_id, reason)


    @llm_tool_with_doc("ide_execute_elevated")
    @validate_with(IdeExecuteElevatedArgs)
    async def ide_execute_elevated(self, event: AstrMessageEvent, command: str) -> str:
        return await CommandToolsMixin.ide_execute_elevated(self, event, command)


    @llm_tool_with_doc("ide_run_test")
    @validate_with(IdeRunTestArgs)
    async def ide_run_test(
        self, event: AstrMessageEvent, test_path: str = "", test_framework: str = "pytest"
    ) -> str:
        return await CommandToolsMixin.ide_run_test(self, event, test_path, test_framework)


    @llm_tool_with_doc("ide_git_clone")
    @validate_with(IdeGitCloneArgs)
    async def ide_git_clone(
        self, event: AstrMessageEvent, repo_url: str, branch: str = ""
    ) -> str:
        return await GitToolsMixin.ide_git_clone(self, event, repo_url, branch)


    @llm_tool_with_doc("ide_list_group_files")
    async def ide_list_group_files(self, event: AstrMessageEvent) -> str:
        return await GroupFileToolsMixin.ide_list_group_files(self, event)


    @llm_tool_with_doc("ide_download_group_file")
    @validate_with(IdeDownloadGroupFileArgs)
    async def ide_download_group_file(
        self, event: AstrMessageEvent, filename: str
    ) -> str:
        return await GroupFileToolsMixin.ide_download_group_file(self, event, filename)


    @llm_tool_with_doc("ide_upload_to_group")
    @validate_with(IdeUploadToGroupArgs)
    async def ide_upload_to_group(
        self, event: AstrMessageEvent, filename: str
    ) -> str:
        return await GroupFileToolsMixin.ide_upload_to_group(self, event, filename)


    @llm_tool_with_doc("ide_think")
    @validate_with(IdeThinkArgs)
    async def ide_think(self, event: AstrMessageEvent, thought: str) -> str:
        return await WorkflowToolsMixin.ide_think(self, event, thought)


    @llm_tool_with_doc("ide_ask_user")
    @validate_with(IdeAskUserArgs)
    async def ide_ask_user(self, event: AstrMessageEvent, question: str) -> str:
        return await WorkflowToolsMixin.ide_ask_user(self, event, question)


    @llm_tool_with_doc("ide_get_history")
    async def ide_get_history(self, event: AstrMessageEvent) -> str:
        return await WorkflowToolsMixin.ide_get_history(self, event)


    @llm_tool_with_doc("ide_list_file_changes")
    @validate_with(IdeListFileChangesArgs)
    async def ide_list_file_changes(self, event: AstrMessageEvent, limit: int = 20) -> str:
        return await WorkflowToolsMixin.ide_list_file_changes(self, event, limit)


    @llm_tool_with_doc("ide_add_todo")
    @validate_with(IdeAddTodoArgs)
    async def ide_add_todo(self, event: AstrMessageEvent, content: str) -> str:
        return await WorkflowToolsMixin.ide_add_todo(self, event, content)


    @llm_tool_with_doc("ide_list_todos")
    async def ide_list_todos(self, event: AstrMessageEvent) -> str:
        return await WorkflowToolsMixin.ide_list_todos(self, event)


    @llm_tool_with_doc("ide_set_todo_list")
    @validate_with(IdeSetTodoListArgs)
    async def ide_set_todo_list(self, event: AstrMessageEvent, todos: str = "") -> str:
        return await WorkflowToolsMixin.ide_set_todo_list(self, event, todos)


    @llm_tool_with_doc("ide_complete_todo")
    @validate_with(IdeCompleteTodoArgs)
    async def ide_complete_todo(
        self, event: AstrMessageEvent, todo_id: int = 0, content_keyword: str = ""
    ) -> str:
        return await WorkflowToolsMixin.ide_complete_todo(self, event, todo_id, content_keyword)


    @llm_tool_with_doc("ide_delete_todo")
    @validate_with(IdeDeleteTodoArgs)
    async def ide_delete_todo(
        self, event: AstrMessageEvent, todo_id: int = 0, content_keyword: str = ""
    ) -> str:
        return await WorkflowToolsMixin.ide_delete_todo(self, event, todo_id, content_keyword)


    @llm_tool_with_doc("ide_pack_and_download")
    @validate_with(IdePackAndDownloadArgs)
    async def ide_pack_and_download(self, event: AstrMessageEvent, dir_name: str = "", zip_name: str = "sandbox_export.zip") -> str:
        return await WorkflowToolsMixin.ide_pack_and_download(self, event, dir_name, zip_name)


    @llm_tool_with_doc("ide_clear_todos")
    @validate_with(IdeClearTodosArgs)
    async def ide_clear_todos(self, event: AstrMessageEvent, confirm: bool = False) -> str:
        return await WorkflowToolsMixin.ide_clear_todos(self, event, confirm)
