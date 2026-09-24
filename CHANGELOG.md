# Changelog

## 0.2.0 — Unreleased

- Add project manifests and lockfiles with a pinned ROS base image and complete Debian package inventory.
- Add dependency groups, locked synchronization, package removal, distro pinning, argv-based tasks, and colcon build/test commands.
- Add package inspection, exports, and prepared-image cache management.
- Retry transient apt failures with refreshed package indexes while preserving exact locked versions.
- Preserve named-environment workflows, private bridge networking, per-user execution, and ownership-checked cleanup.
- Add gated CI, real Docker integration, distribution audits, and checksummed GitHub Release artifacts.

The earlier environment-manager implementation is the basis of this release; it was not a separate published release of this repository.
