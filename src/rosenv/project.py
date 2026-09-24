"""Validated project manifests, comment-preserving edits, and semantic fingerprints."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import tomllib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import tomlkit

from .catalog import DISTROS
from .state import StateError, atomic_write, validate_name

MANIFEST = "rosenv.toml"
VARIANTS = {"ros-base", "ros-core", "perception"}
NETWORKS = {"bridge", "host", "none"}
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?\Z")
_ROS_PACKAGE = re.compile(r"[a-z][a-z0-9_-]*\Z")
_VERSION = re.compile(r"[0-9][A-Za-z0-9.+:~\-]*\Z")
_LOCKS = threading.local()


class ProjectError(Exception):
    """A project manifest or requested project operation is invalid."""


def _name(value: Any, description: str) -> str:
    if not isinstance(value, str):
        raise ProjectError(f"{description} must be a string.")
    try:
        return validate_name(value)
    except StateError as exc:
        raise ProjectError(f"Invalid {description}: {exc}") from None


def normalize_package(distro: str, value: str) -> str:
    """Return a safe Debian package argument, optionally with an exact version."""
    if not isinstance(distro, str) or distro not in DISTROS:
        raise ProjectError(f"Unknown ROS distribution: {distro!r}.")
    if not isinstance(value, str) or not value:
        raise ProjectError("Dependencies must be nonempty strings.")
    package, separator, version = value.partition("=")
    if separator and not _VERSION.fullmatch(version):
        raise ProjectError(f"Invalid exact Debian version in dependency {value!r}.")
    if package.startswith("apt:"):
        package = package[4:]
    elif package.startswith("ros-"):
        if not package.startswith(f"ros-{distro}-"):
            raise ProjectError(
                f"Dependency {value!r} targets a different ROS distribution; "
                f"use a ROS package shorthand for {distro}, or apt: for an explicit Debian package."
            )
        if not _ROS_PACKAGE.fullmatch(package[len(f"ros-{distro}-") :]):
            raise ProjectError(f"Invalid ROS dependency: {value!r}.")
    else:
        if not _ROS_PACKAGE.fullmatch(package):
            raise ProjectError(f"Invalid ROS dependency: {value!r}.")
        package = f"ros-{distro}-{package.replace('_', '-')}"
    if not _PACKAGE.fullmatch(package):
        raise ProjectError(f"Invalid Debian dependency: {value!r}.")
    return package + (f"={version}" if separator else "")


def _merge_specs(specs: Sequence[str]) -> list[str]:
    chosen: dict[str, str] = {}
    for spec in specs:
        package, separator, _ = spec.partition("=")
        previous = chosen.get(package)
        if previous is not None and previous != spec and "=" in previous and separator:
            raise ProjectError(f"Conflicting exact versions for {package}: {previous} and {spec}.")
        if previous is None or separator:
            chosen[package] = spec
    return sorted(chosen.values())


@contextmanager
def project_lock(root: Path | str) -> Iterator[None]:
    """Serialize manifest and lockfile transactions; reentrant in the same thread.

    Callers should take this lock before loading project state for a transaction.
    The lock file is intentionally retained so waiters keep using the same inode.
    """
    directory = Path(root).expanduser().resolve()
    if not directory.is_dir():
        raise ProjectError(f"Project directory does not exist: {directory}")
    held = getattr(_LOCKS, "held", None)
    if held is None:
        held = _LOCKS.held = set()
    if directory in held:
        yield
        return
    try:
        fd = os.open(
            directory / ".rosenv.edit.lock",
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise ProjectError(f"Cannot open project edit lock in {directory}: {exc}") from exc
    with os.fdopen(fd, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        held.add(directory)
        try:
            yield
        finally:
            held.remove(directory)
            fcntl.flock(handle, fcntl.LOCK_UN)


def _table(data: dict, key: str, allowed: set[str] | None = None) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ProjectError(f"[{key}] must be a TOML table.")
    if allowed is not None:
        unknown = set(value) - allowed
        if unknown:
            raise ProjectError(f"Unknown field(s) in [{key}]: {', '.join(sorted(unknown))}.")
    return value


def _array_update(table: Any, key: str, values: list[str]) -> None:
    """Edit only changed array elements, retaining surrounding TOML trivia."""
    if key not in table:
        table[key] = values
        return
    array = table[key]
    original = list(array)
    for operation, i1, i2, j1, j2 in reversed(
        SequenceMatcher(a=original, b=values, autojunk=False).get_opcodes()
    ):
        if operation == "equal":
            continue
        shared = min(i2 - i1, j2 - j1)
        for offset in range(shared):
            array[i1 + offset] = values[j1 + offset]
        for index in range(i2 - 1, i1 + shared - 1, -1):
            del array[index]
        for offset, value in enumerate(values[j1 + shared : j2]):
            array.insert(i1 + shared + offset, value)


@dataclass
class Project:
    root: Path
    name: str
    distro: str = "jazzy"
    variant: str = "ros-base"
    dependencies: list[str] = field(default_factory=list)
    groups: dict[str, list[str]] = field(default_factory=dict)
    tasks: dict[str, list[str]] = field(default_factory=dict)
    network: str = "bridge"
    domain_id: int | None = None
    _source: str | None = field(default=None, repr=False, compare=False)
    _saved: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        self._validate()

    @property
    def path(self) -> Path:
        return self.root / MANIFEST

    def edit_lock(self):
        return project_lock(self.root)

    def _validate(self) -> None:
        _name(self.name, "project name")
        if not isinstance(self.distro, str) or self.distro not in DISTROS:
            raise ProjectError(f"Unknown ROS distribution: {self.distro!r}.")
        if not isinstance(self.variant, str) or self.variant not in VARIANTS:
            raise ProjectError(f"Unknown image variant: {self.variant!r}.")
        if not isinstance(self.network, str) or self.network not in NETWORKS:
            raise ProjectError("environment.network must be bridge, host, or none.")
        if self.domain_id is not None and (
            type(self.domain_id) is not int or not 0 <= self.domain_id <= 232
        ):
            raise ProjectError("environment.domain-id must be an integer between 0 and 232.")
        self._validate_dependencies(self.dependencies, "project.dependencies")
        if not isinstance(self.groups, dict):
            raise ProjectError("[dependency-groups] must be a table of dependency arrays.")
        for name, dependencies in self.groups.items():
            _name(name, "dependency group name")
            self._validate_dependencies(dependencies, f"dependency-groups.{name}")
        if not isinstance(self.tasks, dict):
            raise ProjectError("[tasks] must be a table of argument arrays.")
        for name, command in self.tasks.items():
            _name(name, "task name")
            if (
                not isinstance(command, list)
                or not command
                or any(not isinstance(arg, str) or "\0" in arg for arg in command)
                or not command[0]
                or command[0].startswith("-")
            ):
                raise ProjectError(
                    f"tasks.{name} must be a nonempty argv array with a valid executable; "
                    "commands are not interpreted by a shell."
                )

    def _validate_dependencies(self, values: Any, location: str) -> None:
        if not isinstance(values, list):
            raise ProjectError(f"{location} must be an array of dependency strings.")
        try:
            _merge_specs([normalize_package(self.distro, value) for value in values])
        except ProjectError as exc:
            raise ProjectError(f"{location}: {exc}") from None

    def _spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "distro": self.distro,
            "variant": self.variant,
            "dependencies": list(self.dependencies),
            "groups": {name: list(values) for name, values in self.groups.items()},
            "tasks": {name: list(values) for name, values in self.tasks.items()},
            "network": self.network,
            "domain_id": self.domain_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"root": str(self.root), **self._spec()}

    def selected_groups(
        self, groups: Sequence[str] | None = None, no_dev: bool = False
    ) -> list[str]:
        if groups is None:
            groups = ()
        if isinstance(groups, str):
            raise ProjectError("Dependency groups must be a sequence of group names.")
        chosen = {_name(name, "dependency group name") for name in groups}
        if not no_dev and "dev" in self.groups:
            chosen.add("dev")
        for name in chosen:
            _name(name, "dependency group name")
            if name not in self.groups:
                raise ProjectError(f"Unknown dependency group: {name!r}.")
        return sorted(chosen)

    def effective_dependencies(
        self, groups: Sequence[str] | None = None, no_dev: bool = False
    ) -> list[str]:
        self._validate()
        selected = self.selected_groups(groups, no_dev)
        values = [*self.dependencies]
        for name in selected:
            values.extend(self.groups[name])
        return _merge_specs([normalize_package(self.distro, value) for value in values])

    def fingerprint(self, groups: Sequence[str] | None = None, no_dev: bool = False) -> str:
        spec = {
            "schema": 1,
            "distro": self.distro,
            "variant": self.variant,
            "dependencies": self.effective_dependencies(groups, no_dev),
            "groups": self.selected_groups(groups, no_dev),
            "network": self.network,
            "domain_id": self.domain_id,
        }
        return hashlib.sha256(
            json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _render(self) -> str:
        document = tomlkit.parse(self._source) if self._source is not None else tomlkit.document()
        spec = self._spec()
        if "project" not in document:
            document["project"] = tomlkit.table()
        for key, attribute in (("name", "name"), ("ros", "distro"), ("variant", "variant")):
            if self._saved.get(attribute) != spec[attribute]:
                document["project"][key] = spec[attribute]
        if self._saved.get("dependencies") != self.dependencies:
            _array_update(document["project"], "dependencies", self.dependencies)
        for section, attribute in (("dependency-groups", "groups"), ("tasks", "tasks")):
            values = spec[attribute]
            if self._saved.get(attribute, {}) == values:
                continue
            if section not in document:
                document[section] = tomlkit.table()
            table = document[section]
            for key in list(table):
                if key not in values:
                    del table[key]
            for key, array in values.items():
                _array_update(table, key, array)
        for key, attribute in (("network", "network"), ("domain-id", "domain_id")):
            if attribute in self._saved and self._saved[attribute] == spec[attribute]:
                continue
            value = spec[attribute]
            if value is None:
                if "environment" in document:
                    document["environment"].pop(key, None)
            else:
                if "environment" not in document:
                    document["environment"] = tomlkit.table()
                document["environment"][key] = value
        return tomlkit.dumps(document)

    def save(self) -> Project:
        """Persist edits without overwriting changes made since this instance loaded."""
        self._validate()
        with self.edit_lock():
            try:
                current = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                current = None
            if current != self._source:
                raise ProjectError(
                    f"{self.path} changed since it was loaded; reload before editing."
                )
            if self._source is not None and self._saved == self._spec():
                return self
            text = self._render()
            if current is None:
                _write_new(self.path, text)
            else:
                atomic_write(self.path, text)
            self._source = text
            self._saved = self._spec()
        return self

    def add_dependencies(self, values: Sequence[str], group: str | None = None) -> Project:
        if isinstance(values, str) or not values:
            raise ProjectError("Provide one or more dependency strings.")
        normalized = [normalize_package(self.distro, value) for value in values]
        _merge_specs(normalized)
        if group is not None:
            _name(group, "dependency group name")
        target = list(self.dependencies if group is None else self.groups.get(group, []))
        for value, spec in zip(values, normalized, strict=True):
            name = spec.partition("=")[0]
            matches = [
                index
                for index, old in enumerate(target)
                if normalize_package(self.distro, old).partition("=")[0] == name
            ]
            if matches:
                target[matches[0]] = value
                for index in reversed(matches[1:]):
                    del target[index]
            else:
                target.append(value)
        return self._save_dependencies(target, group)

    def remove_dependencies(self, values: Sequence[str], group: str | None = None) -> Project:
        if isinstance(values, str) or not values:
            raise ProjectError("Provide one or more dependency strings.")
        if group is not None and group not in self.groups:
            raise ProjectError(f"Unknown dependency group: {group!r}.")
        target = list(self.dependencies if group is None else self.groups[group])
        requested = {normalize_package(self.distro, value).partition("=")[0] for value in values}
        existing = {normalize_package(self.distro, value).partition("=")[0] for value in target}
        if missing := requested - existing:
            raise ProjectError(f"Dependencies are not in this group: {', '.join(sorted(missing))}.")
        target = [
            value
            for value in target
            if normalize_package(self.distro, value).partition("=")[0] not in requested
        ]
        return self._save_dependencies(target, group)

    def _save_dependencies(self, values: list[str], group: str | None) -> Project:
        previous_dependencies, previous_groups = self.dependencies, self.groups
        if group is None:
            self.dependencies = values
        else:
            self.groups = {**self.groups, group: values}
        try:
            return self.save()
        except BaseException:
            self.dependencies, self.groups = previous_dependencies, previous_groups
            raise

    def set_distro(self, distro: str) -> Project:
        previous = self.distro
        self.distro = distro
        try:
            return self.save()
        except BaseException:
            self.distro = previous
            raise

    @classmethod
    def load(cls, root: Path | str) -> Project:
        return load(root)

    @classmethod
    def discover(cls, start: Path | str | None = None) -> Project | None:
        return discover(start)

    @classmethod
    def initialize(
        cls,
        root: Path | str,
        name: str,
        distro: str = "jazzy",
        variant: str = "ros-base",
        network: str = "bridge",
        domain_id: int | None = None,
    ) -> Project:
        return initialize(root, name, distro, variant, network, domain_id)


def _write_new(path: Path, text: str) -> None:
    """Publish a complete new file atomically without replacing any existing path."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise ProjectError(f"Project manifest already exists: {path}") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(root: Path | str) -> Project:
    directory = Path(root).expanduser().resolve()
    path = directory / MANIFEST
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        unknown = set(data) - {"project", "dependency-groups", "environment", "tasks"}
        if unknown:
            raise ProjectError(f"Unknown top-level field(s): {', '.join(sorted(unknown))}.")
        metadata = _table(data, "project", {"name", "ros", "variant", "dependencies"})
        for key in ("name", "ros"):
            if key not in metadata:
                raise ProjectError(f"[project] requires {key!r}.")
        environment = _table(data, "environment", {"network", "domain-id"})
        result = Project(
            root=directory,
            name=metadata["name"],
            distro=metadata["ros"],
            variant=metadata.get("variant", "ros-base"),
            dependencies=metadata.get("dependencies", []),
            groups=_table(data, "dependency-groups"),
            tasks=_table(data, "tasks"),
            network=environment.get("network", "bridge"),
            domain_id=environment.get("domain-id"),
        )
        result._source, result._saved = text, result._spec()
        return result
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ProjectError) as exc:
        raise ProjectError(f"Cannot load {path}: {exc}") from None


def discover(start: Path | str | None = None) -> Project | None:
    here = Path(start or Path.cwd()).expanduser().resolve()
    if here.is_file():
        here = here.parent
    for directory in (here, *here.parents):
        path = directory / MANIFEST
        if path.exists() or path.is_symlink():
            return load(directory)
    return None


def initialize(
    root: Path | str,
    name: str,
    distro: str = "jazzy",
    variant: str = "ros-base",
    network: str = "bridge",
    domain_id: int | None = None,
) -> Project:
    project = Project(
        root=Path(root),
        name=name,
        distro=distro,
        variant=variant,
        network=network,
        domain_id=domain_id,
    )
    with project.edit_lock():
        if project.path.exists() or project.path.is_symlink():
            raise ProjectError(f"Project manifest already exists: {project.path}")
        return project.save()
