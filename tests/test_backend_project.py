"""Project-image provenance, exact-package locking and conservative cache tests."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from rosenv.backend import BackendError, DockerBackend

BASE_ID = "sha256:" + "a" * 64
IMAGE_ID = "sha256:" + "b" * 64
DIGEST = "docker.io/library/ros@sha256:" + "c" * 64
REFERENCE = "docker.io/library/ros:jazzy-ros-base-noble"
INVENTORY = [
    {"name": "base-files", "version": "13ubuntu10.3", "architecture": "amd64"},
    {"name": "ros-jazzy-demo-nodes-cpp", "version": "0.33.5-1noble", "architecture": "amd64"},
    {"name": "ros-workspace", "version": "1.0.3-1", "architecture": "all"},
]


class ProjectBackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = DockerBackend()
        self.backend._engine_id = "test-engine"

    @staticmethod
    def result(payload="", code=0, stderr=""):
        return subprocess.CompletedProcess(
            ["docker"], code, payload if isinstance(payload, str) else json.dumps(payload), stderr
        )

    @staticmethod
    def image_info(image_id=BASE_ID, labels=None):
        return {
            "id": image_id,
            "digests": [DIGEST.replace("docker.io/library/", "")],
            "architecture": "amd64",
            "os": "linux",
            "variant": "",
            "labels": labels or {},
            "size": 1234,
            "created": "2026-09-24T00:00:00Z",
            "tags": ["rosenv-cache:test"] if image_id == IMAGE_ID else [REFERENCE],
        }

    def prepare(self, packages=None, *, locked=None, inventory=None, fingerprint="", status=0):
        captured = {}

        def build(argv):
            captured["argv"] = argv
            captured["recipe"] = Path(argv[argv.index("--file") + 1]).read_text()
            captured["context_files"] = list(Path(argv[-1]).iterdir())
            captured["context"] = argv[-1]
            captured["iidfile"] = argv[argv.index("--iidfile") + 1]
            if not status:
                Path(captured["iidfile"]).write_text(IMAGE_ID + "\n")
            return status

        with (
            patch.object(self.backend, "resolve_image", return_value={"id": BASE_ID}) as resolve,
            patch.object(self.backend, "image_info", side_effect=self.image_info),
            patch.object(self.backend, "_stream", side_effect=build),
            patch.object(self.backend, "_image_inventory", return_value=inventory or INVENTORY),
            patch.object(self.backend, "_docker", return_value=self.result()) as docker,
        ):
            captured["result"] = self.backend.prepare_image(
                DIGEST if locked is not None else REFERENCE,
                packages if packages is not None else ["ros-jazzy-demo-nodes-cpp"],
                locked_packages=locked,
                fingerprint=fingerprint,
            )
        captured["resolve"] = resolve.call_args
        captured["commands"] = [call.args[0] for call in docker.call_args_list]
        return captured

    def test_normal_build_pins_base_and_sends_an_empty_context(self):
        built = self.prepare(packages=["ros-jazzy-demo-nodes-cpp", "git=1:2.43.0-1ubuntu7"])
        self.assertEqual(built["context_files"], [])
        self.assertFalse(Path(built["context"]).exists())
        self.assertNotEqual(Path(built["iidfile"]).parent, Path(built["context"]))
        recipe = built["recipe"]
        self.assertTrue(recipe.startswith(f"FROM {DIGEST}\n"))
        self.assertNotIn(REFERENCE, recipe)
        self.assertNotIn("COPY", recipe)
        self.assertNotIn("/workspace", recipe)
        self.assertNotIn("/home/rosenv", recipe)
        self.assertIn("apt-get upgrade -y --with-new-pkgs --no-install-recommends", recipe)
        self.assertIn("git=1:2.43.0-1ubuntu7", recipe)
        self.assertIn("--no-cache", built["argv"])
        self.assertIn("io.rosenv.cache=1", built["argv"])
        self.assertIn(f"io.rosenv.uid={os.getuid()}", built["argv"])
        self.assertEqual(built["result"]["base_digest"], DIGEST)
        self.assertEqual(built["result"]["base_id"], BASE_ID)
        self.assertEqual(built["result"]["packages"], INVENTORY)
        self.assertEqual(built["result"]["engine_id"], "test-engine")

    def test_cache_tag_depends_on_actual_inventory_even_with_the_same_fingerprint(self):
        first = self.prepare(fingerprint="same-project")["result"]["tag"]
        changed = [dict(package) for package in INVENTORY]
        changed[0]["version"] = "13ubuntu10.4"
        second = self.prepare(fingerprint="same-project", inventory=changed)["result"]["tag"]
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^rosenv-cache:[0-9a-f]{64}$")

    def test_users_sharing_a_daemon_get_separate_cache_tags(self):
        with patch("rosenv.backend.os.getuid", return_value=1000):
            first = self.prepare(fingerprint="same-project")
        with patch("rosenv.backend.os.getuid", return_value=1001):
            second = self.prepare(fingerprint="same-project")
        self.assertNotEqual(first["result"]["tag"], second["result"]["tag"])
        self.assertIn("io.rosenv.uid=1000", first["argv"])
        self.assertIn("io.rosenv.uid=1001", second["argv"])

    def test_frozen_build_installs_every_exact_version_without_upgrade(self):
        built = self.prepare(locked=list(reversed(INVENTORY)))
        self.assertEqual(built["resolve"].args, (DIGEST,))
        recipe = built["recipe"]
        self.assertNotIn("apt-get upgrade", recipe)
        self.assertIn("--allow-downgrades", recipe)
        self.assertIn("base-files:amd64=13ubuntu10.3", recipe)
        self.assertIn("ros-workspace=1.0.3-1", recipe)
        self.assertEqual(built["result"]["packages"], INVENTORY)

    def test_frozen_build_fails_on_inventory_drift(self):
        altered = [dict(package) for package in INVENTORY]
        altered[0]["version"] = "new-unlocked-version"
        with self.assertRaisesRegex(BackendError, "Frozen package inventory does not match"):
            self.prepare(locked=INVENTORY, inventory=altered)

    def test_frozen_build_rejects_both_extra_and_missing_packages(self):
        extra = [
            *INVENTORY,
            {"name": "unlocked-dependency", "version": "1.0", "architecture": "amd64"},
        ]
        for inventory in (INVENTORY[:-1], extra):
            with self.subTest(inventory=inventory):
                with self.assertRaisesRegex(
                    BackendError, "Frozen package inventory does not match"
                ):
                    self.prepare(locked=INVENTORY, inventory=inventory)

    def test_frozen_build_qualifies_multiarch_packages_once(self):
        inventory = [
            {"name": "libc6", "version": "2.39-0ubuntu8.6", "architecture": "amd64"},
            {"name": "libc6", "version": "2.39-0ubuntu8.6", "architecture": "i386"},
        ]
        built = self.prepare(locked=inventory, inventory=inventory)
        self.assertIn("libc6:amd64=2.39-0ubuntu8.6", built["recipe"])
        self.assertIn("libc6:i386=2.39-0ubuntu8.6", built["recipe"])
        self.assertNotIn("libc6:amd64:amd64", built["recipe"])
        self.assertNotIn("libc6:i386:i386", built["recipe"])

    def test_frozen_build_failure_has_no_unlocked_fallback(self):
        with self.assertRaisesRegex(BackendError, "no unlocked fallback"):
            self.prepare(locked=INVENTORY, status=100)

    def test_frozen_build_requires_digest_before_touching_docker(self):
        with patch.object(self.backend, "resolve_image") as resolve:
            with self.assertRaisesRegex(BackendError, "pinned base image digest"):
                self.backend.prepare_image(REFERENCE, [], locked_packages=INVENTORY)
        resolve.assert_not_called()

    def test_input_injection_is_rejected_before_resolving_the_base(self):
        for package in ("--help", "git;id", "git\nRUN touch /tmp/oops", "$(id)"):
            with self.subTest(package=package):
                with patch.object(self.backend, "resolve_image") as resolve:
                    with self.assertRaises(BackendError):
                        self.backend.prepare_image(REFERENCE, [package])
                resolve.assert_not_called()
        bad_inventory = [{**INVENTORY[0], "version": "1; touch /tmp/oops"}]
        with patch.object(self.backend, "resolve_image") as resolve:
            with self.assertRaises(BackendError):
                self.backend.prepare_image(DIGEST, [], locked_packages=bad_inventory)
        resolve.assert_not_called()

    def test_duplicate_locked_packages_are_rejected(self):
        with self.assertRaisesRegex(BackendError, "Duplicate package"):
            self.backend.prepare_image(DIGEST, [], locked_packages=[INVENTORY[0], INVENTORY[0]])

    def test_inventory_records_installed_and_held_packages_but_excludes_removed_packages(self):
        output = (
            "rc \tremoved\t1.0\tamd64\n"
            "ii \tzlib1g\t1:1.3.1-1\tamd64\n"
            "hi \tbase-files\t13ubuntu10.3\tamd64\n"
        )
        inventory = self.backend._parse_inventory(output)
        self.assertEqual([package["name"] for package in inventory], ["base-files", "zlib1g"])
        with self.assertRaisesRegex(BackendError, "malformed"):
            self.backend._parse_inventory("ii \tonly-name\n")

    def test_inventory_exec_targets_owned_container_with_nonroot_uid(self):
        state = {"uid": 1000, "gid": 1000}
        with (
            patch.object(self.backend, "ensure_running"),
            patch.object(self.backend, "_required", return_value={"Id": "verified-id"}),
            patch.object(
                self.backend, "_docker", return_value=self.result("ii \tbase-files\t1\tamd64\n")
            ) as docker,
        ):
            self.assertEqual(self.backend.package_inventory(state)[0]["name"], "base-files")
        command = docker.call_args.args[0]
        self.assertEqual(command[:5], ["container", "exec", "--user", "1000:1000", "verified-id"])
        self.assertIn("dpkg-query", command)

    def test_foreign_container_cannot_be_inventoried(self):
        with (
            patch.object(self.backend, "ensure_running", side_effect=BackendError("foreign owner")),
            patch.object(self.backend, "_docker") as docker,
        ):
            with self.assertRaisesRegex(BackendError, "foreign owner"):
                self.backend.package_inventory({"uid": 1000, "gid": 1000})
        docker.assert_not_called()

    def test_image_miss_is_distinct_from_daemon_failure(self):
        with patch.object(
            self.backend,
            "_docker",
            return_value=self.result(code=1, stderr="No such image: missing"),
        ):
            self.assertIsNone(self.backend.image_info(IMAGE_ID))
        with patch.object(
            self.backend,
            "_docker",
            return_value=self.result(code=1, stderr="Cannot connect to the Docker daemon"),
        ):
            with self.assertRaisesRegex(BackendError, "Cannot inspect Docker image"):
                self.backend.image_info(IMAGE_ID)

    def test_cache_list_excludes_foreign_labels_and_deduplicates_ids(self):
        owned = self.image_info(
            IMAGE_ID, {"io.rosenv.cache": "1", "io.rosenv.uid": str(os.getuid())}
        )
        foreign = self.image_info(BASE_ID, {"io.rosenv.cache": "1", "io.rosenv.uid": "other-user"})
        with (
            patch.object(
                self.backend,
                "_docker",
                return_value=self.result(f"{IMAGE_ID}\n{BASE_ID}\n{IMAGE_ID}\n"),
            ),
            patch.object(
                self.backend,
                "image_info",
                side_effect=lambda identity: owned if identity == IMAGE_ID else foreign,
            ),
        ):
            images = self.backend.cache_list()
        self.assertEqual([image["id"] for image in images], [IMAGE_ID])

    def test_cache_clean_never_forces_or_prunes_and_reports_in_use_images(self):
        owned = self.image_info(
            IMAGE_ID, {"io.rosenv.cache": "1", "io.rosenv.uid": str(os.getuid())}
        )
        with (
            patch.object(self.backend, "cache_list", return_value=[{"id": IMAGE_ID}]),
            patch.object(self.backend, "image_info", return_value=owned),
            patch.object(
                self.backend,
                "_docker",
                return_value=self.result(code=1, stderr="image is being used by running container"),
            ) as docker,
        ):
            result = self.backend.cache_clean()
        self.assertEqual(result["removed"], [])
        self.assertIn("used by running container", result["skipped"][0]["reason"])
        self.assertEqual(docker.call_args.args[0], ["image", "rm", "--no-prune", IMAGE_ID])
        self.assertFalse(docker.call_args.kwargs["check"])

    def test_cache_clean_rechecks_ownership_before_deleting(self):
        with (
            patch.object(self.backend, "cache_list", return_value=[{"id": IMAGE_ID}]),
            patch.object(self.backend, "image_info", return_value=self.image_info(IMAGE_ID)),
            patch.object(self.backend, "_docker") as docker,
        ):
            result = self.backend.cache_clean()
        docker.assert_not_called()
        self.assertEqual(result["skipped"][0]["reason"], "ownership labels changed")

    def test_run_preserves_nested_workspace_cwd(self):
        state = {"uid": 1000, "gid": 1000, "ros_distro": "jazzy"}
        with (
            patch.object(self.backend, "ensure_running"),
            patch.object(self.backend, "_required", return_value={"Id": "owned"}),
            patch.object(self.backend, "_stream", return_value=0) as stream,
        ):
            self.backend.run(state, ["pwd"], workdir="/workspace/src/package")
        command = stream.call_args.args[0]
        self.assertEqual(command[command.index("--workdir") + 1], "/workspace/src/package")

    def test_run_refuses_workdir_escape_before_engine_interaction(self):
        for workdir in (
            "/",
            "/tmp",
            "relative",
            "/workspace/../tmp",
            "/workspace-other",
            "/workspace\0",
        ):
            with self.subTest(workdir=workdir):
                with patch.object(self.backend, "ensure_running") as ensure:
                    with self.assertRaisesRegex(BackendError, "under /workspace"):
                        self.backend.run({}, ["pwd"], workdir=workdir)
                ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
