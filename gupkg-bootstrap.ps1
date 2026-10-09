<#
.SYNOPSIS
    Installs gupkg itself, then sets up manager mode, in one step.

.DESCRIPTION
    Downloads (or copies) a gupkg archive, optionally verifies its SHA-256,
    places it at <Root>\gupkg\v<version>, and runs "gupkg install" on it so the
    'current' junction, the gupkg / gupkg-tui commands, and PATH are created.
    It then runs "gupkg manager init" to write a default gupkg-config.toml and
    create the package folders. Every step is safe to repeat: an existing
    version folder is reused, and install re-applies the Windows integration.

    Needs Windows PowerShell 5.1 or newer. No Python, Git, or pip is required in
    advance: gupkg downloads a verified embedded Python the first time it runs
    when none is installed.

.PARAMETER Scope
    user (default): per-user, no elevation. system: all users, needs an
    elevated PowerShell.

.PARAMETER Source
    A ZIP archive (https:// URL or local path) or a folder containing gupkg.
    Accepts a release bundle produced by tools\build_standalone.py or a
    repository archive that contains src\gupkg. Defaults to the 'stable' tag
    of the official repository.

.PARAMETER Sha256
    Expected SHA-256 of the archive. Strongly recommended for anything but the
    default source; the script stops if it does not match.

.PARAMETER Root
    Package root to install under. Defaults to %USERPROFILE%\opt for user scope
    and %SYSTEMDRIVE%\opt for system scope (the same defaults as manager init).

.PARAMETER SkipInstall
    Only place the files; do not create junction, commands, or PATH entries.

.PARAMETER SkipManagerInit
    Do not create a default manager configuration.

.PARAMETER Force
    Replace an existing <Root>\gupkg\v<version> folder instead of reusing it.

.EXAMPLE
    .\gupkg-bootstrap.ps1
    Installs gupkg for the current user and sets up manager mode.

.EXAMPLE
    .\gupkg-bootstrap.ps1 -Scope system -Source C:\drop\gupkg-0.12.0.zip -Sha256 <digest>
    Installs a vetted release bundle for all users (run from an elevated shell).
#>
[CmdletBinding()]
param(
    [ValidateSet('user', 'system')]
    [string]$Scope = 'user',
    [string]$Source = 'https://github.com/guraltsev/pkg/archive/refs/tags/stable.zip',
    [string]$Sha256,
    [string]$Root,
    [switch]$SkipInstall,
    [switch]$SkipManagerInit,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# Older Windows PowerShell defaults to protocols GitHub rejects.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Step([string]$Message) { Write-Host "[gupkg] $Message" }

function Find-GupkgFolder([string]$Folder) {
    # A gupkg folder holds pkg.toml and gupkg\cli.py; choose the shallowest.
    $candidates = @(Get-Item -LiteralPath $Folder) + @(Get-ChildItem -LiteralPath $Folder -Recurse -Directory)
    $found = $candidates |
        Where-Object { (Test-Path (Join-Path $_.FullName 'pkg.toml')) -and (Test-Path (Join-Path $_.FullName 'gupkg\cli.py')) } |
        Sort-Object { $_.FullName.Length } |
        Select-Object -First 1
    if (-not $found) { throw "No gupkg folder (pkg.toml plus gupkg\cli.py) was found in: $Source" }
    return $found.FullName
}

$work = Join-Path ([IO.Path]::GetTempPath()) ("gupkg-bootstrap-" + [Guid]::NewGuid().ToString('N'))
try {
    # 1. Decide where gupkg goes, and refuse early when the scope cannot work.
    if ($Scope -eq 'system') {
        $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw "System scope needs an elevated PowerShell. Re-run as Administrator, or use -Scope user."
        }
    }
    if (-not $Root) {
        $Root = if ($Scope -eq 'system') { Join-Path ($env:SystemDrive + '\') 'opt' } else { Join-Path $env:USERPROFILE 'opt' }
    }
    New-Item -ItemType Directory -Force -Path $work | Out-Null

    # 2. Get the archive (or folder) and verify it before unpacking anything.
    if (Test-Path -LiteralPath $Source -PathType Container) {
        Step "Using folder $Source"
        $tree = $Source
    } else {
        $archive = Join-Path $work 'gupkg.zip'
        if (Test-Path -LiteralPath $Source) {
            Step "Using archive $Source"
            Copy-Item -LiteralPath $Source -Destination $archive
        } elseif ($Source -match '^https?://') {
            Step "Downloading $Source"
            Invoke-WebRequest -UseBasicParsing -Uri $Source -OutFile $archive
        } else {
            throw "Source not found (expected an http(s) URL, a ZIP file, or a folder): $Source"
        }
        if ($Sha256) {
            $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash
            if ($actual -ne $Sha256) { throw "SHA-256 mismatch: expected $Sha256 but the archive is $actual" }
            Step 'SHA-256 verified'
        } else {
            Write-Warning 'No -Sha256 given: the download is not verified.'
        }
        $tree = Join-Path $work 'unpacked'
        Expand-Archive -LiteralPath $archive -DestinationPath $tree -Force
    }

    # 3. Place it at <Root>\gupkg\v<version>, reusing an existing copy.
    $payload = Find-GupkgFolder $tree
    $manifest = Get-Content -LiteralPath (Join-Path $payload 'pkg.toml') -Raw
    if ($manifest -notmatch '(?m)^\s*version\s*=\s*"([^"]+)"') { throw "pkg.toml in $payload declares no version." }
    $version = $Matches[1]
    $target = Join-Path (Join-Path $Root 'gupkg') "v$version"
    if ((Test-Path -LiteralPath $target) -and $Force) {
        Step "Replacing $target"
        Remove-Item -LiteralPath $target -Recurse -Force
    }
    if (Test-Path -LiteralPath $target) {
        Step "Version $version is already at $target; reusing it"
    } else {
        Step "Installing gupkg $version to $target"
        New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
        Copy-Item -LiteralPath $payload -Destination $target -Recurse
    }
    $launcher = Join-Path $target 'gupkg.cmd'
    if (-not (Test-Path -LiteralPath $launcher)) { throw "Missing launcher: $launcher" }
    if ($SkipInstall) {
        Step "Files are in place. Finish later with: `"$launcher`" --scope $Scope install `"$target`""
        return
    }

    # 4. Activate: current junction, gupkg / gupkg-tui commands, PATH.
    Step 'Running gupkg install'
    & $launcher --scope $Scope install $target
    if ($LASTEXITCODE -ne 0) { throw "gupkg install failed (exit $LASTEXITCODE)." }

    # 5. Create the default manager configuration, unless one already exists.
    if (-not $SkipManagerInit) {
        Step 'Setting up manager mode'
        & $launcher manager init
        if ($LASTEXITCODE -eq 2) {
            Step 'A manager configuration already exists; leaving it unchanged'
        } elseif ($LASTEXITCODE -ne 0) {
            throw "gupkg manager init failed (exit $LASTEXITCODE)."
        }
    }
    Step 'Done. Open a NEW terminal, then try:  gupkg --help'
    Step 'Next: gupkg manager registry sync'
}
catch {
    [Console]::Error.WriteLine("[gupkg] ERROR: $($_.Exception.Message)")
    exit 1
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
