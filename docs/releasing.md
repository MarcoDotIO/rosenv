# CI and releases

The repository delivers installable Python wheels and source archives through GitHub Releases. It does not publish to PyPI or a container registry.

## Continuous integration

Pull requests, pushes to `main`, and manual CI runs use the same reusable workflows:

- **Quality:** Python 3.11, 3.12, and 3.13 unit tests; Ruff lint and formatting; wheel/source builds; strict metadata checks; archive inspection; and installation of the built wheel into a clean environment.
- **Docker integration:** Jazzy and Humble on Ubuntu 24.04, using a non-root runner account with Docker access. Each matrix entry exercises named environments and the project workflow, saving logs and JSON reports even after failures when available.
- **Extended integration:** a daily or manually triggered Lyrical run, separate from the ordinary CI and release gate.

Python dependencies are resolved through the committed `uv.lock`. External actions are pinned to full commit IDs, with version comments for review. Dependabot proposes updates to GitHub Actions and uv dependencies.

Workflow definitions describe intended checks. Inspect the [Actions run](https://github.com/MarcoDotIO/rosenv/actions) for the exact commit and completed job results before treating a change as verified. Integration covers containerized ROS userspace; it does not validate robots, hardware drivers, GUI applications, or cross-distribution communication.

## Rehearse a release

The **Release** workflow supports manual dispatch on a branch:

```bash
gh workflow run release.yml --ref main
gh run list --workflow release.yml
```

A manual run checks version consistency, executes fresh quality/integration gates, and uploads `python-distributions` artifacts. **It does not create a GitHub Release**, even if dispatched on a tag. Download the artifacts and inspect the wheel, source archive, and `SHA256SUMS` before tagging.

## Publish a version

1. Set the same version in `pyproject.toml` and `src/rosenv/__init__.py`. Update `CHANGELOG.md`, and regenerate `uv.lock` if needed.
2. Merge the release commit into `main` and verify its CI results. A manual Release rehearsal can verify the complete path without publishing.
3. Tag that reviewed commit and push the tag:

   ```bash
   git tag -a v0.2.0 -m "rosenv v0.2.0"
   git push origin v0.2.0
   ```

4. Follow the Release workflow to completion and verify the resulting assets on the repository's [Releases page](https://github.com/MarcoDotIO/rosenv/releases).

For a tag push, the workflow requires the tag name to equal `v` plus the package version, and the runtime version to match. It runs fresh quality and Docker integration checks for the tagged source. Only after every required job succeeds does the publishing job receive `contents: write`, verify the downloaded checksums, and create the GitHub Release with the wheel, source archive, and checksum file.

The default workflow permission is `contents: read`. No personal access token or PyPI credential is required. Configure branch/tag protection and repository access according to the maintainer team's policy; the workflow alone does not enforce who can push a release tag.

## Verify an artifact

Download the assets into one directory, then on Ubuntu:

```bash
sha256sum --check SHA256SUMS
uv tool install ./rosenv_manager-0.2.0-py3-none-any.whl
rosenv --version
```

Checksums detect changes relative to the published checksum file; they are not an independent signature. Repository build audits reject common generated files, credential patterns, and private workstation receipts. Review distribution contents before publishing new artifact types.

If a job fails before publication, fix the issue and rerun appropriate gates. Do not move a published version tag to different code. If a release was partially created, inspect the existing release and assets before retrying: the workflow intentionally uses `gh release create`, not an overwrite operation.
