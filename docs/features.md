# Features and behavior

rosenv manages ROS distributions and Debian package dependencies in persistent Docker environments. Projects declare their configuration in `rosenv.toml` and record resolved environments in `rosenv.lock`.

## Project workflows

| Feature | Interface | Behavior |
| --- | --- | --- |
| Project initialization | `rosenv init` | Creates a manifest without downloading images or packages. `--sync` also prepares the environment. |
| Dependency management | `rosenv add`, `rosenv remove` | Updates declared ROS or Ubuntu packages and synchronizes the environment. `--no-sync` edits the manifest only. |
| Dependency groups | `[dependency-groups]` | Selects optional dependency profiles. The `dev` group is included by default; `--no-dev` excludes it. |
| Distribution selection | `rosenv pin DISTRO` | Updates the project's ROS distribution. Synchronization applies the change. |
| Locking | `rosenv lock` | Records the immutable base image, exact installed Debian inventory, architecture, and selected profile. |
| Locked synchronization | `rosenv sync --locked` | Requires a lock that matches the manifest and selected groups; fails if it is missing or stale. |
| Frozen synchronization | `rosenv sync --frozen` | Uses the existing lock without checking whether the manifest has changed. |
| Command execution | `rosenv run`, `rosenv shell` | Synchronizes the project and sources ROS plus any built workspace overlay. `--no-sync` uses the existing environment. |
| Build and test | `rosenv build`, `rosenv test` | Runs colcon in the environment. Testing also checks colcon's recorded test results. |
| Tasks | `[tasks]`, `rosenv task` | Runs named argument arrays. Shell expansion requires an explicitly invoked shell. |
| Package inspection | `rosenv tree` | Lists direct locked dependencies; `--all` includes the full installed inventory. Output does not include dependency graph edges. |
| Export | `rosenv export` | Exports a JSON lock, exact apt package requirements, or a Dockerfile. |

## Environments and cache

Named environments can be managed independently with `rosenv env create`, `list`, `show`, `stop`, and `remove`. `rosenv local` selects an environment for a directory; `rosenv global` sets the default. Selection applies to rosenv commands and does not modify the host shell's `PATH`.

Project synchronization replaces a container when the lock changes or its installed package inventory drifts. The host workspace and private home persist across replacement. Deleting an environment removes its managed private home after confirmation; it preserves the host workspace.

`rosenv cache list` and `rosenv cache clean` inspect and remove prepared images owned by the current user. Cleanup does not force removal of images used by containers or prune unrelated Docker resources.

## Reproducibility contract

A lock describes one Linux architecture and one selected dependency profile. Synchronization reuses a prepared image or reconstructs the exact package inventory from the pinned base image. Missing historical package versions or an inventory mismatch cause an error; locked operations never fall back to newer versions.

Locks do not archive upstream `.deb` files, capture source overlays, or guarantee byte-identical image rebuilds. Preserve prepared images when long-term use depends on packages that repositories may stop retaining. See the [user guide](guide.md#locking-and-reproducibility) for lock maintenance and [architecture](architecture.md) for runtime isolation and supported platforms.
