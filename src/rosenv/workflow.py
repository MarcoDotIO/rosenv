"""Project locks and recoverable, exact environment synchronization."""

import hashlib
import json
import os
import platform
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .backend import BackendError, DockerBackend
from .catalog import DISTROS, image_for
from .project import Project, ProjectError, load, normalize_package, project_lock
from .runtime import HOST_SYSTEMS, resolve_devices
from .state import Store, atomic_write

_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SPEC = re.compile(r"[a-z0-9][a-z0-9+.-]*\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+:~_-]*\Z")


def digest(data: dict) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def environment_name(project: Project) -> str:
    identity = project.root if project.manifest_name == "rosenv.toml" else project.path
    location = hashlib.sha256(str(identity).encode()).hexdigest()[:10]
    return f"{project.name[:32]}-{location}"


def read_lock(project: Project) -> dict:
    path = project.lock_path
    try:
        lock = json.loads(path.read_text())
        if (
            not isinstance(lock, dict)
            or type(lock["schema"]) is not int
            or lock["schema"] not in {1, 2}
            or lock["ros"] not in DISTROS
        ):
            raise ValueError("unsupported schema or ROS distribution")
        if lock["variant"] not in {"ros-base", "ros-core", "perception"}:
            raise ValueError("invalid image variant")
        if not isinstance(lock["groups"], list) or not all(
            isinstance(g, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", g)
            for g in lock["groups"]
        ):
            raise ValueError("invalid dependency groups")
        if lock["schema"] == 1:
            settings = lock["environment"]
            if not isinstance(settings, dict) or settings["network"] not in {
                "bridge",
                "host",
                "none",
            }:
                raise ValueError("invalid environment network")
            domain = settings["domain_id"]
            if domain is not None and (type(domain) is not int or not 0 <= domain <= 232):
                raise ValueError("invalid environment domain ID")
        if not re.fullmatch(r"[0-9a-f]{64}", lock["fingerprint"]):
            raise ValueError("invalid manifest fingerprint")
        if not _ID.fullmatch(lock["image_id"]) or not _ID.fullmatch(lock["base_id"]):
            raise ValueError("invalid image identity")
        if not re.fullmatch(r"(?:docker.io/library/)?ros@sha256:[0-9a-f]{64}", lock["base_digest"]):
            raise ValueError("base must be a digest of the official ROS image")
        if lock["os"] != "linux" or lock["architecture"] not in {"amd64", "arm64"}:
            raise ValueError("unsupported locked image platform")
        if not isinstance(lock["packages"], list) or not lock["packages"]:
            raise ValueError("empty package inventory")
        seen = set()
        for package in lock["packages"]:
            if not _SPEC.fullmatch(package["name"]) or not _VERSION.fullmatch(package["version"]):
                raise ValueError("invalid locked package")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", package["architecture"]):
                raise ValueError("invalid package architecture")
            key = (package["name"], package["architecture"])
            if key in seen:
                raise ValueError("duplicate locked package")
            seen.add(key)
        if not isinstance(lock["requested"], list) or not all(
            isinstance(x, str) for x in lock["requested"]
        ):
            raise ValueError("invalid requested dependencies")
        for dependency in lock["requested"]:
            normalize_package(lock["ros"], "apt:" + dependency)
        if lock["schema"] == 2 and lock["fingerprint"] != dependency_fingerprint(lock):
            raise ValueError("dependency fingerprint does not match the locked request")
        return lock
    except FileNotFoundError:
        raise ProjectError(f"No {path.name} found. Run 'rosenv lock' or 'rosenv sync'.") from None
    except (ValueError, TypeError, KeyError) as exc:
        raise ProjectError(f"Invalid {path}: {exc}") from None


def dependency_fingerprint(lock: dict) -> str:
    return digest(
        {
            "schema": 2,
            "distro": lock["ros"],
            "variant": lock["variant"],
            "dependencies": sorted(lock["requested"]),
            "groups": sorted(lock["groups"]),
        }
    )


def package_signature(lock: dict) -> str:
    # Ignore timestamps, generator versions, and legacy runtime configuration.
    return digest(
        {
            key: lock[key]
            for key in (
                "ros",
                "variant",
                "base_id",
                "base_digest",
                "architecture",
                "os",
                "packages",
            )
        }
    )


def runtime_settings(project: Project) -> dict:
    try:
        devices = resolve_devices(project.devices)
    except ValueError as exc:
        raise ProjectError(str(exc)) from None
    return {"network": project.network, "domain_id": project.domain_id, "devices": devices}


def cached_image(lock: dict, old: dict | None, backend: DockerBackend) -> dict | None:
    image = backend.image_info(lock["image_id"])
    if image is None and old and old.get("package_signature") == package_signature(lock):
        # Exact reconstruction may produce a different image ID from the portable lock.
        # Reuse that verified image for subsequent runtime-only replacements.
        if old["image_id"] != lock["image_id"]:
            image = backend.image_info(old["image_id"])
    return image


def lock_project(
    project: Project,
    backend: DockerBackend,
    *,
    groups=None,
    no_dev=False,
    check=False,
    upgrade=False,
) -> dict:
    with project_lock(project.root):
        project = load(project.path)
        fingerprint = project.fingerprint(groups or [], no_dev=no_dev)
        existing = read_lock(project) if project.lock_path.exists() else None
        if existing and dependency_fingerprint(existing) == fingerprint and not upgrade:
            if existing["schema"] == 1 and not check:
                existing = {
                    **existing,
                    "schema": 2,
                    "fingerprint": fingerprint,
                    "generator": f"rosenv {__version__}",
                }
                existing.pop("environment", None)
                atomic_write(
                    project.lock_path, json.dumps(existing, indent=2, sort_keys=True) + "\n"
                )
            return existing
        if check:
            raise ProjectError(
                f"{project.lock_path.name} is missing or stale for these dependencies/groups. Run 'rosenv lock'."
            )
        prepared = backend.prepare_image(
            image_for(project.distro, project.variant),
            project.effective_dependencies(groups or [], no_dev=no_dev),
            fingerprint=fingerprint,
        )
        lock = {
            "schema": 2,
            "generator": f"rosenv {__version__}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "fingerprint": fingerprint,
            "ros": project.distro,
            "variant": project.variant,
            "groups": project.selected_groups(groups or [], no_dev=no_dev),
            "requested": project.effective_dependencies(groups or [], no_dev=no_dev),
            "base_digest": prepared["base_digest"],
            "base_id": prepared["base_id"],
            "image_id": prepared["id"],
            "architecture": prepared["architecture"],
            "os": prepared["os"],
            "packages": prepared["packages"],
        }
        atomic_write(project.lock_path, json.dumps(lock, indent=2, sort_keys=True) + "\n")
        return lock


def _inventory_key(packages):
    return sorted((p["name"], p["version"], p["architecture"]) for p in packages)


def clean_retired(state: dict, store: Store, backend: DockerBackend) -> dict:
    retired = state.get("retired")
    if retired:
        if retired.get("retired"):
            raise ProjectError("Invalid nested retired environment metadata.")
        store.validate(retired, state["name"])
        backend.remove(retired)
        state.pop("retired")
        store.save(state)
    return state


def sync_project(
    project: Project,
    store: Store,
    backend: DockerBackend,
    *,
    groups=None,
    no_dev=False,
    locked=False,
    frozen=False,
) -> dict:
    if platform.system() not in HOST_SYSTEMS:
        raise ProjectError(
            "Sync requires Linux or macOS with Docker. Project manifests can be edited on any Unix host."
        )
    if project.root.is_relative_to(store.root):
        raise ProjectError("Project must be outside ROSENV_HOME.")
    with project_lock(project.root):
        project = load(project.path)
        # Fail on disconnected or invalid devices before resolving packages or replacing anything.
        runtime = runtime_settings(project)
        lock = (
            read_lock(project)
            if frozen
            else lock_project(project, backend, groups=groups, no_dev=no_dev, check=locked)
        )
        if frozen and lock["schema"] == 1:
            runtime.update(lock["environment"])
        host_arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
            platform.machine()
        )
        if lock["architecture"] != host_arch:
            raise ProjectError(
                f"Lock targets {lock['architecture']}; this host is {host_arch}. Generate a lock for this architecture."
            )
        name = environment_name(project)
        signature = package_signature(lock)
        container_signature = digest({"network": runtime["network"], "devices": runtime["devices"]})
        with store.lock():
            old = store.load(name) if store.path(name).exists() else None
            if old:
                if old.get("project_root") != str(project.root):
                    raise ProjectError(f"Environment name collision: {name}")
                if old.get("ready") is False:
                    previous = old.get("retired")
                    if previous:
                        store.validate(previous, name)
                    backend.remove(old)
                    if previous:
                        store.save(previous)
                    else:
                        store.path(name).unlink()
                    old = previous
            if old:
                old = clean_retired(old, store, backend)
                info = backend.inspect(old)  # Verify engine/ownership even if lock changed.
                same_packages = old.get("package_signature") == signature or (
                    old.get("package_signature") is None
                    and old["image_id"] == lock["image_id"]
                    and old["ros_distro"] == lock["ros"]
                )
                same_runtime = old.get("container_signature") == container_signature or (
                    old.get("container_signature") is None
                    and old["network"] == runtime["network"]
                    and not runtime["devices"]
                )
                if info and same_packages and same_runtime:
                    backend.ensure_running(old)
                    if _inventory_key(backend.package_inventory(old)) == _inventory_key(
                        lock["packages"]
                    ):
                        updated = {
                            **old,
                            "domain_id": runtime["domain_id"]
                            if runtime["domain_id"] is not None
                            else old["domain_id"],
                            "package_signature": signature,
                            "container_signature": container_signature,
                            "requested_packages": lock["requested"],
                            "lock_signature": digest(lock),
                            "project_manifest": str(project.path),
                            "devices": runtime["devices"],
                        }
                        if updated != old:
                            store.save(updated)
                        return updated
            image = cached_image(lock, old, backend)
            if image is None:
                image = backend.prepare_image(
                    lock["base_digest"],
                    lock["requested"],
                    locked_packages=lock["packages"],
                    fingerprint=lock["fingerprint"],
                )
                if (
                    image["base_id"] != lock["base_id"]
                    or image["architecture"] != lock["architecture"]
                ):
                    raise ProjectError("Rebuilt image does not match the locked base/platform.")
            domain = runtime["domain_id"]
            if domain is None:
                if old:
                    domain = old["domain_id"]
                else:
                    used = {entry["domain_id"] for entry in store.list()}
                    domain = next((i for i in range(1, 101) if i not in used), None)
                    if domain is None:
                        raise ProjectError("No free ROS domain ID; set environment.domain-id.")
            token = uuid.uuid4().hex
            state = {
                "schema": 1,
                "name": name,
                "token": token,
                "container": f"rosenv-{os.getuid()}-{name}-{token[:12]}",
                "ros_distro": lock["ros"],
                "image": lock["base_digest"],
                "image_id": image["id"],
                "image_digests": image.get("digests", []),
                "engine_id": backend.engine_id(),
                "workspace": str(project.root),
                "home": str(store.env_dir(name) / "home"),
                "uid": os.getuid(),
                "gid": os.getgid(),
                "network": runtime["network"],
                "devices": runtime["devices"],
                "domain_id": domain,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "project_root": str(project.root),
                "lock_signature": digest(lock),
                "package_signature": signature,
                "container_signature": container_signature,
                "project_manifest": str(project.path),
                "requested_packages": lock["requested"],
                "ready": False,
            }
            Path(state["home"]).mkdir(parents=True, exist_ok=True, mode=0o700)
            # Journal the candidate before creation; interrupted replacements remain recoverable.
            if old:
                state["retired"] = old
            store.save(state)
            try:
                backend.create(state)
                state["ready"] = True
                store.save(state)
            except BaseException:
                try:
                    backend.remove(state)
                except (BackendError, OSError):
                    pass  # Journal retains both resources for a retry/remove.
                else:
                    if old:
                        store.save(old)
                    else:
                        shutil.rmtree(store.env_dir(name))
                raise
            clean_retired(state, store, backend)
            return state


def plan_sync(
    project: Project,
    store: Store,
    backend: DockerBackend,
    *,
    groups=None,
    no_dev=False,
    locked=False,
    frozen=False,
) -> str:
    """Read configuration and resource metadata without starting or changing resources."""
    project = load(project.path)
    runtime = runtime_settings(project)
    lock = read_lock(project) if project.lock_path.exists() else None
    if frozen and lock is None:
        read_lock(project)  # Give the same missing-lock diagnostic as sync.
    if not frozen and (
        lock is None or dependency_fingerprint(lock) != project.fingerprint(groups, no_dev)
    ):
        if locked:
            raise ProjectError(
                f"{project.lock_path.name} is missing or stale for these dependencies/groups."
            )
        return "Resolve dependencies and prepare an image: package lock is missing or stale."
    if frozen and lock["schema"] == 1:
        runtime.update(lock["environment"])
    name = environment_name(project)
    old = store.load(name) if store.path(name).exists() else None
    if old and (old.get("ready") is False or old.get("retired")):
        return (
            "Recover interrupted synchronization; run rosenv sync to reconcile recorded resources."
        )
    reasons = []
    if old:
        if old.get("project_root") != str(project.root):
            raise ProjectError(f"Environment name collision: {name}")
        info = backend.inspect(old)
        if not info:
            reasons.append("container is missing")
        if old["network"] != runtime["network"]:
            reasons.append("network changed")
        if old.get("devices", []) != runtime["devices"]:
            reasons.append("devices or their host identities changed")
        if (
            old.get("package_signature") != package_signature(lock)
            and old["image_id"] != lock["image_id"]
        ):
            reasons.append("locked packages changed")
        if not reasons:
            return (
                "Reuse container if its package inventory still matches; apply current command variables/domain ID. "
                "Inventory verification is deferred to sync; no container was started."
            )
    image = cached_image(lock, old, backend)
    action = "Replace container (" + ", ".join(reasons) + ")" if old else "Create container"
    return action + (
        " using the cached dependency image."
        if image
        else " after reconstructing the exact locked dependency image (cache miss)."
    )


def existing_project_environment(project: Project, store: Store) -> dict:
    state = store.load(environment_name(project))
    if state.get("project_root") != str(project.root):
        raise ProjectError("Selected environment belongs to another project.")
    if state.get("ready") is False or state.get("retired"):
        raise ProjectError(
            "Project environment has an interrupted sync. Run 'rosenv sync' to recover."
        )
    return state


def command_workdir(project: Project) -> str:
    try:
        relative = Path.cwd().resolve().relative_to(project.root)
    except ValueError:
        return "/workspace"
    return str(Path("/workspace") / relative)


def export_lock(lock: dict, format: str) -> str:
    if format == "json":
        return json.dumps(lock, indent=2, sort_keys=True) + "\n"
    requirements = [
        f"{p['name']}{(':' + p['architecture']) if p['architecture'] != 'all' else ''}={p['version']}"
        for p in lock["packages"]
    ]
    if format == "requirements":
        return "\n".join(requirements) + "\n"
    # Names and versions have been validated before interpolation.
    continuation = " " + chr(92) + "\n    "
    return (
        "# Generated by rosenv; old versions may expire from upstream repositories.\n"
        f"FROM {lock['base_digest']}\n"
        "ENV DEBIAN_FRONTEND=noninteractive\n"
        "RUN apt-get update && apt-get install -y --allow-downgrades --no-install-recommends --"
        + continuation
        + continuation.join(requirements)
        + "\n"
    )
