"""Project CLI behavior, offline edits, selection precedence, and argv safety."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rosenv.backend import DockerBackend
from rosenv.cli import create, main, parser
from rosenv.project import initialize, load
from rosenv.state import Store
from rosenv.workflow import environment_name


class ProjectCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "robot"
        self.workspace.mkdir()
        self.store = Store(self.root / "registry")
        self.backend = Mock(spec=DockerBackend)
        self.backend.resolve_image.return_value = {
            "id": "sha256:" + "a" * 64,
            "digests": [],
            "engine_id": "test-engine",
        }
        self.backend.run.return_value = 0
        for replacement in (
            patch.dict(os.environ, {"ROSENV_HOME": str(self.store.root)}),
            patch("rosenv.cli.DockerBackend", return_value=self.backend),
            patch("rosenv.cli.platform.system", return_value="Linux"),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        os.environ.pop("ROSENV_ENV", None)
        original_cwd = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, original_cwd)

    def invoke(self, *arguments):
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return main(list(arguments))

    def project(self):
        return initialize(self.workspace, "robot")

    def environment(self, name="legacy", project=None):
        state = create(
            parser().parse_args(["create", name, "--workspace", str(self.workspace)]),
            self.store,
            self.backend,
        )
        if project:
            state.update(project_root=str(project.root), ready=True)
            self.store.save(state)
        self.backend.reset_mock()
        return state

    def project_environment(self, project):
        return self.environment(environment_name(project), project)

    def test_init_add_group_remove_and_pin_can_run_entirely_offline(self):
        (self.workspace / ".gitignore").write_text("# Existing ignores\ncustom/\n")
        self.assertEqual(self.invoke("init", "robot"), 0)
        self.assertTrue((self.workspace / "src").is_dir())
        self.assertFalse((self.workspace / "rosenv.lock").exists())
        self.assertFalse((self.workspace / ".rosenv-version").exists())
        self.assertEqual(self.invoke("add", "rclpy", "apt:git=1:2.43.0-1", "--no-sync"), 0)
        self.assertEqual(self.invoke("add", "demo_nodes_cpp", "--group", "dev", "--no-sync"), 0)
        self.assertEqual(load(self.workspace).groups, {"dev": ["demo_nodes_cpp"]})
        self.assertEqual(self.invoke("remove", "rclpy", "--no-sync"), 0)
        self.assertEqual(self.invoke("pin", "humble"), 0)
        project = load(self.workspace)
        self.assertEqual(project.dependencies, ["apt:git=1:2.43.0-1"])
        self.assertEqual(project.distro, "humble")
        self.assertEqual(self.backend.mock_calls, [])
        ignore = (self.workspace / ".gitignore").read_text()
        self.assertTrue(ignore.startswith("# Existing ignores\ncustom/\n"))
        for line in ("build/", "install/", "log/", ".rosenv.edit.lock"):
            self.assertEqual(ignore.splitlines().count(line), 1)

    def test_invalid_offline_edit_preserves_manifest_and_reports_error(self):
        project = self.project()
        original = project.path.read_bytes()
        self.assertEqual(self.invoke("add", "apt:git;touch /tmp/injected", "--no-sync"), 1)
        self.assertIn("Invalid", self.stderr.getvalue())
        self.assertEqual(project.path.read_bytes(), original)
        self.assertEqual(self.backend.mock_calls, [])

    def test_project_overrides_local_and_global_legacy_selection(self):
        project = self.project()
        selected = self.project_environment(project)
        self.environment()
        self.store.set_local("legacy", self.workspace)
        self.store.set_global("legacy")
        self.assertEqual(self.invoke("run", "--no-sync", "--", "ros2", "--help"), 0)
        self.backend.run.assert_called_once_with(
            selected, ["ros2", "--help"], interactive=False, workdir="/workspace"
        )

    def test_explicit_env_overrides_project_and_environment_variable(self):
        project = self.project()
        self.project_environment(project)
        legacy = self.environment()
        os.environ["ROSENV_ENV"] = "nonexistent"
        self.assertEqual(self.invoke("run", "-e", "legacy", "--", "ros2", "--help"), 0)
        self.backend.run.assert_called_once_with(legacy, ["ros2", "--help"], interactive=False)
        self.backend.ensure_running.assert_called_once_with(legacy)

    def test_environment_variable_selects_legacy_and_rejects_project_commands(self):
        self.project()
        legacy = self.environment()
        os.environ["ROSENV_ENV"] = "legacy"
        self.assertEqual(self.invoke("run", "--", "pwd"), 0)
        self.backend.run.assert_called_once_with(legacy, ["pwd"], interactive=False)
        self.backend.reset_mock()
        self.assertEqual(self.invoke("lock", "--check"), 1)
        self.assertIn("unset ROSENV_ENV", self.stderr.getvalue())
        self.assertEqual(self.backend.mock_calls, [])

    def test_env_show_uses_legacy_selection_even_inside_project(self):
        project = self.project()
        self.project_environment(project)
        legacy = self.environment()
        self.store.set_local("legacy", self.workspace)
        self.assertEqual(self.invoke("env", "show", "--json"), 0)
        self.assertEqual(json.loads(self.stdout.getvalue()), legacy)

    def test_run_preserves_arguments_nested_workdir_and_exit_code(self):
        project = self.project()
        state = self.project_environment(project)
        nested = self.workspace / "src" / "package with spaces"
        nested.mkdir(parents=True)
        os.chdir(nested)
        self.backend.run.return_value = 37
        command = ["printf", "%s", "argument with spaces", "$(whoami); echo hi", "--frozen"]
        self.assertEqual(self.invoke("run", "--no-sync", "--", *command), 37)
        self.backend.run.assert_called_once_with(
            state, command, interactive=False, workdir="/workspace/src/package with spaces"
        )

    def test_run_from_symlink_uses_resolved_project_relative_workdir(self):
        project = self.project()
        state = self.project_environment(project)
        nested = self.workspace / "src"
        nested.mkdir()
        linked = self.root / "workspace-link"
        linked.symlink_to(nested, target_is_directory=True)
        self.assertEqual(self.invoke("-C", str(linked), "run", "--no-sync", "--", "pwd"), 0)
        self.backend.run.assert_called_once_with(
            state, ["pwd"], interactive=False, workdir="/workspace/src"
        )
        self.assertEqual(Path.cwd(), self.workspace)

    def test_task_listing_is_offline_and_task_arguments_are_literal_argv(self):
        project = self.project()
        project.tasks = {"greet": ["printf", "%s", "$(whoami); hi"]}
        project.save()
        self.assertEqual(self.invoke("task"), 0)
        self.assertIn("greet:", self.stdout.getvalue())
        self.assertEqual(self.backend.mock_calls, [])
        state = self.project_environment(project)
        self.assertEqual(self.invoke("task", "--no-sync", "greet", "--", "hello world"), 0)
        self.backend.run.assert_called_once_with(
            state,
            ["printf", "%s", "$(whoami); hi", "hello world"],
            interactive=False,
            workdir="/workspace",
        )
        self.assertEqual(load(self.workspace).tasks, project.tasks)

    def test_unknown_task_and_empty_run_fail_before_syncing(self):
        self.project()
        with patch("rosenv.commands.sync_project") as sync:
            self.assertEqual(self.invoke("task", "missing"), 1)
            self.assertIn("Unknown task", self.stderr.getvalue())
            self.assertEqual(self.invoke("run", "--"), 1)
            self.assertIn("Provide a command", self.stderr.getvalue())
        sync.assert_not_called()
        self.assertEqual(self.backend.mock_calls, [])

    def test_no_sync_refuses_incomplete_environment_before_execution(self):
        project = self.project()
        state = self.project_environment(project)
        state["ready"] = False
        self.store.save(state)
        self.assertEqual(self.invoke("run", "--no-sync", "--", "pwd"), 1)
        self.assertIn("sync", self.stderr.getvalue())
        self.backend.run.assert_not_called()

    def test_no_sync_refuses_unfinished_retirement_before_execution(self):
        project = self.project()
        state = self.project_environment(project)
        state["retired"] = dict(state)
        self.store.save(state)
        self.assertEqual(self.invoke("shell", "--no-sync"), 1)
        self.assertIn("sync", self.stderr.getvalue())
        self.backend.run.assert_not_called()

    def test_project_run_forwards_lock_group_selection_to_sync(self):
        project = self.project()
        state = self.project_environment(project)
        with patch("rosenv.commands.sync_project", return_value=state) as sync:
            self.assertEqual(
                self.invoke("run", "--locked", "--group", "vision", "--no-dev", "--", "pwd"),
                0,
            )
        self.assertEqual(
            sync.call_args.kwargs,
            {"groups": ["vision"], "no_dev": True, "locked": True, "frozen": False},
        )

    def test_build_forwards_colcon_arguments_and_adds_symlink_install_once(self):
        project = self.project()
        state = self.project_environment(project)
        for extra in ([], ["--symlink-install"]):
            with self.subTest(extra=extra):
                self.backend.reset_mock()
                self.assertEqual(
                    self.invoke("build", "--no-sync", "--", "--packages-select", "robot", *extra),
                    0,
                )
                self.backend.run.assert_called_once_with(
                    state, ["colcon", "build", "--packages-select", "robot", "--symlink-install"]
                )

    def test_test_command_reports_test_result_failures_and_stops_on_test_failure(self):
        project = self.project()
        state = self.project_environment(project)
        self.backend.run.side_effect = [0, 2]
        self.assertEqual(self.invoke("test", "--no-sync", "--", "--packages-select", "robot"), 2)
        self.assertEqual(
            self.backend.run.call_args_list[0].args,
            (state, ["colcon", "test", "--packages-select", "robot"]),
        )
        self.assertEqual(
            self.backend.run.call_args_list[1].args, (state, ["colcon", "test-result", "--verbose"])
        )
        self.backend.reset_mock()
        self.backend.run.side_effect = [7]
        self.assertEqual(self.invoke("test", "--no-sync"), 7)
        self.backend.run.assert_called_once_with(state, ["colcon", "test"])

    def test_directory_option_applies_before_default_init_workspace(self):
        destination = self.root / "other project"
        destination.mkdir()
        self.assertEqual(self.invoke("-C", str(destination), "init", "other"), 0)
        self.assertTrue((destination / "rosenv.toml").is_file())
        self.assertFalse((self.workspace / "rosenv.toml").exists())
        self.assertEqual(Path.cwd(), self.workspace)
        self.assertEqual(self.backend.mock_calls, [])

    def test_directory_option_resolves_explicit_relative_workspace_and_restores_cwd(self):
        destination = self.root / "other"
        destination.mkdir()
        self.assertEqual(
            self.invoke("-C", str(destination), "init", "nested", "--workspace", "nested"), 0
        )
        self.assertTrue((destination / "nested" / "rosenv.toml").is_file())
        self.assertEqual(Path.cwd(), self.workspace)
        self.assertEqual(self.invoke("-C", str(destination), "task"), 1)
        self.assertEqual(Path.cwd(), self.workspace)

    def test_bad_directory_reports_error_without_changing_cwd_or_engine(self):
        self.assertEqual(self.invoke("-C", str(self.root / "missing"), "distros"), 1)
        self.assertEqual(Path.cwd(), self.workspace)
        self.assertEqual(self.backend.mock_calls, [])

    def test_export_cannot_overwrite_manifest_lock_or_symlink_to_them(self):
        project = self.project()
        lock = project.root / "rosenv.lock"
        lock.write_text("saved lock contents")
        linked_manifest = project.root / "manifest-link"
        linked_manifest.symlink_to(project.path)
        originals = {path: path.read_bytes() for path in (project.path, lock)}
        with patch("rosenv.commands.read_lock", return_value={"packages": []}):
            for target in (project.path, lock, linked_manifest):
                with self.subTest(target=target):
                    self.assertEqual(self.invoke("export", "--frozen", "--output", str(target)), 1)
                    self.assertIn("cannot overwrite", self.stderr.getvalue())
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertTrue(linked_manifest.is_symlink())
        self.assertEqual(self.backend.mock_calls, [])

    def test_directory_export_output_is_relative_to_selected_directory(self):
        project = self.project()
        nested = project.root / "src"
        nested.mkdir()
        locked = {"packages": [{"name": "bash", "version": "5.2.21", "architecture": "amd64"}]}
        with patch("rosenv.commands.read_lock", return_value=locked):
            self.assertEqual(
                self.invoke("-C", str(nested), "export", "--frozen", "-o", "requirements.txt"),
                0,
            )
        self.assertEqual((nested / "requirements.txt").read_text(), "bash:amd64=5.2.21\n")
        self.assertFalse((project.root / "requirements.txt").exists())
        self.assertEqual(Path.cwd(), project.root)


if __name__ == "__main__":
    unittest.main()
