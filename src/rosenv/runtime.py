"""Validation and host resolution for runtime-only configuration."""

import os
import re
import stat
from pathlib import Path


def validate_variables(variables: dict[str, str]) -> None:
    if not isinstance(variables, dict):
        raise ValueError("environment.variables must be a table of strings.")
    for key, value in variables.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid environment variable name: {key!r}.")
        if key in {"HOME", "ROS_DISTRO"}:
            raise ValueError(f"{key} is managed by rosenv and cannot be overridden.")
        if not isinstance(value, str) or "\0" in value:
            raise ValueError(f"Environment variable {key} must be a string without NUL bytes.")
        if key == "ROS_DOMAIN_ID" and (
            not re.fullmatch(r"[0-9]+", value) or not 0 <= int(value) <= 232
        ):
            raise ValueError("ROS_DOMAIN_ID must be an integer between 0 and 232.")


def parse_variables(assignments: list[str]) -> dict[str, str]:
    variables = {}
    for assignment in assignments:
        key, separator, value = assignment.partition("=")
        if not separator:
            raise ValueError("--set-env requires NAME=VALUE (repeat the flag for more variables).")
        variables[key] = value
    validate_variables(variables)
    return variables


def device_spec(value: str) -> tuple[str, str, str]:
    if not isinstance(value, str):
        raise ValueError("Devices must be strings: /dev/HOST[:/dev/CONTAINER[:rw]].")
    parts = value.split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"Invalid device mapping: {value!r}.")
    source = parts[0]
    target = parts[1] if len(parts) > 1 else source
    permissions = parts[2] if len(parts) > 2 else "rw"
    for path in (source, target):
        if (
            not path.startswith("/dev/")
            or any(part in {"", ".", ".."} for part in path.split("/")[1:])
            or any(char.isspace() or ord(char) < 32 for char in path)
            or path == "/dev/"
        ):
            raise ValueError(f"Device paths must be absolute paths below /dev: {path!r}.")
    if (
        not permissions
        or set(permissions) - set("rwm")
        or len(set(permissions)) != len(permissions)
    ):
        raise ValueError("Device permissions must contain only r, w, and/or m, without repeats.")
    return source, target, "".join(char for char in "rwm" if char in permissions)


def validate_devices(devices: list[str]) -> None:
    if not isinstance(devices, list):
        raise ValueError("environment.devices must be an array of device mappings.")
    targets = set()
    for value in devices:
        _, target, _ = device_spec(value)
        if target in targets:
            raise ValueError(f"Duplicate container device path: {target}.")
        targets.add(target)


def resolve_devices(devices: list[str]) -> list[dict]:
    """Record identity so reconnects and changed device permissions trigger replacement."""
    validate_devices(devices)
    resolved = []
    for value in devices:
        source, target, permissions = device_spec(value)
        try:
            path = Path(source).resolve(strict=True)
            info = path.stat()
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                f"Cannot access device {source}: {exc}. Connect it and retry sync."
            ) from exc
        if not path.is_relative_to("/dev") or not (
            stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode)
        ):
            raise ValueError(
                f"Device {source} must resolve to a character or block device under /dev."
            )
        resolved.append(
            {
                "source": source,
                "path": str(path),
                "target": target,
                "permissions": permissions,
                "major": os.major(info.st_rdev),
                "minor": os.minor(info.st_rdev),
                "gid": info.st_gid,
                "mode": info.st_mode,
                "inode": info.st_ino,
            }
        )
    return sorted(resolved, key=lambda device: device["target"])
