"""Audit built archives without extracting them or printing private file contents."""

import re
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
FORBIDDEN = {".git", ".venv", "__pycache__", ".DS_Store", "__MACOSX", "credentials.json"}
PRIVATE_RECEIPTS = {"VALIDATION.md", "workstation-install.json", "lyrical-abi-recovery.txt"}
PRIVATE_MATERIAL = re.compile(
    rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|"
    rb"\bgithub_pat_[A-Za-z0-9_]{50,}\b|\bAKIA[A-Z0-9]{16}\b"
)


def inspect_member(name: str, content: bytes) -> None:
    parts = PurePosixPath(name).parts
    assert not name.startswith("/") and ".." not in parts, f"Unsafe archive path: {name}"
    assert not any(
        part in FORBIDDEN or part.startswith("._") or part == ".env" or part.startswith(".env.")
        for part in parts
    ), f"Private or generated file in archive: {name}"
    assert not any(
        part in PRIVATE_RECEIPTS or (part.startswith("validation-") and part.endswith(".json"))
        for part in parts
    ), f"Private workstation receipt in archive: {name}"
    assert not PRIVATE_MATERIAL.search(content), f"Potential credential material in archive: {name}"


def main() -> None:
    wheels = list(DIST.glob("*.whl"))
    sdists = list(DIST.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1, "Expected exactly one wheel and one source distribution"
    for wheel in wheels:
        assert f"-{VERSION}-" in wheel.name, "Wheel version does not match project"
        with zipfile.ZipFile(wheel) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    inspect_member(info.filename, archive.read(info))
        print(f"Audited {wheel.name}")
    for sdist in sdists:
        assert sdist.name.endswith(f"-{VERSION}.tar.gz"), "Source version does not match project"
        with tarfile.open(sdist) as archive:
            for member in archive.getmembers():
                assert not member.issym() and not member.islnk(), "Archive contains a link"
                if member.isfile():
                    handle = archive.extractfile(member)
                    assert handle is not None
                    inspect_member(member.name, handle.read())
        print(f"Audited {sdist.name}")


if __name__ == "__main__":
    main()
