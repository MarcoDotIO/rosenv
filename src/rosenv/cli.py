"""Command-line interface. All container operations use argument vectors."""

import argparse
import json
import os
import platform
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .backend import BackendError, DockerBackend
from .catalog import DISTROS, image_for
from .commands import dispatch_project, extend_parser, selection_options
from .project import ProjectError
from .state import StateError, Store, validate_name


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rosenv", description="Isolated, persistent ROS 2 environments on Linux."
    )
    p.add_argument("--version", action="version", version=f"rosenv {__version__}")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor", help="Check Linux and Docker access")
    sub.add_parser("distros", help="List available ROS 2 distributions")
    for action in ("create", "init"):
        c = sub.add_parser(
            action,
            help="Create an environment"
            if action == "create"
            else "Create and select a project environment",
        )
        c.add_argument("name", nargs=None if action == "create" else "?")
        c.add_argument("--ros", choices=DISTROS, default="jazzy", dest="distro")
        c.add_argument(
            "--variant", choices=["ros-base", "ros-core", "perception"], default="ros-base"
        )
        c.add_argument("--workspace", type=Path, default=Path("."))
        c.add_argument(
            "--network",
            choices=["bridge", "host", "none"],
            default="bridge",
            help="Private bridge by default; host permits LAN discovery",
        )
        c.add_argument(
            "--domain-id", type=int, help="ROS_DOMAIN_ID (0–232); automatically allocated in 1–100"
        )
        c.add_argument("--no-pull", action="store_true", help="Require an already cached image")
        if action == "init":
            c.add_argument(
                "--sync", action="store_true", help="Also lock and create the project environment"
            )
    listing = sub.add_parser("list", aliases=["ls"], help="List environments")
    listing.add_argument("--json", action="store_true")
    for action in ("local", "global"):
        c = sub.add_parser(action, help=f"Select the {action} default environment")
        c.add_argument("name")
    for action in ("show", "run", "shell", "add", "stop", "remove", "delete"):
        c = sub.add_parser(
            action,
            help={
                "show": "Show environment metadata",
                "run": "Run a command inside the selected environment",
                "shell": "Open a ROS-configured shell",
                "add": "Install ROS or Ubuntu packages in the environment",
                "stop": "Stop an environment; retain its files",
                "remove": "Delete an environment; retain its workspace",
                "delete": "Delete a named environment (alias for env remove)",
            }[action],
        )
        if action in ("stop", "remove", "delete"):
            c.add_argument("name")
        else:
            c.add_argument("-e", "--env", dest="env", help="Override project/global selection")
        if action == "show":
            c.add_argument("--json", action="store_true")
        elif action == "run":
            c.add_argument(
                "command",
                nargs=argparse.REMAINDER,
                help="Command after --, e.g. -- ros2 topic list",
            )
        elif action == "add":
            c.add_argument(
                "packages",
                nargs="+",
                help="ROS package names (demo_nodes_cpp) or apt:build-essential",
            )
            c.add_argument("--group", help="Project dependency group")
            c.add_argument(
                "--no-sync",
                action="store_true",
                help="Edit the project manifest without provisioning",
            )
        elif action in ("remove", "delete"):
            c.add_argument(
                "-y",
                "--yes",
                action="store_true",
                help="Confirm deleting environment packages and private home",
            )
            c.add_argument(
                "packages", nargs="*", help="Additional dependencies to remove from the project"
            )
            c.add_argument("--group", help="Project dependency group")
            c.add_argument("--no-sync", action="store_true", help="Only edit the project manifest")
        if action in ("run", "shell"):
            selection_options(c, execute=True)
    extend_parser(p, sub)
    return p


def packages_for(distro: str, packages: list[str]) -> list[str]:
    result = []
    for package in packages:
        if package.startswith("apt:"):
            package = package[4:]
        elif not package.startswith("ros-"):
            package = f"ros-{distro}-{package.replace('_', '-')}"
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?", package):
            raise StateError(f"Invalid package name: {package!r}")
        result.append(package)
    return result


def create(args, store: Store, backend: DockerBackend) -> dict:
    if platform.system() != "Linux":
        raise StateError(
            "Environment creation requires Linux with Docker. Run rosenv on your Ubuntu workstation."
        )
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise StateError(f"Workspace does not exist or is not a directory: {workspace}")
    if workspace.is_relative_to(store.root):
        raise StateError(
            "Workspace must be outside ROSENV_HOME so removing an environment cannot delete project files."
        )
    # Docker --mount uses a comma-separated grammar; reject ambiguous host paths.
    if "," in str(workspace) or "," in str(store.root):
        raise StateError(
            "Workspace and ROSENV_HOME paths cannot contain commas (Docker mount syntax)."
        )
    name = args.name or re.sub(r"[^a-z0-9_-]", "-", workspace.name.lower())[:48].strip("-_")
    validate_name(name)
    with store.lock():
        directory = store.env_dir(name)
        if directory.exists():
            raise StateError(f"Environment '{name}' already exists at {directory}.")
        domain = args.domain_id
        if domain is None:
            used = {item["domain_id"] for item in store.list()}
            domain = next((i for i in range(1, 101) if i not in used), None)
            if domain is None:
                raise StateError(
                    "No free automatic ROS domain ID (1–100). Set --domain-id explicitly."
                )
        if not 0 <= domain <= 232:
            raise StateError("--domain-id must be between 0 and 232.")
        reference = image_for(args.distro, args.variant)
        image = backend.resolve_image(reference, pull=not args.no_pull)
        token = uuid.uuid4().hex
        state = {
            "schema": 1,
            "name": name,
            "token": token,
            "container": f"rosenv-{os.getuid()}-{name}-{token[:12]}",
            "ros_distro": args.distro,
            "image": reference,
            "image_id": image["id"],
            "image_digests": image.get("digests", []),
            "engine_id": image["engine_id"],
            "workspace": str(workspace),
            "home": str(directory / "home"),
            "uid": os.getuid(),
            "gid": os.getgid(),
            "network": args.network,
            "domain_id": domain,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (directory / "home").mkdir(parents=True, mode=0o700)
        # Save before engine mutation, so interruption always leaves a removable record.
        store.save(state)
        try:
            backend.create(state)
        except BaseException:
            # Keep state if cleanup fails, permitting a later rosenv remove.
            try:
                backend.remove(state)
            except (BackendError, OSError):
                pass
            else:
                shutil.rmtree(directory)
            raise
        if args.action == "init":
            store.set_local(name, workspace)
        return state


def dispatch(args, store: Store, backend: DockerBackend) -> int:
    if args.action == "delete":
        args.action = "remove"
        args.force_env = True
    project_result = dispatch_project(args, store, backend)
    if project_result is not None:
        return project_result
    action = args.action
    if action == "doctor":
        info = backend.doctor()
        print(
            json.dumps(
                {
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "data_directory": str(store.root),
                    "docker": info,
                },
                indent=2,
            )
        )
        if platform.system() != "Linux":
            raise StateError(
                "This release supports a Linux Docker host; use your Ubuntu workstation."
            )
    elif action == "distros":
        print(f"{'ROS 2':<12} {'UBUNTU':<9} {'EOL':<10} IMAGE")
        for distro, spec in DISTROS.items():
            print(f"{distro:<12} {spec['ubuntu']:<9} {spec['eol']:<10} {image_for(distro)}")
    elif action in ("create", "init"):
        state = create(args, store, backend)
        print(
            f"Created {state['name']} (ROS 2 {state['ros_distro']}, domain {state['domain_id']})."
        )
        print(f"Workspace: {state['workspace']} → /workspace")
        print(f"Run: rosenv run -e {state['name']} -- ros2 --help")
    elif action in ("list", "ls"):
        records = store.list()
        if args.json:
            print(json.dumps(records, indent=2))
        else:
            print(f"{'NAME':<22} {'ROS 2':<10} {'NETWORK':<9} {'DOMAIN':<7} WORKSPACE")
            for record in records:
                print(
                    f"{record['name']:<22} {record['ros_distro']:<10} {record['network']:<9} {record['domain_id']:<7} {record['workspace']}"
                )
    elif action in ("local", "global"):
        with store.lock():
            if action == "local":
                target = store.set_local(args.name)
                print(f"Selected {args.name} in {target}")
            else:
                store.set_global(args.name)
                print(f"Default environment: {args.name}")
    elif action == "show":
        state = store.load(store.select(args.env))
        if args.json:
            print(json.dumps(state, indent=2))
        else:
            for key in (
                "name",
                "ros_distro",
                "workspace",
                "network",
                "domain_id",
                "image",
                "image_id",
                "container",
                "home",
            ):
                print(f"{key}: {state[key]}")
    elif action in ("run", "shell", "add"):
        command = []
        if action == "run":
            command = args.command
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                raise StateError("Provide a command after --, e.g. rosenv run -- ros2 topic list")
        with store.lock():
            state = store.load(store.select(args.env))
            packages = packages_for(state["ros_distro"], args.packages) if action == "add" else []
            backend.ensure_running(state)
        if action == "add":
            # Serialize package transactions, but allow concurrent talker/listener commands.
            with store.lock():
                state = store.load(state["name"])
                packages = packages_for(state["ros_distro"], args.packages)
                code = backend.add(state, packages)
                if code == 0:
                    state["requested_packages"] = sorted(
                        set(state.get("requested_packages", [])) | set(packages)
                    )
                    store.save(state)
                return code
        return backend.run(state, command, interactive=action == "shell")
    elif action == "stop":
        with store.lock():
            backend.stop(store.load(args.name))
        print(f"Stopped {args.name}.")
    elif action == "remove":
        if args.packages:
            raise StateError(
                "Use one environment name with env remove, or remove dependencies inside a project."
            )
        with store.lock():
            state = store.load(args.name)
            directory = store.env_dir(args.name).resolve()
            for record in store.list():
                if Path(record["workspace"]).resolve().is_relative_to(directory):
                    raise StateError(
                        f"Cannot remove {args.name}: its storage contains workspace files for {record['name']}."
                    )
            if not args.yes:
                if not sys.stdin.isatty():
                    raise StateError(
                        "Removal deletes packages and private home. Pass --yes to confirm; workspace is retained."
                    )
                if input(
                    f"Delete environment '{args.name}' and its private home? [y/N] "
                ).lower() not in {"y", "yes"}:
                    return 1
            retired = state.get("retired")
            if retired:
                store.validate(retired, state["name"])
                backend.remove(retired)
            backend.remove(state)
            shutil.rmtree(store.env_dir(args.name))
            default = store.root / "default"
            if default.exists() and default.read_text().strip() == args.name:
                default.unlink()
        print(f"Removed {args.name}. Workspace retained: {state['workspace']}")
    return 0


def main(argv: list[str] | None = None, *, _force_env: bool = False) -> int:
    args = parser().parse_args(argv)
    args.force_env = _force_env
    original_cwd = Path.cwd()
    try:
        if args.directory:
            os.chdir(args.directory)
        if args.action == "env":
            if not args.arguments:
                raise StateError("Use rosenv env create/list/show/stop/remove.")
            if args.arguments[0] not in {
                "create",
                "list",
                "ls",
                "show",
                "stop",
                "remove",
                "local",
                "global",
            }:
                raise StateError("Unknown env subcommand.")
            if args.arguments[0] == "remove":
                args.arguments[0] = "delete"
            return main(args.arguments, _force_env=True)
        code = dispatch(args, Store(), DockerBackend())
        return 128 - code if code < 0 else code
    except (StateError, BackendError, ProjectError, OSError) as exc:
        print(f"rosenv: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        os.chdir(original_cwd)
