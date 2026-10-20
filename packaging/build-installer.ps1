param(
    [string]$Version = "",
    [string]$CertificateThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    # Where the release assets will be downloadable, for example
    # https://github.com/<owner>/<repo>/releases/download/v0.8.0. When set, the
    # update manifest dist\latest.json is written next to the installer.
    [string]$ReleaseBaseUrl = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Version) {
    # The VERSION file is the single source of truth shared with src/version.py
    # and packaging/Bibliobrain.iss.
    $Version = (Get-Content -LiteralPath (Join-Path $RepoRoot "VERSION") -Raw).Trim()
}
if ($Version -notmatch '^\d+\.\d+\.\d+([-.][0-9A-Za-z.]+)?$') {
    throw "Invalid version string '$Version'. Update the VERSION file at the repository root."
}
$InnoScript = Join-Path $PSScriptRoot "Bibliobrain.iss"
# OutputBaseFilename in Bibliobrain.iss.
$OutputPath = Join-Path $RepoRoot "dist\Bibliobrain-Setup-$Version.exe"
# Same files without an executable, for machines whose antivirus quarantines
# the unsigned installer (ARCHIVE_FILENAME in src/update_manifest.py).
$ArchivePath = Join-Path $RepoRoot "dist\Bibliobrain-Windows.zip"

# Python writes the ZIP package and the update manifest (scripts/release_tools.py).
$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $python = (Get-Command "python.exe" -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    throw "Python is required to write the ZIP package and the update manifest."
}

$isccCandidates = @()
$isccCommand = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
if ($isccCommand) {
    $isccCandidates += $isccCommand.Source
}
if ($env:LOCALAPPDATA) {
    $isccCandidates += Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
}
if (${env:ProgramFiles(x86)}) {
    $isccCandidates += Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
}
if ($env:ProgramFiles) {
    $isccCandidates += Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"
}
$isccCandidates = $isccCandidates | Where-Object { Test-Path $_ }
$iscc = $isccCandidates | Select-Object -First 1
if (-not $iscc) {
    throw "Inno Setup 6 is required. Install it with: winget install JRSoftware.InnoSetup"
}

$git = Get-Command "git.exe" -ErrorAction SilentlyContinue
if (-not $git) {
    throw "Git is required to create a clean installer source export."
}

$workingTreeStatus = & $git.Source -C $RepoRoot status --porcelain --untracked-files=all
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect the Git working tree."
}
if ($workingTreeStatus) {
    Write-Host ($workingTreeStatus -join [Environment]::NewLine)
    throw "The working tree is not clean. Commit or stash changes before building an installer."
}

$buildRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "bibliobrain-installer-" + [Guid]::NewGuid().ToString("N")
)
$sourceRoot = Join-Path $buildRoot "source"
# PowerShell variable names ignore case: this must not be called $archivePath,
# which would overwrite $ArchivePath (the ZIP package in dist).
$sourceExportPath = Join-Path $buildRoot "source.zip"
New-Item -ItemType Directory -Path $sourceRoot -Force | Out-Null

try {
    & $git.Source -C $RepoRoot archive --format=zip --output=$sourceExportPath HEAD
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $sourceExportPath)) {
        throw "Unable to create the clean Git source export."
    }
    Expand-Archive -LiteralPath $sourceExportPath -DestinationPath $sourceRoot -Force

    $requiredLegalPaths = @(
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "THIRD_PARTY_LICENSES"
    )
    $missingLegalPaths = @(
        $requiredLegalPaths | Where-Object {
            -not (Test-Path -LiteralPath (Join-Path $sourceRoot $_))
        }
    )
    if ($missingLegalPaths.Count -gt 0) {
        throw "Clean source export is missing required legal material: $($missingLegalPaths -join ', ')"
    }

    & $iscc "/DMyAppVersion=$Version" "/DSourceRoot=$sourceRoot" $InnoScript
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $OutputPath)) {
        throw "Bibliobrain installer build failed."
    }

    & $python (Join-Path $RepoRoot "scripts\release_tools.py") package `
        --source $sourceRoot `
        --output $ArchivePath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $ArchivePath)) {
        throw "The installer was built but the ZIP package could not be written."
    }
}
finally {
    if (Test-Path -LiteralPath $buildRoot) {
        Remove-Item -LiteralPath $buildRoot -Recurse -Force
    }
}

if ($CertificateThumbprint) {
    $signtool = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if (-not $signtool) {
        throw "signtool.exe is required to sign the installer. Install the Windows SDK or omit -CertificateThumbprint."
    }
    & $signtool.Source sign /sha1 $CertificateThumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 $OutputPath
    if ($LASTEXITCODE -ne 0) {
        throw "The installer was built but code signing failed."
    }
}

Write-Host "Installer ready: $OutputPath" -ForegroundColor Green
Write-Host "ZIP package ready: $ArchivePath" -ForegroundColor Green

if ($ReleaseBaseUrl) {
    # The manifest hashes the final (signed) file: build it last.
    & $python (Join-Path $RepoRoot "scripts\release_tools.py") manifest `
        --installer $OutputPath `
        --archive $ArchivePath `
        --release-base-url $ReleaseBaseUrl `
        --version $Version `
        --output-dir (Split-Path -Parent $OutputPath)
    if ($LASTEXITCODE -ne 0) {
        throw "The installer was built but the update manifest could not be written."
    }
}
