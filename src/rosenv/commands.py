"""Project-oriented command parsing and execution, alongside legacy environments."""

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

from .catalog import DISTROS
from .project import ProjectError, discover, initialize, project_lock
from .state import atomic_write
from .workflow import (
    command_workdir,
    existing_project_environment,
    export_lock,
    lock_project,
    read_lock,
    sync_project,
)


def selection_options(parser, *, execute=False):
    parser.add_argument(
        "--group", action="append", default=[], help="Include a dependency group (repeatable)"
    )
    parser.add_argument("--no-dev", action="store_true", help="Exclude the default dev group")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--locked", action="store_true", help="Fail if the lockfile is stale")
    modes.add_argument(
        "--frozen", action="store_true", help="Use the existing lock without freshness checks"
    )
    if execute:
        parser.add_argument(
            "--no-sync",
            action="store_true",
            help="Use the existing environment without locking or syncing",
        )


def extend_parser(parser, sub):
    parser.add_argument("-C", "--directory", type=Path, help="Run from this directory")
    env = sub.add_parser("env", help="Manage named environments (create, list, show, remove, stop)")
    env.add_argument("arguments", nargs=argparse.REMAINDER)
    for action in ("lock", "sync", "export", "tree", "build", "test", "task"):
        item = sub.add_parser(
            action,
            help={
                "lock": "Resolve and pin a project's image and complete apt inventory",
                "sync": "Reconcile the project environment with its lockfile",
                "export": "Export locked apt requirements, JSON, or a Dockerfile",
                "tree": "Show declared dependencies and locked versions",
                "build": "Build the project workspace with colcon",
                "test": "Run colcon tests and report failures",
                "task": "List or run named project tasks",
            }[action],
        )
        selection_options(item, execute=action in {"build", "test", "task"})
        if action == "lock":
            item.add_argument(
                "--check",
                action="store_true",
                help="Check freshness without Docker or updating rosenv.lock",
            )
            item.add_argument(
                "--upgrade", action="store_true", help="Refresh base image and package versions"
            )
        elif action == "export":
            item.add_argument(
                "--format", choices=["json", "requirements", "dockerfile"], default="requirements"
            )
            item.add_argument("-o", "--output", type=Path)
        elif action == "tree":
            item.add_argument(
                "--all", action="store_true", help="List the complete installed package inventory"
            )
            item.add_argument("--json", action="store_true")
        elif action in {"build", "test"}:
            item.add_argument("command", nargs=argparse.REMAINDER)
        elif action == "task":
            item.add_argument("name", nargs="?")
            item.add_argument("command", nargs=argparse.REMAINDER)
    pin = sub.add_parser("pin", help="Set the project's ROS distribution")
    pin.add_argument("distro", choices=DISTROS)
    cache = sub.add_parser("cache", help="Inspect or remove unused rosenv image cache entries")
    cache.add_argument("operation", choices=["list", "clean"], default="list", nargs="?")
    cache.add_argument("--json", action="store_true")
    cache.add_argument("-y", "--yes", action="store_true")


def _options(args):
    return {
        "groups": getattr(args, "group", []),
        "no_dev": getattr(args, "no_dev", False),
        "locked": getattr(args, "locked", False),
        "frozen": getattr(args, "frozen", False),
    }


def _arguments(args):
    command = args.command
    return command[1:] if command and command[0] == "--" else command


def _lock(args, project, backend):
    if getattr(args, "frozen", False):
        return read_lock(project)
    return lock_project(
        project,
        backend,
        groups=getattr(args, "group", []),
        no_dev=getattr(args, "no_dev", False),
        check=getattr(args, "locked", False),
    )


def dispatch_project(args, store, backend):
    """Return None for a legacy command, otherwise the command's exit status."""
    action = args.action
    if action == "init":
        root = args.workspace.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        name = args.name or re.sub(r"[^a-z0-9_-]", "-", root.name.lower())[:48].strip("-_")
        project = initialize(root, name, args.distro, args.variant, args.network, args.domain_id)
        (root / "src").mkdir(exist_ok=True)
        ignore = root / ".gitignore"
        original = ignore.read_text() if ignore.exists() else ""
        additions = [
            line
            for line in (".rosenv.edit.lock", "build/", "install/", "log/")
            if line not in original.splitlines()
        ]
        if additions:
            atomic_write(
                ignore, original.rstrip() + ("\n" if original else "") + "\n".join(additions) + "\n"
            )
        if args.sync:
            sync_project(project, store, backend)
        print(f"Initialized {project.name}: {project.path}")
        print("Next: rosenv add demo_nodes_cpp  ·  rosenv run -- ros2 --help")
        return 0
    if action == "cache":
        if args.operation == "clean":
            if not args.yes:
                if not sys.stdin.isatty() or input(
                    "Remove unused rosenv cache images? [y/N] "
                ).lower() not in {"y", "yes"}:
                    raise ProjectError("Cache cleanup requires --yes or interactive confirmation.")
            result = backend.cache_clean()
        else:
            result = backend.cache_list()
        print(json.dumps(result, indent=2))
        return 0
    project_actions = {"lock", "sync", "export", "tree", "pin", "build", "test", "task"}
    candidates = project_actions | {"run", "shell", "add", "remove", "show"}
    if action not in candidates or getattr(args, "force_env", False):
        return None
    if getattr(args, "env", None) or os.environ.get("ROSENV_ENV"):
        if action in project_actions:
            raise ProjectError(
                "Project commands use rosenv.toml; unset ROSENV_ENV before using this command."
            )
        return None
    project = discover()
    if project is None:
        if action in project_actions:
            raise ProjectError("No rosenv.toml found. Run 'rosenv init NAME' in your workspace.")
        return None
    if action == "remove" and args.yes:
        return None  # Backwards-compatible explicit environment deletion.
    if action == "pin":
        project.set_distro(args.distro)
        print(f"Pinned {project.name} to ROS 2 {args.distro}. Run rosenv sync to update.")
        return 0
    if action in {"add", "remove"}:
        values = args.packages if action == "add" else [args.name, *args.packages]
        with project_lock(project.root):
            if action == "add":
                project.add_dependencies(values, group=args.group)
            else:
                project.remove_dependencies(values, group=args.group)
            if not args.no_sync:
                sync_project(project, store, backend, groups=[args.group] if args.group else [])
        print(f"Updated dependencies in {project.path}")
        return 0
    if action == "lock":
        if args.upgrade and (args.check or args.locked or args.frozen):
            raise ProjectError("--upgrade cannot be combined with --check, --locked, or --frozen.")
        lock = (
            read_lock(project)
            if args.frozen
            else lock_project(
                project,
                backend,
                groups=args.group,
                no_dev=args.no_dev,
                check=args.check or args.locked,
                upgrade=args.upgrade,
            )
        )
        print(
            f"Locked {len(lock['packages'])} packages for {lock['ros']} ({lock['architecture']})."
        )
        return 0
    if action in {"export", "tree"}:
        lock = _lock(args, project, backend)
        if action == "export":
            content = export_lock(lock, args.format)
            if args.output:
                target = args.output.expanduser().resolve()
                if target in {project.path, project.root / "rosenv.lock"}:
                    raise ProjectError("Export cannot overwrite rosenv.toml or rosenv.lock.")
                atomic_write(target, content)
                print(f"Exported {target}")
            else:
                print(content, end="")
        else:
            wanted = {p.split("=", 1)[0] for p in lock["requested"]}
            packages = (
                lock["packages"]
                if args.all
                else [
                    p
                    for p in lock["packages"]
                    if p["name"] in wanted or p["name"].split(":", 1)[0] in wanted
                ]
            )
            if args.json:
                print(json.dumps(packages, indent=2))
            else:
                print(
                    f"{project.name} · ROS 2 {lock['ros']} · {'complete package inventory' if args.all else 'declared dependencies'}"
                )
                for package in packages:
                    print(
                        f"  {package['name']} == {package['version']} ({package['architecture']})"
                    )
        return 0
    if action == "show":
        state = existing_project_environment(project, store)
        print(
            json.dumps(state, indent=2)
            if args.json
            else f"{state['name']}\nROS 2 {state['ros_distro']}\nWorkspace: {state['workspace']}\nImage: {state['image_id']}"
        )
        return 0
    if action == "task" and args.name is None:
        for name, command in sorted(project.tasks.items()):
            print(f"{name}: {shlex.join(command)}")
        return 0
    if action == "run" and not _arguments(args):
        raise ProjectError("Provide a command after --.")
    if action == "task" and args.name not in project.tasks:
        raise ProjectError(f"Unknown task {args.name!r}. Use 'rosenv task' to list tasks.")
    state = (
        existing_project_environment(project, store)
        if getattr(args, "no_sync", False)
        else sync_project(project, store, backend, **_options(args))
    )
    if action == "sync":
        print(f"Synced {project.name} → {state['name']} (ROS 2 {state['ros_distro']}).")
        return 0
    if action in {"run", "shell", "task"}:
        command = (
            []
            if action == "shell"
            else (
                _arguments(args) if action == "run" else project.tasks[args.name] + _arguments(args)
            )
        )
        return backend.run(
            state, command, interactive=action == "shell", workdir=command_workdir(project)
        )
    command = ["colcon", action, *_arguments(args)]
    if action == "build" and "--symlink-install" not in command:
        command.append("--symlink-install")
    status = backend.run(state, command)
    if action == "test" and status == 0:
        return backend.run(state, ["colcon", "test-result", "--verbose"])
    return status
