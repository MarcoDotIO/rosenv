"""Serial relays for hosts whose Docker engine cannot map host device nodes."""

import os
import select
import sys
import termios
import time
import tty
from pathlib import Path

import pytest

from rosenv import relay
from rosenv.runtime import resolve_devices


def read_exactly(fd, size, timeout=10):
    data = b""
    deadline = time.monotonic() + timeout
    while len(data) < size and select.select([fd], [], [], deadline - time.monotonic())[0]:
        data += os.read(fd, size - len(data))
    return data


@pytest.fixture
def environment(tmp_path):
    # The fake engine runs the container helper directly on this host.
    docker = tmp_path / "docker"
    docker.write_text(
        f'#!/bin/sh\nwhile [ "$1" != python3 ]; do shift; done\nshift\nexec {sys.executable} "$@"\n'
    )
    docker.chmod(0o755)
    sensor, port = os.openpty()
    tty.setraw(sensor)
    target = tmp_path / "container-dev" / "ttyACM0"
    (device,) = resolve_devices([os.ttyname(port)])
    device["target"] = str(target)
    (tmp_path / "env" / "home").mkdir(parents=True)
    state = {
        "home": str(tmp_path / "env" / "home"),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "devices": [device],
    }
    yield state, str(docker), sensor, port, target
    relay.stop(state)
    os.close(sensor)
    os.close(port)


def test_relay_carries_bytes_and_baud_rate_between_host_port_and_container_path(environment):
    state, docker, sensor, port, target = environment
    relay.ensure(state, "container-a", docker)
    assert target.is_symlink()
    driver = os.open(target, os.O_RDWR | os.O_NOCTTY)
    try:
        tty.setraw(driver)
        os.write(sensor, b"\x55\xaa point cloud")
        assert read_exactly(driver, 15) == b"\x55\xaa point cloud"
        os.write(driver, b"start")
        assert read_exactly(sensor, 5) == b"start"
        attributes = termios.tcgetattr(driver)
        attributes[4] = attributes[5] = termios.B115200
        termios.tcsetattr(driver, termios.TCSANOW, attributes)
        deadline = time.monotonic() + 10
        while termios.tcgetattr(port)[5] != termios.B115200 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert termios.tcgetattr(port)[5] == termios.B115200
    finally:
        os.close(driver)


def test_relay_is_reused_for_same_container_and_replaced_for_a_new_one(environment):
    state, docker, _, _, target = environment
    files = relay._files(state, state["devices"][0])
    relay.ensure(state, "container-a", docker)
    first = relay._running(files)["pid"]
    relay.ensure(state, "container-a", docker)
    assert relay._running(files)["pid"] == first
    relay.ensure(state, "container-b", docker)
    replaced = relay._running(files)
    assert replaced["pid"] != first and replaced["container"] == "container-b"
    relay.stop(state)
    assert relay._running(files) is None
    assert not target.exists() and not target.is_symlink()


def test_relay_failure_reports_cause(environment):
    state, docker, _, _, target = environment
    target.parent.mkdir()
    target.write_text("an existing container file")
    with pytest.raises(relay.RelayError, match="already exists in the container"):
        relay.ensure(state, "container-a", docker)
    assert target.read_text() == "an existing container file"


def test_non_terminal_devices_cannot_be_relayed():
    with pytest.raises(relay.RelayError, match="not a serial terminal"):
        relay.open_device("/dev/null", "rw")


def test_frames_wait_for_complete_payloads():
    data = relay._frame(relay.DATA, b"abc") + relay._frame(relay.SPEED, b"\0\0\0\1")
    frames, pending = relay._frames(data[:-2])
    assert frames == [(relay.DATA, b"abc")]
    frames, pending = relay._frames(pending + data[-2:])
    assert frames == [(relay.SPEED, b"\0\0\0\1")] and pending == b""


def test_stop_without_relays_is_a_no_op(tmp_path):
    relay.stop({"home": str(tmp_path / "home"), "devices": []})
    assert not Path(tmp_path / "relays").exists()
