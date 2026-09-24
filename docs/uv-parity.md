# How rosenv relates to uv

rosenv adopts [uv's project workflow](https://docs.astral.sh/uv/concepts/projects/) and pyenv's named-version selection. Its dependency model is ROS and Ubuntu packages inside Docker, with Debian's resolver determining compatibility. The locking flags follow uv's distinction between [locked, frozen, and unsynchronized execution](https://docs.astral.sh/uv/concepts/projects/sync/).

| Familiar workflow | rosenv equivalent | Important difference |
| --- | --- | --- |
| `uv init` | `rosenv init` | Writes `rosenv.toml`; creation is offline unless synchronization is requested. |
| `uv add` / `uv remove` | `rosenv add` / `rosenv remove` | Declares ROS/apt dependencies; environment replacement removes stale packages. |
| `uv lock` | `rosenv lock` | Records a full apt inventory and pinned container base, for one architecture/profile. |
| `uv sync --locked` | `rosenv sync --locked` | Refuses a stale lock and reconstructs locked package versions when necessary. |
| `uv sync --frozen` | `rosenv sync --frozen` | Uses the existing lock without refreshing it; it can intentionally differ from the manifest. |
| `uv run` | `rosenv run` | Synchronizes a project, enters its container, and sources ROS and the workspace overlay. |
| Dependency groups | `[dependency-groups]` | `dev` is included by default; the lock represents one selected profile at a time. |
| Script/tool shortcuts | `[tasks]`, `rosenv build`, `rosenv test` | Tasks are argv arrays; build and test wrap colcon. |
| `uv export` / `uv tree` | `rosenv export` / `rosenv tree` | Exports apt inventory or a Dockerfile; inspection distinguishes requested packages from full installed inventory. |
| Interpreter pinning | `rosenv pin DISTRO` | Pins a ROS distribution in the project manifest. |
| `uv cache` | `rosenv cache` | Manages rosenv's prepared-image cache, not a general Docker prune. |
| pyenv local/global | `rosenv local` / `rosenv global` | Selects a named environment; it does not rewrite the host shell's `PATH`. |

This is a focused project manager, not a drop-in implementation of every uv command. It does not provide a Python package resolver, pip compatibility, Python installation, tool installation from arbitrary registries, workspace-member resolution, universal multi-platform locks, or uv's Rust implementation and performance guarantees.

A lock does not archive upstream `.deb` packages. A cached prepared image can preserve a resolved environment locally; reconstruction from a base image depends on the availability of exact historical package versions. When those versions disappear, rosenv fails rather than claiming a different inventory is equivalent.
