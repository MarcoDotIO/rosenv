# Contributing to rosenv

Small, reviewable changes with a reproducible example are welcome. Report the ROS distribution, host OS, Python version, Docker version, and rosenv version with a bug. Remove credentials and private paths from logs.

## Set up

```bash
git clone https://github.com/MarcoDotIO/rosenv.git
cd rosenv
uv sync --locked --group dev
uv run rosenv --help
```

Unit tests run without a Docker daemon. ROS integration requires a non-root Linux account with access to Docker. Do not run the test suite with `sudo`: environment files must remain owned by the invoking user.

## Check a change

```bash
uv run ruff check src tests scripts .github/scripts
uv run ruff format --check src tests scripts .github/scripts
uv run pytest -q
uv build
uv run twine check --strict dist/*
uv run python .github/scripts/verify_dist.py
```

Update `uv.lock` when changing Python dependencies. `uv.lock` manages rosenv's own Python dependencies; project `rosenv.lock` files manage ROS environments. Keep this distinction clear in changes and documentation.

For changes to container operations, dependency resolution, or synchronization, run the relevant real integration test:

```bash
uv run python scripts/smoke_test.py --distros jazzy --report /tmp/rosenv-environment.json
uv run python scripts/project_smoke.py --distro jazzy --report /tmp/rosenv-project.json
```

The project smoke test also checks runtime variables, domain changes, network/device replacement using `/dev/null`, alternate manifests, and read-only sync previews.

The scripts download images and packages, create temporary registries and environments, and exercise cleanup. Keep report paths outside your repository unless preparing a reviewed, sanitized validation artifact. Run against a development machine with sufficient disk space. Add Humble when changing cross-distribution behavior; Lyrical is covered by the extended workflow.

## Design expectations

- Preserve a user's workspace and private home during project synchronization. Environment deletion must make its destructive scope explicit.
- Verify daemon identity and resource ownership before mutation. A missing daemon or permission failure is not evidence that a container is absent.
- Pass user commands as argument vectors. Keep any shell program fixed and pass data through positional arguments.
- Fail on unavailable locked versions or inventory mismatch. Do not silently convert a locked operation into an upgrade.
- Test observable behavior and failure recovery. Mock engine calls in unit tests; use the Docker scripts for actual ROS claims.
- Keep package metadata, runtime version, docs, and changelog consistent. Do not commit `.env`, credentials, workstation receipts, or macOS `._*` files.

Pull requests should explain the concrete behavior change and the checks that support it. CI runs with read-only repository permissions; releases use a separate gated job. There is no requirement to claim hardware support for a change validated only in containers.

## Physical device checks

Use a separate workspace and `ROSENV_HOME` registry for hardware validation. Declare the actual device with a stable host path, for example `devices = ["/dev/serial/by-id/YOUR_DEVICE:/dev/ttyACM0:rw"]` under `[environment]`. Confirm the driver can open the port as the normal host UID; a device node's presence alone is not a successful hardware test. For a LiDAR, additionally subscribe to point-cloud and IMU topics and check for nonempty data. Keep machine-specific paths, serial numbers, measurements, and reports outside the repository.

For the [official Unitree L2 driver](https://github.com/unitreerobotics/unilidar_sdk2), serial operation uses `initialize_type:=1`, `work_mode:=8`, and the mapped `serial_port`. Preserve the serial work-mode bit: the driver sends its work mode on startup. The sensor needs its separate power supply in addition to USB. Follow the vendor's setup instructions for the device before running it.

Check the lifecycle separately: change a command variable and verify the container/image IDs and lockfile are unchanged; change a device mapping or network and verify only the container changes. Unplug the USB adapter, confirm sync reports the missing device without replacing the existing environment, then reconnect, sync, and restart the driver. This exercises explicit resynchronization; it does not establish automatic hotplug support.
