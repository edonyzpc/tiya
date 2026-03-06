from pathlib import Path

from src.provider_defaults import resolve_claude_bin, resolve_codex_bin


def test_resolve_codex_bin_uses_macos_app_candidates_when_path_missing():
    resolved = resolve_codex_bin(
        None,
        system_name="Darwin",
        which=lambda _: None,
        is_executable=lambda path: path == Path("/Applications/Codex.app/Contents/Resources/codex"),
    )

    assert resolved == "/Applications/Codex.app/Contents/Resources/codex"


def test_resolve_claude_bin_uses_macos_homebrew_candidates_when_path_missing():
    resolved = resolve_claude_bin(
        None,
        system_name="Darwin",
        which=lambda _: None,
        is_executable=lambda path: path == Path("/opt/homebrew/bin/claude"),
    )

    assert resolved == "/opt/homebrew/bin/claude"
