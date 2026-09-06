from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from types import FrameType
from typing import BinaryIO

from openfoundry.canonical import sha256_digest

_child: subprocess.Popen[bytes] | None = None
_stop_reason: str | None = None


def _signal_child(kind: signal.Signals) -> None:
    child = _child
    if child is None or child.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(child.pid, kind)


def _force_kill() -> None:
    _signal_child(signal.SIGKILL)


def _handle_stop(signum: int, _frame: FrameType | None) -> None:
    global _stop_reason
    _stop_reason = f"signal:{signal.Signals(signum).name}"
    _signal_child(signal.SIGTERM)
    timer = threading.Timer(5.0, _force_kill)
    timer.daemon = True
    timer.start()


def _write_completion(
    path: Path, *, exit_code: int, reason: str, evidence: dict[str, object] | None = None
) -> None:
    value = {
        "exitCode": exit_code,
        "reason": reason,
        "finished": time.time(),
        "evidence": evidence or {},
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
    os.replace(temporary, path)


def _write_log_tail(source: BinaryIO, path: Path, limit: int) -> None:
    tail = bytearray()
    while block := source.read(65536):
        tail.extend(block)
        if len(tail) > limit:
            del tail[: len(tail) - limit]
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(tail)
    os.replace(temporary, path)


def _precheck(args: argparse.Namespace, evidence: dict[str, object]) -> str | None:
    if evidence["argvDigest"] != args.argv_digest:
        return "argv-digest-mismatch"
    observed = []
    for path_value, expected in args.attested_executable:
        path = Path(path_value)
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        observed.append({"path": str(path), "digest": actual})
        if actual != expected:
            evidence["executables"] = observed
            return "executable-digest-mismatch"
    evidence["executables"] = observed
    return None


def _wait_child(child: subprocess.Popen[bytes], timeout: float | None) -> tuple[int, str]:
    try:
        exit_code = child.wait(timeout=timeout)
        return exit_code, _stop_reason or ("completed" if exit_code == 0 else "nonzero-exit")
    except subprocess.TimeoutExpired:
        _signal_child(signal.SIGTERM)
        try:
            return child.wait(timeout=5.0), "timeout"
        except subprocess.TimeoutExpired:
            _signal_child(signal.SIGKILL)
            return child.wait(), "timeout"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--completion", required=True, type=Path)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--attested-executable", nargs=2, action="append", default=[])
    parser.add_argument("--environment-digest")
    parser.add_argument("--argv-digest", required=True)
    parser.add_argument("--stdout-log", required=True, type=Path)
    parser.add_argument("--stderr-log", required=True, type=Path)
    parser.add_argument("--max-log-bytes", required=True, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command[:1] == ["--"]:
        command.pop(0)
    if not command:
        parser.error("a command is required after --")
    if args.max_log_bytes < 1:
        parser.error("--max-log-bytes must be positive")
    evidence: dict[str, object] = {
        "environmentDigest": args.environment_digest,
        "argvDigest": sha256_digest(command),
        "executables": [],
    }
    failure = _precheck(args, evidence)
    if failure is not None:
        _write_completion(args.completion, exit_code=1, reason=failure, evidence=evidence)
        return 1
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    if _stop_reason:
        _write_completion(args.completion, exit_code=1, reason=_stop_reason, evidence=evidence)
        return 1
    global _child
    _child = subprocess.Popen(
        command,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if _stop_reason:
        _signal_child(signal.SIGTERM)
    assert _child.stdout is not None
    assert _child.stderr is not None
    readers = [
        threading.Thread(
            target=_write_log_tail,
            args=(_child.stdout, args.stdout_log, args.max_log_bytes),
        ),
        threading.Thread(
            target=_write_log_tail,
            args=(_child.stderr, args.stderr_log, args.max_log_bytes),
        ),
    ]
    for reader in readers:
        reader.start()
    exit_code, reason = _wait_child(_child, args.timeout)
    for reader in readers:
        reader.join()
    _write_completion(args.completion, exit_code=exit_code, reason=reason, evidence=evidence)
    return exit_code if 0 <= exit_code <= 255 else 1


if __name__ == "__main__":
    raise SystemExit(main())
