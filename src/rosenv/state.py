"""Small, atomic per-user registry. Never store credentials here."""

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .catalog import DISTROS

SELECTOR = ".rosenv-version"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}\Z")


class StateError(Exception):
    pass


def validate_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise StateError(
            "Names must be 1–48 lowercase letters, digits, '_' or '-', starting with a letter or digit."
        )
    return name


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path | None = None):
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        self.root = (
            (root or Path(os.environ.get("ROSENV_HOME", str(base / "rosenv"))))
            .expanduser()
            .resolve()
        )
        self.envs = self.root / "envs"

    @contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / ".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def env_dir(self, name: str) -> Path:
        return self.envs / validate_name(name)

    def path(self, name: str) -> Path:
        return self.env_dir(name) / "environment.json"

    def load(self, name: str) -> dict:
        path = self.path(name)
        try:
            data = json.loads(path.read_text())
            self.validate(data, name)
            return data
        except FileNotFoundError:
            raise StateError(
                f"Environment '{name}' does not exist. Use 'rosenv create {name}'."
            ) from None
        except (ValueError, KeyError, TypeError) as exc:
            raise StateError(f"Invalid environment metadata in {path}: {exc}") from None

    def validate(self, data: dict, name: str) -> None:
        if data["schema"] != 1 or data["name"] != name or data["uid"] != os.getuid():
            raise ValueError("schema, name or owner mismatch")
        if not re.fullmatch(r"[0-9a-f]{32}", data["token"]):
            raise ValueError("invalid ownership token")
        expected = f"rosenv-{data['uid']}-{name}-{data['token'][:12]}"
        if data["container"] != expected:
            raise ValueError("invalid container name")
        if data["ros_distro"] not in DISTROS or data["network"] not in {"bridge", "host", "none"}:
            raise ValueError("invalid ROS distro or network mode")
        if type(data["domain_id"]) is not int or not 0 <= data["domain_id"] <= 232:
            raise ValueError("invalid ROS domain ID")
        if type(data["gid"]) is not int or data["gid"] < 0:
            raise ValueError("invalid group ID")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", data["image_id"]):
            raise ValueError("invalid pinned image ID")
        if not isinstance(data["engine_id"], str) or not data["engine_id"].strip():
            raise ValueError("invalid Docker daemon identity")
        if Path(data["home"]) != self.env_dir(name) / "home":
            raise ValueError("invalid environment home")
        if not Path(data["workspace"]).is_absolute():
            raise ValueError("workspace must be absolute")

    def save(self, data: dict) -> None:
        self.validate(data, data["name"])
        atomic_write(self.path(data["name"]), json.dumps(data, indent=2, sort_keys=True) + "\n")

    def list(self) -> list[dict]:
        if not self.envs.exists():
            return []
        return [
            self.load(path.parent.name) for path in sorted(self.envs.glob("*/environment.json"))
        ]

    def select(self, explicit: str | None = None, cwd: Path | None = None) -> str:
        if explicit:
            return validate_name(explicit)
        if os.environ.get("ROSENV_ENV"):
            return validate_name(os.environ["ROSENV_ENV"])
        here = (cwd or Path.cwd()).resolve()
        for directory in (here, *here.parents):
            selector = directory / SELECTOR
            if selector.exists():
                return validate_name(selector.read_text().strip())
        default = self.root / "default"
        if default.exists():
            return validate_name(default.read_text().strip())
        raise StateError(
            "No environment selected. Use 'rosenv local NAME', 'rosenv global NAME', or '-e NAME'."
        )

    def set_local(self, name: str, cwd: Path | None = None) -> Path:
        self.load(name)
        target = (cwd or Path.cwd()) / SELECTOR
        atomic_write(target, name + "\n")
        return target

    def set_global(self, name: str) -> None:
        self.load(name)
        atomic_write(self.root / "default", name + "\n")
