# User guide

rosenv manages a ROS distribution and Debian packages in a persistent Docker environment. Your source stays in the host workspace; commands see it at `/workspace`.

## Start a project

Install rosenv as described in the [README](../README.md#install), then check the host:

```bash
rosenv doctor
mkdir -p ~/robot_ws
cd ~/robot_ws
rosenv init --ros jazzy
```

`init` writes `rosenv.toml`, creates `src/`, and adds build outputs and the local edit-lock file to `.gitignore`. It does not download images. Use `rosenv init --ros jazzy --sync` to prepare the environment immediately. An existing manifest is never overwritten.

Project commands discover the nearest `rosenv.toml` in the current directory or its parents. `rosenv -C ~/robot_ws sync` selects a directory explicitly. Put this global option before the subcommand.

## Declare dependencies

```bash
rosenv add demo_nodes_cpp apt:git
rosenv add --group dev apt:python3-pytest
rosenv remove apt:git
```

ROS shorthand such as `demo_nodes_cpp` becomes `ros-jazzy-demo-nodes-cpp` in a Jazzy project. `apt:git` selects the Ubuntu package `git`. Exact Debian versions use `package=version`, for example `apt:git=VERSION` with a version available in the selected distribution's repositories. Version ranges are not supported.

`add` and `remove` edit the manifest, resolve dependencies, and synchronize the environment. `--no-sync` edits only the manifest, which is useful when preparing changes without Docker. If synchronization fails, the manifest retains your requested change so it can be corrected or retried.

```bash
rosenv add --no-sync demo_nodes_cpp
rosenv lock
rosenv sync --locked
```

Removing a declared package does not guarantee it disappears if another requested package still depends on it. Debian's resolver determines the final inventory. Package dependencies in `package.xml` are not automatically imported; declare the binary dependencies your project needs.

## Configuration

```toml
[project]
name = "robot_ws"
ros = "jazzy"
variant = "ros-base"
dependencies = ["demo_nodes_cpp", "apt:git"]

[dependency-groups]
dev = ["apt:python3-pytest"]
tools = ["apt:curl"]

[environment]
network = "bridge"
domain-id = 42

[tasks]
talker = ["ros2", "run", "demo_nodes_cpp", "talker"]
listener = ["ros2", "run", "demo_nodes_cpp", "listener"]
```

Names use lowercase letters, digits, underscores, and hyphens. Valid image variants are `ros-core`, `ros-base`, and `perception`. Omit `domain-id` to allocate an unused ID from 1–100 within your rosenv registry; explicit values may be 0–232.

`dev` is included by default when defined. Additional groups are opt-in, and group flags are repeatable:

```bash
rosenv sync --group tools
rosenv run --group tools -- curl --version
rosenv sync --no-dev
```

**Each lock records one group selection.** Switching from the default profile to `--no-dev` or a different group selection requires a new lock. Use identical group flags when checking or syncing a committed lock. For example, `rosenv lock --no-dev` pairs with `rosenv sync --locked --no-dev`. This is not a universal lock containing every possible profile.

## Locking and reproducibility

Commit `rosenv.toml` and `rosenv.lock` together. The manifest expresses your intent; the JSON lock records the official base image digest, architecture, selected groups, requested dependencies, and complete installed Debian package inventory.

| Command or flag | Behavior |
| --- | --- |
| `rosenv lock` | Reuses a current lock; resolves a missing or stale lock using Docker and the package repositories. |
| `rosenv lock --upgrade` | Pulls the base image and refreshes package versions even when the manifest is unchanged. |
| `rosenv lock --check` | Checks manifest/profile freshness without Docker or writes. It does not verify a running environment. |
| `rosenv sync` | Updates the lock as needed, then reconciles the project container. |
| `rosenv sync --locked` | Fails if the lock is missing or stale; never refreshes it. |
| `rosenv sync --frozen` | Uses an existing lock without checking whether it matches the manifest. |
| `rosenv run --no-sync -- COMMAND` | Uses an existing project environment without reconciling its package inventory. |

`run`, `shell`, `build`, `test`, and named task execution synchronize automatically. They accept `--locked`, `--frozen`, and `--no-sync`. Choose `--locked` in automation to fail on uncommitted dependency changes. `--frozen` is an intentional escape hatch: the locked ROS version and packages can differ from the current manifest.

Synchronization checks the installed package inventory. When packages drift or dependencies change, rosenv replaces the container while retaining the host workspace and private home. Use `rosenv add` for durable dependency changes rather than installing ad hoc packages in a container.

The prepared image is cached locally. On another machine, rosenv reconstructs it from the pinned base and exact package versions. **The lock does not archive `.deb` files.** Reconstruction fails if upstream repositories no longer retain required versions. The lock also targets one architecture; use a lock resolved on the architecture you intend to run. Source files, colcon outputs, and arbitrary modifications to installed files are not package-inventory guarantees.

`--locked` and `--frozen` do not mean offline: image pulls and reconstruction can still require network access. `network = "none"` controls the running environment; image preparation is a separate Docker build.

## Run, build, and test

```bash
rosenv run -- ros2 topic list
rosenv shell
rosenv build
rosenv build -- --packages-select my_package
rosenv test -- --packages-select my_package
```

Put `--` before arguments passed to a child command or colcon. Put rosenv flags before that separator, for example `rosenv build --locked -- --packages-select my_package`.

`build` uses `colcon build --symlink-install` by default. `test` runs `colcon test`, then `colcon test-result --verbose` when the first command succeeds; failing test results produce a nonzero exit status. Both commands work from the workspace root. `run`, `shell`, and tasks preserve your current subdirectory within the workspace.

Commands source the selected ROS installation and `/workspace/install/setup.bash` when present. When changing ROS distributions, rebuild your source workspace with compatible build outputs; old colcon overlays are not automatically removed.

Tasks contain an executable followed by its arguments. They do not expand shell variables, pipes, globs, or redirection:

```bash
rosenv task                       # List tasks without starting Docker
rosenv task talker
rosenv task talker -- --ros-args -r chatter:=status
```

For intentional shell behavior, invoke a shell explicitly, such as `rosenv run -- bash -c 'printf "%s\n" "$ROS_DISTRO"'`.

## Change ROS distributions

```bash
rosenv distros
rosenv pin humble
rosenv sync
```

`pin` edits the manifest without downloading anything. ROS shorthand dependencies adapt to the selected distribution. Fully qualified `ros-jazzy-*` dependencies must be changed before pinning to a different ROS distribution. Availability and source compatibility can differ between releases.

## Inspect and export

```bash
rosenv show --json
rosenv tree --locked
rosenv tree --locked --all
rosenv tree --locked --json
rosenv export --locked --format requirements -o apt-requirements.txt
rosenv export --locked --format json -o environment.json
rosenv export --locked --format dockerfile -o Dockerfile.rosenv
```

`tree` shows requested packages and their resolved versions. `--all` shows the complete installed inventory; this command does not display dependency edges. Without `--locked` or `--frozen`, inspection/export can resolve a stale lock first.

Requirements output is Debian `package=version` syntax, not pip requirements. The Dockerfile contains a pinned base and exact apt installation instructions; it does not copy your project or configure host mounts, users, or ROS networking. It shares the lock's upstream-availability limits.

## Named environments and selection

Named environments are useful for experiments outside a manifest-managed project:

```bash
rosenv env create jazzy-lab --ros jazzy --workspace ~/robot_ws
rosenv run -e jazzy-lab -- ros2 --help
rosenv add -e jazzy-lab demo_nodes_cpp
rosenv local jazzy-lab
rosenv global jazzy-lab
rosenv env list
rosenv env stop jazzy-lab
rosenv env remove jazzy-lab --yes
```

The workspace directory must already exist. Named environments are mutable and do not gain a project lockfile automatically. Stopping preserves their files; removing deletes the container, dedicated network, metadata, and private home while retaining the workspace.

For commands supporting both modes, `--env` and `ROSENV_ENV` explicitly select a named environment. Otherwise, the nearest project manifest takes precedence; without one, rosenv checks the nearest `.rosenv-version`, then the global default. Unset `ROSENV_ENV` to use project-only commands such as `sync`.

Inside a project, `rosenv remove PACKAGE` removes a dependency. Use `rosenv env remove NAME --yes` for unambiguous environment deletion. Top-level `create`, `list`, `local`, `global`, `show`, and `stop` remain available.

## Storage and maintenance

```bash
rosenv cache list
rosenv cache clean --yes
```

Cleanup removes unused rosenv prepared images; it does not run a general Docker prune. Images used by containers are retained. Removing a cache entry can make future synchronization depend on upstream package availability again.

The registry lives in `${XDG_DATA_HOME:-~/.local/share}/rosenv` unless `ROSENV_HOME` is set. Keep workspaces outside this managed storage. Projects at different host paths receive different environment names, even if their manifest names match.

Use `rosenv doctor` for Docker diagnostics, and review its output before sharing because it can include local paths and daemon information. For isolation, networking, and recovery details, see [architecture](architecture.md).
