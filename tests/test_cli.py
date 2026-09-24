"""CLI orchestration tests with a fake engine and real temporary registry."""

from __future__ import annotations

import contextlib
import fcntl
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rosenv.backend import BackendError, DockerBackend
from rosenv.cli import create, dispatch, main, packages_for, parser
from rosenv.state import StateError, Store


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.store = Store(self.base / "registry")
        self.backend = Mock(spec=DockerBackend)
        self.backend.resolve_image.return_value = {
            "id": "sha256:" + "a" * 64,
            "digests": [],
            "engine_id": "test-engine",
        }
        self.linux = patch("rosenv.cli.platform.system", return_value="Linux")
        self.linux.start()
        self.addCleanup(self.linux.stop)

    def create_args(self, name: str = "demo", *extra: str):
        return parser().parse_args(["create", name, "--workspace", str(self.workspace), *extra])

    def test_successful_create_pins_image_and_defaults_to_private_bridge(self) -> None:
        state = create(self.create_args(), self.store, self.backend)
        self.assertEqual(state["image_id"], self.backend.resolve_image.return_value["id"])
        self.assertEqual(state["network"], "bridge")
        self.assertEqual(state["engine_id"], "test-engine")
        self.assertEqual(self.store.load("demo"), state)
        self.backend.create.assert_called_once_with(state)
        self.backend.remove.assert_not_called()

    def test_failed_create_cleans_state_if_engine_cleanup_succeeds(self) -> None:
        self.backend.create.side_effect = BackendError("create failed")
        with self.assertRaisesRegex(BackendError, "create failed"):
            create(self.create_args(), self.store, self.backend)
        self.backend.remove.assert_called_once()
        self.assertFalse(self.store.env_dir("demo").exists())
        self.assertTrue(self.workspace.exists())

    def test_failed_create_keeps_recoverable_state_if_cleanup_fails(self) -> None:
        self.backend.create.side_effect = BackendError("original create failure")
        self.backend.remove.side_effect = BackendError("cleanup failure")
        with self.assertRaisesRegex(BackendError, "original create failure"):
            create(self.create_args(), self.store, self.backend)
        state = self.store.load("demo")
        self.assertEqual(state, self.backend.remove.call_args.args[0])
        self.assertTrue(Path(state["home"]).is_dir())

    def test_duplicate_create_does_not_contact_engine(self) -> None:
        create(self.create_args(), self.store, self.backend)
        self.backend.reset_mock()
        with self.assertRaises(StateError):
            create(self.create_args(), self.store, self.backend)
        self.assertEqual(self.backend.mock_calls, [])

    def test_invalid_domain_fails_before_pull_or_filesystem_mutation(self) -> None:
        for domain in ("-1", "233"):
            with self.subTest(domain=domain):
                with self.assertRaises(StateError):
                    create(
                        self.create_args("demo", "--domain-id", domain), self.store, self.backend
                    )
        self.assertEqual(self.backend.mock_calls, [])
        self.assertFalse(self.store.env_dir("demo").exists())

    def test_automatic_domains_are_distinct(self) -> None:
        first = create(self.create_args("first"), self.store, self.backend)
        second = create(self.create_args("second"), self.store, self.backend)
        self.assertNotEqual(first["domain_id"], second["domain_id"])

    def test_run_releases_registry_lock_and_forwards_exit_and_arguments(self) -> None:
        state = create(self.create_args(), self.store, self.backend)
        command = ["python3", "-c", "print('a; b')", "argument with spaces", "$(whoami)"]

        def run(loaded, arguments, interactive=False):
            self.assertEqual(loaded, state)
            self.assertEqual(arguments, command)
            self.assertFalse(interactive)
            with (self.store.root / ".lock").open("a") as handle:
                # This would raise BlockingIOError if dispatch retained its lock.
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
            return 37

        self.backend.run.side_effect = run
        args = parser().parse_args(["run", "-e", "demo", "--", *command])
        self.assertEqual(dispatch(args, self.store, self.backend), 37)

    def test_empty_run_fails_without_starting_container(self) -> None:
        with self.assertRaises(StateError):
            dispatch(parser().parse_args(["run", "-e", "demo", "--"]), self.store, self.backend)
        self.backend.ensure_running.assert_not_called()

    def test_remove_preserves_workspace_and_retires_registry_after_success(self) -> None:
        state = create(self.create_args(), self.store, self.backend)
        sentinel = self.workspace / "source.txt"
        sentinel.write_text("user source\n")
        self.store.set_global("demo")
        with contextlib.redirect_stdout(io.StringIO()):
            dispatch(parser().parse_args(["remove", "demo", "--yes"]), self.store, self.backend)
        self.backend.remove.assert_called_once_with(state)
        self.assertFalse(self.store.env_dir("demo").exists())
        self.assertFalse((self.store.root / "default").exists())
        self.assertEqual(sentinel.read_text(), "user source\n")

    def test_failed_remove_preserves_registry_for_retry(self) -> None:
        state = create(self.create_args(), self.store, self.backend)
        self.backend.remove.side_effect = BackendError("engine unavailable")
        with self.assertRaises(BackendError):
            dispatch(parser().parse_args(["remove", "demo", "--yes"]), self.store, self.backend)
        self.assertEqual(self.store.load("demo"), state)

    def test_package_names_convert_to_distro_specific_debian_names(self) -> None:
        self.assertEqual(
            packages_for(
                "jazzy",
                ["demo_nodes_cpp", "apt:build-essential", "ros-jazzy-rclpy", "apt:libc6:amd64"],
            ),
            ["ros-jazzy-demo-nodes-cpp", "build-essential", "ros-jazzy-rclpy", "libc6:amd64"],
        )

    def test_package_shell_and_option_injection_is_rejected(self) -> None:
        for package in (
            "apt:--allow-unauthenticated",
            "apt:",
            "pkg;id",
            "$(id)",
            "pkg\nother",
            "../pkg",
        ):
            with self.subTest(package=package):
                with self.assertRaises(StateError):
                    packages_for("jazzy", [package])

    def test_successful_add_records_packages_but_failed_add_does_not(self) -> None:
        create(self.create_args(), self.store, self.backend)
        args = parser().parse_args(["add", "-e", "demo", "demo_nodes_cpp"])
        self.backend.add.return_value = 100
        self.assertEqual(dispatch(args, self.store, self.backend), 100)
        self.assertNotIn("requested_packages", self.store.load("demo"))
        self.backend.add.return_value = 0
        self.assertEqual(dispatch(args, self.store, self.backend), 0)
        self.assertEqual(
            self.store.load("demo")["requested_packages"], ["ros-jazzy-demo-nodes-cpp"]
        )

    def test_main_converts_termination_signal_to_shell_exit_status(self) -> None:
        with patch("rosenv.cli.dispatch", return_value=-15):
            self.assertEqual(main(["run", "-e", "demo", "--", "sleep", "10"]), 143)

    def test_shell_uses_backends_setup_aware_interactive_shell(self) -> None:
        state = create(self.create_args(), self.store, self.backend)
        self.backend.run.return_value = 0
        args = parser().parse_args(["shell", "-e", "demo"])
        self.assertEqual(dispatch(args, self.store, self.backend), 0)
        self.backend.run.assert_called_once_with(state, [], interactive=True)

    def test_add_merges_packages_recorded_between_lock_acquisitions(self) -> None:
        create(self.create_args(), self.store, self.backend)
        original_lock = self.store.lock
        entries = 0

        @contextlib.contextmanager
        def interleaved_lock():
            nonlocal entries
            with original_lock():
                entries += 1
                if entries == 2:
                    # Simulate another add completing after selection but before
                    # this command obtains its package-transaction lock.
                    latest = self.store.load("demo")
                    latest["requested_packages"] = ["ros-jazzy-std-msgs"]
                    self.store.save(latest)
                yield

        self.backend.add.return_value = 0
        args = parser().parse_args(["add", "-e", "demo", "demo_nodes_cpp"])
        with patch.object(self.store, "lock", side_effect=interleaved_lock):
            self.assertEqual(dispatch(args, self.store, self.backend), 0)
        self.assertEqual(
            self.store.load("demo")["requested_packages"],
            ["ros-jazzy-demo-nodes-cpp", "ros-jazzy-std-msgs"],
        )

    def test_create_rejects_workspaces_inside_managed_storage(self) -> None:
        nested = self.store.root / "nested" / "source"
        nested.mkdir(parents=True)
        linked = self.base / "workspace-link"
        linked.symlink_to(nested, target_is_directory=True)
        for workspace in (self.store.root, nested, linked):
            with self.subTest(workspace=workspace):
                args = self.create_args()
                args.workspace = workspace
                with self.assertRaises(StateError):
                    create(args, self.store, self.backend)
        self.assertEqual(self.backend.mock_calls, [])
        self.assertFalse(self.store.env_dir("demo").exists())

    def test_remove_refuses_to_delete_another_environments_workspace(self) -> None:
        create(self.create_args("first"), self.store, self.backend)
        other = create(self.create_args("second"), self.store, self.backend)
        embedded_workspace = self.store.env_dir("first") / "home" / "project"
        embedded_workspace.mkdir()
        sentinel = embedded_workspace / "source.txt"
        sentinel.write_text("user source\n")
        # Simulate a legacy record made before creation prohibited this layout.
        other["workspace"] = str(embedded_workspace)
        self.store.save(other)
        self.backend.reset_mock()
        args = parser().parse_args(["remove", "first", "--yes"])
        with self.assertRaises(StateError):
            dispatch(args, self.store, self.backend)
        self.backend.remove.assert_not_called()
        self.assertEqual(sentinel.read_text(), "user source\n")
        self.assertEqual(self.store.load("second"), other)
        self.assertEqual(self.store.load("first")["name"], "first")


if __name__ == "__main__":
    unittest.main()
