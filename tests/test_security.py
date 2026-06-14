import tempfile
import unittest
from pathlib import Path

from data.plugins.astrbot_plugin_ide_sandbox.security import (
    _is_command_safe,
    _is_elevated_command_allowed,
    _is_path_safe,
    _safe_filename,
    _safe_relative_path,
)


class SecurityTest(unittest.TestCase):
    def test_safe_filename_removes_path_separators_and_illegal_chars(self):
        self.assertEqual(_safe_filename("../bad:name?.py"), "badname.py")

    def test_safe_relative_path_accepts_nested_project_paths(self):
        self.assertEqual(
            _safe_relative_path("src/app/main.py"),
            ["src", "app", "main.py"],
        )

    def test_safe_relative_path_rejects_traversal_and_absolute_paths(self):
        self.assertIsNone(_safe_relative_path("../secret.txt"))
        self.assertIsNone(_safe_relative_path("C:/Windows/system32"))

    def test_is_path_safe_accepts_child_and_rejects_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "sandbox"
            child = base / "file.txt"
            sibling = Path(tmp) / "outside.txt"
            base.mkdir()
            child.write_text("ok", encoding="utf-8")
            sibling.write_text("no", encoding="utf-8")

            self.assertTrue(_is_path_safe(base, child))
            self.assertFalse(_is_path_safe(base, sibling))

    def test_command_safety_allows_whitelisted_command_and_rejects_shell_meta(self):
        self.assertEqual(_is_command_safe("python script.py", {"python"}), (True, ""))

        ok, reason = _is_command_safe("python script.py && dir", {"python"})

        self.assertFalse(ok)
        self.assertIn("shell", reason)

    def test_super_command_safety_allows_and_but_keeps_critical_ban(self):
        self.assertEqual(
            _is_command_safe(
                "python build.py && dir",
                None,
                allow_and=True,
                unrestricted=True,
            ),
            (True, ""),
        )

        ok, reason = _is_command_safe(
            "rm -rf temp",
            None,
            allow_and=True,
            unrestricted=True,
        )

        self.assertFalse(ok)
        self.assertIn("forbidden", reason.lower())

    def test_command_safety_handles_quoted_executable_paths(self):
        ok, reason = _is_command_safe(
            '"C:/Program Files/Python/python.exe" script.py',
            {"python"},
        )

        self.assertTrue(ok, reason)

    def test_command_safety_rejects_batch_variable_expansion(self):
        ok, reason = _is_command_safe("python %TEMP%/payload.py", {"python"})

        self.assertFalse(ok)
        self.assertIn("variable", reason.lower())

    def test_elevated_commands_use_narrow_allowlist(self):
        self.assertEqual(_is_elevated_command_allowed("winget source list"), (True, ""))

        ok, reason = _is_elevated_command_allowed("python build.py")

        self.assertFalse(ok)
        self.assertIn("elevated", reason.lower())


if __name__ == "__main__":
    unittest.main()
