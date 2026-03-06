from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from typing import Callable, Iterable, Optional


def current_system() -> str:
    return platform.system()


def is_macos(system_name: Optional[str] = None) -> bool:
    return (system_name or current_system()) == "Darwin"


def default_codex_session_root() -> Path:
    return Path("~/.codex/sessions").expanduser()


def default_claude_session_root() -> Path:
    return Path("~/.claude/projects").expanduser()


def codex_bin_candidates(system_name: Optional[str] = None) -> tuple[Path, ...]:
    candidates = [Path("/Applications/Codex.app/Contents/Resources/codex")]
    if is_macos(system_name):
        candidates.append(Path("~/Applications/Codex.app/Contents/Resources/codex").expanduser())
    return tuple(candidates)


def claude_bin_candidates(system_name: Optional[str] = None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    if is_macos(system_name):
        candidates.extend(
            [
                Path("/opt/homebrew/bin/claude"),
                Path("/usr/local/bin/claude"),
            ]
        )
    candidates.append(Path("~/.local/bin/claude").expanduser())
    return tuple(candidates)


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _resolve_bin(
    configured: Optional[str],
    command_name: str,
    candidates: Iterable[Path],
    *,
    which: Callable[[str], Optional[str]] = shutil.which,
    is_executable: Callable[[Path], bool] = _is_executable,
) -> str:
    if configured:
        return configured

    found = which(command_name)
    if found:
        return found

    for candidate in candidates:
        expanded = candidate.expanduser()
        if is_executable(expanded):
            return str(expanded)
    return command_name


def resolve_codex_bin(
    configured: Optional[str],
    *,
    system_name: Optional[str] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    is_executable: Callable[[Path], bool] = _is_executable,
) -> str:
    return _resolve_bin(
        configured,
        "codex",
        codex_bin_candidates(system_name),
        which=which,
        is_executable=is_executable,
    )


def resolve_claude_bin(
    configured: Optional[str],
    *,
    system_name: Optional[str] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    is_executable: Callable[[Path], bool] = _is_executable,
) -> str:
    return _resolve_bin(
        configured,
        "claude",
        claude_bin_candidates(system_name),
        which=which,
        is_executable=is_executable,
    )
