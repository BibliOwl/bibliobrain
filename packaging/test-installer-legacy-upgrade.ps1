[CmdletBinding()]
param(
    [string]$IsccPath = "",
    # Released installers to update from, one or two per period of the
    # installation layout. AppId, folder and Python never changed; what changed
    # is what install.ps1 left behind:
    #   0.1.0, 0.6.6, 0.7.3  .env and runtime state (.bibliobrain) in the program folder
    #   0.7.4                runtime state under %LOCALAPPDATA%, .env still in the program folder
    #   0.7.6, 0.7.13        configuration.json, no .env any more
    #   0.7.34               first Setup with [InstallDelete] and the update checker
    [string[]]$FromCommit = @("32ef6b0", "66a88ee", "65b38a4", "bd0a277", "5e7f833", "39bfd9a", "9b039ef"),
    # Python of a development environment, used to run the legacy .env migration
    # and to start the application against the old data. Defaults to the .venv
    # of the main checkout (worktrees have none of their own).
    [string]$PythonPath = "",
    [switch]$SkipDataCheck
)

# For every old version, in a temporary folder: install its Setup, update it
# with the current Setup, uninstall, then check the old user data against the
# current install.ps1 -DryRun, the .env migration and the application.
# - Every installer is a smoke build: the AppId of the smoke test, no Desktop
#   shortcut, no uninstall script (it would stop the real server), and a stub
#   instead of install.ps1, so nothing of the real installation is touched
#   (checked before and after). The stub of an old version leaves in the
#   program folder what that version's install.ps1 left there.
# - The old Setup is rewritten, not built with its own PackagingSmoke: before
#   0.7.10 there was none, and later ones skip install.ps1 entirely.
# - Not covered: PrepareToInstall (the current Setup stops the old server with
#   the old bibliobrain-manager.ps1), absent from smoke builds; the real
#   install.ps1, which needs Python, pip and Ollama; the wizard pages.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
$InnoScript = Join-Path $PSScriptRoot "Bibliobrain.iss"
$Version = (Get-Content -LiteralPath (Join-Path $RepoRoot "VERSION") -Raw).Trim()
$ProductAppId = "{8D9DBE54-77CD-4B79-B443-C42D54303ED7}"
$SmokeAppId = "{F49C6DE4-7D27-49C8-81E7-C910C06F6510}"
$UninstallKeys = Join-Path "HKCU:\Software\Microsoft\Windows\CurrentVersion" "Uninstall"
$SmokeKey = Join-Path $UninstallKeys ($SmokeAppId + "_is1")
$ProductKey = Join-Path $UninstallKeys ($ProductAppId + "_is1")

if (-not $IsccPath) {
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
    $IsccPath = $isccCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $IsccPath -or -not (Test-Path -LiteralPath $IsccPath)) {
    throw "Inno Setup 6 (ISCC.exe) is required for the legacy upgrade smoke test."
}
if (-not $SkipDataCheck -and -not $PythonPath) {
    $commonDir = (& git -C $RepoRoot rev-parse --path-format=absolute --git-common-dir).Trim()
    $PythonPath = Join-Path (Split-Path -Parent $commonDir) ".venv\Scripts\python.exe"
}
if (-not $SkipDataCheck -and -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "The data check needs the Python of a development environment (-PythonPath), or -SkipDataCheck."
}
if (Test-Path -LiteralPath $SmokeKey) {
    $leftover = (Get-ItemProperty -LiteralPath $SmokeKey).InstallLocation
    throw "A smoke installation is still registered ($leftover). Uninstall it before running this test."
}

$systemTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd("\")
$evidenceRoot = Join-Path $systemTempRoot ("bibliobrain-legacy-upgrade-" + [guid]::NewGuid().ToString("N"))
$resolvedEvidenceRoot = [System.IO.Path]::GetFullPath($evidenceRoot)
$expectedPrefix = $systemTempRoot + [System.IO.Path]::DirectorySeparatorChar
if (-not $resolvedEvidenceRoot.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use a smoke-test directory outside the system temporary directory: $resolvedEvidenceRoot"
}

$checks = New-Object System.Collections.Generic.List[object]
$currentFrom = ""
$installRoot = ""
$passed = $false

function Add-Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = "")
    $checks.Add([pscustomobject]@{ From = $currentFrom; Check = $Name; Passed = $Ok; Detail = $Detail })
}

function Get-RelativeFiles {
    param([string]$Root)
    if (-not (Test-Path -LiteralPath $Root)) {
        return @()
    }
    $prefix = [System.IO.Path]::GetFullPath($Root).TrimEnd("\") + "\"
    return @(
        Get-ChildItem -LiteralPath $Root -Recurse -File -Force |
            ForEach-Object { $_.FullName.Substring($prefix.Length) } |
            Sort-Object
    )
}

function Get-PayloadFiles {
    # What Setup installs from a source folder: [Files] excludes bytecode.
    param([string]$Root)
    return @(
        Get-RelativeFiles -Root $Root |
            Where-Object { $_ -notmatch '(^|\\)__pycache__\\' -and $_ -notmatch '\.pyc$' }
    )
}

function Get-TreeDigest {
    param([string]$Root)
    if (-not (Test-Path -LiteralPath $Root)) {
        return "<missing>"
    }
    $lines = foreach ($relative in (Get-RelativeFiles -Root $Root)) {
        $hash = (Get-FileHash -LiteralPath (Join-Path $Root $relative) -Algorithm SHA256).Hash
        "$relative=$hash"
    }
    return ($lines -join "`n")
}

function Get-RealInstallationSnapshot {
    # Everything of the user's own installation this test could reach by mistake.
    $desktop = [Environment]::GetFolderPath("Desktop")
    $programs = [Environment]::GetFolderPath("Programs")
    $paths = @(
        (Join-Path $desktop "Bibliobrain.lnk")
        (Join-Path $programs "Bibliobrain\Bibliobrain.lnk")
        (Join-Path $env:LOCALAPPDATA "Programs\Bibliobrain\VERSION")
        (Join-Path $env:LOCALAPPDATA "Programs\Bibliobrain\unins000.dat")
        (Join-Path $env:LOCALAPPDATA "bibliobrain\configuration.json")
    )
    $lines = foreach ($path in $paths) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $item = Get-Item -LiteralPath $path
            "$path=$($item.Length)@$($item.LastWriteTimeUtc.Ticks)"
        }
        else {
            "$path=<missing>"
        }
    }
    if (Test-Path -LiteralPath $ProductKey) {
        $key = Get-ItemProperty -LiteralPath $ProductKey
        $lines += "registry=$($key.DisplayVersion)@$($key.InstallLocation)"
    }
    else {
        $lines += "registry=<missing>"
    }
    return ($lines -join "`n")
}

function Invoke-Setup {
    param([string]$FilePath, [string[]]$Arguments)
    $options = @{
        FilePath = $FilePath
        ArgumentList = $Arguments
        Wait = $true
        PassThru = $true
        WindowStyle = "Hidden"
    }
    return (Start-Process @options).ExitCode
}

function Invoke-Uninstaller {
    param([string]$Root, [string]$Log)
    $arguments = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
    if ($Log) {
        $arguments += "/LOG=$Log"
    }
    $exitCode = Invoke-Setup -FilePath (Join-Path $Root "unins000.exe") -Arguments $arguments
    # The uninstaller copies itself to %TEMP% and returns before it is done.
    $deadline = (Get-Date).AddSeconds(60)
    while ((Test-Path -LiteralPath $SmokeKey) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Seconds 2
    return $exitCode
}

function Invoke-Captured {
    # Runs a program with the current environment and returns its exit code and output.
    param([string]$FilePath, [string[]]$Arguments, [string]$WorkingDirectory, [string]$Name)
    $stdout = Join-Path $resolvedEvidenceRoot "$Name.out.txt"
    $stderr = Join-Path $resolvedEvidenceRoot "$Name.err.txt"
    $options = @{
        FilePath = $FilePath
        ArgumentList = $Arguments
        WorkingDirectory = $WorkingDirectory
        Wait = $true
        PassThru = $true
        NoNewWindow = $true
        RedirectStandardOutput = $stdout
        RedirectStandardError = $stderr
    }
    $process = Start-Process @options
    $output = (Get-Content -LiteralPath $stdout -Raw -ErrorAction SilentlyContinue) +
        (Get-Content -LiteralPath $stderr -Raw -ErrorAction SilentlyContinue)
    return [pscustomobject]@{ ExitCode = $process.ExitCode; Output = [string]$output }
}

$savedEnvironment = @{}
function Set-TestEnvironment {
    param([hashtable]$Values)
    foreach ($name in $Values.Keys) {
        if (-not $savedEnvironment.ContainsKey($name)) {
            $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        }
        [Environment]::SetEnvironmentVariable($name, $Values[$name], "Process")
    }
}
function Restore-TestEnvironment {
    foreach ($name in @($savedEnvironment.Keys)) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
    }
    $savedEnvironment.Clear()
}

function Read-JsonSettings {
    param([string]$Path)
    $settings = @{}
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json).settings.PSObject.Properties |
            ForEach-Object { $settings[$_.Name] = [string]$_.Value }
    }
    return $settings
}

function Read-EnvValue {
    param([string]$Path, [string]$Name)
    $line = Get-Content -LiteralPath $Path -Encoding UTF8 | Where-Object { $_ -match "^$Name=" } | Select-Object -First 1
    if (-not $line) {
        return ""
    }
    return $line.Substring($Name.Length + 1).Trim()
}

function New-LegacySmokeSetup {
    # Exports $Commit, rewrites its Setup into a smoke build and replaces its
    # install.ps1 with a stub that leaves the same files in the program folder.
    param([string]$Commit, [string]$Root)
    $source = Join-Path $Root "source"
    $archive = Join-Path $Root "source.zip"
    & git -C $RepoRoot archive --format=zip --output=$archive $Commit
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to export $Commit."
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $source
    $issPath = Join-Path $source "packaging\Bibliobrain.iss"
    $versionFile = Join-Path $source "VERSION"
    if (Test-Path -LiteralPath $versionFile) {
        $legacyVersion = (Get-Content -LiteralPath $versionFile -Raw).Trim()
    }
    else {
        $match = Select-String -LiteralPath $issPath -Pattern '#define MyAppVersion "([^"]+)"' | Select-Object -First 1
        if (-not $match) {
            throw "Unable to read the version of $Commit."
        }
        $legacyVersion = $match.Matches[0].Groups[1].Value
    }

    # Every edit must apply, so a script this test does not know fails here
    # instead of silently installing the real product.
    $issLines = Get-Content -LiteralPath $issPath -Encoding UTF8
    $productIdLiteral = "{" + $ProductAppId
    $edits = @{ AppId = 0; Desktop = 0; UninstallRun = 0 }
    $smokeLines = New-Object System.Collections.Generic.List[string]
    $inUninstallRun = $false
    foreach ($line in $issLines) {
        if ($inUninstallRun) {
            if ($line -match '^\s*(\[|#)') {
                $inUninstallRun = $false
            }
            else {
                continue
            }
        }
        if ($line.Contains($productIdLiteral)) {
            $line = $line.Replace($productIdLiteral, "{" + $SmokeAppId)
            $edits.AppId++
        }
        if ($line -like "*{userdesktop}*") {
            $edits.Desktop++; continue
        }
        if ($line.Trim() -eq "[UninstallRun]") {
            $inUninstallRun = $true; $edits.UninstallRun++; continue
        }
        $smokeLines.Add($line)
    }
    if ($edits.AppId -lt 1 -or $edits.UninstallRun -ne 1) {
        throw "The installer script of $Commit no longer matches this test (AppId $($edits.AppId), [UninstallRun] $($edits.UninstallRun))."
    }
    if (@($smokeLines | Where-Object { $_ -match '^\s*\[Icons\]' }).Count -gt 0) {
        throw "$Commit creates shortcuts from Setup: its smoke build would touch the real ones."
    }
    # Files that never were in Git (the private docs/ guide, 0.7.10 to 0.7.33)
    # get a placeholder, as the release build of the time had the real one.
    foreach ($match in [regex]::Matches(($smokeLines -join "`n"), 'Source: "\{#SourceRoot\}\\([^"*]+)"')) {
        $packaged = Join-Path $source $match.Groups[1].Value
        if (-not (Test-Path -LiteralPath $packaged)) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $packaged) -Force | Out-Null
            Set-Content -LiteralPath $packaged -Value "placeholder for a file outside Git" -Encoding ASCII
        }
    }
    $smokeIss = Join-Path $source "packaging\Bibliobrain-smoke.iss"
    [System.IO.File]::WriteAllLines($smokeIss, $smokeLines, (New-Object System.Text.UTF8Encoding($true)))

    # What that version's install.ps1 left in the program folder.
    $installScript = Get-Content -LiteralPath (Join-Path $source "install.ps1") -Raw
    $layout = [pscustomobject]@{
        Version = $legacyVersion
        WritesEnv = $installScript.Contains('Copy-Item (Join-Path $RepoRoot ".env.example")')
        StateInProgramFolder = $installScript -match '\$InstallStateDir = Join-Path \$RepoRoot "\.bibliobrain"'
        HasConfiguration = Test-Path -LiteralPath (Join-Path $source "configuration.example.json")
        Launchers = @([regex]::Matches($installScript, '"([\w-]+\.(?:cmd|bat))"\s*=\s*"') | ForEach-Object { $_.Groups[1].Value })
        Source = $source
    }
    $stubLines = @(
        '$root = Split-Path -Parent $MyInvocation.MyCommand.Path'
        'New-Item -ItemType Directory -Path (Join-Path $root ".venv\Scripts") -Force | Out-Null'
        'Set-Content -LiteralPath (Join-Path $root ".venv\pyvenv.cfg") -Value "version = 3.14.6" -Encoding ASCII'
        'Set-Content -LiteralPath (Join-Path $root ".venv\Scripts\python.exe") -Value "smoke" -Encoding ASCII'
        'Set-Content -LiteralPath (Join-Path $root "bibliobrain-install.log") -Value "legacy" -Encoding ASCII'
        'Set-Content -LiteralPath (Join-Path $root "Bibliobrain.lnk") -Value "smoke" -Encoding ASCII'
    )
    foreach ($launcher in $layout.Launchers) {
        $stubLines += "Set-Content -LiteralPath (Join-Path `$root '$launcher') -Value '@echo off' -Encoding ASCII"
    }
    if ($layout.StateInProgramFolder) {
        $stubLines += 'New-Item -ItemType Directory -Path (Join-Path $root ".bibliobrain") -Force | Out-Null'
        $stubLines += 'Set-Content -LiteralPath (Join-Path $root ".bibliobrain\install-state.json") -Value ''{"torch_profile": "cpu"}'' -Encoding ASCII'
    }
    if ($layout.WritesEnv) {
        # A user who set a key and changed a setting in the .env of the old version.
        $stubLines += @(
            '$envFile = Join-Path $root ".env"'
            'if (-not (Test-Path -LiteralPath $envFile)) {'
            '    $lines = Get-Content -LiteralPath (Join-Path $root ".env.example") -Encoding UTF8'
            '    $lines = $lines -replace "^OPENALEX_API_KEY=$", "OPENALEX_API_KEY=smoke-openalex-key"'
            '    $lines = $lines -replace "^RERANKER_DEVICE=auto$", "RERANKER_DEVICE=cpu"'
            '    Set-Content -LiteralPath $envFile -Value $lines -Encoding UTF8'
            '}'
        )
    }
    $stubLines += "exit 0"
    Set-Content -LiteralPath (Join-Path $source "install.ps1") -Value $stubLines -Encoding ASCII

    $outputDir = Join-Path $Root "setup"
    & $IsccPath "/Q" "/DSourceRoot=$source" "/O$outputDir" "/FBibliobrain-Setup-Smoke-Legacy" $smokeIss
    $setup = Join-Path $outputDir "Bibliobrain-Setup-Smoke-Legacy.exe"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $setup)) {
        throw "The smoke installer of $Commit failed to compile."
    }
    $layout | Add-Member -NotePropertyName Setup -NotePropertyValue $setup
    return $layout
}

function Test-LegacyData {
    # The old user data in a temporary LOCALAPPDATA, against the current code.
    param($Layout, [string]$AppCheck, [string]$Root)
    $localAppData = Join-Path $Root "LocalAppData"
    $appHome = Join-Path $localAppData "bibliobrain"
    foreach ($folder in @("data", "storage", "chroma")) {
        New-Item -ItemType Directory -Path (Join-Path $appHome $folder) -Force | Out-Null
    }
    Set-Content -LiteralPath (Join-Path $appHome "storage\app_settings.json") -Encoding UTF8 -Value '{"reranker_enabled": false, "onboarding_done_v1": true}'
    Set-Content -LiteralPath (Join-Path $appHome "chroma\chroma.sqlite3") -Value "legacy index"
    if (-not $Layout.StateInProgramFolder) {
        New-Item -ItemType Directory -Path (Join-Path $appHome "runtime") -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $appHome "runtime\install-state.json") -Encoding ASCII -Value '{"torch_profile": "cpu"}'
    }
    $configurationPath = Join-Path $appHome "configuration.json"
    $legacyEnv = Join-Path $AppCheck ".env"
    if ($Layout.HasConfiguration) {
        # What that version's install.ps1 copied, then a setting the user changed
        # and the version an earlier .env migration may have written (0.7.5 to 0.7.34).
        $configuration = Get-Content -LiteralPath (Join-Path $Layout.Source "configuration.example.json") -Raw | ConvertFrom-Json
        $configuration.settings | Add-Member -NotePropertyName RERANKER_DEVICE -NotePropertyValue "cpu" -Force
        $configuration.settings | Add-Member -NotePropertyName BIBLIOBRAIN_VERSION -NotePropertyValue $Layout.Version -Force
        Set-Content -LiteralPath $configurationPath -Value ($configuration | ConvertTo-Json -Depth 5) -Encoding UTF8
        $expectedEmbedding = [string]$configuration.settings.OLLAMA_EMBEDDING_MODEL
    }
    else {
        $expectedEmbedding = Read-EnvValue -Path $legacyEnv -Name "OLLAMA_EMBEDDING_MODEL"
    }
    $dataBefore = Get-TreeDigest -Root $appHome
    $installArguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $AppCheck "install.ps1"),
        "-SkipShortcuts", "-NoLaunch", "-ChatModel", "none", "-EmbeddingDownload", "no", "-DryRun"
    )
    $name = $Layout.Version

    Set-TestEnvironment @{
        LOCALAPPDATA = $localAppData
        BIBLIOBRAIN_HOME = $null
        DATA_PATH = $null
        STORAGE_DIR = $null
        CHROMA_PATH = $null
        BIBLIOBRAIN_ENV_FILE = $null
        BIBLIOBRAIN_VERSION = $null
    }
    try {
        $dryRun = Invoke-Captured -FilePath "powershell.exe" -Arguments $installArguments -WorkingDirectory $AppCheck -Name "$name-dry-run-1"
        Add-Check "dry run succeeds" ($dryRun.ExitCode -eq 0) "exit code $($dryRun.ExitCode)"
        Add-Check "dry run writes no user data" ((Get-TreeDigest -Root $appHome) -eq $dataBefore)
        $importScheduled = $dryRun.Output -match [regex]::Escape("[dry-run] Import $legacyEnv once")
        if ($Layout.WritesEnv) {
            Add-Check "dry run schedules the .env migration" $importScheduled
        }
        else {
            Add-Check "dry run keeps the existing configuration" (
                -not $importScheduled -and $dryRun.Output -notmatch [regex]::Escape("[dry-run] Copy configuration.example.json")
            )
        }

        # The wizard's model page, run from Setup's temporary folder before any
        # file is installed: it must offer the model the existing index uses.
        $probeDir = Join-Path $Root "probe"
        New-Item -ItemType Directory -Path $probeDir -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $RepoRoot "install.ps1") -Destination (Join-Path $probeDir "install-probe.ps1")
        $probeFile = Join-Path $probeDir "models.txt"
        $probe = Invoke-Captured -FilePath "powershell.exe" -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-File", (Join-Path $probeDir "install-probe.ps1"),
            "-ProbeModelsFile", $probeFile, "-ExistingInstallDir", $AppCheck
        ) -WorkingDirectory $probeDir -Name "$name-probe"
        $probedEmbedding = if (Test-Path -LiteralPath $probeFile) {
            [string](@(Get-Content -LiteralPath $probeFile | Where-Object { $_ -like "embedding|*" }) | Select-Object -First 1)
        } else { "" }
        Add-Check "wizard offers the embedding model of the index" (
            $probe.ExitCode -eq 0 -and $probedEmbedding -like "embedding|$expectedEmbedding|*"
        ) "probe '$probedEmbedding', expected '$expectedEmbedding'"

        if ($Layout.WritesEnv) {
            # install.ps1 cannot run for real here (Python, pip, Ollama): its
            # migration step is replayed with the same command and marker.
            $migration = Invoke-Captured -FilePath $PythonPath -Arguments @(
                "-I", (Join-Path $AppCheck "scripts\migrate-env-config.py"), "--env-file", $legacyEnv
            ) -WorkingDirectory $AppCheck -Name "$name-migration"
            Add-Check "legacy .env migration" ($migration.ExitCode -eq 0) (($migration.Output -split "`n" | Select-Object -Last 2) -join " ").Trim()
            $marker = @{ version = 1; migrated_at = [DateTimeOffset]::UtcNow.ToString("o") } | ConvertTo-Json
            Set-Content -LiteralPath (Join-Path $appHome "legacy-env-migrated-v1.json") -Value $marker -Encoding UTF8
            $configurationText = Get-Content -LiteralPath $configurationPath -Raw -ErrorAction SilentlyContinue
            Add-Check "secret kept out of configuration.json" ([string]$configurationText -notmatch "smoke-openalex-key")
            Add-Check "secret moved to the encrypted store" (Test-Path -LiteralPath (Join-Path $appHome "storage\api_secrets.dpapi.json"))

            $dryRun = Invoke-Captured -FilePath "powershell.exe" -Arguments $installArguments -WorkingDirectory $AppCheck -Name "$name-dry-run-2"
            Add-Check "second run does not migrate again" (
                $dryRun.ExitCode -eq 0 -and
                $dryRun.Output -notmatch [regex]::Escape("[dry-run] Import") -and
                $dryRun.Output -match "already been migrated"
            ) "exit code $($dryRun.ExitCode)"
        }

        $settings = Read-JsonSettings -Path $configurationPath
        Add-Check "user setting kept" ($settings["RERANKER_DEVICE"] -eq "cpu") "RERANKER_DEVICE '$($settings["RERANKER_DEVICE"])'"
        Add-Check "index embedding model kept" ($settings["OLLAMA_EMBEDDING_MODEL"] -eq $expectedEmbedding) (
            "OLLAMA_EMBEDDING_MODEL '$($settings["OLLAMA_EMBEDDING_MODEL"])', expected '$expectedEmbedding'"
        )
        $configurationBefore = if (Test-Path -LiteralPath $configurationPath) { (Get-FileHash -LiteralPath $configurationPath).Hash } else { "<missing>" }

        # The application itself, without Ollama, reranker or error reports.
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        $listener.Start()
        $port = $listener.LocalEndpoint.Port
        $listener.Stop()
        Set-TestEnvironment @{
            OLLAMA_BASE_URL = "http://127.0.0.1:9"
            RERANKER_ENABLED = "false"
            BIBLIOBRAIN_ERROR_REPORTING_ENABLED = "false"
            PYTHONDONTWRITEBYTECODE = "1"
        }
        $serverOptions = @{
            FilePath = $PythonPath
            ArgumentList = @("-m", "uvicorn", "app_api:app", "--host", "127.0.0.1", "--port", "$port")
            WorkingDirectory = $AppCheck
            PassThru = $true
            WindowStyle = "Hidden"
            RedirectStandardOutput = (Join-Path $resolvedEvidenceRoot "$name-app.out.txt")
            RedirectStandardError = (Join-Path $resolvedEvidenceRoot "$name-app.err.txt")
        }
        $server = Start-Process @serverOptions
        try {
            $capabilities = $null
            $deadline = (Get-Date).AddSeconds(180)
            while (-not $capabilities -and -not $server.HasExited -and (Get-Date) -lt $deadline) {
                try {
                    $capabilities = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/capabilities" -TimeoutSec 5
                }
                catch {
                    Start-Sleep -Seconds 1
                }
            }
            Add-Check "application starts on the old data" ($null -ne $capabilities) "exited: $($server.HasExited)"
            $reportedVersion = if ($capabilities) { [string]$capabilities.version } else { "" }
            Add-Check "application reports the installed version" ($reportedVersion -eq $Version) "version '$reportedVersion'"
        }
        finally {
            if (-not $server.HasExited) {
                & taskkill.exe /PID $server.Id /T /F | Out-Null
            }
        }
        $configurationAfter = if (Test-Path -LiteralPath $configurationPath) { (Get-FileHash -LiteralPath $configurationPath).Hash } else { "<missing>" }
        Add-Check "application keeps the configuration" ($configurationAfter -eq $configurationBefore)
        Add-Check "old index kept" ((Get-Content -LiteralPath (Join-Path $appHome "chroma\chroma.sqlite3") -Raw).Trim() -eq "legacy index")
    }
    finally {
        Restore-TestEnvironment
    }
}

$realBefore = Get-RealInstallationSnapshot
$summary = @()

try {
    New-Item -ItemType Directory -Path $resolvedEvidenceRoot | Out-Null

    # --- Current smoke installer, built once -----------------------------------
    $currentStub = Join-Path $resolvedEvidenceRoot "install-stub.ps1"
    $stubRecord = Join-Path $resolvedEvidenceRoot "install-stub-record.json"
    Set-Content -LiteralPath $currentStub -Encoding ASCII -Value @'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$record = [ordered]@{
    arguments = ($args -join " ")
    venvPresent = Test-Path -LiteralPath (Join-Path $root ".venv\pyvenv.cfg")
    legacyEnvPresent = Test-Path -LiteralPath (Join-Path $root ".env")
    version = (Get-Content -LiteralPath (Join-Path $root "VERSION") -Raw).Trim()
}
$record | ConvertTo-Json | Set-Content -LiteralPath $env:BIBLIOBRAIN_SMOKE_RECORD -Encoding ASCII
$progressIndex = [array]::IndexOf($args, "-ProgressFile")
if ($progressIndex -ge 0) {
    Set-Content -LiteralPath $args[$progressIndex + 1] -Value "Stub step" -Encoding ASCII
}
exit 0
'@
    $currentOutput = Join-Path $resolvedEvidenceRoot "current-setup"
    & $IsccPath "/Q" "/DSourceRoot=$RepoRoot" "/DPackagingSmoke=1" "/DSmokeInstallScript=$currentStub" "/O$currentOutput" $InnoScript
    $currentSetup = Join-Path $currentOutput "Bibliobrain-Setup-Smoke.exe"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $currentSetup)) {
        throw "The current smoke installer failed to compile."
    }

    foreach ($commit in $FromCommit) {
        $versionRoot = Join-Path $resolvedEvidenceRoot $commit
        New-Item -ItemType Directory -Path $versionRoot | Out-Null
        $installRoot = Join-Path $versionRoot "installed"
        $layout = New-LegacySmokeSetup -Commit $commit -Root $versionRoot
        $currentFrom = "$($layout.Version) ($commit)"
        Write-Host "Updating from $currentFrom..."

        # --- Old installation --------------------------------------------------
        $baseArguments = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/DIR=$installRoot")
        $exitCode = Invoke-Setup -FilePath $layout.Setup -Arguments ($baseArguments + @("/LOG=$(Join-Path $versionRoot 'install-legacy.log')"))
        if ($exitCode -ne 0) {
            throw "The smoke installation of $currentFrom failed with exit code $exitCode."
        }
        if (-not (Test-Path -LiteralPath (Join-Path $installRoot ".venv\pyvenv.cfg"))) {
            throw "The installer of $currentFrom did not run its install script stub."
        }
        $legacyKey = Get-ItemProperty -LiteralPath $SmokeKey
        Add-Check "old installation registered" ($legacyKey.DisplayVersion -eq $layout.Version) "DisplayVersion $($legacyKey.DisplayVersion)"

        # Bytecode Python wrote next to the old code, and a file Setup never tracked.
        $legacyBytecode = Join-Path $installRoot "src\__pycache__\removed_module.cpython-314.pyc"
        New-Item -ItemType Directory -Path (Split-Path -Parent $legacyBytecode) -Force | Out-Null
        Set-Content -LiteralPath $legacyBytecode -Value "legacy"
        Set-Content -LiteralPath (Join-Path $installRoot "static\untracked-legacy.js") -Value "legacy"

        $venvBefore = Get-TreeDigest -Root (Join-Path $installRoot ".venv")
        $stateBefore = Get-TreeDigest -Root (Join-Path $installRoot ".bibliobrain")
        $envPath = Join-Path $installRoot ".env"
        $envBefore = if (Test-Path -LiteralPath $envPath) { (Get-FileHash -LiteralPath $envPath).Hash } else { "<missing>" }
        $legacyOnlyFiles = @(
            @("src", "static") | ForEach-Object {
                $folder = $_
                $current = Get-PayloadFiles -Root (Join-Path $RepoRoot $folder)
                Get-PayloadFiles -Root (Join-Path $layout.Source $folder) |
                    Where-Object { $current -notcontains $_ } |
                    ForEach-Object { "$folder\$_" }
            }
        )

        # --- Update with the current installer ---------------------------------
        # /DIR stays explicit: without it a lookup failure would land in the real
        # installation folder. The folder Setup would have proposed is checked below.
        $env:BIBLIOBRAIN_SMOKE_RECORD = $stubRecord
        try {
            $exitCode = Invoke-Setup -FilePath $currentSetup -Arguments ($baseArguments + @("/LOG=$(Join-Path $versionRoot 'install-current.log')"))
        }
        finally {
            Remove-Item Env:\BIBLIOBRAIN_SMOKE_RECORD -ErrorAction SilentlyContinue
        }
        Add-Check "update exit code" ($exitCode -eq 0) "exit code $exitCode"

        $currentKey = Get-ItemProperty -LiteralPath $SmokeKey
        $previousDir = [string]$currentKey."Inno Setup: App Path"
        Add-Check "same installation folder" (
            $previousDir.TrimEnd("\") -ieq $installRoot -and
            ([string]$currentKey.InstallLocation).TrimEnd("\") -ieq $installRoot
        ) "App Path '$previousDir'"
        Add-Check "registered version updated" ($currentKey.DisplayVersion -eq $Version) "DisplayVersion $($currentKey.DisplayVersion)"
        $uninstallers = @(Get-ChildItem -LiteralPath $installRoot -Filter "unins*.exe" | ForEach-Object Name)
        Add-Check "single uninstaller" ($uninstallers.Count -eq 1 -and $uninstallers[0] -eq "unins000.exe") ($uninstallers -join ", ")

        $installedVersion = (Get-Content -LiteralPath (Join-Path $installRoot "VERSION") -Raw).Trim()
        Add-Check "VERSION updated" ($installedVersion -eq $Version) "VERSION $installedVersion"
        foreach ($folder in @("src", "static")) {
            $expected = Get-PayloadFiles -Root (Join-Path $RepoRoot $folder)
            $installed = Get-RelativeFiles -Root (Join-Path $installRoot $folder)
            $leftovers = @($installed | Where-Object { $expected -notcontains $_ })
            $missing = @($expected | Where-Object { $installed -notcontains $_ })
            $changed = @($expected | Where-Object {
                ($installed -contains $_) -and
                (Get-FileHash -LiteralPath (Join-Path $RepoRoot "$folder\$_")).Hash -ne
                    (Get-FileHash -LiteralPath (Join-Path $installRoot "$folder\$_")).Hash
            })
            Add-Check "$folder replaced without leftovers" ($leftovers.Count -eq 0 -and $missing.Count -eq 0 -and $changed.Count -eq 0) (
                "leftovers: $($leftovers -join ', '); missing: $($missing -join ', '); different: $($changed -join ', ')"
            )
        }
        Add-Check ".venv kept" ((Get-TreeDigest -Root (Join-Path $installRoot ".venv")) -eq $venvBefore)
        Add-Check ".bibliobrain kept" ((Get-TreeDigest -Root (Join-Path $installRoot ".bibliobrain")) -eq $stateBefore)
        $envAfter = if (Test-Path -LiteralPath $envPath) { (Get-FileHash -LiteralPath $envPath).Hash } else { "<missing>" }
        Add-Check "legacy .env untouched by Setup" ($envAfter -eq $envBefore)

        $record = Get-Content -LiteralPath $stubRecord -Raw | ConvertFrom-Json
        Remove-Item -LiteralPath $stubRecord
        Add-Check "install.ps1 started on the existing environment" ($record.venvPresent -and $record.legacyEnvPresent -eq $layout.WritesEnv -and $record.version -eq $Version) (
            "venv $($record.venvPresent), .env $($record.legacyEnvPresent), VERSION $($record.version)"
        )
        Add-Check "install.ps1 asks nothing" ($record.arguments -match "^-ProgressFile \S+ -SkipShortcuts -NoLaunch -ChatModel none -EmbeddingDownload no$") $record.arguments

        $appCheck = Join-Path $versionRoot "app-check"
        Copy-Item -LiteralPath $installRoot -Destination $appCheck -Recurse
        Copy-Item -LiteralPath (Join-Path $RepoRoot "install.ps1") -Destination (Join-Path $appCheck "install.ps1") -Force

        # --- Uninstallation ----------------------------------------------------
        # The legacy .env is the one file no Setup ever owned: install.ps1 created
        # it, it may hold keys, and it is kept like the user data. It alone keeps
        # the folder; anything else left behind is a failure.
        $exitCode = Invoke-Uninstaller -Root $installRoot -Log (Join-Path $versionRoot "uninstall.log")
        $remaining = @(Get-RelativeFiles -Root $installRoot)
        $unexpectedRemaining = @($remaining | Where-Object { $_ -ne ".env" })
        Add-Check "uninstall leaves nothing but the legacy .env" (
            $exitCode -eq 0 -and $unexpectedRemaining.Count -eq 0 -and -not (Test-Path -LiteralPath $SmokeKey)
        ) "exit code $exitCode; remaining: $($remaining -join ', ')"

        if (-not $SkipDataCheck) {
            Test-LegacyData -Layout $layout -AppCheck $appCheck -Root $versionRoot
        }

        $versionChecks = @($checks | Where-Object { $_.From -eq $currentFrom })
        $summary += [pscustomobject]@{
            From = $currentFrom
            Layout = $(if ($layout.WritesEnv) { ".env" } else { "configuration.json" }) + $(if ($layout.StateInProgramFolder) { ", state in program folder" } else { "" })
            LegacyOnlyFilesRemoved = $legacyOnlyFiles.Count
            LegacyEnvLeftAfterUninstall = $remaining -contains ".env"
            Checks = $versionChecks.Count
            Failed = @($versionChecks | Where-Object { -not $_.Passed }).Count
        }
    }

    $currentFrom = "all"
    $realAfter = Get-RealInstallationSnapshot
    Add-Check "real installation untouched" ($realAfter -eq $realBefore) $(if ($realAfter -ne $realBefore) { "before:`n$realBefore`nafter:`n$realAfter" } else { "" })

    $failed = @($checks | Where-Object { -not $_.Passed })
    if ($failed.Count -gt 0) {
        $failed | Format-Table -AutoSize -Wrap | Out-String -Width 220 | Write-Host
        $summary | Format-Table -AutoSize | Out-String -Width 220 | Write-Host
        throw "$($failed.Count) of $($checks.Count) legacy upgrade checks failed."
    }
    $passed = $true
}
catch {
    Write-Host "Legacy upgrade smoke-test evidence retained at: $resolvedEvidenceRoot"
    throw
}
finally {
    Restore-TestEnvironment
    # A failure before the uninstaller ran must not leave a registered smoke installation.
    if ((Test-Path -LiteralPath $SmokeKey) -and $installRoot -and (Test-Path -LiteralPath (Join-Path $installRoot "unins000.exe"))) {
        Invoke-Uninstaller -Root $installRoot -Log "" | Out-Null
    }
    if ($passed -and (Test-Path -LiteralPath $resolvedEvidenceRoot)) {
        Remove-Item -LiteralPath $resolvedEvidenceRoot -Recurse -Force
    }
}

$summary | ConvertTo-Json
