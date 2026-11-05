"""Runtime configuration validation and literal command environment forwarding."""

from pathlib import Path
from unittest.mock import patch

import pytest

from rosenv.backend import BackendError, DockerBackend
from rosenv.project import ProjectError, initialize, load
from rosenv.runtime import device_spec, parse_variables, resolve_devices


def test_variables_and_devices_round_trip_preserve_comments(tmp_path):
    path = tmp_path / "rosenv.toml"
    path.write_text("""[project]
name = "robot"
ros = "jazzy"
[environment]
network = "bridge" # keep networking comment
# keep device comment
devices = ["/dev/null:/dev/probe:rw"]
[environment.variables]
LABEL = "before" # keep variable comment
""")
    project = load(tmp_path)
    before = project.fingerprint()
    project.variables["LABEL"] = "literal $(id) = value"
    project.variables["EMPTY"] = ""
    project.domain_id = 42
    project.network = "host"
    project.save()
    loaded = load(tmp_path)
    assert loaded.variables == project.variables
    assert loaded.devices == project.devices
    assert loaded.fingerprint() == before
    assert "# keep variable comment" in path.read_text()
    assert "# keep device comment" in path.read_text()
    assert "# keep networking comment" in path.read_text()
    loaded.variables.pop("LABEL")
    loaded.devices = []
    loaded.save()
    assert load(tmp_path).variables == {"EMPTY": ""}
    assert load(tmp_path).devices == []


@pytest.mark.parametrize(
    "variables",
    [
        {"bad-key": "x"},
        {"HOME": "/tmp"},
        {"ROS_DISTRO": "humble"},
        {"X": 1},
        {"X": "nul\0"},
        {"ROS_DOMAIN_ID": "233"},
        {"ROS_DOMAIN_ID": "-1"},
        {"ROS_DOMAIN_ID": "nope"},
        ["X=1"],
    ],
)
def test_invalid_manifest_variables_fail_offline(tmp_path, variables):
    project = initialize(tmp_path, "robot")
    original = project.path.read_bytes()
    project.variables = variables
    with pytest.raises(ProjectError):
        project.save()
    assert project.path.read_bytes() == original


def test_assignment_values_are_literal_and_last_override_wins():
    assert parse_variables(["X=one", "X=a=b $(id)", "EMPTY="]) == {"X": "a=b $(id)", "EMPTY": ""}
    with pytest.raises(ValueError, match="NAME=VALUE"):
        parse_variables(["X"])


@pytest.mark.parametrize(
    "value",
    [
        "/dev",
        "/dev/",
        "/dev/../etc/passwd",
        "ttyACM0",
        "/dev//ttyACM0",
        "/dev/ttyACM0:/tmp/foo",
        "/dev/ttyACM0:/dev/foo:rx",
        "/dev/ttyACM0:/dev/foo:rr",
        "/dev/ttyACM0:/dev/foo:",
        "/dev/ttyACM0:/dev/foo:rw:extra",
        "/dev/x\0",
        123,
    ],
)
def test_device_mapping_rejects_invalid_paths_and_permissions(value):
    with pytest.raises(ValueError):
        device_spec(value)


def test_device_resolution_requires_actual_device_and_unique_targets(tmp_path):
    resolved = resolve_devices(["/dev/null:/dev/probe:wr"])
    assert resolved[0]["permissions"] == "rw"
    assert resolved[0]["target"] == "/dev/probe"
    assert resolved[0]["path"] == str(Path("/dev/null").resolve())
    assert resolved[0]["gid"] == Path("/dev/null").stat().st_gid
    with pytest.raises(ValueError, match="Duplicate"):
        resolve_devices(["/dev/null:/dev/probe", "/dev/zero:/dev/probe"])
    with pytest.raises(ValueError, match="character or block"):
        resolve_devices(["/dev/fd"])


def test_run_forwards_variables_as_argv_with_current_domain():
    backend = DockerBackend()
    state = {"uid": 1000, "gid": 1000, "ros_distro": "jazzy", "domain_id": 42}
    with (
        patch.object(backend, "ensure_running"),
        patch.object(backend, "_required", return_value={"Id": "owned"}),
        patch.object(backend, "_stream", return_value=0) as stream,
    ):
        backend.run(state, ["printenv"], variables={"ROS_DOMAIN_ID": "51", "X": "$(id); a=b"})
    args = stream.call_args.args[0]
    assert "ROS_DOMAIN_ID=51" in args
    assert "ROS_DOMAIN_ID=42" not in args
    assert "X=$(id); a=b" in args
    assert args.index("X=$(id); a=b") < args.index("owned")


def test_invalid_run_variables_fail_before_docker():
    backend = DockerBackend()
    with patch.object(backend, "ensure_running") as ensure:
        with pytest.raises(BackendError, match="managed"):
            backend.run({}, ["pwd"], variables={"HOME": "/root"})
    ensure.assert_not_called()
