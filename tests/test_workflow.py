"""Reproducible locks and recovery tests against a stateful, in-memory engine."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rosenv.backend import BackendError
from rosenv.project import ProjectError, initialize, load
from rosenv.state import Store
from rosenv.workflow import (
    environment_name,
    existing_project_environment,
    export_lock,
    lock_project,
    plan_sync,
    read_lock,
    sync_project,
)

IMAGE_ID = "sha256:" + "a" * 64
BASE_ID = "sha256:" + "b" * 64
BASE_DIGEST = "ros@sha256:" + "c" * 64
PACKAGES = [
    {"name": "bash", "version": "5.2.21-2ubuntu4", "architecture": "amd64"},
    {"name": "ros-jazzy-rclpy", "version": "7.1.0-1noble", "architecture": "amd64"},
]


class MemoryEngine:
    """Model resource existence so rollback tests detect lost or leaked resources."""

    def __init__(self):
        self.containers = {}
        self.calls = []
        self.cached = True
        self.create_failure = None
        self.remove_failures = set()
        self.on_create = None
        self.image = {
            "id": IMAGE_ID,
            "base_id": BASE_ID,
            "base_digest": BASE_DIGEST,
            "architecture": "amd64",
            "os": "linux",
            "packages": copy.deepcopy(PACKAGES),
        }

    def prepare_image(self, reference, requested, **options):
        self.calls.append(("prepare", reference, copy.deepcopy(requested), options))
        self.cached = True
        return copy.deepcopy(self.image)

    def image_info(self, image_id):
        self.calls.append(("image_info", image_id))
        return copy.deepcopy(self.image) if self.cached else None

    def engine_id(self):
        return "test-engine"

    def inspect(self, state):
        self.calls.append(("inspect", state["container"]))
        return self.containers.get(state["container"])

    def ensure_running(self, state):
        self.calls.append(("ensure_running", state["container"]))
        if state["container"] not in self.containers:
            raise BackendError("missing container")

    def package_inventory(self, state):
        self.calls.append(("inventory", state["container"]))
        return copy.deepcopy(self.containers[state["container"]]["packages"])

    def create(self, state):
        self.calls.append(("create", state["container"]))
        if self.on_create:
            self.on_create(copy.deepcopy(state))
        self.containers[state["container"]] = {"packages": copy.deepcopy(PACKAGES)}
        if self.create_failure:
            raise self.create_failure

    def remove(self, state):
        self.calls.append(("remove", state["container"]))
        if state["container"] in self.remove_failures:
            raise BackendError("cleanup unavailable")
        self.containers.pop(state["container"], None)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        workspace = self.root / "workspace"
        workspace.mkdir()
        self.project = initialize(workspace, "robot")
        self.store = Store(self.root / "registry")
        self.backend = MemoryEngine()
        for target, value in (
            ("rosenv.workflow.platform.system", "Linux"),
            ("rosenv.workflow.platform.machine", "x86_64"),
        ):
            replacement = patch(target, return_value=value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def lock(self):
        return lock_project(self.project, self.backend)

    def sync(self, **options):
        return sync_project(self.project, self.store, self.backend, **options)

    def drift(self, state):
        self.backend.containers[state["container"]]["packages"].append(
            {"name": "unexpected", "version": "1.0", "architecture": "all"}
        )

    def test_lock_records_complete_inventory_and_cached_check_needs_no_engine(self):
        lock = self.lock()
        self.assertEqual(lock["packages"], PACKAGES)
        self.assertEqual(lock["base_digest"], BASE_DIGEST)
        self.assertEqual(read_lock(self.project), lock)
        self.backend.calls.clear()
        self.assertEqual(lock_project(self.project, self.backend, check=True), lock)
        self.assertEqual(self.backend.calls, [])

    def test_locked_stale_manifest_fails_without_build_or_registry_mutation(self):
        self.lock()
        path = self.project.root / "rosenv.lock"
        original = path.read_bytes()
        self.project.add_dependencies(["demo_nodes_cpp"])
        self.backend.calls.clear()
        with self.assertRaisesRegex(ProjectError, "stale"):
            self.sync(locked=True)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.store.root.exists())

    def test_check_missing_lock_is_offline_and_does_not_create_one(self):
        with self.assertRaisesRegex(ProjectError, "missing or stale"):
            lock_project(self.project, self.backend, check=True)
        self.assertEqual(self.backend.calls, [])
        self.assertFalse((self.project.root / "rosenv.lock").exists())

    def test_frozen_uses_existing_lock_without_rewriting_after_manifest_edit(self):
        lock = self.lock()
        path = self.project.root / "rosenv.lock"
        original = path.read_bytes()
        self.project.add_dependencies(["demo_nodes_cpp"])
        self.backend.calls.clear()
        state = self.sync(frozen=True)
        self.assertEqual(state["requested_packages"], lock["requested"])
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(any(call[0] == "prepare" for call in self.backend.calls))

    def test_frozen_missing_lock_fails_without_build(self):
        with self.assertRaisesRegex(ProjectError, "No rosenv.lock"):
            self.sync(frozen=True)
        self.assertEqual(self.backend.calls, [])

    def test_frozen_v2_uses_manifest_runtime_settings_after_edit(self):
        self.project.network = "none"
        self.project.domain_id = 42
        self.project.save()
        self.lock()
        self.project.network = "host"
        self.project.domain_id = 57
        self.project.save()
        state = self.sync(frozen=True)
        self.assertEqual(state["network"], "host")
        self.assertEqual(state["domain_id"], 57)

    def test_matching_inventory_reuses_container_and_does_not_rebuild(self):
        first = self.sync()
        self.backend.containers[first["container"]]["packages"].reverse()
        self.backend.calls.clear()
        second = self.sync(locked=True)
        self.assertEqual(second, first)
        self.assertEqual(set(self.backend.containers), {first["container"]})
        self.assertEqual(
            [call[0] for call in self.backend.calls], ["inspect", "ensure_running", "inventory"]
        )

    def test_runtime_variables_and_domain_reuse_container_and_leave_lock_untouched(self):
        first = self.sync()
        original_lock = self.project.lock_path.read_bytes()
        self.project.domain_id = 51
        self.project.variables = {"RMW_IMPLEMENTATION": "rmw_fastrtps_cpp"}
        self.project.save()
        self.backend.calls.clear()
        second = self.sync(locked=True)
        self.assertEqual(second["container"], first["container"])
        self.assertEqual(second["domain_id"], 51)
        self.assertEqual(self.project.lock_path.read_bytes(), original_lock)
        self.assertFalse(any(c[0] in {"prepare", "create", "remove"} for c in self.backend.calls))
        self.assertNotIn("variables", second)

    def test_network_change_replaces_only_container_and_preserves_lock_and_home(self):
        first = self.sync()
        original_lock = self.project.lock_path.read_bytes()
        Path(first["home"], "keep").write_text("saved")
        self.project.network = "host"
        self.project.save()
        self.backend.calls.clear()
        second = self.sync(locked=True)
        self.assertNotEqual(second["container"], first["container"])
        self.assertEqual(second["image_id"], first["image_id"])
        self.assertEqual(second["network"], "host")
        self.assertEqual(Path(second["home"], "keep").read_text(), "saved")
        self.assertEqual(self.project.lock_path.read_bytes(), original_lock)
        self.assertFalse(any(c[0] == "prepare" for c in self.backend.calls))

    def test_device_identity_change_replaces_container_without_resolving_packages(self):
        self.project.devices = ["/dev/null:/dev/probe:rw"]
        self.project.save()
        first = self.sync()
        changed = copy.deepcopy(first["devices"])
        changed[0]["inode"] += 1
        self.backend.calls.clear()
        with patch("rosenv.workflow.resolve_devices", return_value=changed):
            second = self.sync(locked=True)
        self.assertNotEqual(first["container"], second["container"])
        self.assertEqual(second["devices"], changed)
        self.assertFalse(any(c[0] == "prepare" for c in self.backend.calls))

    def test_missing_device_fails_before_resolving_or_mutating_registry(self):
        self.project.devices = ["/dev/rosenv-missing-test-device"]
        self.project.save()
        with self.assertRaisesRegex(ProjectError, "Connect it"):
            self.sync()
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(self.project.lock_path.exists())
        self.assertFalse(self.store.root.exists())

    def legacy_lock(self):
        lock = self.lock()
        lock["schema"] = 1
        lock["environment"] = {"network": "none", "domain_id": 42}
        # Legacy fingerprints contained runtime settings; freshness now uses package intent.
        lock["fingerprint"] = "d" * 64
        self.project.lock_path.write_text(json.dumps(lock))
        return lock

    def test_legacy_lock_migrates_without_build_and_locked_checks_do_not_write(self):
        legacy = self.legacy_lock()
        before = self.project.lock_path.read_bytes()
        self.backend.calls.clear()
        checked = lock_project(self.project, self.backend, check=True)
        self.assertEqual(checked, legacy)
        self.assertEqual(self.project.lock_path.read_bytes(), before)
        migrated = self.lock()
        self.assertEqual(migrated["schema"], 2)
        self.assertNotIn("environment", migrated)
        self.assertEqual(migrated["packages"], legacy["packages"])
        self.assertEqual(migrated["image_id"], legacy["image_id"])
        self.assertEqual(self.backend.calls, [])

    def test_legacy_frozen_preserves_locked_runtime_but_locked_uses_manifest(self):
        self.legacy_lock()
        original = self.project.lock_path.read_bytes()
        self.project.network = "host"
        self.project.domain_id = 57
        self.project.save()
        frozen = self.sync(frozen=True)
        self.assertEqual((frozen["network"], frozen["domain_id"]), ("none", 42))
        locked = self.sync(locked=True)
        self.assertEqual((locked["network"], locked["domain_id"]), ("host", 57))
        self.assertEqual(self.project.lock_path.read_bytes(), original)

    def test_legacy_receipt_reuses_container_after_lock_migration(self):
        first = self.sync()
        first.pop("package_signature")
        first.pop("container_signature")
        self.store.save(first)
        self.legacy_lock()
        second = self.sync()
        self.assertEqual(first["container"], second["container"])
        self.assertEqual(read_lock(self.project)["schema"], 2)

    def test_lock_metadata_change_does_not_replace_container(self):
        first = self.sync()
        lock = read_lock(self.project)
        lock["created_at"] = "different-timestamp"
        lock["generator"] = "different-generator"
        self.project.lock_path.write_text(json.dumps(lock))
        second = self.sync(locked=True)
        self.assertEqual(first["container"], second["container"])

    def test_runtime_replacement_reuses_reconstructed_image_with_different_id(self):
        first = self.sync()
        reconstructed_id = "sha256:" + "e" * 64
        first["image_id"] = reconstructed_id
        self.store.save(first)
        self.project.network = "host"
        self.project.save()
        self.backend.calls.clear()
        with patch.object(
            self.backend,
            "image_info",
            side_effect=lambda image_id: (
                {**self.backend.image, "id": reconstructed_id}
                if image_id == reconstructed_id
                else None
            ),
        ):
            second = self.sync(locked=True)
        self.assertNotEqual(first["container"], second["container"])
        self.assertEqual(second["image_id"], reconstructed_id)
        self.assertFalse(any(c[0] == "prepare" for c in self.backend.calls))

    def test_changed_request_with_same_inventory_updates_receipt_without_replacement(self):
        first = self.sync()
        self.project.add_dependencies(["apt:bash"])
        second = self.sync()
        self.assertEqual(first["container"], second["container"])
        self.assertEqual(second["requested_packages"], ["bash"])

    def test_alternate_manifests_have_independent_locks_and_environments(self):
        self.sync()
        original = self.project.lock_path.read_bytes()
        alternate = initialize(self.project.root, "robot", manifest_name="rosenv.robot.toml")
        alternate.dependencies = ["rclpy"]
        alternate.save()
        state = sync_project(alternate, self.store, self.backend)
        self.assertEqual(state["requested_packages"], ["ros-jazzy-rclpy"])
        self.assertNotEqual(environment_name(alternate), environment_name(self.project))
        self.assertEqual(self.project.lock_path.read_bytes(), original)
        self.assertTrue(alternate.lock_path.exists())
        self.assertEqual(len(self.store.list()), 2)

    def test_dry_run_does_not_start_write_or_resolve(self):
        self.assertIn("Resolve dependencies", plan_sync(self.project, self.store, self.backend))
        self.assertFalse(self.store.root.exists())
        self.assertFalse(self.project.lock_path.exists())
        self.assertEqual(self.backend.calls, [])
        state = self.sync()
        receipt = self.store.path(state["name"]).read_bytes()
        lock = self.project.lock_path.read_bytes()
        self.backend.calls.clear()
        self.assertIn("Reuse container", plan_sync(self.project, self.store, self.backend))
        self.assertEqual([c[0] for c in self.backend.calls], ["inspect"])
        self.project.network = "host"
        self.project.save()
        self.assertIn("network changed", plan_sync(self.project, self.store, self.backend))
        self.assertEqual(self.store.path(state["name"]).read_bytes(), receipt)
        self.assertEqual(self.project.lock_path.read_bytes(), lock)

    def test_package_drift_replaces_container_preserving_private_home(self):
        first = self.sync()
        private_file = Path(first["home"]) / "history"
        private_file.write_text("retain this\n")
        self.drift(first)

        def before_create(candidate):
            self.assertEqual(self.store.load(candidate["name"]), candidate)
            self.assertFalse(candidate["ready"])
            self.assertEqual(candidate["retired"], first)
            self.assertIn(first["container"], self.backend.containers)

        self.backend.on_create = before_create
        second = self.sync(locked=True)
        self.assertNotEqual(second["container"], first["container"])
        self.assertEqual(second["home"], first["home"])
        self.assertEqual(second["domain_id"], first["domain_id"])
        self.assertEqual(private_file.read_text(), "retain this\n")
        self.assertEqual(set(self.backend.containers), {second["container"]})
        self.assertTrue(second["ready"])
        self.assertNotIn("retired", second)
        self.assertEqual(self.store.load(second["name"]), second)

    def test_failed_replacement_restores_record_and_preserves_old_container(self):
        first = self.sync()
        private_file = Path(first["home"]) / "important"
        private_file.write_text("saved")
        self.drift(first)
        self.backend.create_failure = BackendError("new container failed")
        with self.assertRaisesRegex(BackendError, "new container failed"):
            self.sync(locked=True)
        self.assertEqual(self.store.load(first["name"]), first)
        self.assertEqual(set(self.backend.containers), {first["container"]})
        self.assertEqual(private_file.read_text(), "saved")

    def test_interrupted_create_rolls_back_prior_record(self):
        first = self.sync()
        self.drift(first)
        self.backend.create_failure = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.sync(locked=True)
        self.assertEqual(self.store.load(first["name"]), first)
        self.assertEqual(set(self.backend.containers), {first["container"]})

    def test_failed_candidate_cleanup_remains_journaled_and_next_sync_recovers(self):
        first = self.sync()
        self.drift(first)
        self.backend.create_failure = BackendError("new container failed")
        self.backend.on_create = lambda state: self.backend.remove_failures.add(state["container"])
        with self.assertRaisesRegex(BackendError, "new container failed"):
            self.sync(locked=True)
        pending = self.store.load(first["name"])
        self.assertFalse(pending["ready"])
        self.assertEqual(pending["retired"], first)
        self.assertEqual(set(self.backend.containers), {first["container"], pending["container"]})
        self.backend.create_failure = None
        self.backend.on_create = None
        self.backend.remove_failures.clear()
        recovered = self.sync(locked=True)
        self.assertTrue(recovered["ready"])
        self.assertNotIn("retired", recovered)
        self.assertEqual(set(self.backend.containers), {recovered["container"]})
        self.assertEqual(self.store.load(first["name"]), recovered)

    def test_pending_initial_creation_recovers_without_a_retired_environment(self):
        self.backend.create_failure = BackendError("initial creation failed")
        self.backend.on_create = lambda state: self.backend.remove_failures.add(state["container"])
        with self.assertRaises(BackendError):
            self.sync()
        pending = self.store.load(environment_name(self.project))
        self.assertFalse(pending["ready"])
        self.assertNotIn("retired", pending)
        self.backend.create_failure = None
        self.backend.on_create = None
        self.backend.remove_failures.clear()
        recovered = self.sync(locked=True)
        self.assertTrue(recovered["ready"])
        self.assertEqual(set(self.backend.containers), {recovered["container"]})

    def test_retired_cleanup_failure_stays_journaled_and_retries_without_replacing(self):
        first = self.sync()
        self.drift(first)
        self.backend.remove_failures.add(first["container"])
        with self.assertRaisesRegex(BackendError, "cleanup unavailable"):
            self.sync(locked=True)
        replacement = self.store.load(first["name"])
        self.assertTrue(replacement["ready"])
        self.assertEqual(replacement["retired"], first)
        self.assertEqual(
            set(self.backend.containers), {first["container"], replacement["container"]}
        )
        self.backend.remove_failures.clear()
        self.backend.calls.clear()
        recovered = self.sync(locked=True)
        self.assertEqual(recovered["container"], replacement["container"])
        self.assertNotIn("retired", recovered)
        self.assertFalse(any(call[0] == "create" for call in self.backend.calls))
        self.assertEqual(set(self.backend.containers), {recovered["container"]})

    def test_no_sync_rejects_pending_environment_with_recovery_guidance(self):
        state = self.sync()
        state["ready"] = False
        self.store.save(state)
        with self.assertRaisesRegex(ProjectError, "sync"):
            existing_project_environment(self.project, self.store)

    def test_architecture_mismatch_fails_before_engine_or_registry_mutation(self):
        self.lock()
        self.backend.calls.clear()
        with patch("rosenv.workflow.platform.machine", return_value="aarch64"):
            with self.assertRaisesRegex(ProjectError, "amd64.*arm64"):
                self.sync(frozen=True)
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(self.store.root.exists())

    def test_missing_image_rebuild_uses_locked_base_and_full_inventory(self):
        lock = self.lock()
        self.backend.calls.clear()
        self.backend.cached = False
        state = self.sync(frozen=True)
        preparations = [call for call in self.backend.calls if call[0] == "prepare"]
        self.assertEqual(
            preparations,
            [
                (
                    "prepare",
                    BASE_DIGEST,
                    lock["requested"],
                    {"locked_packages": PACKAGES, "fingerprint": lock["fingerprint"]},
                )
            ],
        )
        self.assertTrue(state["ready"])

    def test_reconstructed_wrong_base_fails_without_creating_an_environment(self):
        self.lock()
        self.backend.cached = False
        self.backend.image["base_id"] = "sha256:" + "d" * 64
        with self.assertRaisesRegex(ProjectError, "locked base/platform"):
            self.sync(frozen=True)
        self.assertEqual(self.backend.containers, {})
        self.assertEqual(self.store.list(), [])

    def test_export_retains_exact_package_pins_and_immutable_base(self):
        lock = self.lock()
        self.assertEqual(json.loads(export_lock(lock, "json")), lock)
        requirements = export_lock(lock, "requirements")
        self.assertEqual(
            requirements, "bash:amd64=5.2.21-2ubuntu4\nros-jazzy-rclpy:amd64=7.1.0-1noble\n"
        )
        dockerfile = export_lock(lock, "dockerfile")
        self.assertIn(f"FROM {BASE_DIGEST}\n", dockerfile)
        self.assertIn("--allow-downgrades", dockerfile)
        self.assertIn("--no-install-recommends --", dockerfile)
        for line in requirements.splitlines():
            self.assertIn(line, dockerfile)

    def test_export_distinguishes_multiarch_packages_and_omits_all_qualifier(self):
        lock = self.lock()
        lock["packages"] = [
            {"name": "libc6", "version": "2.39-0ubuntu8.6", "architecture": "amd64"},
            {"name": "libc6", "version": "2.39-0ubuntu8.6", "architecture": "i386"},
            {"name": "tzdata", "version": "2026a-0ubuntu0.24.04", "architecture": "all"},
        ]
        (self.project.root / "rosenv.lock").write_text(json.dumps(lock))
        loaded = read_lock(self.project)
        self.assertEqual(
            export_lock(loaded, "requirements").splitlines(),
            [
                "libc6:amd64=2.39-0ubuntu8.6",
                "libc6:i386=2.39-0ubuntu8.6",
                "tzdata=2026a-0ubuntu0.24.04",
            ],
        )

    def test_lock_reloads_manifest_changed_since_project_was_discovered(self):
        stale = self.project
        latest = load(stale.root)
        latest.add_dependencies(["demo_nodes_cpp"])
        lock = lock_project(stale, self.backend)
        self.assertEqual(lock["requested"], ["ros-jazzy-demo-nodes-cpp"])
        self.assertEqual(lock["fingerprint"], latest.fingerprint())

    def test_lock_reader_rejects_injection_and_invalid_schema(self):
        valid = self.lock()
        variants = [
            {"schema": True},
            {"schema": 3},
            {"ros": "unknown"},
            {"variant": "unknown"},
            {"groups": "dev"},
            {"groups": ["../escape"]},
            {"schema": 1, "environment": {"network": "public", "domain_id": None}},
            {"schema": 1, "environment": {"network": "bridge", "domain_id": True}},
            {"schema": 1, "environment": {"network": "bridge", "domain_id": 233}},
            {"fingerprint": "a" * 63},
            {"image_id": "ros:latest"},
            {"base_id": "sha256:not-hex"},
            {"base_digest": "ros:jazzy\nRUN touch /tmp/injected"},
            {"base_digest": "attacker/image@sha256:" + "a" * 64},
            {"os": "windows"},
            {"architecture": "ppc64le"},
            {"packages": []},
            {"packages": [PACKAGES[0], PACKAGES[0]]},
            {"requested": ["--allow-unauthenticated"]},
            {"requested": ["bash;touch /tmp/injected"]},
            {"requested": ["bash\nRUN echo injected"]},
            {"requested": [1]},
        ]
        for key, value in (
            ("name", "bash;id"),
            ("name", "--flag"),
            ("version", "1\nRUN id"),
            ("version", "$(id)"),
            ("architecture", "amd64;id"),
        ):
            package = {**PACKAGES[0], key: value}
            variants.append({"packages": [package]})
        path = self.project.root / "rosenv.lock"
        for changes in variants:
            with self.subTest(changes=changes):
                path.write_text(json.dumps({**valid, **changes}))
                with self.assertRaises(ProjectError):
                    read_lock(self.project)
        for invalid in ([], None, {"schema": 1}, "invalid"):
            with self.subTest(invalid=invalid):
                path.write_text(json.dumps(invalid))
                with self.assertRaises(ProjectError):
                    read_lock(self.project)


if __name__ == "__main__":
    unittest.main()
