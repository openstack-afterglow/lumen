# Plugin image packaging recovery

## Goal
Prevent a successful image build from shipping service metadata that declares workspace plugins while the installed environment and runtime source tree omit them.

## Evidence and scope
The failed local lumen-migrate image declares all six plugin dependencies in both pyproject.toml and installed Lumen metadata, but its uv.lock contains only the Lumen root package, none of the plugin distributions are installed, and workspace source directories are absent. Migration exits during import before SQL execution. Current development has already added workspace manifests/source copies and regenerated the lock; preserve those changes.

Use lock-validation (`uv sync --locked`) in Docker dependency installs and execute the real migration CLI `--help` from the final non-root runtime during build. No migration/checksum, configuration, plugin API or database behavior changes. Preserve concurrent development and all existing data/volumes. Verify the current API, worker and shared-runtime controller targets for amd64/arm64, then canonical local Compose migration and authenticated service reads. Sandbox is a separate stage with no shared runtime dependency.
