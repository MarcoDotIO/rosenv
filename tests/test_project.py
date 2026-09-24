"""Project discovery, manifest edits, dependency semantics, and edit isolation."""

from __future__ import annotations

import fcntl
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rosenv.project import (
    Project,
    ProjectError,
    discover,
    initialize,
    load,
    normalize_package,
    project_lock,
)


class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def manifest(self, content: str) -> Project:
        (self.root / "rosenv.toml").write_text(content, encoding="utf-8")
        return load(self.root)

    def create(self) -> Project:
        return initialize(self.root, "robot")

    def test_initialize_roundtrip_with_explicit_environment_settings(self) -> None:
        project = initialize(self.root, "robot", "humble", "perception", "none", 52)
        loaded = load(self.root)
        self.assertEqual(project.to_dict(), loaded.to_dict())
        self.assertEqual(loaded.root, self.root)
        self.assertEqual(loaded.distro, "humble")
        self.assertEqual(loaded.network, "none")
        self.assertEqual(loaded.domain_id, 52)
        self.assertEqual(loaded.dependencies, [])
        self.assertEqual(loaded.selected_groups(), [])
        self.assertEqual(loaded.effective_dependencies(), [])
        self.assertFalse((self.root / "rosenv.lock").exists())
        self.assertFalse((self.root / ".rosenv-version").exists())
        json.dumps(loaded.to_dict())

    def test_initialize_never_overwrites_an_existing_manifest(self) -> None:
        for content in ("", "invalid TOML !", '[project]\nname="existing"\nros="jazzy"\n'):
            with self.subTest(content=content):
                path = self.root / "rosenv.toml"
                path.write_text(content)
                with self.assertRaisesRegex(ProjectError, "already exists"):
                    initialize(self.root, "replacement")
                self.assertEqual(path.read_text(), content)

    def test_initialize_does_not_replace_dangling_manifest_symlink(self) -> None:
        path = self.root / "rosenv.toml"
        path.symlink_to(self.root / "missing")
        with self.assertRaises(ProjectError):
            self.create()
        self.assertTrue(path.is_symlink())
        self.assertFalse((self.root / "missing").exists())

    def test_initialize_requires_existing_directory(self) -> None:
        with self.assertRaisesRegex(ProjectError, "directory does not exist"):
            initialize(self.root / "missing", "robot")
        self.assertFalse((self.root / "missing").exists())

    def test_discover_finds_nearest_ancestor_from_directory_or_file(self) -> None:
        outer = self.create()
        inner_root = self.root / "packages" / "inner"
        inner_root.mkdir(parents=True)
        nested = inner_root / "src"
        nested.mkdir()
        source = nested / "node.py"
        source.write_text("pass\n")
        self.assertEqual(discover(nested).root, outer.root)
        inner = initialize(inner_root, "inner", "humble")
        self.assertEqual(discover(nested).root, inner.root)
        self.assertEqual(Project.discover(source).root, inner.root)
        with patch("rosenv.project.Path.cwd", return_value=nested):
            self.assertEqual(discover().root, inner.root)

    def test_discover_does_not_skip_invalid_nearest_manifest(self) -> None:
        self.create()
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "rosenv.toml").write_text("invalid : toml")
        with self.assertRaisesRegex(ProjectError, "Cannot load"):
            discover(nested)

    def test_discover_returns_none_without_any_ancestor_manifest(self) -> None:
        self.assertIsNone(discover(self.root))

    def test_corrupt_toml_encoding_and_shapes_have_controlled_errors(self) -> None:
        for content in (
            "[project",
            "project = 3",
            "[project]\nname = 'robot'",
            "[project]\nros = 'jazzy'",
            "",
            "[project]\nname = 'robot'\nros = 'jazzy'\ndependencies = 'rclpy'",
            "[project]\nname = 'robot'\nros = 'jazzy'\ndependencies = [1]",
        ):
            with self.subTest(content=content):
                with self.assertRaises(ProjectError):
                    self.manifest(content)
        (self.root / "rosenv.toml").write_bytes(b"\xff")
        with self.assertRaises(ProjectError):
            load(self.root)

    def test_unknown_fields_are_rejected_and_never_silently_discarded(self) -> None:
        base = '[project]\nname = "robot"\nros = "jazzy"\n'
        for content, fragment in (
            (base + "dependecies = []\n", "dependecies"),
            (base + '[environment]\nnetwrok = "bridge"\n', "netwrok"),
            (base + "[tool.custom]\nvalue = 1\n", "tool"),
        ):
            with self.subTest(content=content):
                with self.assertRaisesRegex(ProjectError, fragment):
                    self.manifest(content)
                self.assertEqual((self.root / "rosenv.toml").read_text(), content)

    def test_validate_names_and_environment_values(self) -> None:
        for field, value in (
            ("name", "../escape"),
            ("name", "--flag"),
            ("name", 9),
            ("distro", "fictional"),
            ("distro", []),
            ("variant", "desktop"),
            ("network", "public"),
            ("domain_id", True),
            ("domain_id", -1),
            ("domain_id", 233),
            ("domain_id", "4"),
        ):
            with self.subTest(field=field, value=value):
                values = {"root": self.root, "name": "robot", field: value}
                with self.assertRaises(ProjectError):
                    Project(**values)

    def test_normalization_handles_ros_apt_architecture_and_exact_pins(self) -> None:
        for spec, expected in (
            ("demo_nodes_cpp", "ros-jazzy-demo-nodes-cpp"),
            ("demo-nodes-cpp", "ros-jazzy-demo-nodes-cpp"),
            ("ros-jazzy-rclpy", "ros-jazzy-rclpy"),
            ("rclpy=9.1.0-1noble.20260920.001", "ros-jazzy-rclpy=9.1.0-1noble.20260920.001"),
            ("apt:git=1:2.43.0-1ubuntu7.3", "git=1:2.43.0-1ubuntu7.3"),
            ("apt:libc6:amd64=2.39-0ubuntu8.6", "libc6:amd64=2.39-0ubuntu8.6"),
            ("apt:libstdc++6", "libstdc++6"),
        ):
            with self.subTest(spec=spec):
                self.assertEqual(normalize_package("jazzy", spec), expected)

    def test_dependency_arguments_cannot_be_flags_shell_fragments_or_paths(self) -> None:
        for value in (
            "",
            "apt:",
            "apt:--allow-unauthenticated",
            "--help",
            "../pkg",
            "pkg;id",
            "$(whoami)",
            "apt:git\napt:curl",
            "apt:git curl",
            "rclpy|id",
            "apt:git=",
            "rclpy=>1",
            "apt:git=1;id",
            "apt:git=1=2",
            "apt:git\0",
            "ros-jazzy-",
            "ros-humble-rclpy",
            123,
            False,
        ):
            with self.subTest(value=value):
                with self.assertRaises(ProjectError):
                    normalize_package("jazzy", value)

    def test_default_dev_optional_groups_and_unknown_groups(self) -> None:
        project = Project(
            self.root,
            "robot",
            dependencies=["rclpy"],
            groups={"dev": ["demo_nodes_cpp"], "vision": ["sensor_msgs"]},
        )
        self.assertEqual(project.selected_groups(), ["dev"])
        self.assertEqual(project.selected_groups(["vision", "vision"]), ["dev", "vision"])
        self.assertEqual(project.selected_groups(no_dev=True), [])
        self.assertEqual(project.selected_groups(["dev"], no_dev=True), ["dev"])
        self.assertEqual(
            project.effective_dependencies(),
            [
                "ros-jazzy-demo-nodes-cpp",
                "ros-jazzy-rclpy",
            ],
        )
        self.assertEqual(project.effective_dependencies(no_dev=True), ["ros-jazzy-rclpy"])
        with self.assertRaisesRegex(ProjectError, "Unknown dependency group"):
            project.effective_dependencies(["missing"])
        with self.assertRaises(ProjectError):
            project.selected_groups("dev")
        with self.assertRaises(ProjectError):
            project.selected_groups([True])

    def test_selected_pin_conflicts_fail_while_exact_pin_refines_unpinned(self) -> None:
        project = Project(
            self.root,
            "robot",
            dependencies=["rclpy"],
            groups={"dev": ["rclpy=9.0.0"], "old": ["rclpy=8.0.0"]},
        )
        self.assertEqual(project.effective_dependencies(), ["ros-jazzy-rclpy=9.0.0"])
        with self.assertRaisesRegex(ProjectError, "Conflicting exact versions"):
            project.effective_dependencies(["old"])
        self.assertEqual(
            project.effective_dependencies(["old"], no_dev=True),
            [
                "ros-jazzy-rclpy=8.0.0",
            ],
        )

    def test_fingerprint_ignores_task_comment_order_location_and_unused_group_edits(self) -> None:
        project = Project(
            self.root,
            "robot",
            dependencies=["rclpy", "demo_nodes_cpp", "apt:git"],
            groups={"optional": ["sensor_msgs"]},
            tasks={"hello": ["echo", "before"]},
        )
        before = project.fingerprint()
        project.dependencies = ["apt:git", "demo-nodes-cpp", "ros-jazzy-rclpy", "rclpy"]
        project.tasks["hello"] = ["echo", "after"]
        project.groups["optional"] = ["image_transport"]
        project.name = "renamed"
        project.root = self.root / "different-location"
        self.assertEqual(project.fingerprint(), before)
        self.assertRegex(before, r"^[0-9a-f]{64}$")

    def test_fingerprint_changes_for_effective_dependencies_group_profile_and_runtime(self) -> None:
        original = Project(self.root, "robot", groups={"dev": [], "empty": []})
        baseline = original.fingerprint()
        self.assertNotEqual(original.fingerprint(no_dev=True), baseline)
        self.assertNotEqual(original.fingerprint(["empty"]), baseline)
        for changes in (
            {"distro": "humble"},
            {"variant": "perception"},
            {"dependencies": ["rclpy"]},
            {"network": "none"},
            {"domain_id": 3},
        ):
            with self.subTest(changes=changes):
                changed = Project(self.root, "robot", groups={"dev": [], "empty": []}, **changes)
                self.assertNotEqual(changed.fingerprint(), baseline)

    def test_add_remove_persist_and_replace_specs_by_package_identity(self) -> None:
        project = self.create()
        project.add_dependencies(["rclpy", "apt:git"])
        project.add_dependencies(["apt:git=1:2.3-1", "demo_nodes_cpp"])
        self.assertEqual(
            load(self.root).dependencies, ["rclpy", "apt:git=1:2.3-1", "demo_nodes_cpp"]
        )
        project.add_dependencies(["sensor_msgs"], group="vision")
        self.assertEqual(load(self.root).groups, {"vision": ["sensor_msgs"]})
        project.remove_dependencies(["ros-jazzy-rclpy", "apt:git"])
        project.remove_dependencies(["sensor_msgs"], group="vision")
        self.assertEqual(load(self.root).dependencies, ["demo_nodes_cpp"])
        self.assertEqual(load(self.root).groups, {"vision": []})

    def test_missing_remove_and_invalid_add_do_not_partially_change_manifest(self) -> None:
        project = self.create()
        project.add_dependencies(["rclpy"])
        before = project.path.read_text()
        for operation in (
            lambda: project.remove_dependencies(["rclpy", "missing"]),
            lambda: project.remove_dependencies(["rclpy"], group="missing"),
            lambda: project.add_dependencies(["std_msgs", "bad;arg"]),
            lambda: project.add_dependencies(["rclpy=1", "rclpy=2"]),
            lambda: project.add_dependencies(["rclpy"], group="../escape"),
            lambda: project.add_dependencies("std_msgs"),
        ):
            with self.assertRaises(ProjectError):
                operation()
            self.assertEqual(project.path.read_text(), before)
            self.assertEqual(project.dependencies, ["rclpy"])

    def test_comment_preserving_edits_leave_unrelated_values_and_argv_unchanged(self) -> None:
        content = """# Shared project
[project] # metadata
name = 'robot' # identity
ros = "jazzy"
dependencies = [
  "rclpy", # Python client
  "apt:git", # Source control
]

[tasks]
# Demo runner
talker = ["ros2", "run", "demo_nodes_cpp", "talker"]
"""
        project = self.manifest(content)
        fingerprint = project.fingerprint()
        project.save()
        self.assertEqual(project.path.read_text(), content)
        project.add_dependencies(["apt:git=1:2.3-1", "std_msgs"])
        updated = project.path.read_text()
        for retained in (
            "# Shared project",
            "[project] # metadata",
            "name = 'robot' # identity",
            '"rclpy", # Python client',
            '"apt:git=1:2.3-1", # Source control',
            '# Demo runner\ntalker = ["ros2", "run", "demo_nodes_cpp", "talker"]',
        ):
            self.assertIn(retained, updated)
        project.remove_dependencies(["apt:git", "std_msgs"])
        self.assertIn('"rclpy", # Python client', project.path.read_text())
        self.assertNotEqual(fingerprint, project.fingerprint())

    def test_stale_manifest_edits_fail_without_lost_update(self) -> None:
        first = self.create()
        stale = load(self.root)
        first.add_dependencies(["rclpy"])
        with self.assertRaisesRegex(ProjectError, "changed since it was loaded"):
            stale.add_dependencies(["std_msgs"])
        self.assertEqual(stale.dependencies, [])
        self.assertEqual(load(self.root).dependencies, ["rclpy"])

    def test_external_comment_changes_are_not_overwritten_by_stale_objects(self) -> None:
        project = self.create()
        updated = project.path.read_text() + "\n# Another editor\n"
        project.path.write_text(updated)
        with self.assertRaises(ProjectError):
            project.set_distro("humble")
        self.assertEqual(project.distro, "jazzy")
        self.assertEqual(project.path.read_text(), updated)

    def test_project_edit_lock_is_reentrant_and_releases_after_exception(self) -> None:
        project = self.create()
        with self.assertRaisesRegex(RuntimeError, "test failure"):
            with project_lock(self.root):
                with project.edit_lock():
                    with (self.root / ".rosenv.edit.lock").open("a") as handle:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    raise RuntimeError("test failure")
        with (self.root / ".rosenv.edit.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)

    def test_failed_atomic_save_keeps_manifest_and_rolls_back_dependency_edit(self) -> None:
        project = self.create()
        before = project.path.read_text()
        with patch("rosenv.state.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                project.add_dependencies(["rclpy"])
        self.assertEqual(project.dependencies, [])
        self.assertEqual(project.path.read_text(), before)
        self.assertEqual(list(self.root.glob(".rosenv.toml.*")), [])

    def test_set_distro_retargets_shorthand_and_preserves_comments(self) -> None:
        project = self.manifest("""[project]
name = "robot"
ros = "jazzy" # Tested release
dependencies = ["rclpy", "apt:git"]
""")
        project.set_distro("humble")
        self.assertEqual(load(self.root).effective_dependencies(), ["git", "ros-humble-rclpy"])
        self.assertIn('ros = "humble" # Tested release', project.path.read_text())
        with self.assertRaises(ProjectError):
            project.set_distro("fictional")
        self.assertEqual(load(self.root).distro, "humble")

    def test_tasks_require_argv_but_allow_explicit_shell_and_literal_metacharacters(self) -> None:
        for command in (
            "ros2 run demo_nodes_cpp talker",
            [],
            [""],
            ["--flag"],
            ["echo", 1],
            ["echo", "\0"],
        ):
            with self.subTest(command=command):
                with self.assertRaises(ProjectError):
                    Project(self.root, "robot", tasks={"task": command})
        commands = {
            "literal": ["printf", "%s", "$(whoami);echo hi"],
            "explicit-shell": ["bash", "-c", "echo hello && echo world"],
        }
        project = self.create()
        project.tasks = commands
        project.save()
        self.assertEqual(load(self.root).tasks, commands)

    def test_to_dict_does_not_expose_mutable_internal_lists(self) -> None:
        project = Project(
            self.root,
            "robot",
            dependencies=["rclpy"],
            groups={"dev": ["std_msgs"]},
            tasks={"list": ["ros2", "pkg", "list"]},
        )
        snapshot = project.to_dict()
        snapshot["dependencies"].append("sensor_msgs")
        snapshot["groups"]["dev"].clear()
        snapshot["tasks"]["list"].clear()
        self.assertEqual(project.dependencies, ["rclpy"])
        self.assertEqual(project.groups, {"dev": ["std_msgs"]})
        self.assertEqual(project.tasks, {"list": ["ros2", "pkg", "list"]})


if __name__ == "__main__":
    unittest.main()
