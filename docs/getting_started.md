# Getting started

This walkthrough takes you from nothing to an installed, updatable program in
about five minutes. It uses [pandoc](https://pandoc.org/) as the example; any
program published as a ZIP on GitHub Releases works the same way.

## The idea in one minute

`gupkg` keeps every application in its own folder and describes how it should
appear in Windows in a small file called `pkg.toml`:

```text
C:\opt\pandoc\                 <- the package (the folder name is its name)
  v3.6.1\                      <- one folder per installed version
    pkg.toml                   <- what to put in Start Menu / PATH / etc.
    App\                       <- the program's files
  v3.7.0\                      <- a newer version, installed side by side
  current  ->  v3.7.0          <- a junction to the active version
```

Because versions sit side by side, an update never overwrites what is working,
and rolling back is one command. Because the folder name is the package name
and version, there is nothing to keep in sync by hand.

## 0. Install gupkg

Put `gupkg-bootstrap.ps1` and `gupkg-bootstrap.cmd` in a folder and run
`gupkg-bootstrap.cmd`. It installs gupkg for the current user, creates the
`gupkg` command, and writes a default manager configuration (see
[Installing gupkg](../README.md#installing-gupkg) for system-wide installs and
mirrors). Then open a **new** terminal so the command is on `PATH`.

## 1. Check that it works

```bat
gupkg --version
gupkg --help
```

`gupkg --help` lists the commands with copy-paste examples, and
`gupkg <command> --help` documents every option of one command.

## 2. Describe the program

Create a folder for the package and one folder inside it named
`vbootstrap`. The name `vbootstrap` marks a *template*: `gupkg` will fetch the
newest release for you and create the real versioned folder.

```bat
mkdir C:\opt\pandoc\vbootstrap
notepad C:\opt\pandoc\vbootstrap\pkg.toml
```

Paste this into `pkg.toml`:

```toml
name = "pandoc"
version = "bootstrap"
localVersion = 0

# Where releases come from: the project's GitHub page.
[origin]
url = "https://github.com/jgm/pandoc"

# Which file in each release to download. ${version} is the release version.
[update.check]
mode = "github"
assetName = "pandoc-${version}-windows-x86_64.zip"

[update.payload]
mode = "zip"

# The ZIP contains one folder named pandoc-<version>; copy its contents.
[[update.payload.extract]]
src = "pandoc-*/"
dest = ""

# Put the program on PATH and create a `pandoc` command.
[[path]]
value = "$App"

[[bin]]
name = "pandoc"
target = "$App\\pandoc.exe"
```

`$App` always means "this version's `App` folder"; you never type a
machine-specific path. Other things a `pkg.toml` can declare are Start Menu
shortcuts (`[[shortcut]]`) and environment variables (`[[environment]]`); see the
[`pkg.toml` reference](../README.md#pkgtoml-reference). Many ready-made files
live in the repository's [`pkgs/`](../pkgs) folder, so you can often start by
copying one.

## 3. Check the file

```bat
gupkg config-check C:\opt\pandoc
```

This validates everything without changing anything and reports *all*
problems at once, so a single run tells you what to fix.

Not sure what to write? `gupkg config-fix <folder>` creates a documented
starter `pkg.toml` full of commented examples.

## 4. Install

```bat
gupkg install C:\opt\pandoc
```

`gupkg` finds the newest release, verifies its SHA-256 checksum when GitHub
publishes one, unpacks it into `C:\opt\pandoc\v<version>\App`, points `current`
at it, then creates the command and PATH entry. Open a **new** terminal (Windows
only hands new environment values to new processes) and run:

```bat
pandoc --version
```

Without `--scope`, `gupkg` installs for the current user, or for all users when
your shell is elevated and the package allows it. Add `--scope user` or
`--scope system` to be explicit. System scope needs an Administrator shell.

`install` is safe to repeat. If a shortcut was deleted or PATH was reset,
running it again puts everything back without re-downloading the program.

## 5. Update

```bat
gupkg update C:\opt\pandoc --check-only   # is there a newer release?
gupkg update C:\opt\pandoc                # get it and switch over
```

The new version is downloaded and verified into a fresh folder before anything
changes, so a failed download leaves the working version untouched. The old
version stays on disk.

To prepare now and switch later (for example in a maintenance window):

```bat
gupkg update C:\opt\pandoc --download-only
gupkg update C:\opt\pandoc                # later: activates the staged version
```

## 6. Roll back

Old versions are kept, so going back is just installing one:

```bat
gupkg install C:\opt\pandoc\v3.6.1 --allow-downgrade
```

## Where next

- Many packages at once: [Managing many packages](operations.md#manager-configuration)
  and the [cookbook](cookbook.md).
- Something went wrong: [Troubleshooting](troubleshooting.md), which lists each
  error message with its fix.
- Every `pkg.toml` field: the [reference in the README](../README.md#pkgtoml-reference).
