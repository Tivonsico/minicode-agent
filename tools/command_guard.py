"""Directly refuse recognizable destructive Shell commands before approval.

This is intentionally a blacklist, not a parser for arbitrary scripts. The
filesystem and database boundaries must still enforce their own limits.
"""

import ntpath
import posixpath
import re
from pathlib import Path

from .base import ToolError

_DISK = re.compile(
    r"\b(?:Format-Volume|Clear-Disk|Initialize-Disk|mkfs(?:\.[\w-]+)?|"
    r"diskpart\b[^\r\n;]*\bclean\b|dd\b[^\r\n;]*\bof\s*=\s*/dev/)\b",
    re.IGNORECASE,
)
_REMOTE_EXEC = re.compile(
    r"\b(?:curl|wget|irm|iwr|Invoke-WebRequest|Invoke-RestMethod)\b"
    r"[^\r\n;]*\|\s*(?:&\s*)?(?:bash|sh|iex|Invoke-Expression)\b",
    re.IGNORECASE,
)
_FORCE_PUSH = re.compile(r"\bgit\s+push\b[^\r\n;]*(?:--force(?:-with-lease)?\b|\s-f\b)", re.IGNORECASE)
_DROP_DB = re.compile(
    r"\bDROP\s+DATABASE\b|\bdropdb\b|\bmysqladmin\b[^\r\n;]*\bdrop\b|"
    r"\bdropDatabase\s*\(",
    re.IGNORECASE,
)
_POSIX_RM = re.compile(r"\brm\b([^\r\n;&|]*)", re.IGNORECASE)
_POWERSHELL_RM = re.compile(r"\b(?:Remove-Item|ri)\b[^\r\n;]*", re.IGNORECASE)
_CMD_RMDIR = re.compile(r"\b(?:rmdir|rd)\b[^\r\n;]*", re.IGNORECASE)
_ARGUMENT = re.compile(r"\"[^\"]*\"|'[^']*'|[^\s]+")


def _clean(value: str) -> str:
    return value.strip().strip("'\"`.,;)")


def _dangerous_target(value: str, roots: tuple[Path, ...]) -> bool:
    value = _clean(value)
    if posixpath.normpath(value) == "/":
        return True
    normalized = ntpath.normcase(ntpath.normpath(value))
    if not ntpath.isabs(value):
        return False
    protected = {ntpath.normcase(ntpath.normpath(str(path))) for path in roots}
    protected.update(
        {
            ntpath.normcase(ntpath.normpath(ntpath.splitdrive(str(path))[0] + "\\"))
            for path in roots
            if ntpath.splitdrive(str(path))[0]
        }
    )
    windows = ntpath.normcase(ntpath.normpath("C:\\Windows"))
    return normalized in protected or normalized == windows or normalized.startswith(windows + "\\")


def classify(command: str, roots: tuple[Path, ...] = ()) -> str | None:
    if _DISK.search(command):
        return "disk_management"
    if _REMOTE_EXEC.search(command):
        return "download_and_execute"
    if _FORCE_PUSH.search(command):
        return "force_push"
    if _DROP_DB.search(command):
        return "drop_database"
    for match in _POSIX_RM.finditer(command):
        recursive = force = False
        targets = []
        options_ended = False
        for token in _ARGUMENT.findall(match.group(1)):
            token = _clean(token)
            if token == "--":
                options_ended = True
            elif not options_ended and token.startswith("--"):
                recursive |= token == "--recursive"
                force |= token == "--force"
            elif not options_ended and re.fullmatch(r"-[A-Za-z]+", token):
                recursive |= "r" in token.lower() or "R" in token
                force |= "f" in token.lower()
            else:
                targets.append(token)
        if recursive and force and any(_dangerous_target(target, roots) for target in targets):
            return "recursive_root_delete"
    for match in (*_POWERSHELL_RM.finditer(command), *_CMD_RMDIR.finditer(command)):
        text = match.group(0)
        recursive = bool(
            re.search(r"(?<!\w)-Recurse\b|(?<!\w)/(?:s|S)\b", text, re.IGNORECASE)
        )
        if recursive and any(
            _dangerous_target(token, roots) for token in _ARGUMENT.findall(text)
        ):
            return "recursive_root_delete"
    return None


def check_command(command: str, roots: tuple[Path, ...] = ()) -> None:
    category = classify(command, roots)
    if category:
        raise ToolError(f"High-risk command blocked: {category}.", "dangerous_command")
