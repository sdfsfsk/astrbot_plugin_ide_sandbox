import ast
import json
import re
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
APP_JS = PLUGIN_ROOT / "pages" / "ide-dashboard" / "app.js"
INDEX_HTML = PLUGIN_ROOT / "pages" / "ide-dashboard" / "index.html"
STYLE_CSS = PLUGIN_ROOT / "pages" / "ide-dashboard" / "style.css"
WEB_API = PLUGIN_ROOT / "web_api.py"
MAIN_PY = PLUGIN_ROOT / "main.py"
TOOL_MODELS = PLUGIN_ROOT / "tool_models.py"
TOOL_DOCS = PLUGIN_ROOT / "tool_docs"
CONF_SCHEMA = PLUGIN_ROOT / "_conf_schema.json"


class WebUIContractTest(unittest.TestCase):
    def test_bridge_api_get_passes_params_separately(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertNotIn("new URLSearchParams(params)", source)
        self.assertIn("bridge.apiGet(endpoint, params)", source)

    def test_bridge_api_post_passes_endpoint_without_query(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertIn("bridge.apiPost(endpoint, body)", source)
        self.assertNotIn("bridge.apiPost(`${endpoint}", source)

    def test_web_api_uses_resolved_data_root_for_sandboxes(self):
        source = WEB_API.read_text(encoding="utf-8")

        self.assertIn("def _web_data_root", source)
        self.assertRegex(
            source,
            re.compile(r'self\._web_data_root\(\)\s*/\s*"sandboxes"'),
        )

    def test_web_api_exposes_overview_endpoint(self):
        source = WEB_API.read_text(encoding="utf-8")

        self.assertIn('f"{prefix}/overview"', source)
        self.assertIn("self.web_overview", source)
        self.assertIn("async def web_overview", source)

    def test_frontend_has_overview_tab_and_live_refresh(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")

        self.assertIn('data-tab="overview"', html)
        self.assertIn('id="overviewPanel"', html)
        self.assertIn('apiGet("overview"', js)
        self.assertIn("setInterval(refreshLiveData", js)

    def test_overview_surfaces_all_tool_activity_not_only_commands(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")
        api = WEB_API.read_text(encoding="utf-8")

        self.assertIn("<h3>工具执行</h3>", html)
        self.assertIn("overviewToolList", html)
        self.assertIn("工具活动", js)
        self.assertIn("renderOverviewTools(data.tools || data.commands || [])", js)
        self.assertIn("暂无工具执行记录", js)
        self.assertIn('"tool_count": len(tools)', api)
        self.assertIn('"tools": tools[:history_limit]', api)
        self.assertIn('"recent_tools": recent_tools[-history_limit:]', api)
        self.assertNotIn("renderOverviewCommands(data.commands || [])", js)

    def test_clear_sandbox_tool_is_registered_and_documented(self):
        main = MAIN_PY.read_text(encoding="utf-8")
        models = TOOL_MODELS.read_text(encoding="utf-8")
        doc = (TOOL_DOCS / "ide_clear_sandbox.md").read_text(encoding="utf-8")

        self.assertIn("IdeClearSandboxArgs", models)
        self.assertIn("IdeClearSandboxArgs", main)
        self.assertIn('@llm_tool_with_doc("ide_clear_sandbox")', main)
        self.assertIn("ide_clear_sandbox", doc)
        self.assertIn("不要使用 ide_execute", doc)

    def test_dashboard_commands_have_visible_descriptions(self):
        module = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
        command_handlers = []
        for node in ast.walk(module):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                func = call.func if call else decorator
                if isinstance(func, ast.Attribute) and func.attr == "command":
                    command_handlers.append(node)

        missing = [node.name for node in command_handlers if not ast.get_docstring(node)]
        self.assertEqual([], missing)

    def test_live_badge_does_not_flash_on_each_poll(self):
        js = APP_JS.read_text(encoding="utf-8")

        self.assertNotIn('setLiveStatus("同步中"', js)
        self.assertNotIn("setLiveStatus(`实时 ${time}`", js)
        self.assertIn('setLiveStatus("实时更新", "ok")', js)

    def test_editor_action_buttons_have_readable_dark_and_disabled_states(self):
        css = STYLE_CSS.read_text(encoding="utf-8")

        self.assertNotIn("--primary: #60cdff", css)
        self.assertNotIn("--danger: #ff8a80", css)
        self.assertIn("--primary-foreground: #ffffff", css)
        self.assertIn("--danger-foreground: #ffffff", css)
        self.assertIn("color: var(--primary-foreground)", css)
        self.assertIn("color: var(--danger-foreground)", css)
        self.assertRegex(
            css,
            re.compile(
                r"button\.primary:disabled,\s*\nbutton\.danger:disabled\s*\{[^}]*"
                r"background:\s*var\(--surface-muted\)",
                re.S,
            ),
        )
        self.assertRegex(
            css,
            re.compile(
                r"button\.primary:disabled,\s*\nbutton\.danger:disabled\s*\{[^}]*"
                r"opacity:\s*1",
                re.S,
            ),
        )

    def test_file_tree_selection_keeps_destructive_actions_available(self):
        js = APP_JS.read_text(encoding="utf-8")
        css = STYLE_CSS.read_text(encoding="utf-8")

        self.assertIn('const item = document.createElement("button");', js)
        self.assertIn('item.type = "button";', js)
        self.assertIn('item.setAttribute("aria-selected"', js)
        self.assertIn("setSelectedTreeItem(el);", js)
        self.assertIn("prepareSelectedPath(node);", js)
        self.assertIn("const message = getErrorMessage(e);", js)
        self.assertIn("showUnreadableSelection(path, message);", js)
        self.assertIn('switchTab("editor");', js)
        self.assertRegex(
            css,
            re.compile(r"\.tree-item\s*\{[^}]*width:\s*100%;[^}]*appearance:\s*none", re.S),
        )
        self.assertRegex(
            css,
            re.compile(r"\.tree-item:hover:not\(:disabled\)\s*\{[^}]*transform:\s*none", re.S),
        )

    def test_destructive_actions_use_inline_confirm_and_verify_delete_result(self):
        js = APP_JS.read_text(encoding="utf-8")
        css = STYLE_CSS.read_text(encoding="utf-8")
        api = WEB_API.read_text(encoding="utf-8")

        self.assertIn("async function confirmModal", js)
        self.assertIn("await confirmModal", js)
        self.assertNotIn("confirm(`确定要删除", js)
        self.assertIn(".modal.confirm-mode #modalInput", css)
        self.assertIn("await asyncio.to_thread(shutil.rmtree, path, ignore_errors=False)", api)
        self.assertRegex(
            api,
            re.compile(r"if\s+path\.exists\(\):\s*\n\s*return _err", re.S),
        )

    def test_unpreviewable_files_are_not_reported_as_request_errors(self):
        js = APP_JS.read_text(encoding="utf-8")
        api = WEB_API.read_text(encoding="utf-8")

        self.assertIn("res.previewable === false", js)
        self.assertIn("showUnreadableSelection(path, res.reason)", js)
        self.assertNotIn("showUnreadableSelection(path, e.message);", js)
        self.assertIn('"previewable": False', api)
        self.assertIn('"reason": "二进制文件不支持通过 WebUI 文本编辑器读取"', api)


    def test_overview_cards_have_non_overlapping_vertical_flow(self):
        source = STYLE_CSS.read_text(encoding="utf-8")

        self.assertRegex(
            source,
            re.compile(r"\.overview-item\s*\{[^}]*display:\s*grid", re.S),
        )
        self.assertRegex(
            source,
            re.compile(r"\.overview-item\s*\{[^}]*gap:\s*8px", re.S),
        )
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto", source)
        self.assertIn("-webkit-line-clamp: 2", source)

    def test_overview_has_no_decorative_blob_layer(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        css = STYLE_CSS.read_text(encoding="utf-8")

        self.assertNotIn("bg-fx", html)
        self.assertNotIn(".blob", css)

    def test_background_uses_soft_light_spots_without_blob_dom(self):
        css = STYLE_CSS.read_text(encoding="utf-8")

        self.assertIn("body::before", css)
        self.assertIn("body::after", css)
        self.assertIn(".app::before", css)
        self.assertIn(".app::after", css)
        self.assertIn(".app-header,\n.status-strip,\n.app-body", css)
        self.assertIn("radial-gradient", css)
        self.assertIn("@keyframes mica-drift", css)
        self.assertIn("@keyframes soft-spot-drift", css)
        self.assertIn("animation: mica-drift 24s", css)
        self.assertIn("animation: soft-spot-drift 38s", css)
        self.assertIn("prefers-reduced-motion: reduce", css)
        self.assertNotIn("@keyframes mica-grid", css)
        self.assertNotIn("background-size: 52px 52px", css)
        self.assertNotIn("repeating-linear-gradient", css)

    def test_overview_mode_hides_file_tree_and_top_sandbox_picker_with_motion(self):
        js = APP_JS.read_text(encoding="utf-8")
        css = STYLE_CSS.read_text(encoding="utf-8")

        self.assertIn("appShell: document.querySelector", js)
        self.assertIn('switchTab("overview");', js)
        self.assertIn('els.appShell.classList.toggle("overview-mode", name === "overview")', js)
        self.assertIn(".app.overview-mode .sidebar", css)
        self.assertIn(".app.overview-mode .status-strip", css)
        self.assertIn(".app.overview-mode .header-actions label", css)
        self.assertIn(".app.overview-mode #sandboxSelect", css)
        self.assertIn("animation: panel-enter", css)
        self.assertIn("@keyframes panel-enter", css)
        self.assertIn("transform: translateX(-18px)", css)

    def test_webui_command_output_uses_configured_max_output_len(self):
        api = WEB_API.read_text(encoding="utf-8")

        self.assertIn('output_limit = getattr(self, "max_output_len", MAX_OUTPUT_LEN)', api)
        self.assertIn("self._decode_process_output(stdout)[:output_limit]", api)
        self.assertIn("self._decode_process_output(stderr)[:output_limit]", api)
        self.assertIn('"output": output[-output_limit:]', api)
        self.assertNotIn("self._decode_process_output(stdout)[:MAX_OUTPUT_LEN]", api)
        self.assertNotIn("self._decode_process_output(stderr)[:MAX_OUTPUT_LEN]", api)
        self.assertNotIn('"output": output[-MAX_OUTPUT_LEN:]', api)

    def test_allow_members_config_hint_keeps_command_permission_explicit(self):
        schema = json.loads(CONF_SCHEMA.read_text(encoding="utf-8"))
        hint = schema["ide_sandbox_allow_members"]["hint"]

        self.assertIn("命令执行仍需主人、沙盒管理员或 CMD 管理员", hint)
        self.assertNotIn("操作沙盒文件和执行命令", hint)


if __name__ == "__main__":
    unittest.main()
