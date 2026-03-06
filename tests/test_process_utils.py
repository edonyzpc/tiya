from subprocess import CompletedProcess

from src.process_utils import ProcessSnapshot, parse_ps_output, read_process_snapshot


def test_parse_ps_output_extracts_stat_and_command():
    snapshot = parse_ps_output(42, "Ss   python -m src\n")

    assert snapshot == ProcessSnapshot(pid=42, stat="Ss", cmdline="python -m src")


def test_read_process_snapshot_falls_back_to_ps(monkeypatch):
    monkeypatch.setattr("src.process_utils.pid_exists", lambda pid: True)
    monkeypatch.setattr("src.process_utils._read_proc_snapshot", lambda pid: None)
    monkeypatch.setattr(
        "src.process_utils.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(args=args, returncode=0, stdout="Z    python <defunct>\n", stderr=""),
    )

    snapshot = read_process_snapshot(99)

    assert snapshot is not None
    assert snapshot.stat == "Z"
    assert snapshot.cmdline == "python <defunct>"
    assert snapshot.is_zombie is True
