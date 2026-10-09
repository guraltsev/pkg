# Troubleshooting

Find the message you saw, then apply the fix. Every `gupkg` error is printed
once, prefixed with `ERROR:`, after the command's status line; the exit status
is `2` when something about your input or configuration must change, `3` when a
change to the system failed, and `4` for an unexpected internal error.

## First steps for any problem

```bat
gupkg config-check <package>    :: lists every configuration problem at once
gupkg manager doctor            :: the same, across all managed packages
gupkg manager self status       :: is gupkg's own installation healthy?
```

## Messages and fixes

### Configuration

| Message | What it means and what to do |
| --- | --- |
| `Name/Version/LocalVersion/Portable flag mismatch: directory=..., config=...` | The folder name and `pkg.toml` disagree. The folder name wins. Run `gupkg config-fix <version folder>` to correct the file. |
| `Unknown key 'x' in ...` / `Unsupported legacy key ...` | A typo, or an old spelling. The message names the allowed keys or the replacement. |
| `Package root has no "current" junction and contains multiple version directories` | You gave the package folder, but several versions exist and none is active. Pass the version folder, for example `C:\opt\tool\v1.2.0`. |
| `Version directory does not exist` / `Package root does not exist` | Check the path. Relative paths are resolved from the current folder; with no path, the current folder is used. |
| `Error loading TOML config ...` | `pkg.toml` is not valid TOML; the message includes the line. |
| `Git origin and update check must use the same ref` | `[origin].ref` and `[update.check].ref` differ; make them equal or omit one. |

### Install and scope

| Message | What it means and what to do |
| --- | --- |
| `Machine scope requires administrator privileges` | Open an elevated terminal, or use `--scope user`. |
| `only_portable packages cannot be installed system-wide` | The package stores settings in its own folder (`-portable` in the name). Use `--scope user`. |
| `Origin population failed: ...` | `App` was empty and the download or script failed; the reason follows. Fix and rerun `install`; nothing half-installed is left. |
| `[origin].checksum did not match downloaded file` | The download is corrupt or the file changed upstream. Retry; if upstream really changed, update the checksum. `--no-checksum` skips verification with a warning (avoid). |
| `One or more install steps failed` | Each failing shortcut, variable, PATH entry, or wrapper is listed beneath; the rest were still applied. Fix and rerun `install`. |
| A new command or variable is "not found" | Windows gives new environment values only to new processes. Open a new terminal. |

### Updates

| Message | What it means and what to do |
| --- | --- |
| `An update operation is already active: ...\.gupkg\locks\update.toml` | Another `gupkg` is updating this package. If none is running (it was killed), delete that lock file. |
| `Update candidate requires a sha256 checksum` | The release publishes no digest. Add `ignore_checksum = true` to `[update.payload]` if you trust the source, or pass `--no-checksum` once. |
| `Update checksum did not match downloaded file` | The download was corrupt or tampered with. Nothing was changed; retry. |
| `Cannot stage update because its immutable version already exists` | A version folder with this number exists, from a different release. Compare it with what you expect; delete the folder and the matching file in `.gupkg\receipts\` to restage. |
| `Update candidate is older than the active version` | Upstream's latest is older than what you run. Nothing to do. |
| `No downloaded upgrade is waiting to be installed` | You asked to activate a staged update, but there is none newer than the active version. Run `gupkg update <package> --download-only`. |
| `Package-local dependency unavailable: X` | A package's own update script needs Python module X. Install it, or rerun with `--allow-hook-dependency-install` (those scripts are trusted code, so only for packages you trust). |
| `Updates are not configured for this package` | The `pkg.toml` has no `[update]` table. This is a warning, not an error. |
| `fatal: ... Filename too long` while cloning (Git packages) | Enable long paths: `git config --global core.longpaths true`, and keep the package root path short. |

### Manager and registry

| Message | What it means and what to do |
| --- | --- |
| `No manager configuration found; searched: ...` | Run `gupkg manager init` to create one with defaults, or pass `manager --config <file>`. See [manager configuration](operations.md#manager-configuration). |
| `Registry synchronization failed: ...` | The download or validation failed; the previous catalogue stays active. Check network access and `[registry] source`. |
| `manager update requires --yes` | Without a terminal (or with `--format toml`) there is nobody to confirm. Add `--yes`. |
| `manager install requires --scope user or --scope system` | Put the option before `manager`: `gupkg --scope user manager install NAME`. |
| `No validated registry cache is available` | Run `gupkg manager registry sync` (needs network access to the registry source), or drop `--offline`. |
| `Registry selectors cannot be paths` | Give the registry name, such as `vscode`, not a folder. |
| A root is reported `incomplete` | A configured package folder is missing or unreadable, so bulk updates for that scope are skipped. Create or fix it and rerun. |

## Cleaning up

Safe to delete at any time when no `gupkg` is running:

- `<package>\.gupkg\work\` &mdash; disposable download and staging space.
- `<package>\.gupkg\locks\update.toml` &mdash; a stale lock from a killed run.
- Old `pkg.toml.bak.*` files from `config-fix`.

Never delete the version folder `current` points to, or `<package>\.gupkg\receipts\`
while a staged update is waiting to be activated.

## Still stuck

Re-run with `--format toml` and keep the output: it contains the exact status,
exit code, warnings, and errors, which makes a problem easy to describe or
report. Include the `pkg.toml` (with secrets removed) and the output of
`gupkg --version`.
