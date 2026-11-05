"""Relay host serial devices into containers when Docker cannot map host device nodes.

Docker on macOS runs containers inside a Linux virtual machine, so a host
/dev/cu.* node cannot be passed with --device. A detached host process holds the
serial port open and exchanges framed bytes with a helper inside the container,
which presents a pseudo-terminal at the declared container path. Baud-rate
changes made inside the container are applied to the host port, so a driver
configures the device as it would on Linux.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DATA, SPEED, READY = b"D", b"S", b"R"
_HEADER = struct.Struct(">cI")
_IOSSIOSPEED = 0x80085402  # macOS _IOW('T', 2, speed_t): set an arbitrary baud rate.
_START_TIMEOUT = 20
_STOP_TIMEOUT = 5

# Runs as root inside the container with argv: target uid gid. The helper keeps
# its own slave descriptor open, so drivers can close and reopen the device, and
# reads the slave's termios to report the baud rate the driver selected.
CONTAINER_SCRIPT = r"""
import os, select, struct, sys, termios, tty
target, uid, gid = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
header = struct.Struct(">cI")
speeds = {
    getattr(termios, name): int(name[1:])
    for name in dir(termios)
    if name[:1] == "B" and name[1:].isdigit()
}
if os.path.lexists(target) and not os.path.islink(target):
    sys.exit(f"rosenv relay: {target} already exists in the container.")
master, slave = os.openpty()
tty.setraw(slave)
os.fchown(slave, uid, gid)
os.fchmod(slave, 0o660)
os.makedirs(os.path.dirname(target), exist_ok=True)
link = target + ".rosenv-relay"
if os.path.lexists(link):
    os.unlink(link)
os.symlink(os.ttyname(slave), link)
os.replace(link, target)
os.set_blocking(master, False)


def send(kind, payload=b""):
    data = header.pack(kind, len(payload)) + payload
    while data:
        data = data[os.write(1, data):]


send(b"R")
speed, pending = None, b""
try:
    while True:
        current = speeds.get(termios.tcgetattr(slave)[5])
        if current and current != speed:
            speed = current
            send(b"S", struct.pack(">I", speed))
        readable = select.select([0, master], [], [], 0.05)[0]
        if master in readable:
            try:
                data = os.read(master, 65536)
            except BlockingIOError:
                data = b""
            if data:
                send(b"D", data)
        if 0 in readable:
            chunk = os.read(0, 65536)
            if not chunk:
                break
            pending += chunk
            while len(pending) >= header.size:
                kind, length = header.unpack_from(pending)
                if len(pending) < header.size + length:
                    break
                payload = pending[header.size : header.size + length]
                pending = pending[header.size + length :]
                if kind == b"D":
                    try:
                        os.write(master, payload)
                    except BlockingIOError:
                        pass  # Nobody is reading the device; drop input as an idle UART would.
finally:
    if os.path.islink(target) and os.readlink(target) == os.ttyname(slave):
        os.unlink(target)
"""


class RelayError(Exception):
    """A host device could not be relayed into its container."""


def _frame(kind: bytes, payload: bytes = b"") -> bytes:
    return _HEADER.pack(kind, len(payload)) + payload


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _frames(pending: bytes) -> tuple[list[tuple[bytes, bytes]], bytes]:
    frames = []
    while len(pending) >= _HEADER.size:
        kind, length = _HEADER.unpack_from(pending)
        end = _HEADER.size + length
        if len(pending) < end:
            break
        frames.append((kind, pending[_HEADER.size : end]))
        pending = pending[end:]
    return frames, pending


def open_device(path: str, permissions: str) -> int:
    """Open a host serial port exclusively in raw mode."""
    if "r" in permissions and "w" in permissions:
        flags = os.O_RDWR
    elif "r" in permissions:
        flags = os.O_RDONLY
    else:
        flags = os.O_WRONLY
    try:
        # Non-blocking open avoids waiting for carrier detect on dial-in nodes.
        fd = os.open(path, flags | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as exc:
        raise RelayError(f"Cannot open {path}: {exc.strerror}.") from None
    try:
        if not os.isatty(fd):
            raise RelayError(
                f"{path} is not a serial terminal; this host can relay only serial devices."
            )
        try:
            fcntl.ioctl(fd, termios.TIOCEXCL)
        except OSError as exc:
            raise RelayError(f"Cannot open {path} exclusively: {exc.strerror}.") from None
        tty.setraw(fd, termios.TCSANOW)
        attributes = termios.tcgetattr(fd)
        attributes[2] |= termios.CLOCAL | termios.CREAD
        termios.tcsetattr(fd, termios.TCSANOW, attributes)
        os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def set_speed(fd: int, speed: int) -> None:
    constant = getattr(termios, f"B{speed}", None)
    if constant is not None:
        attributes = termios.tcgetattr(fd)
        attributes[4] = attributes[5] = constant
        termios.tcsetattr(fd, termios.TCSANOW, attributes)
    elif sys.platform == "darwin":
        # Rates such as the 4 Mbaud used by some LiDARs have no termios constant.
        fcntl.ioctl(fd, _IOSSIOSPEED, struct.pack("L", speed))
    else:
        raise RelayError(f"Unsupported baud rate {speed}.")


def serve(config: dict[str, Any], ready_fd: int) -> int:
    """Pump bytes between the host device and the container helper until either ends."""
    lock = os.open(config["lock"], os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    permissions = config["permissions"]
    device = open_device(config["path"], permissions)
    helper = subprocess.Popen(
        [
            config["docker"],
            "container",
            "exec",
            "--interactive",
            "--user",
            "0:0",
            config["container"],
            "python3",
            "-c",
            CONTAINER_SCRIPT,
            config["target"],
            str(config["uid"]),
            str(config["gid"]),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    output, sink = helper.stdout.fileno(), helper.stdin.fileno()
    sources = [output, device] if "r" in permissions else [output]
    pending = b""
    try:
        while True:
            for source in select.select(sources, [], [])[0]:
                if source == device:
                    data = os.read(device, 65536)
                    if not data:
                        raise RelayError(f"{config['path']} was disconnected.")
                    _write_all(sink, _frame(DATA, data))
                    continue
                chunk = os.read(output, 65536)
                if not chunk:
                    return helper.wait()
                frames, pending = _frames(pending + chunk)
                for kind, payload in frames:
                    if kind == READY:
                        Path(config["record"]).write_text(
                            json.dumps(
                                {
                                    "pid": os.getpid(),
                                    "container": config["container"],
                                    "path": config["path"],
                                }
                            )
                        )
                        os.write(ready_fd, b"1")
                        os.close(ready_fd)
                    elif kind == SPEED:
                        (speed,) = struct.unpack(">I", payload)
                        try:
                            set_speed(device, speed)
                        except (OSError, RelayError) as exc:
                            print(f"rosenv relay: cannot set {speed} baud: {exc}", file=sys.stderr)
                    elif kind == DATA and "w" in permissions:
                        _write_all(device, payload)
    finally:
        # End of input lets the helper remove its device path. Stopping the
        # docker client alone would leave the helper running in the container.
        helper.stdin.close()
        try:
            helper.wait(timeout=_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            helper.kill()
        # Exclusive mode outlives this descriptor while another process holds
        # the port open, and would keep the next relay from opening it.
        try:
            fcntl.ioctl(device, termios.TIOCNXCL)
        except OSError:
            pass
        os.close(device)


def _files(state: dict[str, Any], device: dict[str, Any]) -> dict[str, Path]:
    directory = Path(state["home"]).parent / "relays"
    key = hashlib.sha256(device["target"].encode()).hexdigest()[:16]
    return {
        "lock": directory / f"{key}.lock",
        "record": directory / f"{key}.json",
        "log": directory / f"{key}.log",
    }


def _running(files: dict[str, Path]) -> dict[str, Any] | None:
    """Return the live relay's record; its process holds the lock for its lifetime."""
    if not files["lock"].exists():
        return None
    with files["lock"].open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                return json.loads(files["record"].read_text())
            except (OSError, ValueError):
                return {}
        return None


def _terminate(files: dict[str, Path]) -> None:
    record = _running(files)
    if record is None:
        return
    if isinstance(record.get("pid"), int):
        try:
            os.kill(record["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + _STOP_TIMEOUT
    while _running(files) is not None:
        if time.monotonic() > deadline:
            raise RelayError(f"Relay process {record.get('pid')} did not stop.")
        time.sleep(0.05)


@contextmanager
def _spawn_lock(state: dict[str, Any]):
    directory = Path(state["home"]).parent / "relays"
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / ".spawn").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _start(state, device, files, container: str, docker: str) -> None:
    read_fd, write_fd = os.pipe()
    config = {
        "docker": docker,
        "container": container,
        "path": device["path"],
        "target": device["target"],
        "permissions": device["permissions"],
        "uid": state["uid"],
        "gid": state["gid"],
        "lock": str(files["lock"]),
        "record": str(files["record"]),
        "ready_fd": write_fd,
    }
    with files["log"].open("wb") as log:
        # -P keeps a "rosenv" directory in the caller's working tree off sys.path.
        process = subprocess.Popen(
            [sys.executable, "-P", "-m", "rosenv.relay", json.dumps(config)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            pass_fds=(write_fd,),
            start_new_session=True,
        )
    os.close(write_fd)
    try:
        ready = select.select([read_fd], [], [], _START_TIMEOUT)[0] and os.read(read_fd, 1)
    finally:
        os.close(read_fd)
    if not ready:
        if process.poll() is None:
            process.terminate()
        detail = files["log"].read_text(errors="replace").strip().splitlines()
        raise RelayError(
            f"Cannot relay {device['source']} to {device['target']}: "
            + (detail[-1] if detail else "the relay did not start.")
        )


def ensure(state: dict[str, Any], container: str, docker: str) -> None:
    """Start a relay for each mapped device unless one already serves this container."""
    with _spawn_lock(state):
        for device in state.get("devices", []):
            files = _files(state, device)
            record = _running(files)
            if record is not None:
                if record.get("container") == container and record.get("path") == device["path"]:
                    continue
                _terminate(files)
            _start(state, device, files, container, docker)


def stop(state: dict[str, Any]) -> None:
    """Stop this environment's relays; containers lose their device paths."""
    if not (Path(state["home"]).parent / "relays").is_dir():
        return
    with _spawn_lock(state):
        for device in state.get("devices", []):
            _terminate(_files(state, device))


def main(argv: list[str]) -> int:
    config = json.loads(argv[0])
    try:
        return serve(config, config["ready_fd"])
    except BlockingIOError:
        print("rosenv relay: another relay already serves this device.", file=sys.stderr)
    except (OSError, RelayError) as exc:
        print(f"rosenv relay: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
