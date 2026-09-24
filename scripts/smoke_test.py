#!/usr/bin/env python3
"""Bounded real-engine checks. Creates only uniquely named, removable test envs.

Run on a Linux Docker host after installing rosenv. Produces JSON evidence,
including failure details, and removes its own containers/networks afterwards.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def host_ros_fingerprints():
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path("/opt/ros").glob("*/setup.bash"))
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distros", nargs="+", default=["jazzy", "humble"])
    parser.add_argument("--report", type=Path, default=Path("validation.json"))
    args = parser.parse_args()
    if sys.flags.optimize:
        parser.error("Run validation without -O/PYTHONOPTIMIZE; assertions are required.")
    executable = shutil.which("rosenv")
    if not executable:
        parser.error("Install rosenv first")
    run_id = uuid.uuid4().hex[:8]
    root = Path(tempfile.mkdtemp(prefix="rosenv-validation-"))
    environment = {**os.environ, "ROSENV_HOME": str(root / "registry")}
    environment.pop("ROSENV_ENV", None)
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "host": platform.platform(),
        "host_ros_before": host_ros_fingerprints(),
        "checks": [],
        "environments": [],
        "success": False,
    }
    created = []

    def call(argv, *, expected=0, input=None, timeout=600, cwd=root):
        process = subprocess.Popen(
            [executable, *argv],
            env=environment,
            cwd=cwd,
            text=True,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)
            raise
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        report["checks"].append(
            {
                "argv": argv,
                "exit_code": result.returncode,
                "stdout": result.stdout[-5000:],
                "stderr": result.stderr[-3000:],
            }
        )
        if result.returncode != expected:
            raise AssertionError(
                f"{argv}: expected {expected}, got {result.returncode}: {result.stderr[-2000:]}"
            )
        return result.stdout

    try:
        call(["doctor"], timeout=60)
        for distro in args.distros:
            name = f"validation-{distro}-{run_id}"
            workspace = root / distro
            workspace.mkdir()
            created.append(name)
            print(f"Creating {distro} environment...", flush=True)
            call(["create", name, "--ros", distro, "--workspace", str(workspace)], timeout=900)
            state = json.loads(call(["show", "-e", name, "--json"]))
            report["environments"].append(state)
            metadata_code = "import os,json,platform; print(json.dumps(dict(distro=os.getenv('ROS_DISTRO'),domain=os.getenv('ROS_DOMAIN_ID'),uid=os.getuid(),home=os.path.expanduser('~'),python=platform.python_version(),os_release=open('/etc/os-release').read())))"
            metadata = json.loads(call(["run", "-e", name, "--", "python3", "-c", metadata_code]))
            assert metadata["distro"] == distro and metadata["uid"] == os.getuid()
            assert metadata["domain"] == str(state["domain_id"])
            assert metadata["home"] == "/home/rosenv"
            report["environments"][-1]["observed"] = metadata
            literal = "spaces ; $(touch /tmp/rosenv-should-not-exist) ' quotation"
            out = call(
                [
                    "run",
                    "-e",
                    name,
                    "--",
                    "python3",
                    "-c",
                    "import sys; print(sys.argv[1])",
                    literal,
                ]
            )
            assert out.strip() == literal
            assert (
                call(["run", "-e", name, "--", "cat"], input="stdin is preserved\n")
                == "stdin is preserved\n"
            )
            call(["run", "-e", name, "--", "python3", "-c", "raise SystemExit(23)"], expected=23)
            call(
                [
                    "run",
                    "-e",
                    name,
                    "--",
                    "bash",
                    "-c",
                    "printf persistent > /workspace/retained.txt; printf home > ~/retained.txt",
                ]
            )
            assert (workspace / "retained.txt").stat().st_uid == os.getuid()
            call(["local", name], cwd=workspace)
            nested = workspace / "nested"
            nested.mkdir()
            assert call(["run", "--", "printenv", "ROS_DISTRO"], cwd=nested).strip() == distro
            print(f"Installing demo nodes in {distro}...", flush=True)
            call(["add", "-e", name, "demo_nodes_cpp"], timeout=900)
            call(["stop", name])
            assert (
                call(["run", "-e", name, "--", "cat", "/home/rosenv/retained.txt"]).strip()
                == "home"
            )
            call(["run", "-e", name, "--", "ros2", "pkg", "prefix", "demo_nodes_cpp"])
            print(f"Running {distro} talker/listener...", flush=True)
            listener_args = [
                executable,
                "run",
                "-e",
                name,
                "--",
                "timeout",
                "14s",
                "ros2",
                "run",
                "demo_nodes_cpp",
                "listener",
            ]
            listener = subprocess.Popen(
                listener_args,
                env=environment,
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                time.sleep(2)
                call(
                    [
                        "run",
                        "-e",
                        name,
                        "--",
                        "timeout",
                        "8s",
                        "ros2",
                        "run",
                        "demo_nodes_cpp",
                        "talker",
                    ],
                    expected=124,
                    timeout=30,
                )
                heard, _ = listener.communicate(timeout=25)
                report["checks"].append(
                    {
                        "check": f"{distro}-talker-listener",
                        "exit_code": listener.returncode,
                        "stdout": heard[-5000:],
                    }
                )
                assert listener.returncode == 124, (
                    f"Listener exited unexpectedly: {listener.returncode}: {heard}"
                )
                assert "I heard:" in heard, heard
            finally:
                if listener.poll() is None:
                    listener.terminate()
                    listener.communicate(timeout=10)
            # Build a real ament Python package and run it through the resulting overlay.
            print(f"Building and running a {distro} workspace package...", flush=True)
            package = workspace / "src/rosenv_probe"
            (package / "resource").mkdir(parents=True)
            (package / "rosenv_probe").mkdir()
            (package / "resource/rosenv_probe").touch()
            (package / "rosenv_probe/__init__.py").touch()
            (package / "rosenv_probe/node.py").write_text(
                "import os\nimport rclpy\nfrom rclpy.node import Node\n"
                "def main():\n"
                "    rclpy.init()\n"
                "    node = Node('rosenv_build_probe')\n"
                "    print('ROSENV_BUILD_OK:' + os.environ['ROS_DISTRO'])\n"
                "    node.destroy_node()\n"
                "    rclpy.shutdown()\n"
            )
            (package / "setup.py").write_text(
                "from setuptools import setup\n"
                "setup(name='rosenv_probe', version='0.0.1', packages=['rosenv_probe'], "
                "data_files=[('share/ament_index/resource_index/packages', ['resource/rosenv_probe']), "
                "('share/rosenv_probe', ['package.xml'])], "
                "entry_points={'console_scripts': ['probe = rosenv_probe.node:main']})\n"
            )
            (package / "setup.cfg").write_text(
                "[develop]\nscript_dir=$base/lib/rosenv_probe\n"
                "[install]\ninstall_scripts=$base/lib/rosenv_probe\n"
            )
            (package / "package.xml").write_text(
                '<?xml version="1.0"?><package format="3"><name>rosenv_probe</name>'
                "<version>0.0.1</version><description>Integration probe</description>"
                '<maintainer email="test@example.com">ROSenv</maintainer><license>MIT</license>'
                "<buildtool_depend>ament_python</buildtool_depend><exec_depend>rclpy</exec_depend>"
                "<export><build_type>ament_python</build_type></export></package>\n"
            )
            call(["run", "-e", name, "--", "colcon", "build", "--symlink-install"], timeout=180)
            output = call(["run", "-e", name, "--", "ros2", "run", "rosenv_probe", "probe"])
            assert f"ROSENV_BUILD_OK:{distro}" in output
            assert (workspace / "install/setup.bash").stat().st_uid == os.getuid()
        records = report["environments"]
        assert len({record["domain_id"] for record in records}) == len(records)
        networks = []
        for state in records:
            result = subprocess.run(
                ["docker", "container", "inspect", state["container"]],
                text=True,
                capture_output=True,
                check=True,
                timeout=60,
            )
            info = json.loads(result.stdout)[0]
            networks.append(set(info["NetworkSettings"]["Networks"]))
            assert set(info["NetworkSettings"]["Networks"]) == {state["container"] + "-net"}
            assert not info["HostConfig"]["Privileged"]
            assert {"no-new-privileges", "no-new-privileges:true"}.intersection(
                info["HostConfig"]["SecurityOpt"]
            )
            assert {mount["Destination"] for mount in info["Mounts"]} == {
                "/workspace",
                "/home/rosenv",
            }
            assert info["Config"]["User"] == f"{os.getuid()}:{os.getgid()}"
        assert sum(map(len, networks)) == len(set().union(*networks))
        report["checks"].append(
            {"check": "private-networks-and-nonroot-mount-policy", "passed": True}
        )
        report["success"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(report["error"], flush=True)
    finally:
        report["cleanup_errors"] = []
        for name in created:
            if (root / "registry/envs" / name / "environment.json").exists():
                try:
                    state = json.loads(call(["show", "-e", name, "--json"]))
                    call(["remove", name, "--yes"])
                    assert (Path(state["workspace"]) / "retained.txt").exists() or not report[
                        "success"
                    ]
                    result = subprocess.run(
                        ["docker", "container", "inspect", state["container"]],
                        text=True,
                        capture_output=True,
                        timeout=60,
                    )
                    assert result.returncode != 0 and "No such container" in result.stderr
                    result = subprocess.run(
                        ["docker", "network", "inspect", state["container"] + "-net"],
                        text=True,
                        capture_output=True,
                        timeout=60,
                    )
                    assert result.returncode != 0 and "not found" in result.stderr
                except Exception as exc:
                    report["cleanup_errors"].append(str(exc))
                    report["success"] = False
        report["host_ros_after"] = host_ros_fingerprints()
        if report["host_ros_before"] != report["host_ros_after"]:
            report["success"] = False
            report["error"] = "Host ROS setup files changed during validation"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        if not report["cleanup_errors"]:
            shutil.rmtree(root)
        print(
            f"{'PASS' if report['success'] else 'FAIL'}: evidence saved to {args.report}",
            flush=True,
        )
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
