"""Safety and error-boundary tests that do not require a Docker daemon."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rosenv.backend import BackendError, DockerBackend


class BackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "workspace").mkdir()
        (root / "home").mkdir()
        self.state = {
            "name": "test-env",
            "token": "unique-owner-token",
            "container": "rosenv-1000-test-env",
            "ros_distro": "jazzy",
            "image": "ros:jazzy-ros-base",
            "image_id": "sha256:" + "a" * 64,
            "engine_id": "test-engine",
            "workspace": str(root / "workspace"),
            "home": str(root / "home"),
            "uid": 1000,
            "gid": 1000,
            "network": "bridge",
            "domain_id": 42,
        }
        self.backend = DockerBackend(binary="docker")
        self.backend._engine_id = "test-engine"

    def container(self, **labels: str) -> dict:
        ownership = {
            "io.rosenv.uid": "1000",
            "io.rosenv.token": self.state["token"],
        }
        ownership.update(labels)
        return {
            "Id": "container-id",
            "Name": "/" + self.state["container"],
            "Config": {"Labels": ownership},
            "State": {"Running": True},
        }

    @staticmethod
    def result(
        payload: object = "", code: int = 0, stderr: str = ""
    ) -> subprocess.CompletedProcess:
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(["docker"], code, stdout, stderr)

    def test_inspect_accepts_matching_owner_and_token(self) -> None:
        record = self.container()
        with patch.object(self.backend, "_docker", return_value=self.result([record])):
            self.assertEqual(self.backend.inspect(self.state), record)

    def test_inspect_rejects_foreign_token_even_when_uid_matches(self) -> None:
        record = self.container(**{"io.rosenv.token": "some-other-environment"})
        with patch.object(self.backend, "_docker", return_value=self.result([record])):
            with self.assertRaises(BackendError):
                self.backend.inspect(self.state)

    def test_inspect_rejects_foreign_uid_even_when_token_matches(self) -> None:
        record = self.container(**{"io.rosenv.uid": "2000"})
        with patch.object(self.backend, "_docker", return_value=self.result([record])):
            with self.assertRaises(BackendError):
                self.backend.inspect(self.state)

    def test_inspect_rejects_unlabelled_container(self) -> None:
        record = self.container()
        record["Config"]["Labels"] = None
        with patch.object(self.backend, "_docker", return_value=self.result([record])):
            with self.assertRaises(BackendError):
                self.backend.inspect(self.state)

    def test_foreign_container_is_never_stopped_or_removed(self) -> None:
        record = self.container(**{"io.rosenv.token": "foreign"})
        for action in ("stop", "remove"):
            with self.subTest(action=action):
                with patch.object(
                    self.backend, "_docker", return_value=self.result([record])
                ) as docker:
                    with self.assertRaises(BackendError):
                        getattr(self.backend, action)(self.state)
                    for call in docker.call_args_list:
                        self.assertFalse({"stop", "rm", "remove"}.intersection(call.args[0]))

    def test_missing_container_is_reported_as_absent(self) -> None:
        result = self.result(
            code=1, stderr="Error response from daemon: No such container: rosenv-1000-test-env"
        )
        with patch("rosenv.backend.subprocess.run", return_value=result):
            self.assertIsNone(self.backend.inspect(self.state))

    def test_unavailable_daemon_is_not_reported_as_absent(self) -> None:
        result = self.result(
            code=1, stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
        )
        with patch("rosenv.backend.subprocess.run", return_value=result):
            with self.assertRaises(BackendError):
                self.backend.inspect(self.state)

    def test_invalid_inspect_json_is_a_controlled_backend_error(self) -> None:
        with patch("rosenv.backend.subprocess.run", return_value=self.result("{not valid json")):
            with self.assertRaises(BackendError):
                self.backend.inspect(self.state)

    def test_missing_docker_executable_is_a_controlled_backend_error(self) -> None:
        with patch("rosenv.backend.subprocess.run", side_effect=FileNotFoundError("docker")):
            with self.assertRaises(BackendError):
                self.backend.inspect(self.state)

    def test_package_injection_is_rejected_before_engine_execution(self) -> None:
        for package in (
            "--allow-unauthenticated",
            "pkg;touch /tmp/pwned",
            "$(id)",
            "pkg\nother",
            "../pkg",
            "",
        ):
            with self.subTest(package=package):
                with patch.object(self.backend, "_docker") as docker:
                    with self.assertRaises((BackendError, ValueError)):
                        self.backend.add(self.state, [package])
                    docker.assert_not_called()

    def test_control_commands_use_argv_without_a_shell(self) -> None:
        argument = "literal;$(touch /tmp/rosenv-test-must-not-exist)"
        with patch("rosenv.backend.subprocess.run", return_value=self.result()) as run:
            self.backend._docker(["image", "inspect", argument])
        positional, keyword = run.call_args
        self.assertIsInstance(positional[0], list)
        self.assertIn(argument, positional[0])
        self.assertFalse(keyword.get("shell", False))

    def test_create_uses_pinned_image_user_and_private_bridge(self) -> None:
        record = self.container()
        with patch.object(self.backend, "inspect", side_effect=[None, record, record]):
            with patch.object(self.backend, "_network", return_value=None):
                with patch.object(self.backend, "_docker", return_value=self.result()) as docker:
                    self.backend.create(self.state)
        commands = [call.args[0] for call in docker.call_args_list]
        network = next(command for command in commands if command[:2] == ["network", "create"])
        container = next(command for command in commands if command[:2] == ["container", "run"])
        self.assertEqual(
            container[container.index("--network") + 1], self.state["container"] + "-net"
        )
        self.assertEqual(network[-1], self.state["container"] + "-net")
        self.assertEqual(container[container.index("--user") + 1], "1000:1000")
        self.assertIn(self.state["image_id"], container)
        self.assertNotIn(self.state["image"], container)
        self.assertNotIn("--privileged", container)
        for command in (network, container):
            self.assertIn("io.rosenv.token=" + self.state["token"], command)
            self.assertIn("io.rosenv.uid=1000", command)

    def test_partial_docker_run_failure_removes_only_verified_owned_resources(self) -> None:
        record = self.container()
        network = {
            "Id": "owned-network-id",
            "Labels": {"io.rosenv.uid": "1000", "io.rosenv.token": self.state["token"]},
        }

        def docker(args, **kwargs):
            if args[:2] == ["container", "run"]:
                raise BackendError("container startup failed after creation")
            return self.result()

        with patch.object(self.backend, "inspect", side_effect=[None, record]):
            with patch.object(self.backend, "_network", side_effect=[None, network]):
                with patch.object(self.backend, "_docker", side_effect=docker) as calls:
                    with self.assertRaisesRegex(BackendError, "container startup failed"):
                        self.backend.create(self.state)
        commands = [call.args[0] for call in calls.call_args_list]
        self.assertIn(["container", "rm", "--force", record["Id"]], commands)
        self.assertIn(["network", "rm", network["Id"]], commands)

    def test_foreign_network_is_never_deleted_even_if_container_is_absent(self) -> None:
        network = {
            "Id": "foreign-network-id",
            "Labels": {"io.rosenv.uid": "1000", "io.rosenv.token": "foreign"},
        }
        with patch.object(self.backend, "inspect", return_value=None):
            with patch.object(
                self.backend, "_docker", return_value=self.result([network])
            ) as docker:
                with self.assertRaises(BackendError):
                    self.backend.remove(self.state)
        self.assertTrue(all("rm" not in call.args[0] for call in docker.call_args_list))

    def test_command_arguments_and_exit_code_survive_streaming_exec(self) -> None:
        command = ["python3", "-c", "print('literal;$(id)')", "argument with spaces", "--", ""]
        with patch.object(self.backend, "_required", return_value=self.container()):
            with patch("rosenv.backend.sys.stdin.isatty", return_value=False):
                with patch("rosenv.backend.sys.stdout.isatty", return_value=False):
                    with patch(
                        "rosenv.backend.subprocess.run", return_value=self.result(code=37)
                    ) as run:
                        self.assertEqual(self.backend.run(self.state, command), 37)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-len(command) :], command)
        self.assertIn("--interactive", argv)  # Docker must forward piped stdin too.
        self.assertNotIn("--tty", argv)
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertNotIn("capture_output", run.call_args.kwargs)
        self.assertNotIn("stdin", run.call_args.kwargs)

    def test_streaming_signal_and_keyboard_interrupt_have_shell_exit_codes(self) -> None:
        with patch("rosenv.backend.subprocess.run", return_value=self.result(code=-15)):
            self.assertEqual(self.backend._stream(["container", "exec"]), 143)
        with patch("rosenv.backend.subprocess.run", side_effect=KeyboardInterrupt):
            self.assertEqual(self.backend._stream(["container", "exec"]), 130)

    def test_add_stops_after_failed_index_update(self) -> None:
        with patch.object(self.backend, "_required", return_value=self.container()):
            with patch.object(self.backend, "_stream", return_value=100) as stream:
                self.assertEqual(self.backend.add(self.state, ["ros-jazzy-demo-nodes-cpp"]), 100)
        stream.assert_called_once()
        self.assertEqual(stream.call_args.args[0][-2:], ["apt-get", "update"])

    def test_invalid_resource_names_are_rejected_before_docker(self) -> None:
        for name in ("--help", "../escape", "evil;id", "name with spaces"):
            with self.subTest(name=name):
                state = {**self.state, "container": name}
                with patch.object(self.backend, "_docker") as docker:
                    with self.assertRaises(BackendError):
                        self.backend.inspect(state)
                    docker.assert_not_called()

    def test_control_timeout_is_reported_as_backend_error(self) -> None:
        error = subprocess.TimeoutExpired(["docker", "inspect"], 60)
        with patch("rosenv.backend.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(BackendError, "timed out"):
                self.backend.inspect(self.state)

    def test_different_daemon_is_rejected_before_stop_or_remove(self) -> None:
        state = {**self.state, "engine_id": "the-original-daemon"}
        for action in ("stop", "remove"):
            with self.subTest(action=action):
                with patch.object(self.backend, "_docker") as docker:
                    with self.assertRaisesRegex(BackendError, "different Docker daemon"):
                        getattr(self.backend, action)(state)
                docker.assert_not_called()

    def test_engine_identity_is_cached_for_a_single_command_invocation(self) -> None:
        backend = DockerBackend()
        with patch.object(backend, "_docker", return_value=self.result("test-engine\n")) as docker:
            self.assertEqual(backend.engine_id(), "test-engine")
            self.assertEqual(backend.engine_id(), "test-engine")
        docker.assert_called_once()

    def test_missing_engine_identity_is_rejected(self) -> None:
        backend = DockerBackend()
        with patch.object(backend, "_docker", return_value=self.result("  \n")):
            with self.assertRaises(BackendError):
                backend.engine_id()

    def test_add_refreshes_and_upgrades_before_installing_requested_packages(self) -> None:
        packages = ["ros-lyrical-demo-nodes-cpp", "build-essential"]
        state = {**self.state, "ros_distro": "lyrical"}
        with patch.object(self.backend, "_required", return_value=self.container()):
            with patch.object(self.backend, "_stream", return_value=0) as stream:
                self.assertEqual(self.backend.add(state, packages), 0)
        commands = [call.args[0] for call in stream.call_args_list]
        apt_commands = [command[command.index("apt-get") :] for command in commands]
        self.assertEqual([command[1] for command in apt_commands], ["update", "upgrade", "install"])
        self.assertIn("--with-new-pkgs", apt_commands[1])
        self.assertEqual(apt_commands[-1][-len(packages) - 1 :], ["--", *packages])
        # Every apt operation remains inside the verified container.
        self.assertTrue(all(command[:2] == ["container", "exec"] for command in commands))
        self.assertTrue(all("container-id" in command for command in commands))

    def test_add_upgrade_failure_prevents_install_and_preserves_failure_status(self) -> None:
        with patch.object(self.backend, "_required", return_value=self.container()):
            with patch.object(self.backend, "_stream", side_effect=[0, 100]) as stream:
                self.assertEqual(self.backend.add(self.state, ["ros-jazzy-demo-nodes-cpp"]), 100)
        commands = [call.args[0] for call in stream.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertEqual(
            [command[command.index("apt-get") + 1] for command in commands], ["update", "upgrade"]
        )
        self.assertTrue(all("install" not in command for command in commands))


if __name__ == "__main__":
    unittest.main()
