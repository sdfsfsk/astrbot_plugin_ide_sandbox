from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import List, Optional, Set


BANNED_COMMANDS = {
    "rm", "del", "format", "fdisk", "dd", "mkfs", "shutdown", "reboot",
    "poweroff", "halt", "regedit", "reg", "sc", "net", "taskkill",
    "attrib", "cacls", "icacls", "takeown", "cipher", "rd", "rmdir",
    "deltree", "deltree.exe", "format.com", "cmd.exe", "powershell.exe",
    "wscript", "cscript", "mshta", "regsvr32", "certutil",
}

BANNED_PATHS = [
    "C:\\Windows", "C:\\Program Files", "C:\\ProgramData",
    "C:\\Users", "C:\\System", "C:\\", "/etc", "/usr", "/bin",
    "/sbin", "/lib", "/sys", "/proc", "/dev", "/root", "/var",
]

SUPER_BANNED_COMMANDS = {
    "rm", "del", "format", "fdisk", "dd", "mkfs",
    "shutdown", "reboot", "poweroff", "halt",
    "deltree", "deltree.exe", "format.com",
}

DEFAULT_EXECUTION_WHITELIST = {
    "python", "python3", "py", "node", "npm", "npx",
    "pip", "pytest", "dir", "ls", "type", "cat", "git",
    "go", "javac", "java", "mvn", "gradle",
}

ELEVATED_COMMAND_ALLOWLIST = {
    ("winget", "source", "list"),
    ("winget", "source", "reset"),
    ("winget", "source", "update"),
    ("winget", "source", "add"),
    ("winget", "source", "remove"),
}

SEARCH_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    "node_modules", "dist", "build", ".venv", "venv", "env",
}

TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml",
    ".md", ".txt", ".html", ".css", ".scss", ".java", ".kt", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".php", ".rb", ".sh", ".bat", ".ps1",
    ".xml", ".sql", ".ini", ".cfg", ".gradle", ".properties", ".vue",
}

SENSITIVE_FILENAMES = {
    ".env", ".env.local", ".env.development", ".env.production",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", "credentials.json", "secrets.json",
    "token", "token.txt", "private.key", "key.pem",
}

SENSITIVE_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cer",
}

SENSITIVE_NAME_RE = re.compile(
    r"(^|[._-])(secret|secrets|token|credential|credentials|apikey|api_key|password|passwd|private)([._-]|$)",
    re.IGNORECASE,
)

MEDIA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".wav", ".flac", ".ogg",
    ".zip", ".7z", ".rar", ".tar", ".gz", ".exe", ".dll", ".so", ".bin",
}


def _safe_filename(name: str) -> Optional[str]:
    if not name or name in (".", ".."):
        return None
    safe = re.sub(r'[\\/:<>"|?*\x00-\x1f]', "", name)
    safe = safe.strip(". ")
    if not safe or safe in (".", ".."):
        return None
    return safe


def _is_path_safe(base_dir: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base_dir.resolve())
        return True
    except ValueError:
        return False


def _is_protected_path(target: Path) -> bool:
    target_str = str(target.resolve()).lower().replace("\\", "/").rstrip("/")
    for banned in BANNED_PATHS:
        banned_norm = banned.lower().replace("\\", "/").rstrip("/")
        if target_str == banned_norm or target_str.startswith(f"{banned_norm}/"):
            return True
    return False


def _is_sensitive_file(target: Path) -> bool:
    """Best-effort secret file guard for read/search tools."""
    name = target.name.lower()
    if name in SENSITIVE_FILENAMES:
        return True
    if target.suffix.lower() in SENSITIVE_SUFFIXES:
        return True
    return bool(SENSITIVE_NAME_RE.search(name))


def _is_probably_binary_file(target: Path, sample: bytes) -> bool:
    """Return True for media/archive/binary-looking files."""
    if target.suffix.lower() in MEDIA_EXTENSIONS:
        return True
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    control = sum(1 for b in sample if b < 9 or (13 < b < 32))
    return control / max(1, len(sample)) > 0.10


def _safe_relative_path(path: str) -> Optional[List[str]]:
    if not path or "\x00" in path:
        return None
    raw = Path(path)
    if raw.is_absolute() or raw.drive:
        return None
    parts: List[str] = []
    for part in raw.parts:
        part = part.strip()
        if not part or part in (".", ".."):
            return None
        if re.search(r'[<>:"|?*\x00-\x1f]', part):
            return None
        safe = part.strip(" ")
        if not safe or safe in (".", "..") or safe != part or safe.endswith("."):
            return None
        parts.append(safe)
    return parts or None


def _strip_current_sandbox_prefix(parts: List[str], sandbox_id: str) -> List[str]:
    """Normalize paths that accidentally include the current sandbox root."""
    safe_id = re.sub(r'[^\w-]', '', str(sandbox_id))
    if not safe_id:
        return parts

    prefixes = (
        ["data", "astrbot_plugin_ide_sandbox", "sandboxes", safe_id],
        ["astrbot_plugin_ide_sandbox", "sandboxes", safe_id],
        ["sandboxes", safe_id],
    )
    folded = [p.casefold() for p in parts]
    for prefix in prefixes:
        if len(parts) <= len(prefix):
            continue
        prefix_folded = [p.casefold() for p in prefix]
        if folded[: len(prefix_folded)] == prefix_folded:
            return parts[len(prefix) :]
    return parts


def _split_command_segments(cmd: str, allow_and: bool) -> tuple[Optional[List[str]], str]:
    segments: List[str] = []
    current: List[str] = []
    quote = ""
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if ch in ("'", '"'):
            if quote == ch:
                quote = ""
            elif not quote:
                quote = ch
            current.append(ch)
            i += 1
            continue
        if allow_and and not quote and cmd[i:i + 2] == "&&":
            segment = "".join(current).strip()
            if not segment:
                return None, "empty command segment"
            segments.append(segment)
            current = []
            i += 2
            continue
        current.append(ch)
        i += 1
    if quote:
        return None, "unclosed quote"
    segment = "".join(current).strip()
    if not segment:
        return None, "empty command"
    segments.append(segment)
    return segments, ""


def _tokenize_command_segment(segment: str) -> tuple[Optional[List[str]], str]:
    try:
        parts = shlex.split(segment, posix=True)
    except ValueError as e:
        return None, f"invalid command quoting: {e}"
    if not parts:
        return None, "empty command"
    return parts, ""


def _normalize_program_name(program: str) -> tuple[str, str]:
    cleaned = program.strip().strip('"').strip("'").lower()
    basename = os.path.basename(cleaned.replace("\\", "/"))
    return cleaned, os.path.splitext(basename)[0]


def _is_command_safe(
    cmd: str,
    whitelist: Optional[Set[str]] = None,
    allow_and: bool = False,
    unrestricted: bool = False,
) -> tuple[bool, str]:
    raw = (cmd or "").strip()
    if not raw:
        return False, "empty command"
    if re.search(r"[%!]", raw):
        return False, "forbidden shell variable expansion (%VAR% or !VAR!)"

    if unrestricted and allow_and:
        if re.search(r"[;|`$<>\n\r]", raw):
            return False, "forbidden dangerous shell metacharacter (; | ` $ < >)"
    else:
        if re.search(r"[;&|`$<>\n\r]", raw):
            return False, "forbidden shell metacharacter (; | & ` $ < >); only one command is allowed"

    segments, split_error = _split_command_segments(raw, allow_and=unrestricted and allow_and)
    if segments is None:
        return False, split_error

    allowed = {os.path.splitext(x.lower())[0] for x in whitelist} if whitelist else set()
    allowed_raw = {x.lower() for x in whitelist} if whitelist else set()

    for segment in segments:
        parts, token_error = _tokenize_command_segment(segment)
        if parts is None:
            return False, token_error
        prog, prog_base = _normalize_program_name(parts[0])

        if whitelist and prog_base not in allowed and prog not in allowed_raw:
            return False, f"command `{parts[0]}` is not in whitelist: {', '.join(sorted(whitelist))}"

        if unrestricted:
            if prog_base in SUPER_BANNED_COMMANDS or prog in SUPER_BANNED_COMMANDS:
                return False, f"forbidden command `{parts[0]}` (super admin is still restricted)"
        else:
            if prog_base in BANNED_COMMANDS or prog in BANNED_COMMANDS:
                return False, f"forbidden command `{parts[0]}`"

        if prog_base in ("rm", "del"):
            return False, "forbidden delete command"

        if prog_base in ("python", "python3", "py") or prog in ("python", "python3", "py"):
            lower_parts = [p.lower() for p in parts]
            if "-c" in lower_parts:
                return False, "forbidden `python -c`; write a .py file and run it instead"
            dangerous_modules = {"os", "sys", "subprocess", "socket", "shutil", "pathlib"}
            if "-m" in lower_parts:
                idx = lower_parts.index("-m")
                if idx + 1 < len(lower_parts) and lower_parts[idx + 1] in dangerous_modules:
                    return False, f"forbidden `python -m {lower_parts[idx + 1]}`"
    return True, ""


def _is_elevated_command_allowed(cmd: str) -> tuple[bool, str]:
    segments, split_error = _split_command_segments(cmd, allow_and=False)
    if segments is None or len(segments) != 1:
        return False, split_error or "only one elevated command is allowed"
    parts, token_error = _tokenize_command_segment(segments[0])
    if parts is None:
        return False, token_error
    normalized = [_normalize_program_name(parts[0])[1], *[p.lower() for p in parts[1:3]]]
    if normalized[0] != "winget":
        return False, "elevated execution is limited to approved maintenance commands"
    safe, reason = _is_command_safe(cmd, {"winget"})
    if not safe:
        return False, reason
    if tuple(normalized[:3]) in ELEVATED_COMMAND_ALLOWLIST:
        return True, ""
    return False, "elevated execution is limited to approved maintenance commands"
