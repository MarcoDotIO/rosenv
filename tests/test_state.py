"""Persistent state validation and pyenv-style selection precedence."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rosenv.state import StateError, Store, atomic_write, validate_name


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.store = Store(self.base / "registry")
        self.project = self.base / "project"
        self.nested = self.project / "src" / "nested"
        self.nested.mkdir(parents=True)
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("ROSENV_ENV", None)

    def record(self, name: str = "demo") -> dict:
        token = "a" * 32
        return {
            "schema": 1,
            "name": name,
            "token": token,
            "uid": os.getuid(),
            "gid": os.getgid(),
            "container": f"rosenv-{os.getuid()}-{name}-{token[:12]}",
            "ros_distro": "jazzy",
            "image": "ros:jazzy-ros-base",
            "image_id": "sha256:" + "b" * 64,
            "engine_id": "test-engine",
            "workspace": str(self.project),
            "home": str(self.store.env_dir(name) / "home"),
            "network": "bridge",
            "domain_id": 42,
        }

    def test_names_cannot_escape_registry_or_be_cli_options(self) -> None:
        for name in (
            "../elsewhere",
            "/tmp/elsewhere",
            "foo/bar",
            "--help",
            ".",
            "",
            "UPPER",
            "a\nb",
            "a" * 49,
        ):
            with self.subTest(name=name):
                with self.assertRaises(StateError):
                    validate_name(name)
                with self.assertRaises(StateError):
                    self.store.path(name)
        self.assertEqual(validate_name("jazzy_project-1"), "jazzy_project-1")

    def test_selection_precedence_explicit_environment_nearest_project_global(self) -> None:
        for name in ("global-env", "project-env", "nearest-env", "variable-env", "explicit-env"):
            self.store.save(self.record(name))
        self.store.set_global("global-env")
        self.assertEqual(self.store.select(cwd=self.nested), "global-env")
        self.store.set_local("project-env", self.project)
        self.assertEqual(self.store.select(cwd=self.nested), "project-env")
        self.store.set_local("nearest-env", self.nested.parent)
        self.assertEqual(self.store.select(cwd=self.nested), "nearest-env")
        os.environ["ROSENV_ENV"] = "variable-env"
        self.assertEqual(self.store.select(cwd=self.nested), "variable-env")
        self.assertEqual(self.store.select("explicit-env", cwd=self.nested), "explicit-env")

    def test_invalid_project_selector_is_not_silently_ignored(self) -> None:
        self.store.save(self.record("valid"))
        self.store.set_global("valid")
        (self.project / ".rosenv-version").write_text("../../outside\n")
        with self.assertRaises(StateError):
            self.store.select(cwd=self.nested)

    def test_selecting_nonexistent_environment_does_not_write_selector(self) -> None:
        with self.assertRaises(StateError):
            self.store.set_local("missing", self.project)
        self.assertFalse((self.project / ".rosenv-version").exists())
        with self.assertRaises(StateError):
            self.store.set_global("missing")
        self.assertFalse((self.store.root / "default").exists())

    def test_registry_roundtrip(self) -> None:
        record = self.record()
        self.store.save(record)
        self.assertEqual(self.store.load("demo"), record)
        self.assertEqual(self.store.list(), [record])

    def test_corrupt_or_foreign_metadata_is_rejected(self) -> None:
        invalid_fields = {
            "schema": 2,
            "name": "some-other-environment",
            "uid": os.getuid() + 1,
            "token": "not-an-ownership-token",
            "container": "a-foreign-container",
            "ros_distro": "fictional",
            "network": "hostile",
            "domain_id": True,
            "gid": -1,
            "image_id": "ros:latest",
            "engine_id": "",
            "home": str(self.base / "unrelated-home"),
            "workspace": "relative/path",
        }
        for field, value in invalid_fields.items():
            with self.subTest(field=field):
                record = self.record()
                record[field] = value
                atomic_write(self.store.path("demo"), json.dumps(record))
                with self.assertRaises(StateError):
                    self.store.load("demo")

    def test_malformed_json_or_shape_is_a_controlled_state_error(self) -> None:
        for content in ("{broken", "null", "[]", "{}"):
            with self.subTest(content=content):
                atomic_write(self.store.path("demo"), content)
                with self.assertRaises(StateError):
                    self.store.load("demo")

    def test_failed_atomic_replace_preserves_previous_content_and_cleans_temp(self) -> None:
        path = self.base / "config"
        path.write_text("previous\n")
        with patch("rosenv.state.os.replace", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                atomic_write(path, "replacement\n")
        self.assertEqual(path.read_text(), "previous\n")
        self.assertEqual(list(self.base.glob(".config.*")), [])


if __name__ == "__main__":
    unittest.main()
