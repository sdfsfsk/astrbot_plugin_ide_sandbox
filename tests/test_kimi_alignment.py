import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from astrbot.core.star.star_handler import EventType, star_handlers_registry
from data.plugins.astrbot_plugin_ide_sandbox.base import IdeSandboxCore
from data.plugins.astrbot_plugin_ide_sandbox.command_tools import CommandToolsMixin
from data.plugins.astrbot_plugin_ide_sandbox.events import EventCommandMixin
from data.plugins.astrbot_plugin_ide_sandbox.file_tools import FileToolsMixin
from data.plugins.astrbot_plugin_ide_sandbox.main import IdeSandboxPlugin
from data.plugins.astrbot_plugin_ide_sandbox.tool_models import (
    IdeExecuteArgs,
    IdeReadFileArgs,
    IdeTaskListArgs,
    IdeTaskOutputArgs,
)
from data.plugins.astrbot_plugin_ide_sandbox.workflow_tools import WorkflowToolsMixin


class FakeBot:
    async def call_action(self, *_, **__):
        raise RuntimeError("no platform in unit tests")


class FakeEvent:
    def __init__(self, sender_id: str = "owner", group_id: str = "42"):
        self.sender_id = sender_id
        self.group_id = group_id
        self.bot = FakeBot()
        self.unified_msg_origin = f"group:{group_id}"

    def get_sender_id(self):
        return self.sender_id

    def get_group_id(self):
        return self.group_id

    def plain_result(self, message):
        return message

    async def send(self, *_):
        return None

    def is_stopped(self):
        return False


class TestPlugin(IdeSandboxCore, FileToolsMixin, CommandToolsMixin, WorkflowToolsMixin):
    pass


def make_plugin(tmp: Path) -> TestPlugin:
    plugin = object.__new__(TestPlugin)
    plugin.sandbox_root = tmp / "sandboxes"
    plugin.sandbox_root.mkdir(parents=True, exist_ok=True)
    plugin.todos_dir = tmp / "todos"
    plugin.todos_dir.mkdir(parents=True, exist_ok=True)
    plugin.master_qq = "owner"
    plugin.global_admins = set()
    plugin.allow_members = True
    plugin.admins = set()
    plugin.terminal_admins = set()
    plugin.cmd_admins = {"cmd"}
    plugin.broadcast_actions = False
    plugin.status_notice_threshold_kb = 0
    plugin.custom_env = {}
    plugin.custom_paths = []
    plugin.allow_git_clone = False
    plugin.allow_execution = True
    plugin.allow_test = True
    plugin.git_clone_limit_mb = 100
    plugin.auto_download = False
    plugin.auto_download_keywords = set()
    plugin.pip_mirror = ""
    plugin.git_mirror = ""
    plugin.maven_mirror = ""
    plugin.gradle_mirror = ""
    plugin.max_file_size_mb = 10
    plugin.single_write_limit_kb = 256
    plugin.single_write_limit_bytes = 256 * 1024
    plugin.cover_only_mode = False
    plugin.cmd_timeout = 30
    plugin.max_output_len = 4000
    plugin.admins_can_bypass = False
    plugin.allow_elevated = False
    plugin.history = {}
    plugin.todos = {}
    plugin._todo_id_counter = {}
    plugin.file_changes = {}
    plugin._broadcast_tasks = set()
    plugin._background_tasks = set()
    plugin._broadcast_locks = {}
    plugin._llm_heartbeat_tasks = {}
    plugin._background_commands = {}
    plugin.background_log_dir = tmp / "background"
    plugin.background_log_dir.mkdir(parents=True, exist_ok=True)
    return plugin


class KimiAlignmentTest(unittest.IsolatedAsyncioTestCase):
    def test_main_uses_event_command_mixin_as_single_command_source(self):
        self.assertTrue(issubclass(IdeSandboxPlugin, EventCommandMixin))
        self.assertFalse(hasattr(__import__(IdeSandboxPlugin.__module__, fromlist=["BANNED_COMMANDS"]), "BANNED_COMMANDS"))

    def test_main_registers_event_hooks_under_plugin_module(self):
        expected_hooks = {
            EventType.OnWaitingLLMRequestEvent: "on_waiting_llm_request",
            EventType.OnAgentDoneEvent: "on_agent_done",
            EventType.OnUsingLLMToolEvent: "on_using_llm_tool",
        }

        for event_type, handler_name in expected_hooks.items():
            handlers = [
                handler
                for handler in star_handlers_registry.get_handlers_by_event_type(
                    event_type,
                    only_activated=False,
                )
                if handler.handler_module_path == IdeSandboxPlugin.__module__
                and handler.handler_name == handler_name
            ]

            self.assertEqual(
                len(handlers),
                1,
                f"{handler_name} should be registered under {IdeSandboxPlugin.__module__}",
            )

    async def test_non_ide_chat_does_not_start_ide_waiting_heartbeat(self):
        class ChatEvent(FakeEvent):
            def get_message_str(self):
                return "松子摸摸头"

            def get_message_outline(self):
                return ""

        class FakePlugin(IdeSandboxPlugin):
            def __init__(self):
                self.llm_progress_notice = True
                self.llm_progress_heartbeat = True
                self._llm_heartbeat_tasks = {}
                self.messages = []
                self.heartbeat_started = False

            def _get_sandbox_id(self, event):
                return "42"

            async def _status_notice(self, event, message):
                self.messages.append(message)

            async def _llm_heartbeat_loop(self, event, key):
                self.heartbeat_started = True

        plugin = FakePlugin()

        await plugin.on_waiting_llm_request(ChatEvent())
        await asyncio.sleep(0)

        self.assertFalse(plugin.heartbeat_started)
        self.assertEqual(plugin.messages, [])
        self.assertEqual(plugin._llm_heartbeat_tasks, {})

    def test_read_file_model_rejects_zero_offset_and_defaults_to_page(self):
        with self.assertRaises(ValidationError):
            IdeReadFileArgs(filename="x.txt", line_offset=0)

        args = IdeReadFileArgs(filename="x.txt")

        self.assertEqual(args.n_lines, 1000)

    def test_background_execution_requires_description(self):
        with self.assertRaises(ValidationError):
            IdeExecuteArgs(command="python server.py", run_in_background=True)

        args = IdeExecuteArgs(
            command="python server.py",
            run_in_background=True,
            description="run dev server",
        )

        self.assertEqual(args.description, "run dev server")

    def test_task_models_support_list_and_blocking_output(self):
        output_args = IdeTaskOutputArgs(task_id="abc123", block=True, timeout=5)
        list_args = IdeTaskListArgs(active_only=False, limit=10)

        self.assertTrue(output_args.block)
        self.assertEqual(output_args.timeout, 5)
        self.assertFalse(list_args.active_only)
        self.assertEqual(list_args.limit, 10)

    async def test_edit_file_replaces_one_match_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin = make_plugin(Path(tmpdir))
            event = FakeEvent()
            path = plugin._get_group_sandbox("group_42") / "sample.txt"
            path.write_text("foo foo", encoding="utf-8")

            result = await FileToolsMixin.ide_edit_file(
                plugin,
                event,
                "sample.txt",
                old_string="foo",
                new_string="bar",
            )

            self.assertIn("替换 1 处", result)
            self.assertEqual(path.read_text(encoding="utf-8"), "bar foo")

    async def test_edit_file_replace_all_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin = make_plugin(Path(tmpdir))
            event = FakeEvent()
            path = plugin._get_group_sandbox("group_42") / "sample.txt"
            path.write_text("foo foo", encoding="utf-8")

            result = await FileToolsMixin.ide_edit_file(
                plugin,
                event,
                "sample.txt",
                edits='[{"old_string":"foo","new_string":"bar","replace_all":true}]',
            )

            self.assertIn("替换 2 处", result)
            self.assertEqual(path.read_text(encoding="utf-8"), "bar bar")

    async def test_read_file_blocks_sensitive_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin = make_plugin(Path(tmpdir))
            event = FakeEvent()
            path = plugin._get_group_sandbox("group_42") / ".env"
            path.write_text("TOKEN=secret", encoding="utf-8")

            result = await FileToolsMixin.ide_read_file(plugin, event, ".env")

            self.assertIn("敏感", result)
            self.assertNotIn("secret", result)

    async def test_read_file_defaults_to_bounded_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin = make_plugin(Path(tmpdir))
            event = FakeEvent()
            path = plugin._get_group_sandbox("group_42") / "long.txt"
            path.write_text("\n".join(f"line {i}" for i in range(1, 1102)), encoding="utf-8")

            result = await FileToolsMixin.ide_read_file(plugin, event, "long.txt")

            self.assertIn("1:", result)
            self.assertIn("1000:", result)
            self.assertNotIn("1001:", result)
            self.assertIn("已达到", result)

    async def test_glob_rejects_top_level_recursive_pattern(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin = make_plugin(Path(tmpdir))
            event = FakeEvent()
            root = plugin._get_group_sandbox("group_42")
            (root / "app.py").write_text("print('ok')", encoding="utf-8")

            result = await FileToolsMixin.ide_glob(plugin, event, "**/*.py")

            self.assertIn("以 ** 开头", result)
            self.assertIn("app.py", result)

    async def test_search_text_filters_sensitive_files_in_file_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin = make_plugin(Path(tmpdir))
            event = FakeEvent()
            root = plugin._get_group_sandbox("group_42")
            (root / "app.py").write_text("needle\n", encoding="utf-8")
            (root / "secret_token.txt").write_text("needle\n", encoding="utf-8")

            result = await FileToolsMixin.ide_search_text(
                plugin,
                event,
                "needle",
                output_mode="files_with_matches",
            )

            self.assertIn("app.py", result)
            self.assertNotIn("secret_token.txt", result)

    async def test_edit_file_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin = make_plugin(Path(tmpdir))
            event = FakeEvent()
            path = plugin._get_group_sandbox("group_42") / "sample.txt"
            path.write_text("foo", encoding="utf-8")

            result = await FileToolsMixin.ide_edit_file(
                plugin,
                event,
                "sample.txt",
                old_string="foo",
                new_string="bar",
                dry_run=True,
            )

            self.assertIn("未实际写入", result)
            self.assertEqual(path.read_text(encoding="utf-8"), "foo")

    async def test_set_todo_list_uses_kimi_status_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin = make_plugin(Path(tmpdir))
            event = FakeEvent()

            result = await WorkflowToolsMixin.ide_set_todo_list(
                plugin,
                event,
                '[{"title":"read","status":"done"},{"title":"fix","status":"in_progress"},{"title":"verify","status":"pending"}]',
            )
            listed = await WorkflowToolsMixin.ide_list_todos(plugin, event)

            self.assertIn("[done] read", result)
            self.assertIn("1/3 完成", listed)
            self.assertIn("进行中", listed)

    async def test_background_task_output_requires_command_permission_not_membership(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin = make_plugin(Path(tmpdir))
            bg = SimpleNamespace(
                task_id="task1",
                description="server",
                command="python server.py",
                status="running",
                returncode=None,
                error_message="",
                stdout_buffer=[],
                stderr_buffer=[],
                owner_id="cmd",
                output_path=Path(tmpdir) / "task1.log",
            )
            plugin._background_commands["task1"] = bg

            result = await CommandToolsMixin.ide_task_output(plugin, FakeEvent(sender_id="member"), "task1")

            self.assertIn("权限不足", result)

    async def test_clear_sandbox_uses_file_permission_without_command_permission(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin = make_plugin(Path(tmpdir))
            event = FakeEvent(sender_id="member")
            sandbox_id = plugin._get_sandbox_id(event)
            sandbox = plugin._get_group_sandbox(sandbox_id)
            (sandbox / "a.txt").write_text("a", encoding="utf-8")
            (sandbox / "dir").mkdir()
            (sandbox / "dir" / "b.txt").write_text("b", encoding="utf-8")

            self.assertFalse(plugin._can_use_command_tool(event))

            preview = await FileToolsMixin.ide_clear_sandbox(plugin, event, confirm=False)
            result = await FileToolsMixin.ide_clear_sandbox(plugin, event, confirm=True)

            self.assertIn("confirm=true", preview)
            self.assertIn("已清空沙盒", result)
            self.assertEqual(list(sandbox.iterdir()), [])
            self.assertEqual(plugin.history[sandbox_id][-1]["action"], "clear_sandbox")

    def test_background_output_uses_tail_preview_and_full_log_hint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin = make_plugin(Path(tmpdir))
            log_path = Path(tmpdir) / "task.log"
            log_path.write_text("first\n" + ("x" * 5000) + "\nlast\n", encoding="utf-8")
            bg = SimpleNamespace(
                task_id="task1",
                description="server",
                command="python server.py",
                status="completed",
                returncode=0,
                error_message="",
                stdout_buffer=[],
                stderr_buffer=[],
                output_path=log_path,
                owner_id="owner",
            )

            result = plugin._format_background_output(bg, max_len=200)

            self.assertIn("完整日志", result)
            self.assertIn(str(log_path.resolve()), result)
            self.assertIn("last", result)
            self.assertNotIn("first\n", result)


if __name__ == "__main__":
    unittest.main()
