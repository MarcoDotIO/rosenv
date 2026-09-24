#!/usr/bin/env python3
"""Check terminal Ctrl-C delivery and absence of orphaned exec processes.

Run against an existing Linux environment, optionally with demo_nodes_cpp:
  python scripts/tty_probe.py --env jazzy-dev --ros-demo --report tty-report.json

Only this script's uniquely tagged processes are eligible for failure cleanup.
This verifies a terminal Ctrl-C byte, not programmatic/non-TTY signal forwarding.
"""

import argparse
import errno
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PYTHON_PROBE = """import os, signal, sys
def interrupted(signum, frame):
    print('ROSENV_SIGINT_RECEIVED', flush=True)
    raise SystemExit(130)
signal.signal(signal.SIGINT, interrupted)
signal.alarm(45)
print('ROSENV_PROBE_READY', os.getpid(), sys.argv[1], flush=True)
while True:
    signal.pause()
"""

PROCESS_SCAN = """import json, os, pathlib, signal, sys
token = sys.argv[1].encode()
action = sys.argv[2]
found = []
for directory in pathlib.Path('/proc').iterdir():
    if not directory.name.isdigit() or int(directory.name) == os.getpid():
        continue
    try:
        argv = (directory / 'cmdline').read_bytes().split(b'\\0')
        if token not in argv and b'__node:=' + token not in argv:
            continue
        pid = int(directory.name)
        if action == 'kill':
            os.kill(pid, signal.SIGKILL)
        found.append({'pid': pid, 'argv': [part.decode(errors='replace') for part in argv if part]})
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
print(json.dumps(found))
"""


def scan(executable, environment, token, action="scan"):
    result = subprocess.run(
        [executable, "run", "-e", environment, "--", "python3", "-c", PROCESS_SCAN, token, action],
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )
    return json.loads(result.stdout)


def probe(executable, environment, kind):
    token = "rosenv_probe_" + uuid.uuid4().hex
    if kind == "python":
        command = ["python3", "-u", "-c", PYTHON_PROBE, token]
        ready = b"ROSENV_PROBE_READY"
    else:
        command = [
            "ros2",
            "run",
            "demo_nodes_cpp",
            "talker",
            "--ros-args",
            "-r",
            "__node:=" + token,
        ]
        ready = b"Publishing:"
    argv = [executable, "run", "-e", environment, "--", *command]
    result = {"kind": kind, "token": token, "success": False, "cleanup_errors": []}
    pid, terminal = pty.fork()
    if pid == 0:
        try:
            os.execv(executable, argv)
        except BaseException:
            os._exit(127)
    output = bytearray()
    status = None
    terminal_open = True

    def pump(deadline):
        nonlocal status, terminal_open
        while time.monotonic() < deadline:
            if status is None:
                waited, child_status = os.waitpid(pid, os.WNOHANG)
                if waited:
                    status = child_status
            if terminal_open:
                available, _, _ = select.select([terminal], [], [], 0.1)
                if available:
                    try:
                        chunk = os.read(terminal, 65536)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        chunk = b""
                    if chunk:
                        output.extend(chunk)
                    else:
                        terminal_open = False
            elif status is None:
                time.sleep(0.05)
            if ready in output or status is not None:
                return

    try:
        pump(time.monotonic() + 25)
        if ready not in output or status is not None:
            raise RuntimeError("Command did not become ready in the PTY before exit/deadline")
        os.write(terminal, b"\x03")
        result["ctrl_c_sent"] = True
        ready = b"THIS_MARKER_IS_NEVER_PRINTED"
        pump(time.monotonic() + 10)
        if status is None:
            raise RuntimeError("Command did not exit within 10 seconds of terminal Ctrl-C")
        exit_code = os.waitstatus_to_exitcode(status)
        result["exit_code"] = exit_code
        if kind == "python":
            if exit_code != 130 or b"ROSENV_SIGINT_RECEIVED" not in output:
                raise RuntimeError("Python command did not receive SIGINT and return 130")
        elif exit_code not in (0, 130):
            raise RuntimeError(f"ROS command returned unexpected exit code {exit_code}")
        # Allow ordinary child reaping to complete, then inspect the container's
        # actual process table rather than inferring cleanup from CLI termination.
        deadline = time.monotonic() + 3
        while True:
            survivors = scan(executable, environment, token)
            if not survivors or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        result["survivors_after_exit"] = survivors
        if survivors:
            raise RuntimeError("Tagged processes survived terminal command exit")
        result["success"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if status is None:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(terminal)
        try:
            remaining = scan(executable, environment, token)
            result["cleanup_required"] = bool(remaining)
            if remaining:
                result["cleanup_targets"] = scan(executable, environment, token, "kill")
                after_cleanup = scan(executable, environment, token)
                if after_cleanup:
                    raise RuntimeError(f"Tagged processes remain after cleanup: {after_cleanup}")
        except Exception as exc:
            result["cleanup_errors"].append(str(exc))
            result["success"] = False
        result["output"] = output.decode(errors="replace")[-6000:]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    parser.add_argument("--ros-demo", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("tty-report.json"))
    args = parser.parse_args()
    executable = shutil.which("rosenv")
    if not executable:
        parser.error("rosenv must be installed and on PATH")
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "environment": args.env,
        "scope": "Actual PTY Ctrl-C byte; no claim about non-TTY or programmatic signals",
        "checks": [probe(executable, args.env, "python")],
    }
    if args.ros_demo:
        report["checks"].append(probe(executable, args.env, "ros-demo"))
    report["success"] = all(check["success"] for check in report["checks"])
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{'PASS' if report['success'] else 'FAIL'}: {args.report}")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
