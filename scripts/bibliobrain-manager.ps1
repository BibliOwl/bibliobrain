param(
    [ValidateSet("start", "stop", "repair", "update", "backup", "restore", "uninstall")]
    [string]$Action = "start",
    [switch]$NoBrowser,
    [switch]$RemoveUserData,
    [switch]$Confirm,
    [switch]$NativeInstaller,
    [string]$BackupPath = "",
    [string]$InstallerPath = "",
    # A Bibliobrain-Windows.zip package: installs made from the ZIP update
    # from the next one, never from Setup.exe (antivirus quarantine).
    [string]$ArchivePath = "",
    [switch]$IncludeExternalDocuments,
    # Set by the in-app updater (src/update_check.py): silent installer, rotating
    # pre-update backups, a result file for the page, then restart the server.
    [switch]$FromApp,
    # Set by the Desktop shortcut, which has no console: a failed start is shown
    # in a dialog instead of being lost.
    [switch]$ErrorDialog
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Get-BibliobrainHome {
    # Mirrors src/config.py: an ephemeral process override, then the fixed
    # per-user JSON configuration, then %LOCALAPPDATA%\bibliobrain.
    $configured = [string]$env:BIBLIOBRAIN_HOME
    if (-not $configured -and $env:LOCALAPPDATA) {
        $configurationPath = Join-Path $env:LOCALAPPDATA "bibliobrain\configuration.json"
        if (Test-Path -LiteralPath $configurationPath -PathType Leaf) {
            try {
                $configuration = Get-Content -Raw -LiteralPath $configurationPath | ConvertFrom-Json
                $configured = [string]$configuration.settings.BIBLIOBRAIN_HOME
            }
            catch {
                Write-Warning "The local Bibliobrain configuration is unreadable; using the default data folder."
            }
        }
    }
    if (-not $configured) {
        if (-not $env:LOCALAPPDATA) {
            throw "LOCALAPPDATA is unavailable; set BIBLIOBRAIN_HOME to choose the data folder."
        }
        $configured = Join-Path $env:LOCALAPPDATA "bibliobrain"
    }
    return [System.IO.Path]::GetFullPath($configured)
}

# Runtime state (PIDs, update result) and logs live with the user data,
# never inside the installation folder. See src/config.py RUNTIME_DIR / LOG_DIR.
$BibliobrainHome = Get-BibliobrainHome
$StateDir = Join-Path $BibliobrainHome "runtime"
$LogDir = Join-Path $BibliobrainHome "logs"
$LegacyStateDir = Join-Path $RepoRoot ".bibliobrain"
$PidPath = Join-Path $StateDir "server.pid"
$OllamaPidPath = Join-Path $StateDir "ollama.pid"
$UpdateResultPath = Join-Path $StateDir "update-result.json"
# Holds the PID of a running update: starting Bibliobrain meanwhile would run
# a half-replaced application.
$UpdateLockPath = Join-Path $StateDir "update-in-progress"
# ZIP_PACKAGE_MARKER and the package root of scripts/release_tools.py.
$ZipPackageMarker = "ZIP-PACKAGE.txt"
$ZipPackageRoot = "Bibliobrain"
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$BackupModule = "src.backup_restore"
# 7860 is also the default port of Gradio and Stable Diffusion WebUI. When
# another program holds it, Bibliobrain takes the next free port up to 7870 and
# remembers it, so the browser keeps the same address (and its saved state).
$DefaultPort = 7860
$FallbackPorts = 7861..7870
$PortPath = Join-Path $StateDir "server.port"
$script:AppPort = $DefaultPort
$AppUrl = "http://127.0.0.1:7860/api/capabilities"
$BrowserUrl = "http://localhost:7860"
$StartupTimeoutSeconds = 180
$TrayScriptPath = Join-Path $PSScriptRoot "bibliobrain-tray.ps1"
$TrayIconPath = Join-Path $RepoRoot "static\logo_bibliobrain.ico"
$UninstallLogPath = Join-Path ([System.IO.Path]::GetTempPath()) "bibliobrain-uninstall.log"


function Move-LegacyRuntimeState {
    # Installations created before 0.8 kept their state in <repo>\.bibliobrain.
    # Move the files that still matter, then leave the old folder for uninstall.
    if (-not (Test-Path -LiteralPath $LegacyStateDir -PathType Container)) {
        return
    }
    $legacyNames = @("ollama.pid", "server.pid")
    foreach ($name in $legacyNames) {
        $legacyPath = Join-Path $LegacyStateDir $name
        $currentPath = Join-Path $StateDir $name
        if ((Test-Path -LiteralPath $legacyPath -PathType Leaf) -and -not (Test-Path -LiteralPath $currentPath)) {
            New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
            Move-Item -LiteralPath $legacyPath -Destination $currentPath -Force
        }
    }
}

function Start-UninstallLogging {
    try {
        Start-Transcript -Path $UninstallLogPath -Force | Out-Null
        Write-Host "Uninstallation log: $UninstallLogPath" -ForegroundColor DarkGray
        return $true
    }
    catch {
        Write-Host "Warning: unable to start the uninstallation log: $($_.Exception.Message)" -ForegroundColor Yellow
        return $false
    }
}

function Stop-UninstallLogging {
    param([bool]$Started)

    if (-not $Started) {
        return
    }
    try {
        Stop-Transcript | Out-Null
    }
    catch {
        Write-Host "Warning: unable to close the uninstallation log: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function Invoke-UninstallWithLogging {
    $logStarted = Start-UninstallLogging
    $uninstallFailure = $null
    try {
        Uninstall-Bibliobrain
    }
    catch {
        $uninstallFailure = $_
        Write-Host "Uninstallation failed:" -ForegroundColor Red
        Write-Host ($uninstallFailure | Format-List * -Force | Out-String) -ForegroundColor Red
    }
    finally {
        Stop-UninstallLogging -Started $logStarted
    }

    if ($logStarted) {
        Write-Host "Uninstallation log saved to: $UninstallLogPath" -ForegroundColor Cyan
    }
    if ($null -ne $uninstallFailure) {
        throw $uninstallFailure
    }
}

function Test-BibliobrainReady {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $AppUrl -TimeoutSec 2
        $payload = $response.Content | ConvertFrom-Json
        return $response.StatusCode -eq 200 -and $null -ne $payload.rag_connections
    }
    catch {
        return $false
    }
}

function Start-BibliobrainTray {
    if (-not (Test-Path -LiteralPath $TrayScriptPath -PathType Leaf)) {
        Write-Warning "The Bibliobrain notification-area helper is missing: $TrayScriptPath"
        return
    }

    try {
        $powerShellExe = (Get-Command "powershell.exe").Source
        $arguments = @(
            "-NoProfile",
            "-Sta",
            "-ExecutionPolicy", "Bypass",
            "-File", ('"{0}"' -f $TrayScriptPath),
            "-BaseUrl", ('"{0}"' -f $BrowserUrl),
            "-HealthUrl", ('"{0}"' -f $AppUrl),
            "-LogDir", ('"{0}"' -f $LogDir),
            "-IconPath", ('"{0}"' -f $TrayIconPath)
        )
        # The tray follows the server process itself: an HTTP probe alone can time
        # out while the server is busy (GPU reranking, resume from sleep) and used
        # to remove the icon while Bibliobrain kept running.
        $serverPid = 0
        if ((Test-Path -LiteralPath $PidPath -PathType Leaf) -and
            [int]::TryParse((Get-Content -Raw $PidPath).Trim(), [ref]$serverPid) -and
            $serverPid -gt 0) {
            $arguments += @("-ServerPid", [string]$serverPid)
        }
        Start-Process `
            -FilePath $powerShellExe `
            -ArgumentList $arguments `
            -WorkingDirectory $RepoRoot `
            -WindowStyle Hidden | Out-Null
    }
    catch {
        # The tray icon is informative only and must never prevent startup.
        Write-Warning "Unable to start the Bibliobrain notification-area icon: $($_.Exception.Message)"
    }
}

function Get-ManagedServerProcess {
    # The server recorded in the PID file, when it is still alive.
    if (-not (Test-Path -LiteralPath $PidPath -PathType Leaf)) {
        return $null
    }
    $serverPid = 0
    if (-not [int]::TryParse((Get-Content -Raw $PidPath).Trim(), [ref]$serverPid) -or $serverPid -le 0) {
        return $null
    }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid" -ErrorAction SilentlyContinue
    if (-not $processInfo -or [string]$processInfo.CommandLine -notmatch "src\.managed_server") {
        return $null
    }
    return Get-Process -Id $serverPid -ErrorAction SilentlyContinue
}

function Set-AppPort {
    param([int]$Port)
    $script:AppPort = $Port
    $script:AppUrl = "http://127.0.0.1:$Port/api/capabilities"
    $script:BrowserUrl = "http://localhost:$Port"
}

function Get-SavedAppPort {
    if (-not (Test-Path -LiteralPath $PortPath -PathType Leaf)) {
        return $DefaultPort
    }
    $saved = 0
    $text = [string](Get-Content -Raw -LiteralPath $PortPath -ErrorAction SilentlyContinue)
    if ([int]::TryParse($text.Trim(), [ref]$saved) -and ($saved -eq $DefaultPort -or $FallbackPorts -contains $saved)) {
        return $saved
    }
    return $DefaultPort
}

function Select-FreeAppPort {
    # The last port used first, so the address does not change between starts.
    # Returns 0 when every candidate is taken.
    $candidates = @(Get-SavedAppPort) + @($DefaultPort) + @($FallbackPorts) | Select-Object -Unique
    foreach ($candidate in $candidates) {
        if (-not (Get-PortOwnerDescription -Port $candidate)) {
            return [int]$candidate
        }
    }
    return 0
}

function Get-PortOwnerDescription {
    # Other local web applications use port 7860 too. Empty when it is free.
    param([int]$Port = $script:AppPort)
    try {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -First 1
    }
    catch {
        return ""
    }
    if (-not $listener) {
        return ""
    }
    $owner = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    if ($owner) {
        return "$($owner.ProcessName) (PID $($owner.Id))"
    }
    return "another program (PID $($listener.OwningProcess))"
}

function Show-StartFailure {
    param([string]$Message)

    # The Desktop shortcut runs this script without a window: a dialog is the
    # only place where the user can read why nothing opened.
    try {
        Add-Type -AssemblyName System.Windows.Forms
        # DefaultDesktopOnly keeps the dialog above the other windows even
        # though this process owns none.
        [System.Windows.Forms.MessageBox]::Show(
            $Message,
            "Bibliobrain",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error,
            [System.Windows.Forms.MessageBoxDefaultButton]::Button1,
            [System.Windows.Forms.MessageBoxOptions]::DefaultDesktopOnly
        ) | Out-Null
    }
    catch {
        Write-Host $Message -ForegroundColor Red
    }
}

function Get-RunningUpdatePid {
    # 0 when no update runs. A lock left by a crashed update is ignored.
    if (-not (Test-Path -LiteralPath $UpdateLockPath -PathType Leaf)) {
        return 0
    }
    $lockPid = 0
    if (-not [int]::TryParse((Get-Content -Raw -LiteralPath $UpdateLockPath -ErrorAction SilentlyContinue).Trim(), [ref]$lockPid)) {
        return 0
    }
    if ($lockPid -eq $PID) {
        return 0
    }
    $process = Get-Process -Id $lockPid -ErrorAction SilentlyContinue
    if ($process -and $process.ProcessName -match "^(powershell|pwsh)$") {
        return $lockPid
    }
    return 0
}

function Enter-UpdateLock {
    if (Get-RunningUpdatePid) {
        throw "Another Bibliobrain update is already running. Wait for its window to close."
    }
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    Set-Content -LiteralPath $UpdateLockPath -Value $PID -Encoding ASCII
}

function Exit-UpdateLock {
    Remove-Item -LiteralPath $UpdateLockPath -Force -ErrorAction SilentlyContinue
}

function Start-Bibliobrain {
    if (-not (Test-Path $PythonExe)) {
        throw "Bibliobrain is not installed. Run Install-Bibliobrain.cmd first."
    }
    if (Get-RunningUpdatePid) {
        throw "Bibliobrain is being updated. Wait until the update window says it is finished, then open Bibliobrain again."
    }

    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    $BindHost = "127.0.0.1"
    # A server already running answers on the port it was given last time.
    Set-AppPort -Port (Get-SavedAppPort)

    if (-not (Test-BibliobrainReady)) {
        # A server that is still loading (or too busy to answer in two seconds)
        # must be waited for, not doubled by a second one fighting for the port.
        $process = Get-ManagedServerProcess
        if (-not $process) {
            $freePort = Select-FreeAppPort
            if (-not $freePort) {
                $portOwner = Get-PortOwnerDescription -Port $DefaultPort
                throw "Bibliobrain cannot start: port 7860 is already used by $portOwner, and ports 7861 to 7870 are taken too. Close that program, then start Bibliobrain again."
            }
            if ($freePort -ne $DefaultPort) {
                Write-Host "Port $DefaultPort is busy: Bibliobrain uses port $freePort." -ForegroundColor Yellow
            }
            Set-AppPort -Port $freePort
            Set-Content -LiteralPath $PortPath -Value ([string]$freePort) -Encoding ASCII
            New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
            try {
                # Console output is captured for crash diagnostics only; the durable,
                # rotating application log is written by the server itself under $LogDir.
                $process = Start-Process `
                    -FilePath $PythonExe `
                    -ArgumentList @("-m", "src.managed_server", "--host", $BindHost, "--port", ([string]$script:AppPort)) `
                    -WorkingDirectory $RepoRoot `
                    -WindowStyle Hidden `
                    -RedirectStandardOutput (Join-Path $LogDir "server.out.log") `
                    -RedirectStandardError (Join-Path $LogDir "server.err.log") `
                    -PassThru
            }
            finally {
            }
            Set-Content -Path $PidPath -Value ([string]$process.Id) -Encoding ASCII
        }

        # The first start after an installation is the slow one: the antivirus
        # scans every freshly unpacked library as Python loads it.
        $ready = $false
        $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
            if (Test-BibliobrainReady) {
                $ready = $true
                break
            }
            if ($process.HasExited) {
                break
            }
        }
        if (-not $ready) {
            $reason = if ($process.HasExited) {
                "Bibliobrain stopped while starting."
            }
            else {
                "Bibliobrain did not become ready within $StartupTimeoutSeconds seconds."
            }
            throw "$reason Check the logs under '$LogDir', run Repair-Bibliobrain.cmd, then try again."
        }
    }
    Start-BibliobrainTray
    if (-not $NoBrowser) {
        Start-Process $BrowserUrl
    }
}

function Stop-Bibliobrain {
    if (-not (Test-Path $PidPath)) {
        Write-Host "Bibliobrain is not running (no managed server PID)."
        return
    }
    $serverPid = 0
    if (-not [int]::TryParse((Get-Content -Raw $PidPath).Trim(), [ref]$serverPid)) {
        Remove-Item -LiteralPath $PidPath -Force
        throw "Invalid Bibliobrain server PID file was removed."
    }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid" -ErrorAction SilentlyContinue
    if ($processInfo -and [string]$processInfo.CommandLine -match "(uvicorn\s+app_api:app|src\.managed_server)") {
        Stop-Process -Id $serverPid -Force
        # Callers restart, back up or replace files right after: wait until the
        # port and the file handles are really released.
        Wait-Process -Id $serverPid -Timeout 10 -ErrorAction SilentlyContinue
        Write-Host "Bibliobrain stopped."
    }
    else {
        Write-Host "The managed Bibliobrain process is no longer running."
    }
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
}

function Stop-ManagedOllama {
    $candidatePids = @()
    if (Test-Path $OllamaPidPath) {
        $ollamaPid = 0
        if ([int]::TryParse((Get-Content -Raw $OllamaPidPath).Trim(), [ref]$ollamaPid)) {
            $candidatePids += $ollamaPid
        }
    }
    else {
        # Installations created before ollama.pid was introduced can be
        # recognized by their redirected ollama-serve logs. Stop a legacy
        # process only when there is exactly one unambiguous `ollama serve`.
        $legacyLogs = @(
            @($StateDir, $LegacyStateDir) |
                Where-Object { Test-Path -LiteralPath $_ -PathType Container } |
                ForEach-Object { Get-ChildItem -LiteralPath $_ -Filter "ollama-serve-*.err.log" -ErrorAction SilentlyContinue }
        )
        if ($legacyLogs.Count -gt 0) {
            $legacyProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'ollama.exe'" -ErrorAction SilentlyContinue |
                Where-Object { [string]$_.CommandLine -match "(^|\s)serve(\s|$)" })
            if ($legacyProcesses.Count -eq 1) {
                $candidatePids += [int]$legacyProcesses[0].ProcessId
            }
            elseif ($legacyProcesses.Count -eq 0) {
                $basicProcesses = @(Get-Process -Name "ollama" -ErrorAction SilentlyContinue)
                if ($basicProcesses.Count -eq 1) {
                    $candidatePids += [int]$basicProcesses[0].Id
                }
            }
        }
    }

    foreach ($candidatePid in ($candidatePids | Select-Object -Unique)) {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $candidatePid" -ErrorAction SilentlyContinue
        $basicProcess = Get-Process -Id $candidatePid -ErrorAction SilentlyContinue
        $isManagedOllama = (
            ($processInfo -and
                [string]$processInfo.Name -ieq "ollama.exe" -and
                [string]$processInfo.CommandLine -match "(^|\s)serve(\s|$)") -or
            (-not $processInfo -and $basicProcess -and $basicProcess.ProcessName -eq "ollama")
        )
        if ($isManagedOllama) {
            Stop-Process -Id $candidatePid -Force -ErrorAction SilentlyContinue
            for ($attempt = 0; $attempt -lt 20; $attempt++) {
                if (-not (Get-Process -Id $candidatePid -ErrorAction SilentlyContinue)) {
                    break
                }
                Start-Sleep -Milliseconds 250
            }
            Write-Host "Managed Ollama server stopped."
        }
    }
    Remove-Item -LiteralPath $OllamaPidPath -Force -ErrorAction SilentlyContinue
}

function Remove-RuntimeTarget {
    param([string]$Path)
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return $true
        }
        catch {
            if ($attempt -eq 5) {
                Write-Host "Could not remove '$Path': $($_.Exception.Message)" -ForegroundColor Yellow
                return $false
            }
            Start-Sleep -Seconds 1
        }
    }
    return $false
}

function Repair-Bibliobrain {
    $installArguments = @("-NoLaunch")
    if ($FromApp) {
        # Same as the packaged update (/UPDATEMODE=1 in Bibliobrain.iss): an
        # update started from the page asks no question about Ollama models.
        $installArguments += "-SkipOllamaSetup"
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "install.ps1") @installArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Bibliobrain repair failed. Details: $(Join-Path $RepoRoot 'bibliobrain-install.log')"
    }
}

function Get-DefaultBackupPath {
    $documents = [Environment]::GetFolderPath("MyDocuments")
    if (-not $documents) {
        $documents = [Environment]::GetFolderPath("Desktop")
    }
    if (-not $documents) {
        throw "Windows did not provide a Documents or Desktop folder for the backup."
    }
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    return Join-Path $documents "Bibliobrain-Backup-$stamp.zip"
}

function Select-BibliobrainFile {
    param(
        [string]$Title,
        [string]$Filter
    )
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    try {
        $dialog.Title = $Title
        $dialog.Filter = $Filter
        $dialog.CheckFileExists = $true
        $dialog.Multiselect = $false
        $downloads = Join-Path ([Environment]::GetFolderPath("UserProfile")) "Downloads"
        if (Test-Path -LiteralPath $downloads -PathType Container) {
            $dialog.InitialDirectory = $downloads
        }
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            return ""
        }
        return [System.IO.Path]::GetFullPath($dialog.FileName)
    }
    finally {
        $dialog.Dispose()
    }
}

function Invoke-BackupRestoreTool {
    param([string[]]$Arguments)
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        throw "The Bibliobrain Python environment is unavailable. Run Repair-Bibliobrain.cmd first."
    }
    $toolOutput = @(& $PythonExe -m $BackupModule @Arguments 2>&1)
    $toolExitCode = $LASTEXITCODE
    foreach ($line in $toolOutput) {
        Write-Host ([string]$line)
    }
    if ($toolExitCode -ne 0) {
        throw "Bibliobrain backup or restore failed."
    }
}

function Backup-Bibliobrain {
    param([string]$Destination = "")
    $target = if ($Destination) {
        [System.IO.Path]::GetFullPath($Destination)
    }
    else {
        Get-DefaultBackupPath
    }
    # Give the browser's short draft-save debounce enough time to reach the
    # server before the consistent on-disk snapshot begins.
    Start-Sleep -Milliseconds 750
    Write-Host "Stopping Bibliobrain for a consistent backup..." -ForegroundColor Cyan
    Stop-Bibliobrain
    $arguments = @("backup", "--output", $target)
    if ($IncludeExternalDocuments) {
        $arguments += "--include-external-documents"
    }
    Invoke-BackupRestoreTool -Arguments $arguments
    Write-Host "Backup created: $target" -ForegroundColor Green
    return $target
}

function Restore-Bibliobrain {
    param([string]$Source = "")
    $selected = $Source
    if (-not $selected) {
        $selected = Select-BibliobrainFile `
            -Title "Select a Bibliobrain backup" `
            -Filter "Bibliobrain backup (*.zip)|*.zip"
    }
    if (-not $selected) {
        throw "No Bibliobrain backup was selected."
    }
    $selected = [System.IO.Path]::GetFullPath($selected)
    if (-not (Test-Path -LiteralPath $selected -PathType Leaf)) {
        throw "Bibliobrain backup not found: $selected"
    }
    if (-not $Confirm) {
        $answer = Read-Host "Restore '$selected' over the current Bibliobrain data [y/N]"
        if ($answer -notmatch "^(y|yes|o|oui)$") {
            Write-Host "Restore cancelled."
            return
        }
    }
    Write-Host "Stopping Bibliobrain before restore..." -ForegroundColor Cyan
    Stop-Bibliobrain
    Invoke-BackupRestoreTool -Arguments @("restore", "--input", $selected, "--confirm")
    Write-Host "Restore complete. A pre-restore rollback archive was created beside the selected backup." -ForegroundColor Green
    Write-Host "DPAPI secrets only remain usable with the same Windows account." -ForegroundColor Yellow
    Start-Bibliobrain
}

function ConvertTo-BibliobrainVersion {
    param([string]$Text)

    # Same grammar as src/update_manifest.py. [version] cannot be used: it
    # rejects the prerelease suffix of a beta such as "0.8.0-beta.1".
    if ($Text -notmatch '^(\d+)\.(\d+)\.(\d+)(?:[-.]([0-9A-Za-z.]+))?$') {
        return $null
    }
    return [pscustomobject]@{
        Core = @([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
        Prerelease = @(([string]$Matches[4]).Split(".") | Where-Object { $_ })
    }
}

function Compare-BibliobrainVersion {
    param($Left, $Right)

    # Returns -1, 0 or 1 like compare_versions in src/update_manifest.py: a
    # release ranks above its own prereleases (SemVer).
    for ($index = 0; $index -lt 3; $index++) {
        if ($Left.Core[$index] -ne $Right.Core[$index]) {
            return [math]::Sign($Left.Core[$index] - $Right.Core[$index])
        }
    }
    $leftParts = @($Left.Prerelease)
    $rightParts = @($Right.Prerelease)
    if ($leftParts.Count -eq 0 -or $rightParts.Count -eq 0) {
        return [math]::Sign($rightParts.Count - $leftParts.Count)
    }
    for ($index = 0; $index -lt [math]::Min($leftParts.Count, $rightParts.Count); $index++) {
        $leftPart = [string]$leftParts[$index]
        $rightPart = [string]$rightParts[$index]
        $leftIsNumber = $leftPart -match '^\d+$'
        $rightIsNumber = $rightPart -match '^\d+$'
        $order = if ($leftIsNumber -and $rightIsNumber) {
            [math]::Sign([long]$leftPart - [long]$rightPart)
        }
        elseif ($leftIsNumber -ne $rightIsNumber) {
            # A numeric identifier ranks below an alphanumeric one.
            if ($leftIsNumber) { -1 } else { 1 }
        }
        else {
            [math]::Sign([string]::CompareOrdinal($leftPart, $rightPart))
        }
        if ($order -ne 0) {
            return $order
        }
    }
    return [math]::Sign($leftParts.Count - $rightParts.Count)
}

function Test-BibliobrainInstaller {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "Bibliobrain installer not found: $resolved"
    }
    $file = Get-Item -LiteralPath $resolved
    # Inno Setup pads its version strings with spaces to a fixed width,
    # so the raw value is "Bibliobrain" followed by about 50 spaces.
    if (([string]$file.VersionInfo.ProductName).Trim() -ne "Bibliobrain") {
        throw "The selected executable is not identified as a Bibliobrain installer."
    }
    $installerVersionText = ([string]$file.VersionInfo.ProductVersion).Trim()
    $currentVersionText = (Get-Content -LiteralPath (Join-Path $RepoRoot "VERSION") -Raw).Trim()
    $installerVersion = ConvertTo-BibliobrainVersion -Text $installerVersionText
    $currentVersion = ConvertTo-BibliobrainVersion -Text $currentVersionText
    if (-not $installerVersion) {
        throw "The selected installer has no valid product version."
    }
    if (-not $currentVersion) {
        throw "The installed Bibliobrain version is invalid."
    }
    if ((Compare-BibliobrainVersion -Left $installerVersion -Right $currentVersion) -lt 0) {
        throw "Refusing to replace Bibliobrain $currentVersionText with older version $installerVersionText."
    }

    $signature = Get-AuthenticodeSignature -LiteralPath $resolved
    if ($signature.Status -eq [System.Management.Automation.SignatureStatus]::NotSigned) {
        Write-Host "Warning: this alpha installer is not digitally signed." -ForegroundColor Yellow
        if (-not $Confirm) {
            $answer = Read-Host "Continue with the unsigned Bibliobrain installer [y/N]"
            if ($answer -notmatch "^(y|yes|o|oui)$") {
                throw "Unsigned installer update cancelled."
            }
        }
    }
    elseif ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "The installer signature is invalid: $($signature.Status)"
    }
    return $resolved
}

function Backup-BeforeUpdate {
    if ($BackupPath -or -not $FromApp) {
        Backup-Bibliobrain -Destination $BackupPath | Out-Null
        return
    }
    # In-app updates can happen often: keep their backups under one recognizable
    # name and only the two most recent, instead of filling Documents.
    $folder = Split-Path -Parent (Get-DefaultBackupPath)
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    Backup-Bibliobrain -Destination (Join-Path $folder "Bibliobrain-Backup-BeforeUpdate-$stamp.zip") | Out-Null
    Get-ChildItem -LiteralPath $folder -Filter "Bibliobrain-Backup-BeforeUpdate-*.zip" -File -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Select-Object -Skip 2 |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
}

function Write-UpdateResult {
    param(
        [bool]$Ok,
        [string]$Message = ""
    )
    # Read by src/update_check.py once the restarted server answers the page.
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    $payload = [ordered]@{
        ok = $Ok
        message = $Message
        finished_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    Set-Content -LiteralPath $UpdateResultPath -Value ($payload | ConvertTo-Json) -Encoding UTF8
}

function Invoke-UpdateFromApp {
    $succeeded = $false
    try {
        Enter-UpdateLock
        try {
            Update-Bibliobrain
        }
        finally {
            Exit-UpdateLock
        }
        Write-UpdateResult -Ok $true
        $succeeded = $true
    }
    catch {
        $failure = $_.Exception.Message
        Write-UpdateResult -Ok $false -Message $failure
        Write-Host ""
        Write-Host "The update failed: $failure" -ForegroundColor Red
    }
    # Whatever happened, bring Bibliobrain back so the open page reconnects.
    try {
        Start-Bibliobrain
    }
    catch {
        $succeeded = $false
        Write-Host "Bibliobrain could not restart: $($_.Exception.Message)" -ForegroundColor Red
    }
    if (-not $succeeded) {
        # Keep the explanation on screen; on success this window just closes.
        Read-Host "Press Enter to close this window"
    }
}

function Invoke-ManualUpdate {
    # Update-Bibliobrain.cmd: its console closes as soon as this returns, so
    # the end of the update must be announced where the user cannot miss it.
    $isCheckout = Test-Path (Join-Path $RepoRoot ".git")
    $fromZip = (-not $isCheckout) -and (Test-Path -LiteralPath (Join-Path $RepoRoot $ZipPackageMarker) -PathType Leaf)
    if ($InstallerPath -or (-not $isCheckout -and -not $fromZip -and -not $ArchivePath)) {
        # Setup.exe has its own finish page, whose "Launch Bibliobrain" box
        # runs while this script still waits: no lock and no second dialog.
        Update-Bibliobrain
        return
    }
    Enter-UpdateLock
    try {
        Update-Bibliobrain
    }
    finally {
        Exit-UpdateLock
    }
    $version = (Get-Content -LiteralPath (Join-Path $RepoRoot "VERSION") -Raw).Trim()
    Write-Host ""
    Write-Host "Bibliobrain $version is installed. You can open it now." -ForegroundColor Green
    $openNow = $true
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $answer = [System.Windows.Forms.MessageBox]::Show(
            "The update is finished: Bibliobrain $version is installed.`n`nOpen Bibliobrain now?",
            "Bibliobrain update finished",
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Information,
            [System.Windows.Forms.MessageBoxDefaultButton]::Button1,
            [System.Windows.Forms.MessageBoxOptions]::DefaultDesktopOnly
        )
        $openNow = $answer -eq [System.Windows.Forms.DialogResult]::Yes
    }
    catch {
        $reply = Read-Host "Open Bibliobrain now? [Y/n]"
        $openNow = $reply -notmatch "^(n|no|non)$"
    }
    if ($openNow) {
        Start-Bibliobrain
    }
}

function Update-PackagedBibliobrain {
    $selected = $InstallerPath
    if (-not $selected) {
        $selected = Select-BibliobrainFile `
            -Title "Select the new Bibliobrain installer" `
            -Filter "Bibliobrain installer (Bibliobrain-Setup*.exe)|Bibliobrain-Setup*.exe"
    }
    if (-not $selected) {
        throw "No Bibliobrain installer was selected."
    }
    $validatedInstaller = Test-BibliobrainInstaller -Path $selected
    Backup-BeforeUpdate
    Write-Host "Starting the Bibliobrain update installer..." -ForegroundColor Cyan
    $installerArguments = @("/CURRENTUSER", "/NORESTART")
    if ($FromApp) {
        # Progress bar only, and install.ps1 gets -NoLaunch: this script restarts
        # the server itself and the open page reloads, no second browser tab.
        $installerArguments += @("/SILENT", "/SUPPRESSMSGBOXES", "/UPDATEMODE=1")
    }
    $process = Start-Process `
        -FilePath $validatedInstaller `
        -ArgumentList $installerArguments `
        -Wait `
        -PassThru
    if ($process.ExitCode -eq 20) {
        # InstallScriptFailedExitCode in Bibliobrain.iss: the new files are in
        # place but install.ps1 could not finish (network, disk space...).
        $installLog = Join-Path $RepoRoot "bibliobrain-install.log"
        throw "The new version was copied but its components could not be installed. Your data is untouched: run Repair-Bibliobrain.cmd to finish the update. Details: $installLog"
    }
    if ($process.ExitCode -ne 0) {
        throw "The Bibliobrain installer ended with exit code $($process.ExitCode)."
    }
    Write-Host "Bibliobrain package update completed." -ForegroundColor Green
}

function Test-BibliobrainArchive {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "Bibliobrain ZIP package not found: $resolved"
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = $null
    try {
        $archive = [System.IO.Compression.ZipFile]::OpenRead($resolved)
        $marker = $archive.GetEntry("$ZipPackageRoot/$ZipPackageMarker")
        $versionEntry = $archive.GetEntry("$ZipPackageRoot/VERSION")
        if (-not $marker -or -not $versionEntry) {
            throw "The selected file is not a Bibliobrain ZIP package."
        }
        $reader = New-Object System.IO.StreamReader($versionEntry.Open())
        try {
            $packageVersionText = $reader.ReadToEnd().Trim()
        }
        finally {
            $reader.Dispose()
        }
    }
    catch {
        for ($cause = $_.Exception; $cause; $cause = $cause.InnerException) {
            if ($cause -is [System.IO.InvalidDataException]) {
                throw "The selected file is not a valid ZIP archive."
            }
        }
        throw
    }
    finally {
        if ($archive) {
            $archive.Dispose()
        }
    }
    $currentVersionText = (Get-Content -LiteralPath (Join-Path $RepoRoot "VERSION") -Raw).Trim()
    $packageVersion = ConvertTo-BibliobrainVersion -Text $packageVersionText
    $currentVersion = ConvertTo-BibliobrainVersion -Text $currentVersionText
    if (-not $packageVersion) {
        throw "The selected ZIP package has no valid version."
    }
    if (-not $currentVersion) {
        throw "The installed Bibliobrain version is invalid."
    }
    if ((Compare-BibliobrainVersion -Left $packageVersion -Right $currentVersion) -lt 0) {
        throw "Refusing to replace Bibliobrain $currentVersionText with older version $packageVersionText."
    }
    return $resolved
}

function Update-ZipBibliobrain {
    $selected = $ArchivePath
    if (-not $selected) {
        $selected = Select-BibliobrainFile `
            -Title "Select the new Bibliobrain ZIP package" `
            -Filter "Bibliobrain ZIP package (Bibliobrain-Windows*.zip)|Bibliobrain-Windows*.zip"
    }
    if (-not $selected) {
        throw "No Bibliobrain ZIP package was selected."
    }
    $validatedArchive = Test-BibliobrainArchive -Path $selected
    # Also stops the server, so no file of the application stays locked.
    Backup-BeforeUpdate
    $staging = Join-Path ([System.IO.Path]::GetTempPath()) (
        "bibliobrain-update-" + [Guid]::NewGuid().ToString("N")
    )
    try {
        # Extract before touching the installation: a damaged package fails here.
        Write-Host "Extracting the new version..." -ForegroundColor Cyan
        [System.IO.Compression.ZipFile]::ExtractToDirectory($validatedArchive, $staging)
        $packageRoot = Join-Path $staging $ZipPackageRoot
        try {
            # Same as [InstallDelete] in Bibliobrain.iss: modules the new version
            # removed must not stay importable. .venv is reused by install.ps1.
            foreach ($folder in @("src", "static")) {
                $target = Join-Path $RepoRoot $folder
                if (Test-Path -LiteralPath $target) {
                    Remove-Item -LiteralPath $target -Recurse -Force
                }
            }
            $prefixLength = $packageRoot.TrimEnd("\").Length + 1
            Get-ChildItem -LiteralPath $packageRoot -Recurse -File | ForEach-Object {
                $destination = Join-Path $RepoRoot $_.FullName.Substring($prefixLength)
                New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
                Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
            }
        }
        catch {
            throw "The new files could not all be copied ($($_.Exception.Message)). Your data is untouched: run Update-Bibliobrain.cmd again and select the same ZIP package."
        }
    }
    finally {
        if (Test-Path -LiteralPath $staging) {
            Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "New version copied. Installing its components..." -ForegroundColor Cyan
    Repair-Bibliobrain
    Write-Host "Bibliobrain ZIP update completed." -ForegroundColor Green
}

function Invoke-GitFastForwardPull {
    param([switch]$AllowDifferentOwner)

    $gitArguments = @()
    if ($AllowDifferentOwner) {
        # The user is already running this manager from RepoRoot. Trust that
        # exact checkout for this Git process only; never modify global config.
        $safeRepoRoot = [System.IO.Path]::GetFullPath($RepoRoot).Replace("\", "/")
        $gitArguments += @("-c", "safe.directory=$safeRepoRoot")
    }
    $gitArguments += @("-C", $RepoRoot, "pull", "--ff-only")

    # Git writes normal fetch progress to stderr. Under Windows PowerShell 5.1,
    # that stream must not become a terminating error; the exit code decides.
    $ErrorActionPreference = "Continue"
    $gitOutput = @(& git @gitArguments 2>&1)
    $gitExitCode = $LASTEXITCODE
    $renderedOutput = ($gitOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
    return [pscustomobject]@{
        ExitCode = $gitExitCode
        Output = $renderedOutput
    }
}

function Test-GitAuthenticationFailure {
    param([string]$Output)

    return $Output -match "(?i)authentication failed|failed to authenticate|logon failed|could not read username|http basic: access denied"
}

function Test-GitPermanentFailure {
    param([string]$Output)

    # Failures a retry can never fix: they need a user decision, a different
    # trust setting, or a clean working tree. Everything else (dropped
    # connections, DNS hiccups, proxy or TLS resets, a server that answers
    # slowly on the first request) is treated as transient and retried.
    return $Output -match "(?i)detected dubious ownership|safe\.directory|authentication failed|failed to authenticate|logon failed|could not read username|http basic: access denied|not possible to fast-forward|non-fast-forward|diverged|local changes|would be overwritten|commit your changes or stash|unmerged files|not a git repository|no tracking information|refusing to merge unrelated histories"
}

function Invoke-GitFastForwardPullWithRetry {
    param(
        [switch]$AllowDifferentOwner,
        [int]$MaxAttempts = 4
    )

    # The first pull of a session often fails on a cold network path (VPN or
    # Wi-Fi still coming up, proxy handshake, slow DNS). Retry with a short
    # backoff instead of sending the user back to the launcher.
    $retryDelaysSeconds = @(2, 5, 10)
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        $pullResult = Invoke-GitFastForwardPull -AllowDifferentOwner:$AllowDifferentOwner
        if ($pullResult.ExitCode -eq 0) {
            return $pullResult
        }
        if ($attempt -ge $MaxAttempts -or (Test-GitPermanentFailure -Output $pullResult.Output)) {
            return $pullResult
        }
        $delaySeconds = $retryDelaysSeconds[[Math]::Min($attempt - 1, $retryDelaysSeconds.Count - 1)]
        Write-Host ("Git could not reach the remote repository (attempt {0} of {1})." -f $attempt, $MaxAttempts) -ForegroundColor Yellow
        if ($pullResult.Output) {
            Write-Host $pullResult.Output -ForegroundColor DarkGray
        }
        Write-Host ("Retrying in {0} seconds..." -f $delaySeconds) -ForegroundColor Yellow
        Start-Sleep -Seconds $delaySeconds
    }
}

function Update-Bibliobrain {
    $isCheckout = Test-Path (Join-Path $RepoRoot ".git")
    if ($InstallerPath) {
        Update-PackagedBibliobrain
        return
    }
    # Same order as install_mode() in src/update_check.py.
    $fromZip = (-not $isCheckout) -and (Test-Path -LiteralPath (Join-Path $RepoRoot $ZipPackageMarker) -PathType Leaf)
    if ($ArchivePath -or $fromZip) {
        Update-ZipBibliobrain
        return
    }
    if (-not $isCheckout) {
        Update-PackagedBibliobrain
        return
    }
    if (-not (Get-Command "git" -ErrorAction SilentlyContinue)) {
        throw "Git is required to update this checkout. Install Git or download the newest Bibliobrain package."
    }
    Backup-BeforeUpdate
    $allowDifferentOwner = $false
    $pullResult = Invoke-GitFastForwardPullWithRetry
    if ($pullResult.ExitCode -ne 0 -and $pullResult.Output -match "(?i)detected dubious ownership|safe\.directory") {
        Write-Host "Git reports that this folder belongs to another Windows account." -ForegroundColor Yellow
        Write-Host "Retrying with a trust exception limited to this update and this exact folder." -ForegroundColor Yellow
        $allowDifferentOwner = $true
        $pullResult = Invoke-GitFastForwardPullWithRetry -AllowDifferentOwner
    }
    if ($pullResult.ExitCode -ne 0 -and (Test-GitAuthenticationFailure -Output $pullResult.Output)) {
        # Git Credential Manager can first submit a stale cached credential, then
        # remove it only after the server rejects it. A single retry lets GCM ask
        # the user to sign in without requiring them to restart the updater.
        Write-Host "The saved Git credentials were rejected. Retrying so Git can ask you to sign in..." -ForegroundColor Yellow
        $pullResult = Invoke-GitFastForwardPullWithRetry -AllowDifferentOwner:$allowDifferentOwner
    }
    if ($pullResult.Output) {
        Write-Host $pullResult.Output
    }
    if ($pullResult.ExitCode -ne 0) {
        throw "Git could not update this checkout after several attempts. Review the Git error above; local changes, network or authentication issues, or a non-fast-forward branch can prevent the update."
    }
    Repair-Bibliobrain
}

function Write-UninstallProgress {
    param(
        [int]$Percent,
        [string]$Status
    )
    Write-Progress -Activity "Uninstalling Bibliobrain" -Status $Status -PercentComplete $Percent
    Write-Host ("[{0,3}%] {1}" -f $Percent, $Status) -ForegroundColor Cyan
}

function Uninstall-Bibliobrain {
    if (-not $Confirm) {
        $answer = Read-Host "Remove the Bibliobrain runtime and launchers? User data will be kept [y/N]"
        if ($answer -notmatch "^(y|yes|o|oui)$") {
            Write-Host "Uninstall cancelled."
            return
        }
    }

    Write-UninstallProgress -Percent 5 -Status "Preparing uninstallation..."
    Write-UninstallProgress -Percent 10 -Status "Stopping the Bibliobrain server..."
    Stop-Bibliobrain
    Write-UninstallProgress -Percent 12 -Status "Stopping the Ollama server started by Bibliobrain..."
    Stop-ManagedOllama

    if ($NativeInstaller) {
        # Inno Setup owns application-file removal and can display its native
        # progress bar. Only stop the runtime here so the files are unlocked.
        Write-UninstallProgress -Percent 20 -Status "Runtime stopped. Windows is removing application files..."
        Write-Progress -Activity "Uninstalling Bibliobrain" -Completed
        return
    }

    $runtimeTargets = @(
        @{ Path = (Join-Path $RepoRoot ".venv"); Label = "Removing the Python environment (this can take a few minutes)..." },
        @{ Path = $StateDir; Label = "Removing runtime state..." },
        @{ Path = $LegacyStateDir; Label = "Removing legacy runtime state..." },
        @{ Path = (Join-Path $RepoRoot "run.bat"); Label = "Removing the start launcher..." },
        @{ Path = (Join-Path $RepoRoot "Bibliobrain.lnk"); Label = "Removing the project shortcut..." },
        @{ Path = (Join-Path $RepoRoot "Stop-Bibliobrain.cmd"); Label = "Removing the stop launcher..." },
        @{ Path = (Join-Path $RepoRoot "Repair-Bibliobrain.cmd"); Label = "Removing the repair launcher..." },
        @{ Path = (Join-Path $RepoRoot "Update-Bibliobrain.cmd"); Label = "Removing the update launcher..." },
        @{ Path = (Join-Path $RepoRoot "Backup-Bibliobrain.cmd"); Label = "Removing the backup launcher..." },
        @{ Path = (Join-Path $RepoRoot "Restore-Bibliobrain.cmd"); Label = "Removing the restore launcher..." },
        @{ Path = (Join-Path $RepoRoot "Uninstall-Bibliobrain.cmd"); Label = "Removing the uninstall launcher..." }
    )
    $desktopPath = [Environment]::GetFolderPath("Desktop")
    if ($desktopPath) {
        $runtimeTargets += @{
            Path = (Join-Path $desktopPath "Bibliobrain.lnk")
            Label = "Removing the Desktop shortcut..."
        }
    }
    $failedTargets = @()
    for ($index = 0; $index -lt $runtimeTargets.Count; $index++) {
        $target = $runtimeTargets[$index]
        $percent = 15 + [int][math]::Floor((70 * $index) / [math]::Max(1, $runtimeTargets.Count))
        Write-UninstallProgress -Percent $percent -Status $target.Label
        if ((Split-Path -Leaf $target.Path) -eq "Uninstall-Bibliobrain.cmd" -and $failedTargets.Count -gt 0) {
            Write-Host "Keeping the uninstall launcher so the cleanup can be retried." -ForegroundColor Yellow
            continue
        }
        if (Test-Path $target.Path) {
            if (-not (Remove-RuntimeTarget -Path $target.Path)) {
                $failedTargets += $target.Path
            }
        }
    }

    if ($RemoveUserData) {
        Write-UninstallProgress -Percent 90 -Status "Removing Bibliobrain user data..."
        if (-not $env:LOCALAPPDATA) {
            throw "LOCALAPPDATA is unavailable; user data was not removed."
        }
        $localAppData = [System.IO.Path]::GetFullPath([string]$env:LOCALAPPDATA)
        $userData = [System.IO.Path]::GetFullPath((Join-Path $localAppData "bibliobrain"))
        if (-not $userData.StartsWith($localAppData, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove user data outside LOCALAPPDATA."
        }
        if (Test-Path $userData) {
            Remove-Item -LiteralPath $userData -Recurse -Force
        }
    }
    else {
        Write-UninstallProgress -Percent 90 -Status "Keeping user data in LOCALAPPDATA..."
    }
    Write-UninstallProgress -Percent 100 -Status "Uninstallation complete."
    Write-Progress -Activity "Uninstalling Bibliobrain" -Completed
    if ($failedTargets.Count -gt 0) {
        throw "Uninstallation could not remove: $($failedTargets -join ', '). Close Ollama and rerun the uninstaller."
    }
    if ($RemoveUserData) {
        Write-Host "Bibliobrain runtime and user data removed." -ForegroundColor Green
    }
    else {
        Write-Host "Bibliobrain runtime removed. The source folder and user data were preserved." -ForegroundColor Green
    }
}

Set-Location $RepoRoot
Move-LegacyRuntimeState
switch ($Action) {
    "start" {
        if ($ErrorDialog) {
            try {
                Start-Bibliobrain
            }
            catch {
                Show-StartFailure -Message $_.Exception.Message
                exit 1
            }
        }
        else {
            Start-Bibliobrain
        }
    }
    "stop" { Stop-Bibliobrain }
    "repair" { Repair-Bibliobrain }
    "update" {
        if ($FromApp) { Invoke-UpdateFromApp } else { Invoke-ManualUpdate }
    }
    "backup" { Backup-Bibliobrain -Destination $BackupPath | Out-Null }
    "restore" { Restore-Bibliobrain -Source $BackupPath }
    "uninstall" { Invoke-UninstallWithLogging }
}
