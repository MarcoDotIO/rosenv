# Changelog

## 0.2.0 — 2026-11-05

- Separate runtime configuration from dependency locks; network/domain edits no longer refresh packages or rebuild images. Read existing schema 1 locks and migrate to dependency-only schema 2 without re-resolving.
- Add TOML command variables and repeatable `--set-env` overrides for execution commands.
- Map explicit Linux devices with their required numeric groups and detect changed device identities during sync.
- Support macOS hosts with OrbStack or Docker Desktop, relaying declared serial devices into containers with baud-rate forwarding.
- Add `--config` for independent manifests/locks/environments and `sync --dry-run` for read-only reconciliation previews.

- Add project manifests and lockfiles with a pinned ROS base image and complete Debian package inventory.
- Add dependency groups, locked synchronization, package removal, distro pinning, argv-based tasks, and colcon build/test commands.
- Add package inspection, exports, and prepared-image cache management.
- Retry transient apt failures with refreshed package indexes while preserving exact locked versions.
- Preserve named-environment workflows, private bridge networking, per-user execution, and ownership-checked cleanup.
- Add gated CI, real Docker integration, distribution audits, and checksummed GitHub Release artifacts.

The earlier environment-manager implementation is the basis of this release; it was not a separate published release of this repository.
