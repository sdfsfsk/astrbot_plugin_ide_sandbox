"""IDE 沙盒插件 WebUI 后端 API。

为 AstrBot Dashboard 插件页面提供文件管理、命令执行、任务监控、待办管理等功能。
所有接口均受 Dashboard JWT 登录保护，并进一步校验用户名白名单。
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger
from quart import Response, g, jsonify, request, send_file

from .security import (
    DEFAULT_EXECUTION_WHITELIST,
    SEARCH_SKIP_DIRS,
    TEXT_EXTENSIONS,
    _is_command_safe,
    _is_path_safe,
    _is_protected_path,
    _is_sensitive_file,
    _safe_filename,
    _safe_relative_path,
    _strip_current_sandbox_prefix,
)

MAX_TREE_DEPTH = 8
MAX_TREE_ENTRIES = 500
MAX_READ_LINES = 2000
MAX_READ_BYTES = 2 * 1024 * 1024
MAX_READ_LINE_LENGTH = 2000
READ_SNIFF_BYTES = 8192
MAX_OUTPUT_LEN = 8000
PLUGIN_ROUTE_PREFIX = "/astrbot_plugin_ide_sandbox"
COMMAND_ACTIONS = {"execute", "execute_bg", "execute_elevated", "run_test", "git_clone"}


def _ok(data: Any = None, message: str = "") -> Response:
    payload: dict[str, Any] = {"status": "ok"}
    if data is not None:
        payload["data"] = data
    if message:
        payload["message"] = message
    return jsonify(payload)


def _err(message: str, code: int = 400) -> Response:
    r = jsonify({"status": "error", "message": message})
    r.status_code = code
    return r


def _truncate_line(line: str) -> str:
    if len(line) <= MAX_READ_LINE_LENGTH:
        return line
    return line[:MAX_READ_LINE_LENGTH] + " ...[line truncated]"


class WebApiMixin:
    """为 Dashboard 插件页面暴露 HTTP API。"""

    # ==================== 权限与工具 ====================

    def _webui_enabled(self) -> bool:
        return getattr(self, "ide_sandbox_webui_enabled", False)

    def _webui_allowed_users(self) -> set[str]:
        raw = getattr(self, "ide_sandbox_webui_allowed_users", "")
        if not raw:
            return set()
        return {x.strip().lower() for x in str(raw).split(",") if x.strip()}

    def _check_web_permission(self) -> tuple[bool, Optional[Response]]:
        """检查当前 Dashboard 用户是否有权访问 WebUI。"""
        if not self._webui_enabled():
            return False, _err("WebUI 未启用，请在插件配置中开启 ide_sandbox_webui_enabled", 403)
        username = g.get("username", "").strip().lower()
        if not username:
            return False, _err("未获取到 Dashboard 用户信息", 401)
        allowed = self._webui_allowed_users()
        if not allowed:
            return False, _err("WebUI 访问白名单为空，请在 ide_sandbox_webui_allowed_users 中添加允许的用户", 403)
        if username not in allowed:
            return False, _err(f"用户 `{username}` 不在 WebUI 访问白名单中", 403)
        return True, None

    def _safe_sandbox_id(self, sandbox_id: str) -> str:
        return re.sub(r'[^\w-]', '', str(sandbox_id))

    def _web_data_root(self) -> Path:
        sandbox_root = getattr(self, "sandbox_root", None)
        if sandbox_root:
            sandbox_path = Path(sandbox_root)
            if sandbox_path.is_absolute():
                return sandbox_path.parent
        return Path(__file__).resolve().parents[3] / "data" / "astrbot_plugin_ide_sandbox"

    def _sandbox_path(self, sandbox_id: str) -> Path:
        safe_id = self._safe_sandbox_id(sandbox_id)
        d = self._web_data_root() / "sandboxes" / safe_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _resolve_web_path(
        self,
        sandbox_id: str,
        filename: str,
        allow_bypass: bool = False,
    ) -> Optional[Path]:
        """解析 WebUI 请求中的文件路径。"""
        base = self._sandbox_path(sandbox_id)
        raw_path = Path(filename)
        if raw_path.is_absolute():
            target = raw_path.resolve()
            if _is_path_safe(base, target):
                return target
            if not allow_bypass:
                return None
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

    def _history_records_for_web(self, sandbox_id: str, limit: int) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if hasattr(self, "_load_history_records"):
            records = self._load_history_records(sandbox_id, limit)  # type: ignore[attr-defined]
        memory_records = getattr(self, "history", {}).get(sandbox_id, [])
        if memory_records:
            seen = {
                (item.get("time"), item.get("action"), item.get("detail"))
                for item in records
                if isinstance(item, dict)
            }
            for item in memory_records:
                key = (item.get("time"), item.get("action"), item.get("detail"))
                if key not in seen:
                    records.append(item)
        if not records:
            records = memory_records
        return records[-limit:]

    def _sandbox_overview(self, sandbox: Path, history_limit: int, recent_file_limit: int) -> dict[str, Any]:
        sandbox_id = sandbox.name
        file_count = 0
        dir_count = 0
        latest_mtime = 0.0
        scanned = 0
        truncated = False
        recent_files: list[dict[str, Any]] = []

        try:
            iterator = sandbox.rglob("*")
            for child in iterator:
                if child.is_symlink():
                    continue
                if any(part in SEARCH_SKIP_DIRS for part in child.relative_to(sandbox).parts):
                    continue
                scanned += 1
                if scanned > MAX_TREE_ENTRIES * 4:
                    truncated = True
                    break
                try:
                    stat = child.stat()
                    latest_mtime = max(latest_mtime, stat.st_mtime)
                except Exception:
                    stat = None
                if child.is_dir():
                    dir_count += 1
                    continue
                if child.is_file():
                    file_count += 1
                    recent_files.append({
                        "name": child.name,
                        "path": str(child.relative_to(sandbox)).replace("\\", "/"),
                        "size": stat.st_size if stat else 0,
                        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat() if stat else "",
                    })
        except Exception as e:
            logger.debug(f"[IdeSandbox] 总览扫描沙盒失败 {sandbox_id}: {e}")

        recent_files.sort(key=lambda item: item.get("mtime", ""), reverse=True)
        history = self._history_records_for_web(sandbox_id, history_limit)
        recent_tools = []
        recent_commands = []
        cwd = str(self._sandbox_path(sandbox_id))
        for record in history:
            tool = dict(record)
            tool["sandbox_id"] = sandbox_id
            tool["cwd"] = cwd
            recent_tools.append(tool)
            if record.get("action") in COMMAND_ACTIONS:
                recent_commands.append(tool)

        return {
            "id": sandbox_id,
            "path": str(sandbox),
            "file_count": file_count,
            "dir_count": dir_count,
            "latest_mtime": datetime.fromtimestamp(latest_mtime).isoformat() if latest_mtime else "",
            "recent_files": recent_files[:recent_file_limit],
            "recent_activity": history[-1] if history else None,
            "recent_tools": recent_tools[-history_limit:],
            "recent_commands": recent_commands[-history_limit:],
            "truncated": truncated,
        }

    def _build_run_env(self) -> dict:
        """复用基类环境构建逻辑（如果基类已实现）。"""
        if hasattr(super(), "_build_run_env"):
            return super()._build_run_env()  # type: ignore[misc]
        run_env = os.environ.copy()
        run_env.setdefault("PYTHONUTF8", "1")
        run_env.setdefault("PYTHONIOENCODING", "utf-8")
        run_env.setdefault("NO_COLOR", "1")
        run_env.setdefault("TERM", "dumb")
        custom_env = getattr(self, "custom_env", {}) or {}
        for k, v in custom_env.items():
            run_env[str(k)] = str(v)
        path_sep = ";" if os.name == "nt" else ":"
        path_parts = [p.strip() for p in getattr(self, "custom_paths", []) if p.strip()]
        if os.name == "nt":
            system_root = run_env.get("SystemRoot") or run_env.get("WINDIR") or r"C:\Windows"
            path_parts.extend(
                [
                    str(Path(system_root) / "System32"),
                    str(Path(system_root) / "System32" / "Wbem"),
                    str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0"),
                    str(system_root),
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
        if hasattr(super(), "_decode_process_output"):
            return super()._decode_process_output(data)  # type: ignore[misc]
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

    # ==================== API 注册 ====================

    def _register_web_apis(self) -> None:
        prefix = PLUGIN_ROUTE_PREFIX
        self.context.register_web_api(
            f"{prefix}/info",
            self.web_info,
            ["GET"],
            "获取插件 WebUI 信息与当前用户",
        )
        self.context.register_web_api(
            f"{prefix}/sandboxes",
            self.web_list_sandboxes,
            ["GET"],
            "列出所有沙盒",
        )
        self.context.register_web_api(
            f"{prefix}/overview",
            self.web_overview,
            ["GET"],
            "获取所有沙盒的总览与最近活动",
        )
        self.context.register_web_api(
            f"{prefix}/file_tree",
            self.web_file_tree,
            ["GET"],
            "获取沙盒文件树",
        )
        self.context.register_web_api(
            f"{prefix}/read_file",
            self.web_read_file,
            ["GET"],
            "读取沙盒文件内容",
        )
        self.context.register_web_api(
            f"{prefix}/write_file",
            self.web_write_file,
            ["POST"],
            "写入或覆盖沙盒文件",
        )
        self.context.register_web_api(
            f"{prefix}/delete_file",
            self.web_delete_file,
            ["POST"],
            "删除沙盒文件或目录",
        )
        self.context.register_web_api(
            f"{prefix}/mkdir",
            self.web_mkdir,
            ["POST"],
            "创建沙盒目录",
        )
        self.context.register_web_api(
            f"{prefix}/rename",
            self.web_rename,
            ["POST"],
            "重命名沙盒文件或目录",
        )
        self.context.register_web_api(
            f"{prefix}/execute",
            self.web_execute,
            ["POST"],
            "在沙盒中执行命令",
        )
        self.context.register_web_api(
            f"{prefix}/tasks",
            self.web_task_list,
            ["GET"],
            "列出后台任务",
        )
        self.context.register_web_api(
            f"{prefix}/task_output",
            self.web_task_output,
            ["GET"],
            "获取后台任务输出",
        )
        self.context.register_web_api(
            f"{prefix}/task_stop",
            self.web_task_stop,
            ["POST"],
            "停止后台任务",
        )
        self.context.register_web_api(
            f"{prefix}/todos",
            self.web_todos,
            ["GET", "POST"],
            "获取或设置待办事项",
        )
        self.context.register_web_api(
            f"{prefix}/history",
            self.web_history,
            ["GET"],
            "获取操作历史",
        )
        self.context.register_web_api(
            f"{prefix}/config",
            self.web_config,
            ["GET"],
            "获取插件配置摘要",
        )

    # ==================== API 实现 ====================

    async def web_info(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        username = g.get("username", "")
        return _ok({
            "plugin_name": "ide_sandbox",
            "display_name": "IDE 管理",
            "username": username,
            "webui_enabled": self._webui_enabled(),
            "sandbox_root": str(self._web_data_root() / "sandboxes"),
        })

    async def web_list_sandboxes(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        root = self._web_data_root() / "sandboxes"
        sandboxes = []
        if root.exists():
            for item in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if item.is_dir():
                    try:
                        file_count = sum(1 for _ in item.rglob("*") if _.is_file())
                    except Exception:
                        file_count = -1
                    sandboxes.append({
                        "id": item.name,
                        "path": str(item),
                        "file_count": file_count,
                    })
        return _ok({"sandboxes": sandboxes})

    async def web_overview(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        history_limit = max(1, min(int(request.args.get("history_limit", 20) or 20), 100))
        recent_file_limit = max(0, min(int(request.args.get("recent_files", 8) or 8), 30))
        root = self._web_data_root() / "sandboxes"
        sandboxes: list[dict[str, Any]] = []
        tools: list[dict[str, Any]] = []
        commands: list[dict[str, Any]] = []
        total_files = 0
        total_dirs = 0

        if root.exists():
            for sandbox in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if not sandbox.is_dir():
                    continue
                info = await asyncio.to_thread(
                    self._sandbox_overview,
                    sandbox,
                    history_limit,
                    recent_file_limit,
                )
                sandboxes.append(info)
                total_files += int(info.get("file_count", 0) or 0)
                total_dirs += int(info.get("dir_count", 0) or 0)
                tools.extend(info.get("recent_tools", []))
                commands.extend(info.get("recent_commands", []))

        tools.sort(key=lambda item: item.get("time", ""), reverse=True)
        commands.sort(key=lambda item: item.get("time", ""), reverse=True)
        sandboxes.sort(
            key=lambda item: (
                (item.get("recent_activity") or {}).get("time", ""),
                item.get("latest_mtime", ""),
                item.get("id", ""),
            ),
            reverse=True,
        )
        return _ok({
            "summary": {
                "sandbox_count": len(sandboxes),
                "file_count": total_files,
                "dir_count": total_dirs,
                "tool_count": len(tools),
                "command_count": len(commands),
                "updated_at": datetime.now().isoformat(),
            },
            "sandboxes": sandboxes,
            "tools": tools[:history_limit],
            "commands": commands[:history_limit],
        })

    async def web_file_tree(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        sandbox_id = request.args.get("sandbox_id", "").strip()
        root = request.args.get("root", "").strip()
        max_depth = int(request.args.get("max_depth", 4) or 4)
        if not sandbox_id:
            return _err("缺少 sandbox_id")
        if max_depth < 1:
            max_depth = 1
        if max_depth > MAX_TREE_DEPTH:
            max_depth = MAX_TREE_DEPTH

        sandbox = self._sandbox_path(sandbox_id)
        target = sandbox if not root else self._resolve_web_path(sandbox_id, root)
        if not target or not target.exists() or not target.is_dir():
            return _err(f"目录 `{root or '.'}` 不存在")

        def _build_tree(path: Path, depth: int):
            count = 0
            truncated = False

            def _walk(p: Path, d: int) -> list[dict[str, Any]]:
                nonlocal count, truncated
                if truncated or d > depth:
                    return []
                try:
                    children = sorted(
                        [c for c in p.iterdir() if not c.is_symlink()],
                        key=lambda x: (not x.is_dir(), x.name.lower()),
                    )
                except Exception:
                    return []
                visible = [c for c in children if c.name not in SEARCH_SKIP_DIRS]
                nodes: list[dict[str, Any]] = []
                for child in visible:
                    if count >= MAX_TREE_ENTRIES:
                        truncated = True
                        return nodes
                    count += 1
                    node: dict[str, Any] = {
                        "name": child.name,
                        "path": str(child.relative_to(sandbox)).replace("\\", "/"),
                        "type": "directory" if child.is_dir() else "file",
                    }
                    if child.is_file():
                        try:
                            node["size"] = child.stat().st_size
                            node["mtime"] = datetime.fromtimestamp(child.stat().st_mtime).isoformat()
                        except Exception:
                            pass
                    else:
                        node["children"] = _walk(child, d + 1) if d < depth else []
                    nodes.append(node)
                return nodes

            return _walk(path, 1), truncated, count

        tree, truncated, total = await asyncio.to_thread(_build_tree, target, max_depth)
        return _ok({
            "sandbox_id": sandbox_id,
            "root": str(target.relative_to(sandbox)).replace("\\", "/") if target != sandbox else "",
            "tree": tree,
            "truncated": truncated,
            "total": total,
        })

    async def web_read_file(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        sandbox_id = request.args.get("sandbox_id", "").strip()
        path_name = request.args.get("path", "").strip()
        line_offset = int(request.args.get("line_offset", 1) or 1)
        n_lines = int(request.args.get("n_lines", 500) or 500)

        def _unpreviewable(reason: str) -> Response:
            return _ok({
                "sandbox_id": sandbox_id,
                "path": path_name,
                "previewable": False,
                "content": "",
                "reason": reason,
            })

        if not sandbox_id or not path_name:
            return _err("缺少 sandbox_id 或 path")

        path = self._resolve_web_path(sandbox_id, path_name)
        if not path or not path.exists():
            return _err(f"文件 `{path_name}` 不存在")
        if path.is_dir():
            return _err(f"`{path_name}` 是目录，不能直接读取")
        if _is_sensitive_file(path):
            return _err("该文件被识别为敏感文件，禁止通过 WebUI 读取")

        try:
            stat = path.stat()
            if stat.st_size > MAX_READ_BYTES:
                return _unpreviewable(f"文件大小 {stat.st_size:,}B 超过 WebUI 文本预览限制 {MAX_READ_BYTES:,}B")
        except Exception as e:
            return _err(f"无法读取文件信息: {e}")

        def _read():
            with open(path, "rb") as f:
                sample = f.read(READ_SNIFF_BYTES)
                is_binary = b"\x00" in sample
                f.seek(0)
                raw = f.read()
            return raw, is_binary

        raw, is_binary = await asyncio.to_thread(_read)
        if is_binary:
            return _ok({
                "sandbox_id": sandbox_id,
                "path": path_name,
                "previewable": False,
                "content": "",
                "reason": "二进制文件不支持通过 WebUI 文本编辑器读取",
            })

        text = self._decode_process_output(raw)
        all_lines = text.splitlines()
        total_lines = len(all_lines)
        line_offset = max(1, line_offset)
        end_line = min(line_offset + n_lines - 1, total_lines)
        selected = all_lines[line_offset - 1 : end_line]
        selected = [_truncate_line(line) for line in selected]

        return _ok({
            "sandbox_id": sandbox_id,
            "path": path_name,
            "absolute_path": str(path),
            "line_offset": line_offset,
            "n_lines": len(selected),
            "total_lines": total_lines,
            "content": "\n".join(selected),
            "has_more": end_line < total_lines,
            "previewable": True,
        })

    async def web_write_file(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        payload = await request.get_json(silent=True) or {}
        sandbox_id = str(payload.get("sandbox_id", "")).strip()
        path_name = str(payload.get("path", "")).strip()
        content = payload.get("content", "")
        if not sandbox_id or not path_name:
            return _err("缺少 sandbox_id 或 path")

        path = self._resolve_web_path(sandbox_id, path_name)
        if not path:
            return _err(f"路径 `{path_name}` 不合法")

        max_bytes = getattr(self, "max_file_size_mb", 10) * 1024 * 1024
        single_limit = getattr(self, "single_write_limit_bytes", 256 * 1024)
        data = content.encode("utf-8") if isinstance(content, str) else content
        if len(data) > single_limit:
            return _err(f"单次写入内容超过 {single_limit:,}B 限制")
        if len(data) > max_bytes:
            return _err(f"内容超过 {max_bytes:,}B 限制")

        try:
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            old_text = ""
            if path.exists():
                try:
                    old_text = path.read_text(encoding="utf-8")
                except Exception:
                    pass
            await asyncio.to_thread(path.write_bytes, data)
            new_text = content if isinstance(content, str) else content.decode("utf-8", errors="replace")
            added = max(0, len(new_text.splitlines()) - len(old_text.splitlines()))
            removed = max(0, len(old_text.splitlines()) - len(new_text.splitlines()))
            self._record(sandbox_id, "write_file", f"{path_name} +{added} -{removed}")
            return _ok({
                "sandbox_id": sandbox_id,
                "path": path_name,
                "bytes": len(data),
                "added": added,
                "removed": removed,
            })
        except Exception as e:
            return _err(f"写入失败: {e}")

    async def web_delete_file(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        payload = await request.get_json(silent=True) or {}
        sandbox_id = str(payload.get("sandbox_id", "")).strip()
        path_name = str(payload.get("path", "")).strip()
        recursive = bool(payload.get("recursive", False))
        if not sandbox_id or not path_name:
            return _err("缺少 sandbox_id 或 path", 200)

        path = self._resolve_web_path(sandbox_id, path_name)
        if not path or not path.exists():
            return _err(f"路径 `{path_name}` 不存在", 200)

        try:
            if path.is_dir():
                if recursive:
                    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=False)
                else:
                    return _err(f"`{path_name}` 是目录，请使用递归删除", 200)
            else:
                await asyncio.to_thread(path.unlink)
            if path.exists():
                return _err(f"删除失败: 路径 `{path_name}` 仍然存在", 200)
            self._record(sandbox_id, "delete", path_name)
            return _ok({"sandbox_id": sandbox_id, "path": path_name})
        except Exception as e:
            return _err(f"删除失败: {e}", 200)

    async def web_mkdir(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        payload = await request.get_json(silent=True) or {}
        sandbox_id = str(payload.get("sandbox_id", "")).strip()
        path_name = str(payload.get("path", "")).strip()
        if not sandbox_id or not path_name:
            return _err("缺少 sandbox_id 或 path")

        path = self._resolve_web_path(sandbox_id, path_name)
        if not path:
            return _err(f"路径 `{path_name}` 不合法")
        try:
            await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
            self._record(sandbox_id, "mkdir", path_name)
            return _ok({"sandbox_id": sandbox_id, "path": path_name})
        except Exception as e:
            return _err(f"创建目录失败: {e}")

    async def web_rename(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        payload = await request.get_json(silent=True) or {}
        sandbox_id = str(payload.get("sandbox_id", "")).strip()
        old_name = str(payload.get("old_path", "")).strip()
        new_name = str(payload.get("new_path", "")).strip()
        if not sandbox_id or not old_name or not new_name:
            return _err("缺少 sandbox_id、old_path 或 new_path")

        old_path = self._resolve_web_path(sandbox_id, old_name)
        new_path = self._resolve_web_path(sandbox_id, new_name)
        if not old_path or not old_path.exists():
            return _err(f"源路径 `{old_name}` 不存在")
        if not new_path:
            return _err(f"目标路径 `{new_name}` 不合法")
        try:
            await asyncio.to_thread(shutil.move, str(old_path), str(new_path))
            self._record(sandbox_id, "rename", f"{old_name} -> {new_name}")
            return _ok({"sandbox_id": sandbox_id, "old_path": old_name, "new_path": new_name})
        except Exception as e:
            return _err(f"重命名失败: {e}")

    async def web_execute(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        payload = await request.get_json(silent=True) or {}
        sandbox_id = str(payload.get("sandbox_id", "")).strip()
        command = str(payload.get("command", "")).strip()
        run_in_background = bool(payload.get("run_in_background", False))
        description = str(payload.get("description", "")).strip()

        if not sandbox_id or not command:
            return _err("缺少 sandbox_id 或 command")
        if not getattr(self, "allow_execution", False):
            return _err("命令执行功能已关闭，请在插件配置中开启 ide_sandbox_allow_execution", 403)
        if getattr(self, "cover_only_mode", False):
            return _err("仅翻唱联动模式已开启，禁止执行命令", 403)

        # 安全检查：WebUI 用户视为普通管理员，使用白名单限制
        raw_whitelist = getattr(self, "execution_whitelist", None)
        if raw_whitelist is None:
            whitelist = set(DEFAULT_EXECUTION_WHITELIST)
        else:
            whitelist = raw_whitelist  # 空集表示不限制白名单
        if whitelist == {"*"}:
            whitelist = None
        safe, reason = _is_command_safe(command, whitelist, allow_and=False, unrestricted=False)
        if not safe:
            return _err(f"命令被拒绝: {reason}", 403)

        cwd = self._sandbox_path(sandbox_id)
        actual_command = command
        pip_mirror = getattr(self, "pip_mirror", "")
        maven_mirror = getattr(self, "maven_mirror", "")
        gradle_mirror = getattr(self, "gradle_mirror", "")

        if pip_mirror and command.lower().startswith("pip install"):
            lower_cmd = command.lower()
            if " -i " not in lower_cmd and " --index-url " not in lower_cmd and " --extra-index-url " not in lower_cmd:
                actual_command = f"{command} --index-url {pip_mirror}"

        if maven_mirror and command.lower().startswith("mvn "):
            lower_cmd = command.lower()
            if " -s " not in lower_cmd and " --settings " not in lower_cmd:
                if hasattr(self, "_ensure_java_configs"):
                    self._ensure_java_configs(cwd, need_maven=True)
                actual_command = f"{command} -s maven-settings.xml"

        if gradle_mirror and command.lower().startswith("gradle "):
            lower_cmd = command.lower()
            if " --init-script " not in lower_cmd:
                if hasattr(self, "_ensure_java_configs"):
                    self._ensure_java_configs(cwd, need_gradle=True)
                actual_command = f"{command} --init-script gradle-init.gradle"

        run_env = self._build_run_env()
        try:
            proc = await asyncio.create_subprocess_shell(
                actual_command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=run_env,
            )
        except Exception as e:
            return _err(f"启动命令失败: {e}")

        if run_in_background:
            if not description:
                description = command[:80]
            bg = self._start_background_command(
                actual_command,
                description,
                proc,
                owner_id=g.get("username", "webui"),
                sandbox_id=sandbox_id,
            )
            self._record(sandbox_id, "execute_bg", command[:100])
            return _ok({
                "task_id": bg.task_id,
                "command": actual_command,
                "status": "running",
            })

        cmd_timeout = getattr(self, "cmd_timeout", 30)
        output_limit = getattr(self, "max_output_len", MAX_OUTPUT_LEN)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=cmd_timeout)
        except asyncio.TimeoutError:
            await self._kill_process_tree(proc)
            return _err(f"命令执行超时（{cmd_timeout} 秒）")

        out = self._decode_process_output(stdout)[:output_limit]
        err = self._decode_process_output(stderr)[:output_limit]
        self._record(sandbox_id, "execute", command[:100])
        return _ok({
            "sandbox_id": sandbox_id,
            "command": actual_command,
            "returncode": proc.returncode,
            "stdout": out,
            "stderr": err,
        })

    async def web_task_list(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        sandbox_id = request.args.get("sandbox_id", "").strip()
        tasks = []
        for task_id, bg in self._background_commands.items():
            if sandbox_id and getattr(bg, "sandbox_id", "") not in {"", sandbox_id}:
                continue
            tasks.append({
                "task_id": bg.task_id,
                "sandbox_id": getattr(bg, "sandbox_id", ""),
                "description": bg.description,
                "command": bg.command[:200],
                "status": bg.status,
                "returncode": bg.returncode,
                "start_time": bg.start_time,
                "output_path": str(bg.output_path) if bg.output_path else None,
            })
        tasks.sort(key=lambda x: x["start_time"], reverse=True)
        return _ok({"tasks": tasks})

    async def web_task_output(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        task_id = request.args.get("task_id", "").strip()
        if not task_id:
            return _err("缺少 task_id")
        bg = self._background_commands.get(task_id)
        if not bg:
            return _err(f"找不到任务 `{task_id}`")
        output = ""
        if bg.output_path and bg.output_path.exists():
            try:
                output = bg.output_path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                output = f"读取日志失败: {e}"
        output_limit = getattr(self, "max_output_len", MAX_OUTPUT_LEN)
        return _ok({
            "task_id": bg.task_id,
            "description": bg.description,
            "command": bg.command,
            "status": bg.status,
            "returncode": bg.returncode,
            "output": output[-output_limit:],
        })

    async def web_task_stop(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        payload = await request.get_json(silent=True) or {}
        task_id = str(payload.get("task_id", "")).strip()
        if not task_id:
            return _err("缺少 task_id")
        msg = await self._stop_background_command(task_id)
        return _ok({"task_id": task_id, "message": msg})

    async def web_todos(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        sandbox_id = request.args.get("sandbox_id", "").strip() if request.method == "GET" else ""
        if request.method == "POST":
            payload = await request.get_json(silent=True) or {}
            sandbox_id = str(payload.get("sandbox_id", "")).strip()
            todos = payload.get("todos")
            if not sandbox_id:
                return _err("缺少 sandbox_id")
            if todos is not None:
                if not isinstance(todos, list):
                    return _err("todos 必须是数组")
                normalized = []
                for idx, item in enumerate(todos, start=1):
                    if not isinstance(item, dict):
                        return _err(f"第 {idx} 项不是对象")
                    title = str(item.get("title") or item.get("content") or "").strip()
                    status = str(item.get("status") or "pending").strip()
                    if not title:
                        return _err(f"第 {idx} 项缺少 title")
                    if status not in {"pending", "in_progress", "done"}:
                        return _err(f"第 {idx} 项 status 不合法")
                    normalized.append({
                        "id": idx,
                        "content": title,
                        "title": title,
                        "status": status,
                        "completed": status == "done",
                        "created_at": item.get("created_at") or datetime.now().isoformat()[:19],
                    })
                self.todos[sandbox_id] = normalized
                self._todo_id_counter[sandbox_id] = len(normalized)
                await self._save_todos(sandbox_id)
                self._record(sandbox_id, "set_todos", f"{len(normalized)} 项")
                return _ok({"sandbox_id": sandbox_id, "todos": normalized})

        if not sandbox_id:
            return _err("缺少 sandbox_id")
        if sandbox_id not in self.todos:
            self._load_todos(sandbox_id)
        return _ok({"sandbox_id": sandbox_id, "todos": self.todos.get(sandbox_id, [])})

    async def web_history(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        sandbox_id = request.args.get("sandbox_id", "").strip()
        limit = int(request.args.get("limit", 50) or 50)
        if not sandbox_id:
            return _err("缺少 sandbox_id")
        records = self._history_records_for_web(sandbox_id, limit)
        return _ok({
            "sandbox_id": sandbox_id,
            "history": records[-limit:],
        })

    async def web_config(self):
        ok, resp = self._check_web_permission()
        if not ok:
            return resp
        return _ok({
            "allow_execution": getattr(self, "allow_execution", False),
            "allow_test": getattr(self, "allow_test", False),
            "allow_git_clone": getattr(self, "allow_git_clone", False),
            "cover_only_mode": getattr(self, "cover_only_mode", False),
            "cmd_timeout": getattr(self, "cmd_timeout", 30),
            "max_output_len": getattr(self, "max_output_len", 4000),
            "max_file_size_mb": getattr(self, "max_file_size_mb", 10),
            "execution_whitelist": sorted(getattr(self, "execution_whitelist", None) or set(DEFAULT_EXECUTION_WHITELIST)),
        })
