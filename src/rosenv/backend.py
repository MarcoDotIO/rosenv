"""Docker-backed ROS environments with explicit resource ownership.

Containers isolate ROS distributions and their operating-system dependencies.
They are development environments, not a security boundary for hostile code:
the workspace and environment home are writable host bind mounts.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

OWNER_LABEL = "io.rosenv.uid"
TOKEN_LABEL = "io.rosenv.token"
CACHE_LABEL = "io.rosenv.cache"
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]*\Z")
_PACKAGE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+:~_-]*\Z")
_ARCHITECTURE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_INVENTORY_COMMAND = [
    "dpkg-query",
    "--show",
    "--showformat=${db:Status-Abbrev}\t${Package}\t${Version}\t${Architecture}\n",
]
_RESOURCE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_PACKAGE = re.compile(
    r"^[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?"
    r"(?:=[A-Za-z0-9][A-Za-z0-9.+:~_-]*)?$"
)

# Positional parameters, rather than shell interpolation, carry user input.
_RUN_SCRIPT = """set -e
source "/opt/ros/$1/setup.bash"
shift
if [ -f /workspace/install/setup.bash ]; then
    source /workspace/install/setup.bash
fi
exec "$@"
"""

_BOOTSTRAP_SCRIPT = """set -e
uid="$1"
gid="$2"
if ! getent group "$gid" >/dev/null; then
    groupadd --gid "$gid" "rosenv-$gid"
fi
username="$(getent passwd "$uid" | cut -d: -f1)"
if [ -z "$username" ]; then
    useradd --uid "$uid" --gid "$gid" --home-dir /home/rosenv \\
        --shell /bin/bash --no-create-home "rosenv-$uid"
fi
chown "$uid:$gid" /home/rosenv
cat > /etc/rosenv.bashrc <<'ROSENV_RC'
if [ -f /etc/bash.bashrc ]; then source /etc/bash.bashrc; fi
if [ -f "$HOME/.bashrc" ]; then source "$HOME/.bashrc"; fi
source "/opt/ros/$ROS_DISTRO/setup.bash"
if [ -f /workspace/install/setup.bash ]; then
    source /workspace/install/setup.bash
fi
PS1="(rosenv:$ROS_DISTRO) ${PS1:-\\u@\\h:\\w\\$ }"
ROSENV_RC
"""


class BackendError(Exception):
    """A container operation failed or could not be performed safely."""


class DockerBackend:
    """Manage persistent containers using the locally configured Docker engine."""

    def __init__(self, binary: str = "docker") -> None:
        self.binary = binary
        self._engine_id: str | None = None

    def engine_id(self) -> str:
        """Identify the configured daemon, once per command/backend instance."""
        if self._engine_id is None:
            identity = self._docker(["info", "--format", "{{.ID}}"]).stdout.strip()
            if not identity or not re.fullmatch(r"[A-Za-z0-9:._-]+", identity):
                raise BackendError("Docker returned an empty or invalid daemon ID.")
            self._engine_id = identity
        return self._engine_id

    def _verify_engine(self, state: dict[str, Any]) -> None:
        expected = state.get("engine_id")
        if expected is not None and self.engine_id() != expected:
            raise BackendError(
                f"Environment {state['name']!r} belongs to a different Docker daemon. "
                "Restore the Docker context or DOCKER_HOST used when it was created. "
                "No environment resources or files were removed."
            )

    def _docker(
        self, args: list[str], *, check: bool = True, timeout: float = 60
    ) -> subprocess.CompletedProcess[str]:
        """Capture control-plane output, keeping failures readable."""
        try:
            result = subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError(
                f"Docker {args[0]} timed out after {timeout:g} seconds. "
                "Check that the Docker daemon is responsive."
            ) from exc
        except OSError as exc:
            raise BackendError(
                f"Cannot run Docker ({self.binary}): {exc}. "
                "Install Docker Engine and ensure your account can access it."
            ) from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout or "no error details").strip()
            raise BackendError(f"Docker {args[0]} failed: {detail[-4000:]}")
        return result

    def _stream(self, args: list[str]) -> int:
        """Inherit terminal streams and preserve the command's exit status."""
        try:
            # The child shares the terminal's process group and therefore receives
            # terminal signals directly. Do not turn Ctrl-C into a traceback.
            result = subprocess.run([self.binary, *args], check=False)
        except KeyboardInterrupt:
            return 128 + signal.SIGINT
        except OSError as exc:
            raise BackendError(f"Cannot run Docker ({self.binary}): {exc}") from exc
        return result.returncode if result.returncode >= 0 else 128 - result.returncode

    @staticmethod
    def _json(text: str, description: str) -> Any:
        try:
            return json.loads(text)
        except (ValueError, TypeError) as exc:
            raise BackendError(f"Docker returned invalid JSON for {description}.") from exc

    @staticmethod
    def _name(state: dict[str, Any]) -> str:
        name = state["container"]
        if not isinstance(name, str) or not _RESOURCE_NAME.fullmatch(name):
            raise BackendError("Environment contains an invalid Docker resource name.")
        return name

    @staticmethod
    def _labels(state: dict[str, Any]) -> dict[str, str]:
        return {OWNER_LABEL: str(state["uid"]), TOKEN_LABEL: str(state["token"])}

    def _verify_owner(self, state: dict[str, Any], info: dict[str, Any], kind: str) -> None:
        labels = (
            info.get("Config", {}).get("Labels", {})
            if kind == "container"
            else info.get("Labels", {})
        ) or {}
        if any(labels.get(key) != value for key, value in self._labels(state).items()):
            name = self._name(state) + ("-net" if kind == "network" else "")
            raise BackendError(
                f"Refusing to use {kind} {name!r}: "
                "its ownership labels do not match this environment."
            )

    def _inspect_resource(self, kind: str, name: str) -> dict[str, Any] | None:
        result = self._docker([kind, "inspect", name], check=False)
        if result.returncode:
            # Do not mistake unreachable daemons, permissions or malformed output
            # for an absent resource; otherwise an outage could destroy state.
            errors = result.stderr.lower()
            missing = (
                f"no such {kind}:" in errors
                or "no such object:" in errors
                or (kind == "network" and f"network {name.lower()} not found" in errors)
            )
            if missing:
                return None
            detail = (result.stderr or result.stdout or "no error details").strip()
            raise BackendError(f"Cannot inspect Docker {kind}: {detail[-4000:]}")
        data = self._json(result.stdout, f"{kind} inspection")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise BackendError(f"Docker returned an unexpected {kind} inspection.")
        if not data[0].get("Id"):
            raise BackendError(f"Docker returned a {kind} without an ID.")
        return data[0]

    def inspect(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """Inspect an owned container; return None only when it is absent."""
        self._verify_engine(state)
        info = self._inspect_resource("container", self._name(state))
        if info is not None:
            self._verify_owner(state, info, "container")
        return info

    def _required(self, state: dict[str, Any]) -> dict[str, Any]:
        info = self.inspect(state)
        if info is None:
            raise BackendError(
                f"Container for environment {state['name']!r} is missing. "
                "Remove its stale environment record, then create it again. "
                "The workspace and environment home remain on disk."
            )
        return info

    def _network(self, state: dict[str, Any]) -> dict[str, Any] | None:
        self._verify_engine(state)
        info = self._inspect_resource("network", self._name(state) + "-net")
        if info is not None:
            self._verify_owner(state, info, "network")
        return info

    def _ensure_network(self, state: dict[str, Any]) -> bool:
        if self._network(state) is not None:
            return False
        args = ["network", "create", "--driver", "bridge"]
        for key, value in self._labels(state).items():
            args += ["--label", f"{key}={value}"]
        args.append(self._name(state) + "-net")
        self._docker(args)
        return True

    def _remove_network(self, state: dict[str, Any]) -> None:
        if state["network"] == "bridge":
            info = self._network(state)
            if info is not None:
                self._docker(["network", "rm", info["Id"]])

    def doctor(self) -> dict[str, Any]:
        """Verify Docker client/daemon access and report useful host details."""
        version = self._json(
            self._docker(["version", "--format", "{{json .}}"]).stdout,
            "version",
        )
        info = self._json(
            self._docker(["info", "--format", "{{json .}}"]).stdout,
            "engine information",
        )
        if not isinstance(version, dict) or not isinstance(info, dict):
            raise BackendError("Docker returned unexpected engine information.")
        return {
            "version": version.get("Server", {}).get("Version", "unknown"),
            "engine_id": self.engine_id(),
            "details": {
                "client_version": version.get("Client", {}).get("Version", "unknown"),
                "operating_system": info.get("OperatingSystem", "unknown"),
                "architecture": info.get("Architecture", "unknown"),
                "security_options": info.get("SecurityOptions", []),
                "docker_root_dir": info.get("DockerRootDir", "unknown"),
            },
        }

    def resolve_image(self, reference: str, pull: bool = True) -> dict[str, Any]:
        """Resolve an image reference to the immutable locally available ID."""
        if not reference or reference.startswith("-") or any(c.isspace() for c in reference):
            raise BackendError("Image reference must be a nonempty Docker image name.")
        if pull:
            status = self._stream(["image", "pull", reference])
            if status:
                raise BackendError(f"Docker image pull failed with exit status {status}.")
        result = self._docker(["image", "inspect", reference])
        images = self._json(result.stdout, "image inspection")
        if (
            not isinstance(images, list)
            or len(images) != 1
            or not isinstance(images[0], dict)
            or not images[0].get("Id")
        ):
            raise BackendError("Docker returned an unexpected image inspection.")
        return {
            "id": images[0]["Id"],
            "digests": images[0].get("RepoDigests") or [],
            "engine_id": self.engine_id(),
        }

    def image_info(self, image_id: str) -> dict[str, Any] | None:
        """Inspect a local image without treating daemon failures as cache misses."""
        if (
            not isinstance(image_id, str)
            or not image_id
            or image_id.startswith("-")
            or any(character.isspace() for character in image_id)
        ):
            raise BackendError("Invalid Docker image reference.")
        info = self._inspect_resource("image", image_id)
        if info is None:
            return None
        if not _IMAGE_ID.fullmatch(info["Id"]):
            raise BackendError("Docker returned an invalid immutable image ID.")
        return {
            "id": info["Id"],
            "digests": info.get("RepoDigests") or [],
            "architecture": info.get("Architecture", ""),
            "os": info.get("Os", ""),
            "variant": info.get("Variant", ""),
            "labels": (info.get("Config") or {}).get("Labels") or {},
            "size": info.get("Size", 0),
            "created": info.get("Created", ""),
            "tags": [tag for tag in info.get("RepoTags") or [] if tag != "<none>:<none>"],
        }

    @staticmethod
    def _normalize_inventory(packages: list[dict[str, str]]) -> list[dict[str, str]]:
        if not isinstance(packages, list):
            raise BackendError("Locked package inventory must be a list.")
        normalized = []
        seen = set()
        for package in packages:
            if not isinstance(package, dict):
                raise BackendError("Invalid package inventory entry.")
            entry = {key: package.get(key) for key in ("name", "version", "architecture")}
            for key, expression in (
                ("name", _PACKAGE_NAME),
                ("version", _PACKAGE_VERSION),
                ("architecture", _ARCHITECTURE),
            ):
                if not isinstance(entry[key], str) or not expression.fullmatch(entry[key]):
                    raise BackendError(f"Invalid package inventory {key}: {entry[key]!r}.")
            identity = (entry["name"], entry["architecture"])
            if identity in seen:
                raise BackendError(f"Duplicate package in inventory: {identity[0]}:{identity[1]}.")
            seen.add(identity)
            normalized.append(entry)
        return sorted(normalized, key=lambda package: (package["name"], package["architecture"]))

    @classmethod
    def _parse_inventory(cls, output: str) -> list[dict[str, str]]:
        packages = []
        for line in output.splitlines():
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) != 4 or len(fields[0]) != 3:
                raise BackendError("dpkg-query returned malformed package inventory.")
            # The second status character is the actual installed state. A held
            # installed package (hi) belongs in the inventory as well as ii.
            if fields[0][1] != "i":
                continue
            packages.append(dict(zip(("name", "version", "architecture"), fields[1:])))
        inventory = cls._normalize_inventory(packages)
        if not inventory:
            raise BackendError("The image returned no installed Debian packages.")
        return inventory

    def package_inventory(self, state: dict[str, Any]) -> list[dict[str, str]]:
        """Read all installed package versions from the owned environment."""
        self.ensure_running(state)
        info = self._required(state)
        result = self._docker(
            [
                "container",
                "exec",
                "--user",
                f"{state['uid']}:{state['gid']}",
                info["Id"],
                *_INVENTORY_COMMAND,
            ]
        )
        return self._parse_inventory(result.stdout)

    def _image_inventory(self, image_id: str) -> list[dict[str, str]]:
        if not _IMAGE_ID.fullmatch(image_id):
            raise BackendError("Package inventory requires an immutable image ID.")
        result = self._docker(
            [
                "container",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--entrypoint",
                _INVENTORY_COMMAND[0],
                image_id,
                *_INVENTORY_COMMAND[1:],
            ],
            timeout=120,
        )
        return self._parse_inventory(result.stdout)

    @staticmethod
    def _repository(reference: str) -> str:
        repository = reference.split("@", 1)[0]
        # Remove the tag, preserving a possible registry port.
        if ":" in repository.rsplit("/", 1)[-1]:
            repository = repository.rsplit(":", 1)[0]
        if not re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*", repository):
            raise BackendError("Invalid image repository in immutable base reference.")
        first = repository.split("/", 1)[0]
        if "/" not in repository or not ("." in first or ":" in first or first == "localhost"):
            repository = "docker.io/" + repository
        if repository.startswith("index.docker.io/"):
            repository = "docker.io/" + repository.split("/", 1)[1]
        if repository.startswith("docker.io/") and repository.count("/") == 1:
            repository = "docker.io/library/" + repository.split("/", 1)[1]
        return repository

    @classmethod
    def _digest_reference(cls, reference: str) -> str:
        if not isinstance(reference, str) or reference.count("@") != 1:
            raise BackendError("A fully pinned base image digest is required for reconstruction.")
        repository, digest = reference.split("@", 1)
        if not _IMAGE_ID.fullmatch(digest):
            raise BackendError("Base image digest must be an immutable SHA-256 digest.")
        return cls._repository(repository) + "@" + digest

    def prepare_image(
        self,
        reference: str,
        packages: list[str],
        *,
        locked_packages: list[dict[str, str]] | None = None,
        fingerprint: str = "",
    ) -> dict[str, Any]:
        """Build a reusable dependency image from a digest and record its inventory.

        The build context is an empty temporary directory: project files, private
        homes and credentials are never sent to Docker. Frozen reconstruction never
        falls back to an unlocked upgrade when an exact package version is unavailable.
        """
        if not isinstance(packages, list) or any(
            not isinstance(package, str) or not _PACKAGE.fullmatch(package) for package in packages
        ):
            raise BackendError("Specify valid Debian package names (optionally package=version).")
        if not isinstance(fingerprint, str):
            raise BackendError("Project fingerprint must be a string.")
        locked = None
        if locked_packages is not None:
            locked = self._normalize_inventory(locked_packages)
            if not locked:
                raise BackendError("Frozen reconstruction requires a nonempty package inventory.")
            reference = self._digest_reference(reference)
        resolved = self.resolve_image(reference)
        base = self.image_info(resolved["id"])
        if base is None:
            raise BackendError("The resolved base image disappeared before the build.")
        if (
            base["os"] != "linux"
            or not isinstance(base["architecture"], str)
            or not _ARCHITECTURE.fullmatch(base["architecture"])
        ):
            raise BackendError("Project images require a Linux base with a known architecture.")
        if locked is not None:
            base_digest = reference
        else:
            digests = [self._digest_reference(value) for value in base["digests"]]
            if not digests:
                raise BackendError("The resolved base image has no immutable registry digest.")
            repository = self._repository(reference)
            base_digest = next(
                (digest for digest in digests if digest.split("@", 1)[0] == repository), digests[0]
            )
        apt = ["apt-get update"]
        if locked is None:
            apt.append("apt-get upgrade -y --with-new-pkgs --no-install-recommends")
            specifications = sorted(set(packages))
            install_options = ""
        else:
            specifications = [
                entry["name"]
                + (":" + entry["architecture"] if entry["architecture"] != "all" else "")
                + "="
                + entry["version"]
                for entry in locked
            ]
            install_options = " --allow-downgrades"
        if specifications:
            apt.append(
                "apt-get install -y --no-install-recommends"
                + install_options
                + " -- "
                + " ".join(shlex.quote(package) for package in specifications)
            )
        apt.append("rm -rf /var/lib/apt/lists/*")
        # JSON RUN prevents Dockerfile interpretation of package characters.
        # Shell arguments are separately validated and quoted above.
        recipe = (
            f"FROM {base_digest}\n"
            "USER 0:0\n"
            + "RUN "
            + json.dumps(
                [
                    "/bin/sh",
                    "-c",
                    "DEBIAN_FRONTEND=noninteractive; export DEBIAN_FRONTEND; " + " && ".join(apt),
                ]
            )
            + "\n"
        )
        with tempfile.TemporaryDirectory(prefix="rosenv-image-") as temporary:
            directory = Path(temporary)
            context = directory / "empty-context"
            context.mkdir()
            dockerfile = directory / "Dockerfile"
            dockerfile.write_text(recipe)
            iidfile = directory / "image-id"
            platform = f"{base['os']}/{base['architecture']}"
            if base["variant"]:
                if not _ARCHITECTURE.fullmatch(base["variant"]):
                    raise BackendError("Invalid base image platform variant.")
                platform += "/" + base["variant"]
            status = self._stream(
                [
                    "build",
                    "--no-cache",
                    "--platform",
                    platform,
                    "--iidfile",
                    str(iidfile),
                    "--label",
                    f"{CACHE_LABEL}=1",
                    "--label",
                    f"{OWNER_LABEL}={os.getuid()}",
                    "--label",
                    f"io.rosenv.base.digest={base_digest}",
                    "--file",
                    str(dockerfile),
                    str(context),
                ]
            )
            if status:
                detail = (
                    " Exact locked package versions may no longer be available in the repositories; "
                    "no unlocked fallback was attempted."
                    if locked is not None
                    else " See the Docker/apt output above."
                )
                raise BackendError(
                    f"Dependency image build failed with exit status {status}.{detail}"
                )
            try:
                image_id = iidfile.read_text().strip()
            except OSError as exc:
                raise BackendError("Docker build did not produce an image ID file.") from exc
        if not _IMAGE_ID.fullmatch(image_id):
            raise BackendError("Docker build returned an invalid immutable image ID.")
        inventory = self._image_inventory(image_id)
        if locked is not None and inventory != locked:
            expected = {(item["name"], item["architecture"]): item["version"] for item in locked}
            actual = {(item["name"], item["architecture"]): item["version"] for item in inventory}
            changed = [
                f"{name}:{architecture} (wanted {expected.get((name, architecture), 'absent')}, "
                f"got {actual.get((name, architecture), 'absent')})"
                for name, architecture in sorted(expected.keys() | actual.keys())
                if expected.get((name, architecture)) != actual.get((name, architecture))
            ]
            raise BackendError(
                "Frozen package inventory does not match the lock: "
                + "; ".join(changed[:8])
                + ". No unlocked fallback was attempted."
            )
        actual_image = self.image_info(image_id)
        if actual_image is None:
            raise BackendError("The built dependency image is not available in the Docker daemon.")
        if (actual_image["os"], actual_image["architecture"]) != (base["os"], base["architecture"]):
            raise BackendError("Built dependency image platform differs from the resolved base.")
        identity = json.dumps(
            {
                "base": base_digest,
                "packages": inventory,
                "platform": platform,
                "fingerprint": fingerprint,
                # Users sharing a daemon own different labelled images. Their
                # identical project inputs must not overwrite each other's tag.
                "owner": os.getuid(),
                "recipe": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        tag = "rosenv-cache:" + hashlib.sha256(identity).hexdigest()
        self._docker(["image", "tag", image_id, tag])
        return {
            "id": image_id,
            "base_id": base["id"],
            "base_digest": base_digest,
            "architecture": base["architecture"],
            "os": base["os"],
            "packages": inventory,
            "engine_id": self.engine_id(),
            "reference": reference,
            "tag": tag,
            "digests": actual_image["digests"],
        }

    @staticmethod
    def _managed_image(info: dict[str, Any]) -> bool:
        return info["labels"].get(CACHE_LABEL) == "1" and info["labels"].get(OWNER_LABEL) == str(
            os.getuid()
        )

    def cache_list(self) -> list[dict[str, Any]]:
        """List labelled dependency images belonging to this host user."""
        result = self._docker(
            [
                "image",
                "ls",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label={CACHE_LABEL}=1",
                "--filter",
                f"label={OWNER_LABEL}={os.getuid()}",
            ]
        )
        images = []
        for image_id in sorted(set(result.stdout.split())):
            if not _IMAGE_ID.fullmatch(image_id):
                raise BackendError("Docker returned an invalid cache image ID.")
            info = self.image_info(image_id)
            if info is not None and self._managed_image(info):
                images.append({key: info[key] for key in ("id", "size", "created", "tags")})
        return images

    def cache_clean(self) -> dict[str, list]:
        """Remove only owned cache images; Docker protects images still in use."""
        result = {"removed": [], "skipped": []}
        for candidate in self.cache_list():
            image_id = candidate["id"]
            # Recheck immutable ID and ownership immediately before deletion.
            info = self.image_info(image_id)
            if info is None:
                continue
            if not self._managed_image(info):
                result["skipped"].append({"id": image_id, "reason": "ownership labels changed"})
                continue
            removed = self._docker(["image", "rm", "--no-prune", image_id], check=False)
            if removed.returncode:
                result["skipped"].append(
                    {"id": image_id, "reason": (removed.stderr or removed.stdout).strip()[-2000:]}
                )
            else:
                result["removed"].append(image_id)
        return result

    @staticmethod
    def _mount(source: str, destination: str) -> str:
        # Docker parses --mount as CSV; quoting handles commas in host paths.
        output = io.StringIO()
        csv.writer(output, lineterminator="").writerow(
            ["type=bind", f"src={source}", f"dst={destination}"]
        )
        return output.getvalue()

    def create(self, state: dict[str, Any]) -> None:
        """Create and bootstrap a persistent environment; clean up on failure."""
        name = self._name(state)
        if self.inspect(state) is not None:
            raise BackendError(f"Container {name!r} already exists.")
        if state["network"] not in ("bridge", "host", "none"):
            raise BackendError("Network must be bridge, host, or none.")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", state["image_id"]):
            raise BackendError("The image must be resolved to an immutable SHA-256 ID.")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", state["ros_distro"]):
            raise BackendError("Invalid ROS distribution name.")
        for key in ("workspace", "home"):
            if not os.path.isabs(state[key]) or not os.path.isdir(state[key]):
                raise BackendError(f"Environment {key} must be an existing absolute directory.")
        if not 0 <= int(state["domain_id"]) <= 232:
            raise BackendError("ROS_DOMAIN_ID must be between 0 and 232.")
        uid, gid = int(state["uid"]), int(state["gid"])
        if uid <= 0 or gid < 0:
            raise BackendError("Environments require a non-root host user and valid group ID.")
        network_created = False
        container_attempted = False
        try:
            if state["network"] == "bridge":
                network_created = self._ensure_network(state)
                network = name + "-net"
            else:
                network = state["network"]
            args = [
                "container",
                "run",
                "--detach",
                "--init",
                "--name",
                name,
                "--user",
                f"{uid}:{gid}",
                "--workdir",
                "/workspace",
                "--security-opt",
                "no-new-privileges",
                "--network",
                network,
                "--env",
                "HOME=/home/rosenv",
                "--env",
                f"ROS_DISTRO={state['ros_distro']}",
                "--env",
                f"ROS_DOMAIN_ID={state['domain_id']}",
                "--mount",
                self._mount(state["workspace"], "/workspace"),
                "--mount",
                self._mount(state["home"], "/home/rosenv"),
            ]
            for key, value in self._labels(state).items():
                args += ["--label", f"{key}={value}"]
            args += ["--entrypoint", "/bin/sleep", state["image_id"], "infinity"]
            # Docker run may create a container and then fail during startup.
            # Cleanup is still safe because names were absent at the preflight
            # and we inspect both unique token and owner labels before removal.
            container_attempted = True
            self._docker(args)
            info = self._required(state)
            self._docker(
                [
                    "container",
                    "exec",
                    "--user",
                    "0:0",
                    info["Id"],
                    "/bin/bash",
                    "-c",
                    _BOOTSTRAP_SCRIPT,
                    "rosenv-bootstrap",
                    str(uid),
                    str(gid),
                ],
                timeout=120,
            )
            # Fail creation immediately if the requested ROS distribution is absent.
            info = self._required(state)
            self._docker(
                [
                    "container",
                    "exec",
                    "--user",
                    f"{uid}:{gid}",
                    info["Id"],
                    "/bin/bash",
                    "-c",
                    _RUN_SCRIPT,
                    "rosenv",
                    state["ros_distro"],
                    "/bin/true",
                ]
            )
        except (BackendError, KeyboardInterrupt) as exc:
            cleanup_errors = []
            if container_attempted:
                try:
                    info = self.inspect(state)
                    if info is not None:
                        self._docker(["container", "rm", "--force", info["Id"]])
                except BackendError as cleanup_exc:
                    cleanup_errors.append(str(cleanup_exc))
            if network_created:
                try:
                    self._remove_network(state)
                except BackendError as cleanup_exc:
                    cleanup_errors.append(str(cleanup_exc))
            if cleanup_errors:
                raise BackendError(
                    f"Environment creation failed: {exc}. Cleanup also failed: "
                    + "; ".join(cleanup_errors)
                ) from exc
            raise

    def ensure_running(self, state: dict[str, Any]) -> None:
        """Start an existing owned environment if necessary."""
        info = self._required(state)
        if not info.get("State", {}).get("Running", False):
            self._docker(["container", "start", info["Id"]])

    def run(
        self,
        state: dict[str, Any],
        command: list[str],
        interactive: bool = False,
        *,
        workdir: str | None = None,
    ) -> int:
        """Run argv inside ROS, or open a setup-aware shell with empty argv."""
        if not command:
            if not interactive:
                raise BackendError("A command is required.")
            command = ["/bin/bash", "--rcfile", "/etc/rosenv.bashrc", "-i"]
        if any(not isinstance(arg, str) or "\0" in arg for arg in command):
            raise BackendError("Command arguments must be strings without NUL bytes.")
        workdir = "/workspace" if workdir is None else workdir
        if (
            not isinstance(workdir, str)
            or "\0" in workdir
            or not workdir.startswith("/")
            or ".." in workdir.split("/")
            or not (
                posixpath.normpath(workdir) == "/workspace"
                or posixpath.normpath(workdir).startswith("/workspace/")
            )
        ):
            raise BackendError(
                "Command working directory must be an absolute path under /workspace."
            )
        workdir = posixpath.normpath(workdir)
        self.ensure_running(state)
        info = self._required(state)
        args = ["container", "exec", "--interactive"]
        if sys.stdin.isatty() and sys.stdout.isatty():
            args.append("--tty")
        args += [
            "--user",
            f"{state['uid']}:{state['gid']}",
            "--workdir",
            workdir,
            info["Id"],
            "/bin/bash",
            "-c",
            _RUN_SCRIPT,
            "rosenv",
            state["ros_distro"],
            *command,
        ]
        return self._stream(args)

    def add(self, state: dict[str, Any], packages: list[str]) -> int:
        """Install explicitly named apt packages into this environment only."""
        if not packages or any(not _PACKAGE.fullmatch(package) for package in packages):
            raise BackendError("Specify valid Debian package names (optionally package=version).")
        self.ensure_running(state)
        # ROS repositories are synchronized independently of Docker image builds.
        # Updating only a newly requested package can mix incompatible ROS ABIs.
        # Match the upstream apt-upgrade prerequisite inside this container only.
        for command in (
            ["apt-get", "update"],
            ["apt-get", "upgrade", "-y", "--with-new-pkgs", "--no-install-recommends"],
            ["apt-get", "install", "-y", "--no-install-recommends", "--", *packages],
        ):
            info = self._required(state)
            status = self._stream(
                [
                    "container",
                    "exec",
                    "--user",
                    "0:0",
                    "--env",
                    "HOME=/root",
                    "--env",
                    "DEBIAN_FRONTEND=noninteractive",
                    info["Id"],
                    *command,
                ]
            )
            if status:
                return status
        return 0

    def stop(self, state: dict[str, Any]) -> None:
        """Stop an owned container without deleting its packages or mounts."""
        info = self._required(state)
        self._docker(["container", "stop", info["Id"]])

    def remove(self, state: dict[str, Any]) -> None:
        """Remove owned runtime resources, preserving all host directories."""
        info = self.inspect(state)
        if info is not None:
            self._docker(["container", "rm", "--force", info["Id"]])
        self._remove_network(state)
