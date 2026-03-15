#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import cProfile
import json
import os
import pstats
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SOURCE_TREE = Path(__file__).resolve().parents[1]
DEFAULT_SESSION_COUNT = 200
DEFAULT_STATUS_ITERATIONS = 25
DEFAULT_DIAGNOSTICS_ITERATIONS = 10
DEFAULT_SUBSCRIPTION_WAIT_MS = 2200
DEFAULT_PROFILE_TOP = 12
TOKEN_VALUE = "123456:abcdefghijklmnopqrstuvwxyz12345"


@dataclass(frozen=True)
class Sandbox:
    root: Path
    env_file: Path
    runtime_root: Path
    codex_root: Path
    claude_root: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark desktop-adjacent supervisor performance for a given source tree."
    )
    parser.add_argument(
        "--source-tree",
        default=str(DEFAULT_SOURCE_TREE),
        help="Path to the tiya source tree to benchmark. Defaults to the current checkout.",
    )
    parser.add_argument(
        "--session-count",
        type=int,
        default=DEFAULT_SESSION_COUNT,
        help="Number of synthetic Codex session files to generate for sessions.list benchmarks.",
    )
    parser.add_argument(
        "--status-iterations",
        type=int,
        default=DEFAULT_STATUS_ITERATIONS,
        help="Number of repeated service.status RPC calls for warm-latency statistics.",
    )
    parser.add_argument(
        "--diagnostics-iterations",
        type=int,
        default=DEFAULT_DIAGNOSTICS_ITERATIONS,
        help="Number of repeated diagnostics.report RPC calls for warm-latency statistics.",
    )
    parser.add_argument(
        "--subscription-wait-ms",
        type=int,
        default=DEFAULT_SUBSCRIPTION_WAIT_MS,
        help="Idle subscription dwell time used to count unsolicited events.",
    )
    parser.add_argument(
        "--profile-top",
        type=int,
        default=DEFAULT_PROFILE_TOP,
        help="Number of cProfile entries to keep for cold direct-call profiling.",
    )
    parser.add_argument(
        "--json-indent",
        type=int,
        default=2,
        help="Indent level for JSON output.",
    )
    return parser.parse_args()


def insert_source_tree(source_tree: Path) -> None:
    sys.path.insert(0, str(source_tree))


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[rank]


def summarize_samples(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "min_ms": 0.0, "median_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    return {
        "count": len(values),
        "min_ms": min(values),
        "median_ms": statistics.median(values),
        "p95_ms": percentile(values, 95),
        "max_ms": max(values),
    }


def round_payload(payload: Any) -> Any:
    if isinstance(payload, float):
        return round(payload, 3)
    if isinstance(payload, dict):
        return {key: round_payload(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [round_payload(item) for item in payload]
    return payload


def _write_codex_session(path: Path, session_id: str, cwd: str, user_text: str, assistant_text: str) -> None:
    lines = [
        {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": "2026-03-05T00:00:00Z",
                "cwd": cwd,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": user_text,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": assistant_text,
            },
        },
        "not-json-line",
        "",
    ]
    with path.open("w", encoding="utf-8") as handle:
        for item in lines:
            if isinstance(item, str):
                handle.write(item + "\n")
            else:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def create_sandbox(base_root: Path, session_count: int, write_env_file: Any) -> Sandbox:
    sandbox = Sandbox(
        root=base_root,
        env_file=base_root / "config" / "tiya.env",
        runtime_root=base_root / "runtime",
        codex_root=base_root / "sessions" / "codex",
        claude_root=base_root / "sessions" / "claude",
    )
    sandbox.codex_root.mkdir(parents=True, exist_ok=True)
    sandbox.claude_root.mkdir(parents=True, exist_ok=True)
    workspace_root = base_root / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    write_env_file(
        sandbox.env_file,
        {
            "DEFAULT_PROVIDER": "codex",
            "DEFAULT_CWD": str(workspace_root),
            "CODEX_SESSION_ROOT": str(sandbox.codex_root),
            "CLAUDE_SESSION_ROOT": str(sandbox.claude_root),
        },
    )
    for index in range(session_count):
        session_id = f"session-{index:04d}"
        user_text = (
            f"benchmark user prompt {index} "
            " ".join(["trace"] * 12)
        )
        assistant_text = (
            f"benchmark assistant response {index} "
            " ".join(["payload"] * 18)
        )
        _write_codex_session(
            sandbox.codex_root / f"{session_id}.jsonl",
            session_id,
            str(workspace_root / f"project-{index % 7}"),
            user_text,
            assistant_text,
        )
    return sandbox


def configure_runtime_env(sandbox: Sandbox) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "ENV_FILE": str(sandbox.env_file),
            "TIYA_HOME": str(sandbox.runtime_root),
            "TIYA_SECRET_STORE_BACKEND": "file",
            "TIYA_DESKTOP_PID": str(os.getpid()),
            "PYTHONUNBUFFERED": "1",
        }
    )
    os.environ.update(env)
    return env


def seed_secret(TiyaSupervisor: Any, TELEGRAM_TOKEN_SECRET: str) -> None:
    supervisor = TiyaSupervisor()
    supervisor.secret_store.set(TELEGRAM_TOKEN_SECRET, TOKEN_VALUE)


def profile_stats_entries(profile: cProfile.Profile, source_tree: Path, top_n: int) -> list[dict[str, object]]:
    stats = pstats.Stats(profile)
    stats.sort_stats("cumulative")
    entries: list[dict[str, object]] = []
    functions = stats.fcn_list or []
    for func in functions[:top_n]:
        ccalls, ncalls, total_time, cumulative_time, _callers = stats.stats[func]
        filename, lineno, func_name = func
        try:
            display_path = str(Path(filename).resolve().relative_to(source_tree))
        except ValueError:
            display_path = filename
        entries.append(
            {
                "function": f"{display_path}:{lineno}:{func_name}",
                "primitive_calls": int(ccalls),
                "total_calls": int(ncalls),
                "self_ms": total_time * 1000.0,
                "cum_ms": cumulative_time * 1000.0,
            }
        )
    return entries


async def profile_direct_calls(
    source_tree: Path,
    profile_top: int,
    TiyaSupervisor: Any,
) -> dict[str, object]:
    async def profile_one(method_name: str) -> dict[str, object]:
        supervisor = TiyaSupervisor()
        profiler = cProfile.Profile()
        started_at = time.perf_counter()
        profiler.enable()
        await getattr(supervisor, method_name)()
        profiler.disable()
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        return {
            "elapsed_ms": elapsed_ms,
            "top": profile_stats_entries(profiler, source_tree, profile_top),
        }

    return {
        "service_status": await profile_one("service_status"),
        "diagnostics_report": await profile_one("diagnostics_report"),
    }


async def rpc_call(
    socket_path: Path,
    method: str,
    params: dict[str, object] | None = None,
    timeout_sec: float = 10.0,
) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    request = {
        "id": f"bench-{method}",
        "method": method,
        "params": params or {},
    }
    writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
    await writer.drain()
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout_sec)
    finally:
        writer.close()
        await writer.wait_closed()
    if not line:
        raise RuntimeError(f"empty response for RPC {method}")
    payload = json.loads(line.decode("utf-8"))
    if payload.get("ok") is not True:
        error = payload.get("error") or {}
        raise RuntimeError(f"{method} failed: {error}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{method} returned a non-object payload")
    return result


async def collect_subscription_events(socket_path: Path, dwell_ms: int) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    request = {
        "id": "bench-subscription",
        "method": "service.subscribe",
        "params": {},
    }
    started_at = time.perf_counter()
    writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=10.0)
    connect_ms = (time.perf_counter() - started_at) * 1000.0
    payload = json.loads(line.decode("utf-8"))
    if payload.get("ok") is not True:
        error = payload.get("error") or {}
        raise RuntimeError(f"service.subscribe failed: {error}")
    event_counts: Counter[str] = Counter()
    deadline = time.perf_counter() + (dwell_ms / 1000.0)
    try:
        while True:
            timeout = deadline - time.perf_counter()
            if timeout <= 0:
                break
            try:
                event_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            if not event_line:
                break
            envelope = json.loads(event_line.decode("utf-8"))
            if envelope.get("type") == "event":
                event_name = str(envelope.get("event") or "")
                event_counts[event_name] += 1
    finally:
        writer.close()
        await writer.wait_closed()
    return {
        "connect_ms": connect_ms,
        "events": dict(event_counts),
        "total_events": int(sum(event_counts.values())),
    }


async def measure_rpc_samples(
    socket_path: Path,
    method: str,
    params: dict[str, object] | None,
    iterations: int,
) -> list[float]:
    samples: list[float] = []
    for _ in range(iterations):
        started_at = time.perf_counter()
        await rpc_call(socket_path, method, params)
        samples.append((time.perf_counter() - started_at) * 1000.0)
    return samples


async def measure_parallel_bootstrap(socket_path: Path) -> float:
    started_at = time.perf_counter()
    await asyncio.gather(
        rpc_call(socket_path, "service.status", {}),
        rpc_call(socket_path, "config.get", {}),
        rpc_call(socket_path, "diagnostics.report", {}),
    )
    return (time.perf_counter() - started_at) * 1000.0


async def wait_for_supervisor_boot(socket_path: Path, expected_desktop_pid: int, timeout_sec: float = 10.0) -> float:
    started_at = time.perf_counter()
    deadline = started_at + timeout_sec
    while time.perf_counter() < deadline:
        try:
            status = await rpc_call(socket_path, "service.status", {})
        except Exception:
            await asyncio.sleep(0.05)
            continue
        if int(status.get("desktopPid") or 0) == expected_desktop_pid:
            return (time.perf_counter() - started_at) * 1000.0
        await asyncio.sleep(0.05)
    raise TimeoutError(f"timed out waiting for supervisor boot at {socket_path}")


@dataclass
class SupervisorProcess:
    process: subprocess.Popen[bytes]
    stdout_handle: Any
    stderr_handle: Any
    stdout_path: Path
    stderr_path: Path


def spawn_supervisor(source_tree: Path, sandbox: Sandbox) -> SupervisorProcess:
    env = dict(os.environ)
    stdout_path = sandbox.root / "supervisor.stdout.log"
    stderr_path = sandbox.root / "supervisor.stderr.log"
    stdout_handle = stdout_path.open("wb")
    stderr_handle = stderr_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "src.supervisor"],
        cwd=str(source_tree),
        env=env,
        stdout=stdout_handle,
        stderr=stderr_handle,
    )
    return SupervisorProcess(
        process=process,
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


async def shutdown_supervisor(proc: SupervisorProcess, socket_path: Path) -> None:
    try:
        await rpc_call(socket_path, "supervisor.shutdown", {})
    except Exception:
        pass
    try:
        await asyncio.to_thread(proc.process.wait, 5.0)
    except subprocess.TimeoutExpired:
        proc.process.terminate()
        try:
            await asyncio.to_thread(proc.process.wait, 3.0)
        except subprocess.TimeoutExpired:
            proc.process.kill()
            await asyncio.to_thread(proc.process.wait, 1.0)
    finally:
        proc.stdout_handle.close()
        proc.stderr_handle.close()


def read_log_excerpt(log_path: Path) -> str:
    if not log_path.is_file():
        return ""
    content = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    return "\n".join(content[-20:])


async def run_startup_benchmarks(source_tree: Path, sandbox: Sandbox, subscription_wait_ms: int) -> dict[str, object]:
    socket_path = sandbox.runtime_root / "supervisor" / "tiya.sock"
    proc = spawn_supervisor(source_tree, sandbox)
    try:
        boot_ms = await wait_for_supervisor_boot(socket_path, os.getpid())
        subscription = await collect_subscription_events(socket_path, subscription_wait_ms)
        legacy_bootstrap_ms = await measure_parallel_bootstrap(socket_path)
        return {
            "supervisor_boot_ms": boot_ms,
            "service_subscribe_connect_ms": subscription["connect_ms"],
            "idle_subscription": {
                "wait_ms": subscription_wait_ms,
                "events": subscription["events"],
                "total_events": subscription["total_events"],
            },
            "legacy_window_block_ms_estimate": boot_ms + float(subscription["connect_ms"]),
            "legacy_bootstrap_parallel_rpc_ms": legacy_bootstrap_ms,
        }
    finally:
        await shutdown_supervisor(proc, socket_path)


async def run_runtime_benchmarks(
    source_tree: Path,
    sandbox: Sandbox,
    status_iterations: int,
    diagnostics_iterations: int,
) -> dict[str, object]:
    socket_path = sandbox.runtime_root / "supervisor" / "tiya.sock"
    proc = spawn_supervisor(source_tree, sandbox)
    try:
        await wait_for_supervisor_boot(socket_path, os.getpid())
        diagnostics_first_started = time.perf_counter()
        await rpc_call(socket_path, "diagnostics.report", {})
        diagnostics_first_ms = (time.perf_counter() - diagnostics_first_started) * 1000.0
        status_samples = await measure_rpc_samples(socket_path, "service.status", {}, status_iterations)
        diagnostics_samples = await measure_rpc_samples(socket_path, "diagnostics.report", {}, diagnostics_iterations)
        first_sessions_started = time.perf_counter()
        first_sessions = await rpc_call(
            socket_path,
            "sessions.list",
            {"provider": "codex", "limit": 20},
            timeout_sec=120.0,
        )
        sessions_first_ms = (time.perf_counter() - first_sessions_started) * 1000.0
        second_sessions_started = time.perf_counter()
        await rpc_call(
            socket_path,
            "sessions.list",
            {"provider": "codex", "limit": 20},
            timeout_sec=120.0,
        )
        sessions_second_ms = (time.perf_counter() - second_sessions_started) * 1000.0
        await asyncio.sleep(2.1)
        cooldown_sessions_started = time.perf_counter()
        await rpc_call(
            socket_path,
            "sessions.list",
            {"provider": "codex", "limit": 20},
            timeout_sec=120.0,
        )
        sessions_after_cooldown_ms = (time.perf_counter() - cooldown_sessions_started) * 1000.0
        item_count = len(first_sessions.get("items", [])) if isinstance(first_sessions.get("items"), list) else 0
        return {
            "service_status_rpc_warm": summarize_samples(status_samples),
            "diagnostics_report_rpc_first_ms": diagnostics_first_ms,
            "diagnostics_report_rpc_warm": summarize_samples(diagnostics_samples),
            "sessions_list": {
                "session_count": item_count,
                "first_ms": sessions_first_ms,
                "second_ms": sessions_second_ms,
                "after_cooldown_ms": sessions_after_cooldown_ms,
            },
        }
    finally:
        await shutdown_supervisor(proc, socket_path)


async def benchmark_source_tree(args: argparse.Namespace) -> dict[str, object]:
    source_tree = Path(args.source_tree).resolve()
    insert_source_tree(source_tree)

    from src.envfile import write_env_file
    from src.secret_store import TELEGRAM_TOKEN_SECRET
    from src.supervisor import TiyaSupervisor

    with (
        tempfile.TemporaryDirectory(prefix="tiya-perf-profile-") as profile_tmp,
        tempfile.TemporaryDirectory(prefix="tiya-perf-startup-") as startup_tmp,
        tempfile.TemporaryDirectory(prefix="tiya-perf-runtime-") as runtime_tmp,
    ):
        profile_sandbox = create_sandbox(Path(profile_tmp), args.session_count, write_env_file)
        configure_runtime_env(profile_sandbox)
        seed_secret(TiyaSupervisor, TELEGRAM_TOKEN_SECRET)
        direct_profile = await profile_direct_calls(source_tree, args.profile_top, TiyaSupervisor)

        startup_sandbox = create_sandbox(Path(startup_tmp), args.session_count, write_env_file)
        configure_runtime_env(startup_sandbox)
        seed_secret(TiyaSupervisor, TELEGRAM_TOKEN_SECRET)
        startup_metrics = await run_startup_benchmarks(source_tree, startup_sandbox, args.subscription_wait_ms)

        runtime_sandbox = create_sandbox(Path(runtime_tmp), args.session_count, write_env_file)
        configure_runtime_env(runtime_sandbox)
        seed_secret(TiyaSupervisor, TELEGRAM_TOKEN_SECRET)
        runtime_metrics = await run_runtime_benchmarks(
            source_tree,
            runtime_sandbox,
            args.status_iterations,
            args.diagnostics_iterations,
        )

        return {
            "source_tree": str(source_tree),
            "session_count": int(args.session_count),
            "status_iterations": int(args.status_iterations),
            "diagnostics_iterations": int(args.diagnostics_iterations),
            "direct_profile": direct_profile,
            "startup_metrics": startup_metrics,
            "runtime_metrics": runtime_metrics,
        }


async def async_main() -> int:
    args = parse_args()
    payload = await benchmark_source_tree(args)
    print(json.dumps(round_payload(payload), ensure_ascii=False, indent=args.json_indent))
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
