# Manual smoke checklist

The supported executable boundary is `gupkg.cli:main`, available as `gupkg`,
`python -m gupkg`, or the source-checkout wrapper. Implementation domains live
directly under `gupkg/`, covering shared
utilities, Windows integration, package layout, configuration, metadata
editing, components, origins, and update staging.

Suggested manual checks on a Windows machine:

1. Run `gupkg --help`.
2. Run `gupkg --version`.
3. Run `gupkg config-fix <version-dir>` against a package without
   `pkg.toml` and confirm a documented starter config with commented examples
   is created.
4. Run `gupkg install <version-dir>` against a package with shortcuts,
   environment variables, PATH entries, and wrapper files.
5. Confirm the package `current` junction points to the expected version.
6. Confirm shortcuts and environment changes land in the selected scope.

Manager release checks:

1. Create the documented `gupkg-config.toml`; run user-only and system-only
   `list`, including a missing root, and inspect human and `--toml` output.
2. Put one selector in both roots and verify separate scoped rows, then test
   valid, absent, broken, and bootstrap `current` entries.
3. Run `manager doctor`, `manager update --check-only`, and `manager update --download-only`. Open TUI Upgrade
   All and verify planning performs no install, settings are visible, and the
   result scrolls with per-target states.
4. Run a mixed-scope batch with UAC accepted and declined; declined elevation
   must leave user packages unchanged.
5. Make one target fail while another succeeds, repair the failed target, and
   safely rerun. Confirm returning to the browser shows new versions.
6. Repeat in narrow and short terminals; verify human and TOML output remains
   usable without relying on color or fixed widths.

Standalone and registry checks:

1. Build a release ZIP with `tools\build_standalone.py` using a known runtime
   archive and SHA-256 digest. Confirm the digest is required, a source
   checkout `python\` directory is not copied, and the versioned payload
   contains the embedded runtime, `pkg.toml`, and native shims.
2. Install the release once with `gupkg-bootstrap.exe --scope user`, then
   repeat the install/repair path. Confirm it does not require elevation and
   that `gupkg manager self status` reports a healthy embedded runtime.
3. Run `gupkg --scope user manager self repair` after removing or renaming only a
   user-scope shim/configuration file. Confirm repair restores it. Repeat in
   system scope with UAC accepted and declined; a declined prompt must leave
   system files unchanged.
4. Create a schema-v2 manager config with separate package roots, bin roots,
   and an external registry cache. Verify `gupkg manager doctor`, `gupkg manager
   registry status`, and `gupkg manager search --offline` are read-only and do not create
   missing package roots.
5. Run `gupkg manager registry sync` with network available, then run `gupkg manager search`
   and install one known selector. Disconnect the network and verify cached
   `search --offline` still works while a selector absent from the cache fails
   cleanly.
6. Corrupt or make unavailable a temporary registry checkout and retry sync.
   Confirm the last validated active tree remains usable and the failed sync
   does not replace it.
7. Confirm that a v1 manager config is rejected with a migration diagnostic;
   schema migration is an explicit administrative task outside the runtime CLI.
