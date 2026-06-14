import tempfile
import unittest
from pathlib import Path

from data.plugins.astrbot_plugin_ide_sandbox.base import IdeSandboxCore
from data.plugins.astrbot_plugin_ide_sandbox.web_api import WebApiMixin
from data.plugins.astrbot_plugin_ide_sandbox.workflow_tools import _collect_zip_entries


class FakeEvent:
    def __init__(self, sender_id: str = "10001"):
        self.sender_id = sender_id

    def get_sender_id(self):
        return self.sender_id


class WebRuntimePlugin(IdeSandboxCore, WebApiMixin):
    pass


class RuntimeGuardTest(unittest.TestCase):
    def test_allow_members_does_not_grant_command_tools(self):
        plugin = object.__new__(IdeSandboxCore)
        plugin.master_qq = "1"
        plugin.global_admins = set()
        plugin.admins = set()
        plugin.cmd_admins = set()
        plugin.allow_members = True

        self.assertFalse(plugin._can_use_command_tool(FakeEvent()))

    def test_admin_can_use_command_tools_when_members_allowed(self):
        plugin = object.__new__(IdeSandboxCore)
        plugin.master_qq = "1"
        plugin.global_admins = set()
        plugin.admins = {"10001"}
        plugin.cmd_admins = set()
        plugin.allow_members = True

        self.assertTrue(plugin._can_use_command_tool(FakeEvent()))

    def test_zip_collection_stops_before_total_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_bytes(b"a" * 8)
            (root / "b.txt").write_bytes(b"b" * 8)

            entries, skipped, total_size, truncated_reason = _collect_zip_entries(
                root,
                max_total_bytes=10,
                max_entries=10,
                per_file_limit_bytes=100,
            )

            self.assertEqual(len(entries), 1)
            self.assertEqual(total_size, 8)
            self.assertIn("超过打包大小限制", truncated_reason)
            self.assertEqual(skipped, [])

    def test_record_persists_history_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(IdeSandboxCore)
            plugin.history = {}
            plugin.history_dir = Path(tmp)

            plugin._record("group_1", "write", "system_info.py")

            records = plugin._load_history_records("group_1", limit=10)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["action"], "write")
            self.assertEqual(records[0]["detail"], "system_info.py")

    def test_overview_includes_non_command_tool_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = object.__new__(WebRuntimePlugin)
            plugin.sandbox_root = root / "sandboxes"
            plugin.sandbox_root.mkdir(parents=True, exist_ok=True)
            plugin.history_dir = root / "history"
            plugin.history = {}

            sandbox = plugin._sandbox_path("group_1")
            (sandbox / "system_info.py").write_text("print('ok')", encoding="utf-8")
            plugin._record("group_1", "list_files", "列出 1 个文件")
            plugin._record("group_1", "read_range", "system_info.py:1-10")
            plugin._record("group_1", "execute", "python system_info.py")

            overview = plugin._sandbox_overview(sandbox, history_limit=10, recent_file_limit=5)
            tool_actions = [item["action"] for item in overview["recent_tools"]]
            command_actions = [item["action"] for item in overview["recent_commands"]]

            self.assertEqual(tool_actions, ["list_files", "read_range", "execute"])
            self.assertEqual(command_actions, ["execute"])
            self.assertTrue(all(item["sandbox_id"] == "group_1" for item in overview["recent_tools"]))

    def test_resolve_strips_current_sandbox_prefix_from_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(IdeSandboxCore)
            plugin.sandbox_root = Path(tmp) / "sandboxes"

            target = plugin._resolve(
                "group_1",
                "data/astrbot_plugin_ide_sandbox/sandboxes/group_1/Leaf/src/Main.java",
            )

            expected = plugin.sandbox_root / "group_1" / "Leaf" / "src" / "Main.java"
            self.assertEqual(target, expected.resolve())

    def test_resolve_allows_absolute_path_inside_current_sandbox_without_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(IdeSandboxCore)
            plugin.sandbox_root = Path(tmp) / "sandboxes"
            inside = plugin._get_group_sandbox("group_1") / "Leaf" / "src" / "Main.java"
            outside = Path(tmp) / "outside.txt"

            self.assertEqual(plugin._resolve("group_1", str(inside)), inside.resolve())
            self.assertIsNone(plugin._resolve("group_1", str(outside)))


if __name__ == "__main__":
    unittest.main()
