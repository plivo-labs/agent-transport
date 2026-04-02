# Bump Version

Bump Python and/or Node native package versions for agent-transport releases.

## Version files

### Python
- `crates/agent-transport-python/pyproject.toml`

### Node
- `crates/agent-transport-node/package.json`
- `crates/agent-transport-node/npm/darwin-arm64/package.json`
- `crates/agent-transport-node/npm/darwin-x64/package.json`
- `crates/agent-transport-node/npm/linux-arm64-gnu/package.json`
- `crates/agent-transport-node/npm/linux-x64-gnu/package.json`
- `crates/agent-transport-node/npm/win32-arm64-msvc/package.json`
- `crates/agent-transport-node/npm/win32-x64-msvc/package.json`

## Steps

### 1. Read current versions

Read the current version from:
- `crates/agent-transport-python/pyproject.toml` (the `version` field under `[project]`)
- `crates/agent-transport-node/package.json` (the `"version"` field)

Report both to the user.

### 2. Ask which packages to bump

Use `AskUserQuestion` to ask:
- **Question**: "Which packages do you want to bump?"
- **Options**: `Python`, `Node`, `All`

### 3. Ask for the target version

Based on the current version, compute semver suggestions (patch, minor, major). For example, if the current version is `0.1.1`, suggest `0.1.2` (patch), `0.2.0` (minor), `1.0.0` (major).

Use `AskUserQuestion` to ask for each selected package:
- **Question**: "What version should {package} be bumped to? (currently {current_version})"
- **Options**: the three semver suggestions (patch as recommended), plus the user can type a custom version via "Other"

If "All" was selected and both packages are on the same version, ask once. If they differ, ask separately for each.

### 4. Confirm before proceeding

Use `AskUserQuestion` to show a summary and ask for confirmation:
- **Question**: "Confirm version bump: {summary of changes}?"
- **Options**: `Yes, proceed`, `No, cancel`

If the user cancels, stop.

### 5. Apply version changes

For **Python**: edit the `version` field in `crates/agent-transport-python/pyproject.toml`.

For **Node**: edit the `"version"` field in ALL of these files:
- `crates/agent-transport-node/package.json`
- `crates/agent-transport-node/npm/darwin-arm64/package.json`
- `crates/agent-transport-node/npm/darwin-x64/package.json`
- `crates/agent-transport-node/npm/linux-arm64-gnu/package.json`
- `crates/agent-transport-node/npm/linux-x64-gnu/package.json`
- `crates/agent-transport-node/npm/win32-arm64-msvc/package.json`
- `crates/agent-transport-node/npm/win32-x64-msvc/package.json`

### 6. Update lockfile

If Node was bumped, run `npm install --package-lock-only` in `crates/agent-transport-node/` to update `package-lock.json`.

### 7. Verify

After applying changes, read all modified files and confirm the versions are correct. Print a summary of what was changed.

## Important

- Do NOT commit or push changes. This skill only modifies version files.
- Do NOT modify any files other than the ones listed above.
- All user input must go through `AskUserQuestion` — do not assume versions or package selections.
