from __future__ import annotations

import asyncio
import difflib
import fnmatch
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from collections import deque
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
    _is_probably_binary_file,
    _is_protected_path,
    _is_sensitive_file,
    _safe_filename,
    _safe_relative_path,
)

MAX_SCAN_ENTRIES = 5000
MAX_READ_LINES = 1000
MAX_READ_BYTES = 100 * 1024
MAX_READ_LINE_LENGTH = 2000
READ_SNIFF_BYTES = 8192


def _truncate_line(line: str) -> tuple[str, bool]:
    if len(line) <= MAX_READ_LINE_LENGTH:
        return line, False
    return line[:MAX_READ_LINE_LENGTH] + " ...[line truncated]", True


def _build_unified_preview(filename: str, old_text: str, new_text: str, max_chars: int = 6000) -> str:
    diff = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"{filename} (before)",
        tofile=f"{filename} (after)",
        lineterm="",
    )
    text = "\n".join(diff)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... diff preview truncated ..."
    return text or "(无文本差异)"


class FileToolsMixin:
    async def ide_list_files(self, event: AstrMessageEvent) -> str:
        """列出当前沙盒中的所有文件、大小及其绝对路径。

        使用场景：
        - 首次进入沙盒，先了解已有哪些文件。
        - 需要把沙盒内文件的绝对路径传给其他插件（如翻唱插件）。
        - 简单确认某个文件是否存在。

        Tips:
        - 结果最多展示 200 个文件；文件过多时建议使用 ide_list_tree 查看目录结构。
        - 如果只想确认单个文件的元信息，使用 ide_file_info 更轻量。
        - 查找特定内容时，优先使用 ide_search_text 而不是逐个打开文件。

        Returns:
            文件列表，包含文件名、大小和绝对路径。
        """
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以查看沙盒文件。"
        sandbox_id = self._get_sandbox_id(event)
        d = self._get_group_sandbox(sandbox_id)
        def _snapshot_files():
            entries = []
            count = 0
            truncated = False
            for f in d.rglob("*"):
                if not f.is_file():
                    continue
                count += 1
                if count > MAX_SCAN_ENTRIES:
                    truncated = True
                    break
                if len(entries) < 200:
                    rel = f.relative_to(d)
                    entries.append(f"{rel} ({f.stat().st_size:,}B) - 绝对路径: {str(f.resolve())}")
            return count, entries, truncated

        file_count, file_strs, truncated = await asyncio.to_thread(_snapshot_files)
        await self._broadcast(event, f"🤖 AI 正在查看沙盒文件列表（当前 {file_count} 个文件）...")
        if not file_strs:
            return "沙盒中暂无文件。"
        self._record(sandbox_id, "list_files", f"列出 {file_count} 个文件")
        suffix = ""
        if file_count > 200:
            suffix = f"\n... 另有 {file_count - 200} 个文件未展示。"
        if truncated:
            suffix += f"\n... 已达到 {MAX_SCAN_ENTRIES} 个文件扫描限制，请使用 ide_list_tree 或指定子目录。"
        return "沙盒文件列表:\n" + "\n".join(file_strs) + suffix


    async def ide_list_tree(
        self,
        event: AstrMessageEvent,
        root: str = "",
        max_depth: int = 3,
        max_entries: int = 200,
    ) -> str:
        """以目录树形式列出沙盒目录结构。

        使用场景：
        - 项目结构复杂，需要像 Codex 一样先理解目录层级。
        - 定位某个模块、资源或配置文件所在位置。
        - 文件数量过多，ide_list_files 展示不全时。

        Args:
            root(string, optional): 要查看的相对目录，留空表示沙盒根目录。
            max_depth(number, optional): 最大递归深度，范围 1-8，默认 3。
            max_entries(number, optional): 最多展示条目数，范围 20-1000，默认 200。

        Returns:
            目录树文本。
        """
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以查看目录树。"
        sandbox_id = self._get_sandbox_id(event)
        sandbox = self._get_group_sandbox(sandbox_id)
        target = sandbox if not root else self._resolve(sandbox_id, root, allow_bypass=self._is_super_admin(event))
        if not target or not await asyncio.to_thread(lambda: target.exists() and target.is_dir()):
            return f"错误：目录 `{root or '.'}` 不存在或不合法。"

        max_depth = max(1, min(int(max_depth or 3), 8))
        max_entries = max(20, min(int(max_entries or 200), 1000))
        def _build_tree():
            entries: List[str] = [f"📁 {target.name or str(target)}"]
            count = 0
            truncated = False

            def walk_dir(path: Path, depth: int, prefix: str = ""):
                nonlocal count, truncated
                if depth > max_depth or truncated:
                    return
                try:
                    children = sorted(
                        [p for p in path.iterdir() if not p.is_symlink()],
                        key=lambda p: (not p.is_dir(), p.name.lower()),
                    )
                except Exception as e:
                    entries.append(f"{prefix}└─ [无法读取: {e}]")
                    return
                visible = [p for p in children if p.name not in SEARCH_SKIP_DIRS]
                for idx, child in enumerate(visible):
                    if count >= max_entries:
                        truncated = True
                        return
                    count += 1
                    connector = "└─ " if idx == len(visible) - 1 else "├─ "
                    next_prefix = prefix + ("   " if idx == len(visible) - 1 else "│  ")
                    if child.is_dir():
                        entries.append(f"{prefix}{connector}📁 {child.name}/")
                        walk_dir(child, depth + 1, next_prefix)
                    else:
                        try:
                            size = child.stat().st_size
                            entries.append(f"{prefix}{connector}📄 {child.name} ({size:,}B)")
                        except Exception:
                            entries.append(f"{prefix}{connector}📄 {child.name}")

            walk_dir(target, 1)
            return entries, truncated

        entries, truncated = await asyncio.to_thread(_build_tree)
        self._record(sandbox_id, "list_tree", f"{root or '.'} depth={max_depth}")
        if truncated:
            entries.append(f"... 已达到 {max_entries} 条限制。")
        return "\n".join(entries)


    async def ide_glob(
        self,
        event: AstrMessageEvent,
        pattern: str,
        directory: str = "",
        include_dirs: bool = True,
        max_matches: int = 1000,
    ) -> str:
        """按 Glob 模式查找文件/目录，作为 ide_list_tree 的精确搜索补充。"""
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以搜索沙盒路径。"
        if not pattern:
            return "错误：pattern 不能为空。"
        sandbox_id = self._get_sandbox_id(event)
        sandbox = self._get_group_sandbox(sandbox_id)
        root = sandbox if not directory else self._resolve(sandbox_id, directory, allow_bypass=self._is_super_admin(event))
        if not root or not await asyncio.to_thread(lambda: root.exists() and root.is_dir()):
            return f"错误：目录 `{directory or '.'}` 不存在或不合法。"
        if pattern.startswith("**"):
            top = []
            try:
                for child in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    top.append(f"{child.name}/" if child.is_dir() else child.name)
                    if len(top) >= 80:
                        break
            except Exception:
                pass
            hint = "\n".join(top) if top else "(目录为空或无法读取)"
            return (
                f"错误：pattern `{pattern}` 以 ** 开头，容易递归扫描过大目录。\n"
                f"请先使用更具体的目录或模式。当前顶层条目：\n{hint}"
            )

        max_matches = max(1, min(int(max_matches or 1000), 1000))

        def _glob_sync():
            matches: list[str] = []
            skipped_sensitive = 0
            truncated = False
            for path in root.glob(pattern):
                if path.is_symlink():
                    continue
                if path.is_dir() and not include_dirs:
                    continue
                if path.is_file() and _is_sensitive_file(path):
                    skipped_sensitive += 1
                    continue
                try:
                    rel = path.resolve().relative_to(root.resolve())
                except ValueError:
                    continue
                suffix = "/" if path.is_dir() else ""
                matches.append(str(rel).replace("\\", "/") + suffix)
                if len(matches) >= max_matches:
                    truncated = True
                    break
            matches.sort()
            return matches, skipped_sensitive, truncated

        matches, skipped_sensitive, truncated = await asyncio.to_thread(_glob_sync)
        self._record(sandbox_id, "glob", f"{directory or '.'}:{pattern}")
        if not matches:
            msg = f"未找到匹配 `{pattern}` 的路径。"
            if skipped_sensitive:
                msg += f" 已过滤 {skipped_sensitive} 个敏感文件。"
            return msg
        suffix = ""
        if truncated:
            suffix += f"\n... 已达到 {max_matches} 条限制，请使用更具体的模式。"
        if skipped_sensitive:
            suffix += f"\n已过滤 {skipped_sensitive} 个敏感文件。"
        return f"Glob 匹配结果（{len(matches)}）:\n" + "\n".join(matches) + suffix


    async def ide_file_info(self, event: AstrMessageEvent, path_name: str) -> str:
        """查看文件或目录的元信息（大小、类型、修改时间、目录规模等）。

        使用场景：
        - 确认某个文件是否存在及大小。
        - 判断路径是文件还是目录。
        - 查看目录下有多少文件/子目录及总大小。

        Args:
            path_name(string): 文件或目录的相对路径，超级管理员可传绝对路径。

        Returns:
            文件/目录信息。
        """
        if not await self._check_permission(event):
            return "权限不足。"
        sandbox_id = self._get_sandbox_id(event)
        is_super = self._is_super_admin(event)
        path = self._resolve(sandbox_id, path_name, allow_bypass=is_super)
        if not path or not await asyncio.to_thread(path.exists):
            return f"错误：`{path_name}` 不存在或不合法。"

        stat = await asyncio.to_thread(path.stat)
        is_dir = await asyncio.to_thread(path.is_dir)
        lines = [
            f"路径: {path}",
            f"类型: {'目录' if is_dir else '文件'}",
            f"大小: {stat.st_size:,}B",
            f"修改时间: {datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds')}",
        ]
        if is_dir:
            def _dir_stats():
                file_count = 0
                dir_count = 0
                total_size = 0
                truncated = False
                for child in path.rglob("*"):
                    if child.is_symlink():
                        continue
                    try:
                        if child.is_dir():
                            dir_count += 1
                        elif child.is_file():
                            file_count += 1
                            total_size += child.stat().st_size
                        if file_count + dir_count >= MAX_SCAN_ENTRIES:
                            truncated = True
                            break
                    except Exception:
                        pass
                return file_count, dir_count, total_size, truncated

            file_count, dir_count, total_size, truncated = await asyncio.to_thread(_dir_stats)
            lines.extend([
                f"目录数: {dir_count}",
                f"文件数: {file_count}",
                f"目录总大小: {total_size:,}B",
            ])
            if truncated:
                lines.append(f"提示: 已达到 {MAX_SCAN_ENTRIES} 个条目的统计限制，结果为截断值。")
        self._record(sandbox_id, "file_info", path_name)
        return "\n".join(lines)


    async def ide_read_file_range(
        self,
        event: AstrMessageEvent,
        filename: str,
        start_line: int = 1,
        end_line: int = 120,
    ) -> str:
        """按行号读取文件片段（兼容旧版，新代码建议直接用 ide_read_file 的 line_offset/n_lines）。

        使用场景：
        - 只需要查看某段代码，避免一次读取大文件。
        - 根据 ide_search_text 的搜索结果定位到具体行号后，读取附近代码。

        Args:
            filename(string): 文件相对路径，超级管理员可传绝对路径。
            start_line(number, optional): 起始行号，从 1 开始。
            end_line(number, optional): 结束行号，最多读取 400 行。

        Returns:
            带行号的文件片段。
        """
        if self.cover_only_mode:
            return "⛔ 仅翻唱模式已开启：禁止读取文件。"
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以读取沙盒文件。"
        sandbox_id = self._get_sandbox_id(event)
        path = self._resolve(sandbox_id, filename, allow_bypass=self._is_super_admin(event))
        if not path or not await asyncio.to_thread(lambda: path.exists() and path.is_file()):
            return f"错误：文件 `{filename}` 不存在。"
        size = (await asyncio.to_thread(path.stat)).st_size
        if size > self.max_file_size_mb * 1024 * 1024:
            return f"错误：文件大小 {size:,}B 超过 {self.max_file_size_mb}MB 限制。"

        start_line = max(1, int(start_line or 1))
        end_line = max(start_line, int(end_line or start_line))
        if end_line - start_line + 1 > 400:
            end_line = start_line + 399
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            lines = text.splitlines()
        except Exception as e:
            return f"读取文件失败: {e}"
        total = len(lines)
        if start_line > total:
            return f"错误：起始行 {start_line} 超过文件总行数 {total}。"
        selected = lines[start_line - 1:min(end_line, total)]
        body = "\n".join(
            f"{line_no:>5}: {line}"
            for line_no, line in enumerate(selected, start=start_line)
        )
        self._record(sandbox_id, "read_range", f"{filename}:{start_line}-{min(end_line, total)}")
        return f"📄 {filename} 行 {start_line}-{min(end_line, total)} / {total}:\n```\n{body}\n```"


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
        """在沙盒内搜索文本，优先调用系统 ripgrep（rg），回退到 Python 实现。

        使用场景：
        - 查找函数名、类名、变量名定义或引用位置。
        - 定位报错信息、TODO、FIXME 等标记。
        - 在修改文件前，先确认旧字符串在哪些文件中出现。

        Tips:
        - 总是优先使用本工具定位内容，而不是先读取整个大文件再手动查找。
        - 搜索到结果后，可结合 ide_read_file 的 line_offset/n_lines 读取上下文。
        - 使用 filename_pattern 限定文件类型可显著减少无关结果。

        Args:
            query(string): 要搜索的文本或正则表达式。
            root(string, optional): 搜索目录或文件，留空表示沙盒根目录。
            filename_pattern(string, optional): 文件名过滤，如 *.py 或 *.json。
            regex(bool, optional): 是否按正则搜索，默认 False。
            case_sensitive(bool, optional): 是否区分大小写，默认 False。
            max_results(number, optional): 最大结果数，范围 1-200，默认 50。

        Returns:
            匹配行列表，格式为 `相对路径:行号: 匹配内容`。
        """
        if self.cover_only_mode:
            return "⛔ 仅翻唱模式已开启：禁止搜索文件内容。"
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以搜索沙盒文件。"
        if not query:
            return "错误：搜索内容不能为空。"
        sandbox_id = self._get_sandbox_id(event)
        sandbox = self._get_group_sandbox(sandbox_id)
        target = sandbox if not root else self._resolve(sandbox_id, root, allow_bypass=self._is_super_admin(event))
        if not target or not await asyncio.to_thread(target.exists):
            return f"错误：搜索路径 `{root or '.'}` 不存在或不合法。"

        max_results = max(1, min(int(max_results or 50), 200))
        output_mode = output_mode if output_mode in {"content", "files_with_matches", "count_matches"} else "content"
        head_limit = max(0, min(int(head_limit if head_limit is not None else 250), 1000))
        offset = max(0, int(offset or 0))

        # 优先调用系统 ripgrep，速度快且能处理大仓库
        try:
            results, skipped, truncated = await self._search_with_rg(
                target, sandbox, query, filename_pattern, regex, case_sensitive, max_results,
                output_mode, head_limit, offset, include_ignored
            )
            used_rg = True
        except Exception as e:
            logger.debug(f"[IdeSandbox] rg search failed, fallback to python: {e}")
            results, skipped, truncated = await self._search_with_python(
                target, sandbox, query, filename_pattern, regex, case_sensitive, max_results,
                output_mode, head_limit, offset, include_ignored
            )
            used_rg = False

        self._record(sandbox_id, "search_text", query[:80])
        if not results:
            extra = f"（跳过 {skipped} 个二进制/超大/不可读文件）" if skipped else ""
            return f"未找到匹配结果。{extra}"
        suffix = ""
        if len(results) >= max_results:
            suffix = f"\n... 已达到 {max_results} 条结果限制。"
        if skipped:
            suffix += f"\n跳过 {skipped} 个二进制/超大/不可读文件。"
        if truncated:
            suffix += f"\n已达到 {MAX_SCAN_ENTRIES} 个条目的扫描限制。"
        if not used_rg:
            suffix += "\n（rg 不可用，已回退到 Python 搜索）"
        return "搜索结果:\n" + "\n".join(results) + suffix

    async def _search_with_rg(
        self,
        target: Path,
        sandbox: Path,
        query: str,
        filename_pattern: str,
        regex: bool,
        case_sensitive: bool,
        max_results: int,
        output_mode: str = "content",
        head_limit: int = 250,
        offset: int = 0,
        include_ignored: bool = False,
    ) -> tuple[list[str], int, bool]:
        """使用系统 ripgrep 搜索文本。返回 (results, skipped, truncated)。"""
        import shutil
        import shlex

        rg_path = shutil.which("rg")
        if not rg_path:
            raise RuntimeError("ripgrep (rg) not found in PATH")

        args = [rg_path, "-n", "--no-heading", "--with-filename", "--max-columns", "240"]
        if include_ignored:
            args.append("--no-ignore")
        args.append("--hidden")
        for skip_dir in SEARCH_SKIP_DIRS:
            args.extend(["-g", f"!{skip_dir}"])
        if not regex:
            args.append("-F")
        if not case_sensitive:
            args.append("-i")
        if filename_pattern:
            args.extend(["-g", filename_pattern])
        if output_mode == "files_with_matches":
            args.append("--files-with-matches")
        elif output_mode == "count_matches":
            args.append("--count-matches")
        # -m 限制每个文件匹配数，整体结果再在输出后截断
        args.extend(["-m", str(max_results)])
        args.append(query)
        args.append(str(target))

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(sandbox),
        )
        stdout, stderr = await proc.communicate()
        # rg 返回 0 表示有匹配，1 表示无匹配，其他为错误
        if proc.returncode not in (0, 1):
            raise RuntimeError(f"rg exit {proc.returncode}: {stderr.decode('utf-8', errors='replace')}")

        text = stdout.decode("utf-8", errors="replace")
        raw_results = []
        sensitive_skipped = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            # 将绝对路径转为相对路径（如可能）
            parts = line.split(":", 2)
            if len(parts) >= 3:
                file_part, line_no, content = parts[0], parts[1], parts[2]
                try:
                    file_path = Path(file_part)
                    if _is_sensitive_file(file_path):
                        sensitive_skipped += 1
                        continue
                    if file_path.is_absolute() and _is_path_safe(sandbox, file_path):
                        rel = file_path.relative_to(sandbox)
                        line = f"{rel}:{line_no}: {content}"
                except Exception:
                    pass
            else:
                candidate = Path(line.rsplit(":", 1)[0] if output_mode == "count_matches" and ":" in line else line)
                if _is_sensitive_file(candidate):
                    sensitive_skipped += 1
                    continue
                try:
                    if candidate.is_absolute() and _is_path_safe(sandbox, candidate):
                        if output_mode == "count_matches" and ":" in line:
                            line = f"{candidate.relative_to(sandbox)}:{line.rsplit(':', 1)[1]}"
                        else:
                            line = str(candidate.relative_to(sandbox))
                except Exception:
                    pass
            raw_results.append(line[:512])
            if len(raw_results) >= max_results:
                break

        results = raw_results[offset:]
        if head_limit:
            results = results[:head_limit]
        return results, sensitive_skipped, len(raw_results) >= max_results

    async def _search_with_python(
        self,
        target: Path,
        sandbox: Path,
        query: str,
        filename_pattern: str,
        regex: bool,
        case_sensitive: bool,
        max_results: int,
        output_mode: str = "content",
        head_limit: int = 250,
        offset: int = 0,
        include_ignored: bool = False,
    ) -> tuple[list[str], int, bool]:
        """Python 实现的文本搜索，作为 rg 不可用时回退。"""
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query if regex else re.escape(query), flags)
        except re.error as e:
            raise ValueError(f"正则表达式不合法: {e}")

        max_bytes = self.max_file_size_mb * 1024 * 1024

        def _search_sync():
            candidates = (target,) if target.is_file() else target.rglob("*")
            content_results: list[str] = []
            file_counts: dict[str, int] = {}
            skipped = 0
            scanned = 0
            truncated = False
            for file_path in candidates:
                if len(content_results) >= max_results:
                    break
                scanned += 1
                if scanned > MAX_SCAN_ENTRIES:
                    truncated = True
                    break
                if (
                    not file_path.is_file()
                    or file_path.is_symlink()
                    or (not include_ignored and any(part in SEARCH_SKIP_DIRS for part in file_path.parts))
                ):
                    continue
                if _is_sensitive_file(file_path):
                    skipped += 1
                    continue
                if filename_pattern and not fnmatch.fnmatch(file_path.name, filename_pattern):
                    continue
                try:
                    if file_path.stat().st_size > max_bytes:
                        skipped += 1
                        continue
                    if file_path.suffix.lower() not in TEXT_EXTENSIONS:
                        probe = file_path.read_bytes()[:4096]
                        if _is_probably_binary_file(file_path, probe):
                            skipped += 1
                            continue
                    text = file_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    skipped += 1
                    continue
                rel = file_path.relative_to(sandbox) if _is_path_safe(sandbox, file_path) else file_path
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if pattern.search(line):
                        rel_str = str(rel).replace("\\", "/")
                        file_counts[rel_str] = file_counts.get(rel_str, 0) + 1
                        if output_mode == "content":
                            content_results.append(f"{rel_str}:{line_no}: {line[:240]}")
                        if len(content_results) >= max_results and output_mode == "content":
                            break
            if output_mode == "files_with_matches":
                results = list(file_counts.keys())
            elif output_mode == "count_matches":
                results = [f"{path}:{count}" for path, count in file_counts.items()]
            else:
                results = content_results
            results = results[offset:]
            if head_limit:
                results = results[:head_limit]
            return results, skipped, truncated

        return await asyncio.to_thread(_search_sync)


    async def ide_read_file(
        self,
        event: AstrMessageEvent,
        filename: str,
        line_offset: int = 1,
        n_lines: int = MAX_READ_LINES,
    ) -> str:
        """读取沙盒中指定文件的内容，支持按行号范围读取。

        使用场景：
        - 查看已存在文件的全部或部分内容。
        - 根据 ide_search_text 的结果，读取匹配行附近的上下文。

        Tips:
        - 默认最多读取 1000 行 / 100KB，并自动截断过长行。
        - 大文件不要一次读取全部内容，建议先用 ide_search_text 定位，再用 line_offset/n_lines 读取片段。
        - line_offset 从 1 开始；可传负值表示从文件末尾倒数（如 -50 表示倒数第 50 行）。
        - n_lines 为读取行数，上限 1000。
        - 如需精确控制起止行号，也可继续使用 ide_read_file_range。

        Args:
            filename(string): 要读取的文件名（不含路径），或超级管理员使用的绝对路径。
            line_offset(number, optional): 起始行号，从 1 开始；负值表示从末尾倒数。默认 1。
            n_lines(number, optional): 要读取的行数，默认最多 1000 行。

        Returns:
            文件内容文本。如果文件不存在会返回错误信息。
        """
        if self.cover_only_mode:
            return "⛔ 仅翻唱模式已开启：禁止读取文件。"
        sandbox_id = self._get_sandbox_id(event)
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以读取沙盒文件。"
        is_super = self._is_super_admin(event)
        path = self._resolve(sandbox_id, filename, allow_bypass=is_super)
        if not path or not await asyncio.to_thread(lambda: path.exists() and path.is_file()):
            return f"错误：文件 `{filename}` 不存在。"
        size = (await asyncio.to_thread(path.stat)).st_size
        if size > self.max_file_size_mb * 1024 * 1024:
            return f"错误：文件大小 {size:,}B 超过 {self.max_file_size_mb}MB 限制。"
        if _is_sensitive_file(path):
            return f"⛔ 已阻止读取 `{filename}`：文件名疑似包含密钥、令牌或凭据等敏感信息。"
        try:
            sample = await asyncio.to_thread(lambda: path.read_bytes()[:READ_SNIFF_BYTES])
        except Exception as e:
            return f"读取文件失败: {e}"
        if _is_probably_binary_file(path, sample):
            return f"⛔ `{filename}` 不是可安全读取的文本文件，请使用合适的文件或媒体工具处理。"
        super_tag = self._get_super_tag(event, filename)
        await self._broadcast(event, f"{super_tag}🤖 AI 正在读取文件 `{filename}`（{size:,}B）...")
        try:
            page = await asyncio.to_thread(
                self._read_file_page,
                path,
                int(line_offset or 1),
                int(n_lines or MAX_READ_LINES),
            )
            if page["error"]:
                return page["error"]
            selected = page["lines"]
            total = page["total"]
            start = page["start_index"]
            body = "\n".join(
                f"{line_no:>5}: {line}"
                for line_no, line in enumerate(selected, start=start + 1)
            )
            end_line = start + len(selected)
            self._record(
                sandbox_id,
                "read",
                f"{filename}:{start + 1}-{end_line}{' [SUPER]' if is_super else ''}",
            )
            notes = []
            if page["max_lines_reached"]:
                notes.append(f"已达到 {MAX_READ_LINES} 行读取限制")
            if page["max_bytes_reached"]:
                notes.append(f"已达到 {MAX_READ_BYTES}B 读取限制")
            if page["truncated_lines"]:
                preview = ", ".join(str(x) for x in page["truncated_lines"][:10])
                notes.append(f"长行已截断: {preview}")
            if end_line < total:
                notes.append(f"继续读取可设置 line_offset={end_line + 1}")
            note_text = "\n" + "\n".join(f"提示：{note}" for note in notes) if notes else ""
            return f"📄 {filename} 行 {start + 1}-{end_line} / {total} ({size:,}B):\n```\n{body}\n```{note_text}"
        except Exception as e:
            return f"读取文件失败: {e}"


    def _read_file_page(self, path: Path, line_offset: int, n_lines: int) -> dict:
        if line_offset == 0:
            return {"error": "错误：line_offset 不能为 0，请使用 1 或负数。"}
        limit = max(1, min(n_lines, MAX_READ_LINES))
        selected: list[str] = []
        truncated_lines: list[int] = []
        max_lines_reached = False
        max_bytes_reached = False
        total = 0
        start_index = 0

        if line_offset < 0:
            tail_count = min(abs(line_offset), MAX_READ_LINES)
            tail_buf: deque[tuple[int, str, bool]] = deque(maxlen=tail_count)
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for total, raw_line in enumerate(f, start=1):
                    line, truncated = _truncate_line(raw_line.rstrip("\n\r"))
                    tail_buf.append((total, line, truncated))
            candidates = list(tail_buf)[:limit]
            start_index = candidates[0][0] - 1 if candidates else total
            byte_count = 0
            for line_no, line, truncated in candidates:
                next_bytes = len(line.encode("utf-8"))
                if selected and byte_count + next_bytes > MAX_READ_BYTES:
                    max_bytes_reached = True
                    break
                byte_count += next_bytes
                selected.append(line)
                if truncated:
                    truncated_lines.append(line_no)
            max_lines_reached = len(tail_buf) > limit
        else:
            collecting = True
            byte_count = 0
            start_index = line_offset - 1
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for total, raw_line in enumerate(f, start=1):
                    if not collecting:
                        continue
                    if total < line_offset:
                        continue
                    line, truncated = _truncate_line(raw_line.rstrip("\n\r"))
                    next_bytes = len(line.encode("utf-8"))
                    if selected and byte_count + next_bytes > MAX_READ_BYTES:
                        max_bytes_reached = True
                        collecting = False
                        continue
                    byte_count += next_bytes
                    selected.append(line)
                    if truncated:
                        truncated_lines.append(total)
                    if len(selected) >= limit:
                        max_lines_reached = True
                        collecting = False
            if line_offset > total:
                return {"error": f"错误：起始行 {line_offset} 超过文件总行数 {total}。"}

        return {
            "error": "",
            "lines": selected,
            "total": total,
            "start_index": start_index,
            "max_lines_reached": max_lines_reached and (start_index + len(selected) < total),
            "max_bytes_reached": max_bytes_reached,
            "truncated_lines": truncated_lines,
        }


    async def ide_write_file(
        self, event: AstrMessageEvent, filename: str, content: str, dry_run: bool = False
    ) -> str:
        """向沙盒中写入或覆盖一个文件。

        使用场景：
        - 创建新文件或完全替换已有文件内容。
        - 写入单文件脚本、配置文件、HTML/CSS/JS 模块等。

        ⚠️ 重要规则：
        - content 参数有大小限制（默认 10MB），但更重要的是 LLM 请求有 60 秒超时限制。
        - 中等大小脚本可以一次写入；大型项目建议拆分为多个模块文件（如 html/css/js 分离、或按功能拆分）。
        - 如果内容超过单次写入上限，应拆分为多个文件，或先写骨架再用 ide_append_to_file 分段追加。
        - 不要在一个 content 中放入完整的游戏/应用代码，而是分步骤：先写框架，再写模块，最后组装。

        Args:
            filename(string): 文件名（不含路径），或超级管理员使用的绝对路径。
            content(string): 要写入的完整内容。建议单文件脚本直接一次写入，大型项目拆分模块。

        Returns:
            操作结果说明。
        """
        if self.cover_only_mode:
            return "⛔ 仅翻唱模式已开启：禁止写入文件。"
        sandbox_id = self._get_sandbox_id(event)
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以使用文件操作工具。"
        is_super = self._is_super_admin(event)
        path = self._resolve(sandbox_id, filename, allow_bypass=is_super)
        if not path:
            return f"错误：文件名 `{filename}` 不合法。"
        # 大小检查
        content_bytes = content.encode("utf-8")
        content_size = len(content_bytes)
        logger.info(f"[IdeSandbox] ide_write_file start: {filename!r}, size={content_size:,}B")
        if content_size > self.max_file_size_mb * 1024 * 1024:
            return f"错误：内容超过 {self.max_file_size_mb}MB 限制。"
        if content_size > self.single_write_limit_bytes:
            await self._step_notice(
                event,
                f"⚠️ `{filename}` 内容 {content_size:,}B 超过单次写入上限 {self.single_write_limit_kb}KB，松子会让 AI 拆分后再写~",
            )
            return (
                f"⛔ 错误：内容大小 {content_size:,}B 超过单次写入上限 {self.single_write_limit_kb}KB。\n"
                f"请按以下方式处理：\n"
                f"1. 将内容拆分为多个模块文件\n"
                f"2. 或使用 `ide_append_to_file` 工具分多次追加到同一个文件\n"
                f"3. 先写入文件骨架，再逐段追加内容"
            )
        super_tag = self._get_super_tag(event, filename)
        await self._broadcast(event, f"{super_tag}🤖 AI 正在写入文件 `{filename}`（{content_size:,}B）...")
        try:
            old_text = ""
            track_changes = self.broadcast_actions
            existed = await asyncio.to_thread(lambda: path.exists() and path.is_file())
            if existed and track_changes:
                try:
                    old_text = await asyncio.to_thread(path.read_text, encoding="utf-8")
                except Exception:
                    old_text = ""
            if dry_run:
                if existed and not old_text:
                    try:
                        old_text = await asyncio.to_thread(path.read_text, encoding="utf-8")
                    except Exception:
                        old_text = ""
                preview = _build_unified_preview(filename, old_text, content)
                return f"🔎 写入预览 `{filename}`（未实际写入）：\n```diff\n{preview}\n```"
            await self._write_bytes_with_progress(
                event,
                path,
                content_bytes,
                filename,
                prefix=super_tag,
            )
            size = (await asyncio.to_thread(path.stat)).st_size
            logger.info(f"[IdeSandbox] ide_write_file wrote: {filename!r}, size={size:,}B")
            if track_changes:
                added, removed = self._line_delta(old_text, content)
                if not existed:
                    removed = 0
                await self._broadcast_file_change(
                    event,
                    sandbox_id,
                    "已写入",
                    path,
                    added,
                    removed,
                    prefix=super_tag,
                )
            else:
                added = 0
                removed = 0
            self._record(sandbox_id, "write", f"{filename}{' [SUPER]' if is_super else ''}")
            logger.info(f"[IdeSandbox] ide_write_file done: {filename!r}, +{added} -{removed}")
            return f"✅ 已写入 `{filename}`（{size:,}B）。"
        except Exception as e:
            logger.exception(f"[IdeSandbox] ide_write_file failed: {filename!r}")
            await self._step_notice(event, f"❌ `{filename}` 写入失败：{e}")
            return f"写入失败: {e}"


    async def ide_append_to_file(
        self, event: AstrMessageEvent, filename: str, content: str
    ) -> str:
        """向已有文件末尾追加内容。

        使用场景：
        - `ide_write_file` 写入骨架后，用此工具逐段追加内容。
        - 需要分多次写入同一个大文件，避免单次 content 过大导致 LLM 超时。

        ⚠️ 重要规则：
        - 每次追加内容不要超过单次写入上限。
        - 大型项目优先拆成多个模块文件，而不是把所有内容追加到单一文件。
        - 目标文件必须先存在，不存在时请先用 ide_write_file 创建。

        Args:
            filename(string): 文件名（不含路径），或超级管理员使用的绝对路径。
            content(string): 要追加到文件末尾的内容。

        Returns:
            操作结果说明。
        """
        if self.cover_only_mode:
            return "⛔ 仅翻唱模式已开启：禁止写入文件。"
        sandbox_id = self._get_sandbox_id(event)
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以使用文件操作工具。"
        is_super = self._is_super_admin(event)
        path = self._resolve(sandbox_id, filename, allow_bypass=is_super)
        if not path:
            return f"错误：文件名 `{filename}` 不合法。"
        content_bytes = content.encode("utf-8")
        content_size = len(content_bytes)
        logger.info(f"[IdeSandbox] ide_append_to_file start: {filename!r}, append={content_size:,}B")
        if content_size > self.single_write_limit_bytes:
            await self._step_notice(
                event,
                f"⚠️ `{filename}` 追加内容 {content_size:,}B 超过单次写入上限 {self.single_write_limit_kb}KB，松子会让 AI 拆小一点~",
            )
            return (
                f"⛔ 错误：追加内容 {content_size:,}B 超过单次写入上限 {self.single_write_limit_kb}KB。\n"
                f"请拆分为更小的段落，分多次追加。"
            )
        super_tag = self._get_super_tag(event, filename)
        try:
            if not await asyncio.to_thread(lambda: path.exists() and path.is_file()):
                return f"错误：文件 `{filename}` 不存在，请先使用 `ide_write_file` 创建文件。"
            old_size = (await asyncio.to_thread(path.stat)).st_size
            if old_size + content_size > self.max_file_size_mb * 1024 * 1024:
                return f"错误：追加后文件将超过 {self.max_file_size_mb}MB 限制。"
            await self._broadcast(event, f"{super_tag}🤖 AI 正在追加内容到 `{filename}`（+{content_size:,}B）...")
            def _append():
                with path.open("ab") as f:
                    f.write(content_bytes)
            await asyncio.to_thread(_append)
            new_size = (await asyncio.to_thread(path.stat)).st_size
            logger.info(f"[IdeSandbox] ide_append_to_file done: {filename!r}, {old_size:,}B -> {new_size:,}B")
            self._record(sandbox_id, "append", f"{filename}{' [SUPER]' if is_super else ''}")
            await self._step_notice(event, f"✅ `{filename}` 已追加完成（{old_size:,}B → {new_size:,}B）")
            return f"✅ 已追加到 `{filename}` 末尾（{old_size:,}B → {new_size:,}B）。"
        except Exception as e:
            logger.exception(f"[IdeSandbox] ide_append_to_file failed: {filename!r}")
            await self._step_notice(event, f"❌ `{filename}` 追加失败：{e}")
            return f"追加失败: {e}"


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
        """在沙盒文件中进行查找替换编辑，支持单次或批量编辑。

        使用场景：
        - 修改已有文件的某一部分内容，比完整重写更安全。
        - 进行小幅调整：修 bug、改配置、重命名局部变量等。
        - 需要一次修改多处时，使用 edits 参数批量传入。

        ⚠️ 重要规则：
        - old_string 必须精确匹配；默认只替换第一处。
        - 如果确实要替换全部匹配，请设置 replace_all=true，或在 edits 每项中传 replace_all=true。
        - old_string 和 new_string 只放需要替换的片段，不要粘贴整份文件。
        - 批量编辑时，所有 old_string 会先被校验是否存在，任一不存在则整体失败，避免文件被改到一半。
        - 如果文件已经很大，不要反复用 ide_edit_file 做大量小修改，这会导致多次 LLM 工具调用累积超时。
        - 对于大文件，优先拆分成多个小文件，然后对各个小文件独立编辑。

        Args:
            filename(string): 要编辑的文件名，或超级管理员使用的绝对路径。
            old_string(string, optional): 单条编辑时要替换的旧字符串。
            new_string(string, optional): 单条编辑时用于替换的新字符串。
            replace_all(bool, optional): 是否替换全部匹配，默认 False。
            edits(string, optional): 批量编辑的 JSON 列表，格式如：
                `[{"old_string":"a","new_string":"b","replace_all":false}]`
                传入 edits 时，old_string/new_string 参数会被忽略。

        Returns:
            替换结果说明，包含替换次数。
        """
        if self.cover_only_mode:
            return "⛔ 仅翻唱模式已开启：禁止编辑文件。"
        sandbox_id = self._get_sandbox_id(event)
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以使用文件操作工具。"
        is_super = self._is_super_admin(event)
        path = self._resolve(sandbox_id, filename, allow_bypass=is_super)
        if not path or not await asyncio.to_thread(lambda: path.exists() and path.is_file()):
            return f"错误：文件 `{filename}` 不存在。"
        size = (await asyncio.to_thread(path.stat)).st_size
        if size > self.max_file_size_mb * 1024 * 1024:
            return f"错误：文件大小 {size:,}B 超过 {self.max_file_size_mb}MB 限制。"

        # 解析编辑列表
        edit_list: list[dict[str, object]] = []
        if edits and edits.strip():
            try:
                parsed = json.loads(edits)
                if not isinstance(parsed, list):
                    return "错误：edits 必须是 JSON 数组。"
                for item in parsed:
                    if not isinstance(item, dict):
                        return "错误：edits 数组中的每项必须是对象。"
                    old = item.get("old_string", "")
                    new = item.get("new_string", "")
                    if not old:
                        return "错误：edits 中的每项必须包含非空的 old_string。"
                    edit_list.append({
                        "old_string": str(old),
                        "new_string": str(new),
                        "replace_all": bool(item.get("replace_all", False)),
                    })
            except json.JSONDecodeError as e:
                return f"错误：edits 不是合法的 JSON: {e}"
        elif old_string:
            edit_list.append({
                "old_string": old_string,
                "new_string": new_string,
                "replace_all": bool(replace_all),
            })
        else:
            return "错误：请提供 old_string/new_string 或 edits 参数。"

        try:
            content = await asyncio.to_thread(path.read_text, encoding="utf-8")

            # 先校验所有 old_string 是否存在
            missing = []
            for item in edit_list:
                if item["old_string"] not in content:
                    missing.append(item["old_string"][:80])
            if missing:
                return f"错误：在 `{filename}` 中未找到以下旧字符串（避免部分修改）：\n" + "\n".join(f"  - {m}" for m in missing)

            # 计算总替换次数并应用编辑
            total_count = 0
            new_content = content
            for item in edit_list:
                old = str(item["old_string"])
                new = str(item["new_string"])
                item_replace_all = bool(item.get("replace_all", False))
                count = new_content.count(old)
                if not item_replace_all:
                    count = 1 if count else 0
                    new_content = new_content.replace(old, new, 1)
                else:
                    new_content = new_content.replace(old, new)
                total_count += count

            if dry_run:
                preview = _build_unified_preview(filename, content, new_content)
                return f"🔎 编辑预览 `{filename}`（未实际写入，共将替换 {total_count} 处）：\n```diff\n{preview}\n```"

            super_tag = self._get_super_tag(event, filename)
            edit_count = len(edit_list)
            await self._broadcast(
                event,
                f"{super_tag}🤖 AI 正在编辑文件 `{filename}`（{edit_count} 条编辑，共替换 {total_count} 处）...",
            )
            await self._write_bytes_with_progress(
                event,
                path,
                new_content.encode("utf-8"),
                filename,
                prefix=super_tag,
            )
            added, removed = self._line_delta(content, new_content)
            await self._broadcast_file_change(
                event,
                sandbox_id,
                "已编辑",
                path,
                added,
                removed,
                prefix=super_tag,
            )
            self._record(sandbox_id, "edit", f"{filename}: {edit_count} 条编辑，共替换 {total_count} 处{' [SUPER]' if is_super else ''}")
            return f"✅ 已在 `{filename}` 中完成 {edit_count} 条编辑，共替换 {total_count} 处。"
        except Exception as e:
            return f"编辑失败: {e}"


    async def ide_delete_file(self, event: AstrMessageEvent, filename: str, dry_run: bool = False) -> str:
        """删除沙盒中的指定文件。

        使用场景：
        - 确认某个文件不再需要，清理临时文件或旧版本文件。
        - 配合文件写入操作，先删除旧文件再重建。

        ⚠️ 注意：
        - 删除操作不可恢复，删除前请确认文件名正确。
        - 超级管理员可删除沙盒外文件，请谨慎使用。

        Args:
            filename(string): 要删除的文件名，或超级管理员使用的绝对路径。

        Returns:
            操作结果说明。
        """
        if self.cover_only_mode:
            return "⛔ 仅翻唱模式已开启：禁止删除文件。"
        sandbox_id = self._get_sandbox_id(event)
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以使用文件操作工具。"
        is_super = self._is_super_admin(event)
        path = self._resolve(sandbox_id, filename, allow_bypass=is_super)
        if not path or not await asyncio.to_thread(lambda: path.exists() and path.is_file()):
            return f"错误：文件 `{filename}` 不存在。"
        size = (await asyncio.to_thread(path.stat)).st_size
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            removed_lines = len(text.splitlines())
        except Exception:
            removed_lines = 0
        if dry_run:
            return (
                f"🔎 删除预览 `{filename}`（未实际删除）：\n"
                f"路径: {path}\n大小: {size:,}B\n预计删除行数: {removed_lines}"
            )
        super_tag = self._get_super_tag(event, filename)
        await self._broadcast(event, f"{super_tag}🤖 AI 正在删除文件 `{filename}`（{size:,}B）...")
        try:
            await asyncio.to_thread(path.unlink)
            await self._broadcast_file_change(
                event,
                sandbox_id,
                "已删除",
                path,
                0,
                removed_lines,
                prefix=super_tag,
            )
            self._record(sandbox_id, "delete", f"{filename}{' [SUPER]' if is_super else ''}")
            return f"🗑️ 已删除 `{filename}`。"
        except Exception as e:
            return f"删除失败: {e}"


    async def ide_clear_sandbox(
        self,
        event: AstrMessageEvent,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> str:
        """清空当前沙盒中的所有文件和目录，但保留沙盒根目录。"""
        if self.cover_only_mode:
            return "⛔ 仅翻唱模式已开启：禁止清空沙盒。"
        sandbox_id = self._get_sandbox_id(event)
        if not await self._check_permission(event):
            return "权限不足：只有管理员或主人才可以使用文件操作工具。"
        sandbox = self._get_group_sandbox(sandbox_id)

        def _snapshot():
            children = sorted(sandbox.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            file_count = 0
            dir_count = 0
            total_bytes = 0
            preview = []
            for child in children:
                if len(preview) < 12:
                    preview.append(child.name + ("/" if child.is_dir() and not child.is_symlink() else ""))
                if child.is_symlink() or child.is_file():
                    file_count += 1
                    try:
                        total_bytes += child.stat().st_size
                    except OSError:
                        pass
                    continue
                if child.is_dir():
                    dir_count += 1
                    for path in child.rglob("*"):
                        if path.is_symlink() or path.is_file():
                            file_count += 1
                            try:
                                total_bytes += path.stat().st_size
                            except OSError:
                                pass
                        elif path.is_dir():
                            dir_count += 1
            return children, file_count, dir_count, total_bytes, preview

        children, file_count, dir_count, total_bytes, preview = await asyncio.to_thread(_snapshot)
        if not children:
            return "📂 当前沙盒已经是空的。"

        preview_text = "\n".join(f"- {name}" for name in preview)
        if len(children) > len(preview):
            preview_text += f"\n- ... 另有 {len(children) - len(preview)} 个顶层项目"
        summary = f"{file_count} 个文件、{dir_count} 个目录（约 {total_bytes:,}B）"
        if dry_run or not confirm:
            return (
                f"🔎 清空沙盒预览（未实际删除）：将删除 {summary}。\n"
                f"{preview_text}\n"
                "确认要清空时请调用 ide_clear_sandbox(confirm=true)。"
            )

        await self._broadcast(event, f"🤖 AI 正在清空当前沙盒（{summary}）...")

        def _clear():
            for child in children:
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child, ignore_errors=False)
            return list(sandbox.iterdir())

        try:
            remaining = await asyncio.to_thread(_clear)
            if remaining:
                names = ", ".join(path.name for path in remaining[:8])
                return f"清空失败：仍有 {len(remaining)} 个项目未删除：{names}"
            self._record(sandbox_id, "clear_sandbox", summary)
            return f"🧹 已清空沙盒：删除 {summary}。"
        except Exception as e:
            return f"清空失败: {e}"
