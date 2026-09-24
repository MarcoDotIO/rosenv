<p align="center">
  <img src="assets/wordmark.svg" alt="rosenv — ROS 2 environments, defined in your project" width="800">
</p>

<p align="center">
  <a href="https://github.com/MarcoDotIO/rosenv/actions/workflows/ci.yml"><img src="https://github.com/MarcoDotIO/rosenv/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-65e2b8" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/Python-3.11%2B-6a8aac" alt="Python 3.11 or later">
  <img src="https://img.shields.io/badge/host-Ubuntu%2024.04-6a8aac" alt="Ubuntu 24.04 host">
</p>

**A project and dependency manager for ROS 2.** Choose a ROS distribution, declare packages, commit a lockfile, and run your workspace inside a persistent Docker environment.

[Get started](#quick-start) · [User guide](docs/guide.md) · [Features](docs/features.md) · [Architecture](docs/architecture.md) · [Contributing](CONTRIBUTING.md)

## Features

- **Distribution isolation.** Run Humble and Jazzy in separate environments on the same Ubuntu 24.04 host.
- **Declarative dependencies.** `rosenv.toml` declares ROS and Ubuntu packages; `rosenv.lock` records the resolved package inventory and base image.
- **Workspace execution.** `rosenv run`, `build`, and `test` enter the environment and source ROS plus your workspace overlay.
- **Persistent storage.** The workspace is mounted from the host. Container replacement preserves the project workspace and private home.

rosenv manages ROS userspace with Docker. It does not install ROS into the host operating system or promise isolation from hostile code.

## Install

You need **Linux, Python 3.11+, and access to a running Docker Engine**. Ubuntu 24.04 is the tested host platform. Follow [Docker's Ubuntu installation guide](https://docs.docker.com/engine/install/ubuntu/) if necessary; rosenv does not change Docker permissions.

```bash
git clone https://github.com/MarcoDotIO/rosenv.git
cd rosenv
uv tool install .
rosenv doctor
```

Use `pipx install .` if you prefer pipx. With uv, `uv tool update-shell` configures your tool directory in future shells. The Python distribution is `rosenv-manager`; the command is `rosenv`. Installation from PyPI is not assumed. [Release artifacts](https://github.com/MarcoDotIO/rosenv/releases) can also be installed with `uv tool install ./rosenv_manager-<version>-py3-none-any.whl` when available.

## Quick start

```bash
mkdir -p ~/robot_ws/src
cd ~/robot_ws

rosenv init --ros jazzy             # Write rosenv.toml; no Docker download yet
rosenv add demo_nodes_cpp apt:git   # Declare dependencies and prepare the environment
rosenv run -- ros2 run demo_nodes_cpp talker
```

In another terminal in the same project:

```bash
rosenv run -- ros2 run demo_nodes_cpp listener
```

Press Ctrl-C to stop each node. Commit `rosenv.toml` and `rosenv.lock` with your source. On another compatible Linux machine:

```bash
rosenv sync --locked
rosenv build                      # Uses colcon --symlink-install by default
rosenv test
```

A lock pins the base image and exact Debian package versions, including transitive dependencies. rosenv reuses a prepared image when available or reconstructs that inventory from the pinned base. **Reconstruction requires the locked package versions to remain available upstream**; unavailable versions cause an error instead of an unrequested upgrade. See [locking and reproducibility](docs/guide.md#locking-and-reproducibility).

For a runnable package with a ROS node and tests, start with [the hello-ros example](examples/hello-ros):

```bash
cd examples/hello-ros
rosenv build
rosenv test
rosenv task hello
```

## Project configuration

```toml
[project]
name = "robot_ws"
ros = "jazzy"
variant = "ros-base"
dependencies = ["demo_nodes_cpp", "apt:git"]

[dependency-groups]
dev = ["apt:python3-pytest"]

[environment]
network = "bridge"
domain-id = 42

[tasks]
talker = ["ros2", "run", "demo_nodes_cpp", "talker"]
```

ROS names such as `demo_nodes_cpp` resolve to `ros-jazzy-demo-nodes-cpp`; `apt:` selects ordinary Ubuntu packages. Tasks are argument arrays, with no implicit shell expansion. [Full workflow and configuration](docs/guide.md).

## Commands

| Workflow | Commands |
| --- | --- |
| Start a project | `rosenv init`, `rosenv pin jazzy` |
| Declare packages | `rosenv add demo_nodes_cpp`, `rosenv remove demo_nodes_cpp` |
| Resolve and reproduce | `rosenv lock`, `rosenv lock --check`, `rosenv sync --locked` |
| Run and develop | `rosenv run -- ros2 …`, `rosenv shell`, `rosenv build`, `rosenv test` |
| Inspect and export | `rosenv tree`, `rosenv export --format dockerfile` |
| Manage named environments | `rosenv env create`, `rosenv env list`, `rosenv env stop`, `rosenv env remove` |
| Maintain the local cache | `rosenv cache list`, `rosenv cache clean --yes` |

Existing `create`, `list`, `local`, `global`, `show`, and `stop` workflows remain available. Use `rosenv --help` and each command's `--help` for flags. See [feature behavior and scope](docs/features.md) for synchronization modes, dependency profiles, and cache behavior.

## ROS distributions

All five distributions run in their own container userspace on the Ubuntu 24.04 host. Catalog entries do not imply equal integration coverage.

| ROS 2 | Container Ubuntu | Upstream support ends |
| --- | --- | --- |
| Humble | 22.04 Jammy | May 2027 |
| Jazzy | 24.04 Noble | May 2029 |
| Kilted | 24.04 Noble | December 2026 |
| Lyrical | 26.04 Resolute | May 2031 |
| Rolling | 26.04 Resolute | Development distribution |

Jazzy is the default. `ros-core`, `ros-base`, and `perception` select official image variants. CI exercises Jazzy and Humble; the extended workflow exercises Lyrical. Kilted, Rolling, ARM64, hardware, and GUI support are not established by that matrix. Distribution data follows [ROS releases](https://github.com/ros2/ros2_documentation/blob/rolling/source/Releases.rst) and [official image definitions](https://github.com/docker-library/official-images/blob/master/library/ros).

## Runtime limitations

Default networking uses a dedicated Docker bridge and an allocated `ROS_DOMAIN_ID`. Host networking is opt-in for LAN discovery. Normal commands run with your host UID/GID; package management runs as root inside the container. Your mounted workspace and private home are writable, and files inside them are visible to container commands.

A lock describes one architecture and selected dependency-group profile. Source overlays, arbitrary shell changes, host devices, and robot behavior are outside its package guarantee. This release does not provide GUI/GPU/USB passthrough, a native macOS/Windows backend, or automatic host-shell activation. [Architecture and isolation](docs/architecture.md) explains the details.

## Development

```bash
uv sync --locked --group dev
uv run ruff check src tests scripts .github/scripts
uv run ruff format --check src tests scripts .github/scripts
uv run pytest -q
uv build
```

CI checks Python 3.11–3.13, formatting, unit tests, distribution contents, clean wheel installation, and real Docker/ROS workflows. [Contributing](CONTRIBUTING.md) covers integration tests; [releasing](docs/releasing.md) describes gated GitHub Releases and the manual rehearsal workflow.

Licensed under [MIT](LICENSE).
