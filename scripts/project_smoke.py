#!/usr/bin/env python3
"""Exercise actual project locks, sync, replacement, reconstruction, tasks and colcon."""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import tty
import uuid
from datetime import datetime, timezone
from pathlib import Path

import tomlkit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distro", default="jazzy")
    parser.add_argument("--report", type=Path, default=Path("project-validation.json"))
    args = parser.parse_args()
    if sys.flags.optimize:
        parser.error("Validation requires assertions; unset PYTHONOPTIMIZE.")
    executable = shutil.which("rosenv")
    if not executable:
        parser.error("Install rosenv first.")
    root = Path(tempfile.mkdtemp(prefix="rosenv-project-validation-"))
    workspace = root / "workspace"
    workspace.mkdir()
    environment = {**os.environ, "ROSENV_HOME": str(root / "registry")}
    environment.pop("ROSENV_ENV", None)
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "distro": args.distro,
        "success": False,
        "checks": [],
        "cleanup_errors": [],
    }
    state = None
    images = set()
    if sys.platform == "darwin":
        # Docker cannot map macOS device nodes; exercise the serial relay with a pseudo-terminal.
        sensor, port = os.openpty()
        tty.setraw(sensor)
        probe = os.ttyname(port)
        os.close(port)
    else:
        sensor, probe = None, "/dev/null"

    def call(argv, *, expected=0, cwd=workspace, input=None, timeout=1200):
        print("rosenv " + " ".join(argv[:5]), flush=True)
        process = subprocess.Popen(
            [executable, *argv],
            env=environment,
            cwd=cwd,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)
            raise
        report["checks"].append(
            {
                "argv": argv,
                "exit_code": process.returncode,
                "stdout": stdout[-5000:],
                "stderr": stderr[-5000:],
            }
        )
        if process.returncode != expected:
            raise AssertionError(
                f"{argv}: expected {expected}, got {process.returncode}: {stderr[-4000:]}"
            )
        return stdout

    try:
        call(["init", "probe-" + uuid.uuid4().hex[:8], "--ros", args.distro])
        call(["add", "demo_nodes_cpp", "apt:figlet", "--no-sync"])
        call(["add", "apt:python3-pytest", "--group", "dev", "--no-sync"])
        manifest = workspace / "rosenv.toml"
        document = tomlkit.parse(manifest.read_text())
        document["tasks"] = {
            "hello": ["ros2", "run", "hello_rosenv", "hello"],
            "check": ["python3", "-c", "print('task-ok')"],
        }
        manifest.write_text(tomlkit.dumps(document))
        call(["lock"])
        lockpath = workspace / "rosenv.lock"
        lockbytes = lockpath.read_bytes()
        lock = json.loads(lockbytes)
        images.add(lock["image_id"])
        assert len(lock["packages"]) > 100
        assert "ros-" + args.distro + "-demo-nodes-cpp" in lock["requested"]
        assert lock["groups"] == ["dev"]
        call(["lock", "--check"])
        call(["sync", "--locked"])
        state = json.loads(call(["show", "--json"]))
        old_container = state["container"]
        call(["run", "--locked", "--", "ros2", "pkg", "prefix", "demo_nodes_cpp"])
        assert call(["run", "--locked", "--", "cat"], input="pipe-ok\n") == "pipe-ok\n"
        call(
            [
                "run",
                "--locked",
                "--",
                "bash",
                "-c",
                "printf retained > ~/keep; printf workspace > /workspace/keep",
            ]
        )
        nested = workspace / "nested"
        nested.mkdir()
        assert call(["run", "--locked", "--", "pwd"], cwd=nested).strip() == "/workspace/nested"
        assert call(["task", "--locked", "check"]).strip() == "task-ok"
        tree = json.loads(call(["tree", "--locked", "--json"]))
        assert any(p["name"] == "figlet" for p in tree)
        exported = json.loads(call(["export", "--locked", "--format", "json"]))
        assert exported == lock
        call(["export", "--locked", "--format", "dockerfile", "-o", str(root / "Dockerfile")])
        assert "FROM docker.io/library/ros@sha256:" in (root / "Dockerfile").read_text()
        # Runtime edits must not re-resolve packages or rebuild dependency images.
        document = tomlkit.parse(manifest.read_text())
        document["environment"]["domain-id"] = 117
        document["environment"]["variables"] = {"ROSENV_PROBE": "from-toml"}
        manifest.write_text(tomlkit.dumps(document))
        assert call(["run", "--locked", "--", "printenv", "ROS_DOMAIN_ID"]).strip() == "117"
        assert call(["run", "--locked", "--", "printenv", "ROSENV_PROBE"]).strip() == "from-toml"
        assert (
            call(
                [
                    "run",
                    "--locked",
                    "--set-env",
                    "ROSENV_PROBE=override",
                    "--",
                    "printenv",
                    "ROSENV_PROBE",
                ]
            ).strip()
            == "override"
        )
        assert json.loads(call(["show", "--json"]))["container"] == old_container
        assert lockpath.read_bytes() == lockbytes
        document["environment"]["network"] = "none"
        document["environment"]["devices"] = [f"{probe}:/dev/rosenv-probe:rw"]
        manifest.write_text(tomlkit.dumps(document))
        assert "network changed" in call(["sync", "--locked", "--dry-run"])
        assert json.loads(call(["show", "--json"]))["container"] == old_container
        call(["sync", "--locked"])
        state = json.loads(call(["show", "--json"]))
        assert state["container"] != old_container
        assert state["image_id"] == lock["image_id"]
        assert lockpath.read_bytes() == lockbytes
        call(
            [
                "run",
                "--locked",
                "--",
                "python3",
                "-c",
                "import os; f=os.open('/dev/rosenv-probe', os.O_RDWR); os.close(f)",
            ]
        )
        if sensor is not None:
            received = []

            def answer():
                data = b""
                while len(data) < 4:
                    data += os.read(sensor, 4 - len(data))
                received.append(data)
                os.write(sensor, b"pong")

            responder = threading.Thread(target=answer, daemon=True)
            responder.start()
            script = (
                "import os, tty; f = os.open('/dev/rosenv-probe', os.O_RDWR | os.O_NOCTTY); "
                "tty.setraw(f); os.write(f, b'ping'); print(os.read(f, 4).decode())"
            )
            reply = call(["run", "--locked", "--", "python3", "-c", script], timeout=60)
            responder.join(10)
            assert received == [b"ping"] and reply.strip() == "pong"
        assert call(["run", "--locked", "--", "cat", "/home/rosenv/keep"]) == "retained"
        # Alternate manifests keep their own receipts/locks and reuse the same cached image.
        alternate = workspace / "rosenv.robot.toml"
        alternate.write_text(manifest.read_text())
        alternate.with_suffix(".lock").write_bytes(lockbytes)
        call(["--config", alternate.name, "sync", "--locked"])
        other = json.loads(call(["--config", alternate.name, "show", "--json"]))
        assert other["container"] != state["container"]
        assert other["home"] != state["home"]
        assert other["image_id"] == state["image_id"]
        call(["env", "remove", other["name"], "--yes"])
        alternate.unlink()
        alternate.with_suffix(".lock").unlink()
        old_container = state["container"]
        call(["remove", "apt:figlet", "--no-sync"])
        call(["sync", "--locked"], expected=1)
        assert json.loads(call(["show", "--json"]))["container"] == old_container
        call(["run", "--frozen", "--", "figlet", "frozen"])
        assert lockpath.read_bytes() == lockbytes
        call(["sync"])
        state = json.loads(call(["show", "--json"]))
        assert state["container"] != old_container
        assert call(["run", "--locked", "--", "cat", "/home/rosenv/keep"]) == "retained"
        assert (workspace / "keep").read_text() == "workspace"
        call(["run", "--locked", "--", "dpkg-query", "-W", "figlet"], expected=1)
        lock = json.loads(lockpath.read_text())
        images.add(lock["image_id"])
        assert not any(p["name"] == "figlet" for p in lock["packages"])
        example = Path(__file__).resolve().parents[1] / "examples/hello-ros/src"
        shutil.copytree(example, workspace / "src", dirs_exist_ok=True)
        call(["build", "--locked"])
        call(["test", "--locked"])
        call(["task", "--locked", "hello"])
        call(["env", "stop", state["name"]])
        call(["run", "--locked", "--", "printenv", "ROS_DISTRO"])
        # A fresh registry must be able to rebuild exact packages from the portable lock.
        call(["env", "remove", state["name"], "--yes"])
        state = None
        subprocess.run(
            ["docker", "image", "rm", "--no-prune", lock["image_id"]],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        before = lockpath.read_bytes()
        call(["sync", "--locked"])
        assert lockpath.read_bytes() == before
        state = json.loads(call(["show", "--json"]))
        images.add(state["image_id"])
        call(["task", "--locked", "hello"])
        report["lock"] = lock
        report["success"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(report["error"], flush=True)
    finally:
        # Read this test's private registry so a failure mid-sync is still cleaned up.
        try:
            records = json.loads(call(["env", "list", "--json"], cwd=root))
            for record in records:
                call(["env", "remove", record["name"], "--yes"], cwd=root)
            assert not list((root / "registry/envs").glob("*/environment.json"))
        except Exception as exc:
            report["cleanup_errors"].append(str(exc))
            report["success"] = False
        for image in images:
            # Never force removal; Docker protects any image used by another environment.
            subprocess.run(
                ["docker", "image", "rm", "--no-prune", image],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        if not report["cleanup_errors"]:
            shutil.rmtree(root)
        if sensor is not None:
            os.close(sensor)
        print(f"{'PASS' if report['success'] else 'FAIL'}: {args.report}", flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
