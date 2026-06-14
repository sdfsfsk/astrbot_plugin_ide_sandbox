import unittest
from types import SimpleNamespace

from astrbot.dashboard.routes.plugin import PluginRoute


class DashboardWebuiGateTest(unittest.TestCase):
    def test_plugin_page_enabled_by_config_respects_short_webui_flag(self):
        plugin = SimpleNamespace(
            name="astrbot_plugin_ide_sandbox",
            config={"ide_sandbox_webui_enabled": False},
        )

        self.assertFalse(PluginRoute._plugin_pages_enabled_by_config(plugin))

    def test_plugin_page_enabled_by_config_defaults_to_enabled(self):
        plugin = SimpleNamespace(name="sample_plugin", config={})

        self.assertTrue(PluginRoute._plugin_pages_enabled_by_config(plugin))


if __name__ == "__main__":
    unittest.main()
