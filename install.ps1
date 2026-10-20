param(
    [ValidateSet("auto", "cpu", "cu126", "cu130")]
    [string]$TorchProfile = "auto",
    [string]$ChatModel = "auto",
    [string]$EmbeddingModel = "embeddinggemma",
    # "ask" prompts in the console; Setup passes the answer chosen in its wizard.
    [ValidateSet("ask", "yes", "no")]
    [string]$EmbeddingDownload = "ask",
    # Setup polls this file to show the current step in its progress page.
    [string]$ProgressFile = "",
    # Writes what the model page of Setup needs (hardware, catalog fit) and exits.
    [string]$ProbeModelsFile = "",
    # The installation the probe runs for: it starts from Setup's temporary
    # folder, so it would not see the legacy .env of that installation otherwise.
    [string]$ExistingInstallDir = "",
    # Setup creates the Start menu and Desktop shortcuts itself.
    [switch]$SkipShortcuts,
    [switch]$SkipRerankerWarmup,
    [switch]$SkipPrerequisiteInstall,
    [switch]$SkipOllamaSetup,
    [switch]$SkipModelDownloads,
    [switch]$NoLaunch,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 redraws its progress bar for every block received,
# which slows a large Invoke-WebRequest download down several times over.
$ProgressPreference = "SilentlyContinue"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
function Get-BibliobrainHome {
    # Mirrors src/config.py and scripts/bibliobrain-manager.ps1: an ephemeral
    # process override, then the fixed per-user JSON, then %LOCALAPPDATA%.
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

function Read-JsonFileOrNull {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Get-ConfiguredEmbeddingModel {
    # The embedding model an existing installation uses, in the order of
    # src/llm_provider.py: the one chosen in Settings (provider_state.json),
    # then OLLAMA_EMBEDDING_MODEL from configuration.json, then from a legacy
    # .env not migrated yet. Its index was built with that model: downloading
    # the default one instead would be useless. Empty on a first installation.
    param([string[]]$LegacyEnvFiles)
    $configurationDir = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "bibliobrain" } else { "" }
    $settings = $null
    if ($configurationDir) {
        $configuration = Read-JsonFileOrNull -Path (Join-Path $configurationDir "configuration.json")
        if ($configuration) {
            $settings = $configuration.settings
        }
    }
    $storageDir = if ($settings -and [string]$settings.STORAGE_DIR) {
        [string]$settings.STORAGE_DIR
    }
    else {
        Join-Path (Get-BibliobrainHome) "storage"
    }
    $providerState = Read-JsonFileOrNull -Path (Join-Path $storageDir "provider_state.json")
    if ($providerState -and ([string]$providerState.ollama_embedding_model).Trim()) {
        return ([string]$providerState.ollama_embedding_model).Trim()
    }
    if ($settings -and ([string]$settings.OLLAMA_EMBEDDING_MODEL).Trim()) {
        return ([string]$settings.OLLAMA_EMBEDDING_MODEL).Trim()
    }
    # Once migrated, the .env is ignored by the application.
    if ($configurationDir -and (Test-Path -LiteralPath (Join-Path $configurationDir "legacy-env-migrated-v1.json"))) {
        return ""
    }
    foreach ($envFile in $LegacyEnvFiles) {
        if (-not $envFile -or -not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
            continue
        }
        $line = Get-Content -LiteralPath $envFile -Encoding UTF8 |
            Where-Object { $_ -match '^\s*OLLAMA_EMBEDDING_MODEL\s*=' } |
            Select-Object -Last 1
        if ($line) {
            $value = ($line -replace '^\s*OLLAMA_EMBEDDING_MODEL\s*=', '').Trim().Trim('"', "'")
            if ($value) {
                return $value
            }
        }
    }
    return ""
}

# An update keeps the embedding model of the existing index; -EmbeddingModel
# given explicitly still wins.
if (-not $PSBoundParameters.ContainsKey("EmbeddingModel")) {
    $legacyEnvFiles = @(Join-Path $RepoRoot ".env")
    if ($ExistingInstallDir) {
        $legacyEnvFiles += Join-Path $ExistingInstallDir ".env"
    }
    $configuredEmbeddingModel = Get-ConfiguredEmbeddingModel -LegacyEnvFiles $legacyEnvFiles
    if ($configuredEmbeddingModel) {
        $EmbeddingModel = $configuredEmbeddingModel
    }
}

# Runtime state lives with the user data (see src/config.py RUNTIME_DIR), not in
# the installation folder, so the code directory stays read-only after setup.
$InstallStateDir = Join-Path (Get-BibliobrainHome) "runtime"
$InstallStatePath = Join-Path $InstallStateDir "install-state.json"
$DependencyInstallMarker = Join-Path $InstallStateDir "dependency-installing.json"
$OllamaPidPath = Join-Path $InstallStateDir "ollama.pid"
$InstallLogPath = Join-Path $RepoRoot "bibliobrain-install.log"
$InstallTranscriptStarted = $false
$VisualCppRuntimeUrl = "https://aka.ms/vc14/vc_redist.x64.exe"
$OllamaInstallerUrl = "https://ollama.com/download/OllamaSetup.exe"
$DownloadTimeoutMinutes = 30
$DownloadRetryCount = 3
$OllamaInstallerTimeoutMinutes = 10
$PythonVersion = "3.14.6"
$PythonInstallerUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
$PythonInstallerSha256 = "14b3e9a710a3fcf0bd9b55ab6b60412bd91227563f813fc49040cabc0209e0bd"
# Chat models the installer offers, best first. Nothing is downloaded without an
# explicit choice. Status and thresholds follow
# docs/5-DEVELOPMENT/model-release-validation.md; update both together.
$ChatModelCatalog = @(
    [pscustomobject]@{
        Model                   = "qwen3.5:9b"
        Download                = "6.1 GiB"
        Status                  = "validated"
        MinGpuVramGiB           = 8
        MinGpuRamGiB            = 16
        MinGpuLogicalProcessors = 6
        MinCpuRamGiB            = 24
        MinCpuLogicalProcessors = 8
    },
    [pscustomobject]@{
        Model                   = "granite4.2:8b"
        Download                = "5.0 GiB"
        Status                  = "caveats"
        MinGpuVramGiB           = 6
        MinGpuRamGiB            = 12
        MinGpuLogicalProcessors = 4
        MinCpuRamGiB            = 16
        MinCpuLogicalProcessors = 8
    },
    [pscustomobject]@{
        Model                   = "qwen3.5:4b"
        Download                = "3.2 GiB"
        Status                  = "caveats"
        MinGpuVramGiB           = 5
        MinGpuRamGiB            = 8
        MinGpuLogicalProcessors = 4
        MinCpuRamGiB            = 12
        MinCpuLogicalProcessors = 6
    },
    # "limited": small models for modest computers. Offered on request, never
    # recommended: they failed library48 or were not run through it. The one
    # marked Fallback is recommended only when nothing better fits, and the app
    # then warns that answers may be poor.
    # gemma4:e2b computes like a 2B model but its download carries the vision
    # and audio encoders, hence the size.
    [pscustomobject]@{
        Model                   = "gemma4:e2b"
        Download                = "6.7 GiB"
        Status                  = "limited"
        MinGpuVramGiB           = 8
        MinGpuRamGiB            = 12
        MinGpuLogicalProcessors = 4
        MinCpuRamGiB            = 16
        MinCpuLogicalProcessors = 6
    },
    [pscustomobject]@{
        Model                   = "qwen3.5:2b"
        Download                = "2.5 GiB"
        Status                  = "limited"
        Fallback                = $true
        MinGpuVramGiB           = 3
        MinGpuRamGiB            = 6
        MinGpuLogicalProcessors = 4
        MinCpuRamGiB            = 8
        MinCpuLogicalProcessors = 4
    },
    [pscustomobject]@{
        Model                   = "qwen3.5:0.8b"
        Download                = "1.2 GiB"
        Status                  = "limited"
        MinGpuVramGiB           = 2
        MinGpuRamGiB            = 4
        MinGpuLogicalProcessors = 2
        MinCpuRamGiB            = 4
        MinCpuLogicalProcessors = 2
    }
)
$ModelChoiceTimeoutSeconds = 300
$SelectedChatModel = ""
$DownloadEmbeddingModel = $false
$EmbeddingModelAlreadyInstalled = $false
# The console's simple installation was accepted: it covers the search module too.
$SimpleInstallAccepted = $false
$GpuSummaryCache = $null
$HardwareSummaryCache = $null
# The step Setup is showing, as written to -ProgressFile; download progress is
# appended to it.
$CurrentStepLine = ""
# PyTorch publishes no cu128 build after 2.11: cu130 covers RTX 50 (Blackwell)
# and every card from Turing on, cu126 the older cards and pre-580 drivers.
$TorchProfiles = @{
    cpu   = @{ IndexUrl = "https://download.pytorch.org/whl/cpu"; Version = "2.14.0" }
    cu126 = @{ IndexUrl = "https://download.pytorch.org/whl/cu126"; Version = "2.14.0" }
    cu130 = @{ IndexUrl = "https://download.pytorch.org/whl/cu130"; Version = "2.14.0" }
}
# CUDA 13 (cu130) needs an NVIDIA driver from the 580 branch or newer.
$Cuda13MinimumDriverMajor = 580
# Oldest GPU generation each CUDA build of PyTorch 2.14 has kernels for:
# Turing (7.5) for cu130, Maxwell (5.0) for cu126. Trying cu130 on an older
# card downloads 2 GB only to fail the kernel check.
$Cuda13MinimumComputeCap = 7.5
$Cuda12MinimumComputeCap = 5.0

function Write-Step {
    param(
        [string]$Message,
        # Setup translates the step from its id (Step_<id> in Bibliobrain.iss);
        # the detail is the one variable part of the label, a model name.
        [string]$Id = "",
        [string]$Detail = ""
    )
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
    $script:CurrentStepLine = "$Id|$Detail|$Message"
    Set-ProgressFileContent -Line $script:CurrentStepLine
}

function Set-ProgressFileContent {
    param([string]$Line)
    if (-not $ProgressFile) {
        return
    }
    # Setup polls the file ten times a second: write a copy and swap it in, so
    # that it never reads a half-written line. The swap fails while Setup has
    # the file open; a direct write is then still better than a stale step.
    try {
        $pending = "$ProgressFile.tmp"
        Set-Content -LiteralPath $pending -Value $Line -Encoding ASCII
        try {
            Move-Item -LiteralPath $pending -Destination $ProgressFile -Force
        }
        catch {
            Set-Content -LiteralPath $ProgressFile -Value $Line -Encoding ASCII
        }
    }
    catch {
        # Progress display is cosmetic: never let it fail an installation.
    }
}

function New-DownloadProgress {
    param([string]$Label)
    return @{
        Label = $Label
        Total = 0.0
        Samples = New-Object System.Collections.Generic.List[object]
        Shown = $false
        LastWrite = [datetime]::MinValue
        LastLog = [datetime]::MinValue
    }
}

function Format-DownloadSize {
    param([double]$Bytes)
    if ($Bytes -ge 1e9) {
        return ("{0:0.0} GB" -f ($Bytes / 1e9)).Replace(",", ".")
    }
    return ("{0:0} MB" -f ($Bytes / 1e6))
}

function Update-DownloadProgress {
    param(
        [hashtable]$Tracker,
        [double]$Done,
        [double]$Total
    )
    # Small files go by too fast for a useful estimate; pip and Ollama both
    # fetch a few of them around the large one.
    if ($Total -lt 20e6) {
        if ($Tracker.Shown) {
            Set-ProgressFileContent -Line $script:CurrentStepLine
            $Tracker.Shown = $false
        }
        return
    }
    $now = Get-Date
    $samples = $Tracker.Samples
    if ($Total -ne $Tracker.Total -or ($samples.Count -gt 0 -and $Done -lt $samples[$samples.Count - 1].Done)) {
        # A new file, or a download that restarted from the beginning.
        $samples.Clear()
        $Tracker.Total = $Total
    }
    $samples.Add([pscustomobject]@{ Time = $now; Done = $Done })
    # The rate over the last 30 seconds follows a connection that slows down
    # without jumping at every chunk.
    while ($samples.Count -gt 2 -and ($now - $samples[0].Time).TotalSeconds -gt 30) {
        $samples.RemoveAt(0)
    }
    $etaSeconds = -1
    $bytesPerSecond = 0.0
    $span = ($now - $samples[0].Time).TotalSeconds
    if ($span -ge 3) {
        $bytesPerSecond = ($Done - $samples[0].Done) / $span
        if ($bytesPerSecond -gt 0) {
            $etaSeconds = [int][math]::Ceiling(($Total - $Done) / $bytesPerSecond)
        }
    }
    $finished = $Done -ge $Total
    if ($finished -or ($now - $Tracker.LastWrite).TotalSeconds -ge 1) {
        $line = "{0}|{1}|{2}|{3}" -f $script:CurrentStepLine, [int64][math]::Floor($Done / 1e6), [int64][math]::Floor($Total / 1e6), $etaSeconds
        Set-ProgressFileContent -Line $line
        $Tracker.LastWrite = $now
        $Tracker.Shown = $true
    }
    if ($finished -or ($now - $Tracker.LastLog).TotalSeconds -ge 30) {
        $message = "{0}: {1} of {2} downloaded" -f $Tracker.Label, (Format-DownloadSize $Done), (Format-DownloadSize $Total)
        if ($bytesPerSecond -gt 0 -and -not $finished) {
            $message += (", {0}/s, about {1} min left" -f (Format-DownloadSize $bytesPerSecond), [math]::Ceiling($etaSeconds / 60))
        }
        Write-Host $message -ForegroundColor DarkGray
        $Tracker.LastLog = $now
    }
}

function Grant-FileDaclOnly {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][System.Security.Principal.SecurityIdentifier[]]$Identities
    )

    # icacls only ever rewrites the DACL, and SIDs keep the call independent of
    # the display language of the account names. Same approach as
    # Protect-PrivateFileAcl in scripts/bibliobrain-manager.ps1.
    $arguments = @($Path, "/inheritance:r", "/grant:r")
    foreach ($identity in $Identities) {
        $arguments += "*$($identity.Value):(F)"
    }

    # A native tool writing to stderr must not become a terminating error here;
    # the exit code is what decides.
    $ErrorActionPreference = "Continue"
    $output = & icacls.exe @arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        # ToString() keeps only the message; Out-String would drag along the
        # NativeCommandError position block that Windows PowerShell 5.1 adds.
        throw (($output | ForEach-Object { $_.ToString() }) -join " ").Trim()
    }
}

function Grant-FileDaclViaDotNet {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][System.Security.Principal.SecurityIdentifier[]]$Identities
    )

    $acl = [System.Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($identity in $Identities) {
        $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
                $identity,
                [System.Security.AccessControl.FileSystemRights]::FullControl,
                [System.Security.AccessControl.AccessControlType]::Allow
            ))
    }

    # Deliberately not Set-Acl: that cmdlet round-trips the descriptor through
    # its binary form, which flags every section as modified and makes Windows
    # write the SACL as well. Writing a SACL requires SeSecurityPrivilege, which
    # a standard domain account does not hold, so Set-Acl fails with
    # PrivilegeNotHeldException on a managed machine. The .NET call below
    # persists only the sections that were actually touched, i.e. the DACL.
    $file = Get-Item -LiteralPath $Path -Force
    if ($file | Get-Member -Name SetAccessControl -MemberType Method) {
        $file.SetAccessControl($acl)
    }
    else {
        # PowerShell 7+ exposes it as an extension method instead.
        [System.IO.FileSystemAclExtensions]::SetAccessControl($file, $acl)
    }
}

function Protect-ConfigurationFileAcl {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    if ($DryRun) {
        Write-Host "[dry-run] Restrict ACL on $Path to the current user, SYSTEM and Administrators" -ForegroundColor DarkGray
        return
    }

    $identities = @(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().User,
        [System.Security.Principal.SecurityIdentifier]::new("S-1-5-18"),
        [System.Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
    )

    $failures = @()
    foreach ($method in @("Grant-FileDaclOnly", "Grant-FileDaclViaDotNet")) {
        try {
            & $method -Path $Path -Identities $identities
            return
        }
        catch {
            $failures += "$method : $($_.Exception.Message)"
        }
    }

    # Restricting the file is defence in depth, not a prerequisite: it already
    # lives under the per-user profile. Never abort the installation for it -- a
    # run that dies here throws away the multi-gigabyte PyTorch download.
    Write-Warning "Could not restrict the permissions on $Path. It keeps the permissions inherited from its folder; review them by hand if it holds credentials."
    foreach ($failure in $failures) {
        Write-Host "  $failure" -ForegroundColor DarkGray
    }
}

function Get-FreeDiskSpaceGiB {
    param([string]$Path)
    try {
        $root = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($Path))
        return [math]::Round(([System.IO.DriveInfo]::new($root)).AvailableFreeSpace / 1GB, 1)
    }
    catch {
        # A network share has no drive to query: let the installation try.
        return $null
    }
}

function Assert-FreeDiskSpace {
    # Only a first installation downloads and unpacks the whole Python stack,
    # and running out of room there fails late, after a long download.
    if (Test-Path $VenvPython) {
        return
    }
    # Rounded up from a measured environment: about 4 GiB with a CUDA build of
    # PyTorch (1.5 GiB with the CPU build), and as much again for the wheels pip
    # keeps in its cache while it installs them.
    $perLocationGiB = if ((Get-GpuSummary).HasNvidia) { 5 } else { 2 }
    $neededByDrive = @{}
    foreach ($location in @($RepoRoot, [System.IO.Path]::GetTempPath())) {
        $root = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($location)).ToUpperInvariant()
        $neededByDrive[$root] = [int]$neededByDrive[$root] + $perLocationGiB
    }
    foreach ($root in $neededByDrive.Keys) {
        $freeGiB = Get-FreeDiskSpaceGiB -Path $root
        if ($null -ne $freeGiB -and $freeGiB -lt $neededByDrive[$root]) {
            throw "Not enough free disk space on drive ${root}: $freeGiB GiB free, at least $($neededByDrive[$root]) GiB needed for the Python environment alone (Ollama and the models come on top). Free some space, then rerun the installer."
        }
    }
}

function Format-Command {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )
    $parts = @($FilePath) + $Arguments
    return ($parts | ForEach-Object {
            if ($_ -match "\s") {
                '"' + $_ + '"'
            }
            else {
                $_
            }
        }) -join " "
}

function Invoke-External {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$Label,
        # pip run with "--progress-bar raw": its "Progress <done> of <total>"
        # lines feed the download estimate instead of the log.
        [switch]$PipProgress,
        # Runs once when pip starts installing what it downloaded.
        [scriptblock]$OnPipInstall = $null
    )
    $rendered = Format-Command -FilePath $FilePath -Arguments $Arguments
    if ($DryRun) {
        Write-Host "[dry-run] $rendered" -ForegroundColor DarkGray
        return
    }

    # Run native tools in a background PowerShell process so their stdout and
    # stderr can both be copied into the transcript while the parent emits a
    # heartbeat during silent extraction/install phases.
    $invocation = @{
        FilePath = $FilePath
        Arguments = @($Arguments)
        WorkingDirectory = $RepoRoot
    }
    $job = $null
    $exitCode = $null
    $lastHeartbeat = Get-Date
    $download = New-DownloadProgress -Label $Label
    # Dot-sourced, so that clearing $OnPipInstall lasts.
    $writeOutput = {
        param([string]$Text)
        if ($PipProgress -and $Text -match '^Progress (\d+) of (\d+)$') {
            Update-DownloadProgress -Tracker $download -Done ([double]$Matches[1]) -Total ([double]$Matches[2])
            return
        }
        Write-Host $Text
        if ($OnPipInstall -and $Text -match '^Installing collected packages') {
            & $OnPipInstall
            $OnPipInstall = $null
        }
    }
    try {
        $job = Start-Job -ArgumentList (,$invocation) -ScriptBlock {
            param($Invocation)
            $nativeExitCode = 1
            try {
                Set-Location ([string]$Invocation.WorkingDirectory)
                $nativeArguments = @($Invocation.Arguments)
                & ([string]$Invocation.FilePath) @nativeArguments 2>&1 |
                    ForEach-Object {
                        # An empty stderr line would otherwise print as the
                        # name of its exception type and look like an error.
                        if ($_ -is [System.Management.Automation.ErrorRecord]) {
                            [string]$_.Exception.Message
                        }
                        else {
                            [string]$_
                        }
                    }
                $nativeExitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
            }
            catch {
                [string]$_
                $nativeExitCode = 1
            }
            [pscustomobject]@{
                BibliobrainNativeResult = $true
                ExitCode = $nativeExitCode
            }
        }

        do {
            foreach ($item in @(Receive-Job -Job $job)) {
                if ($item.PSObject.Properties.Name -contains "BibliobrainNativeResult") {
                    $exitCode = [int]$item.ExitCode
                }
                else {
                    . $writeOutput ([string]$item)
                }
            }
            if ($job.State -eq "Running") {
                $now = Get-Date
                if (($now - $lastHeartbeat).TotalSeconds -ge 20) {
                    Write-Host "$Label is still running; please keep this window open..." -ForegroundColor DarkGray
                    $lastHeartbeat = $now
                }
                Wait-Job -Job $job -Timeout 1 | Out-Null
            }
        } while ($job.State -eq "Running")

        foreach ($item in @(Receive-Job -Job $job)) {
            if ($item.PSObject.Properties.Name -contains "BibliobrainNativeResult") {
                $exitCode = [int]$item.ExitCode
            }
            else {
                . $writeOutput ([string]$item)
            }
        }
        if ($job.State -eq "Failed" -or $null -eq $exitCode) {
            throw "$Label could not be executed by the installer process."
        }
    }
    finally {
        if ($job) {
            if ($job.State -eq "Running") {
                Stop-Job -Job $job -ErrorAction SilentlyContinue
            }
            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        }
    }
    if ($exitCode -ne 0) {
        throw "$Label failed with exit code $exitCode."
    }
}

function Test-PythonCandidate {
    param(
        [string]$FilePath,
        [string[]]$PrefixArgs
    )
    try {
        & $FilePath @PrefixArgs "-c" "import sys; raise SystemExit(0 if sys.version_info >= (3, 14) else 1)" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Find-PythonBootstrap {
    $localPython = if ($env:LOCALAPPDATA) {
        Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"
    }
    else {
        ""
    }
    $candidates = @(
        @{ FilePath = "py"; PrefixArgs = @("-3.14") },
        @{ FilePath = $localPython; PrefixArgs = @() },
        @{ FilePath = "python"; PrefixArgs = @() }
    ) | Where-Object { $_.FilePath }
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.FilePath -ErrorAction SilentlyContinue)) {
            continue
        }
        if (Test-PythonCandidate -FilePath $candidate.FilePath -PrefixArgs $candidate.PrefixArgs) {
            return $candidate
        }
    }
    return $null
}

function Get-PythonScriptArguments {
    param(
        [string]$Script,
        [string[]]$ScriptArguments = @()
    )
    # Windows PowerShell 5 removes embedded double quotes when it builds the
    # command line for a native executable. Encoding keeps Python source intact.
    $encodedScript = [Convert]::ToBase64String(
        [System.Text.Encoding]::UTF8.GetBytes($Script)
    )
    $bootstrap = "import base64;exec(compile(base64.b64decode('$encodedScript'),'<bibliobrain-installer>','exec'))"
    return @("-c", $bootstrap) + @($ScriptArguments)
}

function Test-VisualCppRuntime {
    $registryPaths = @(
        "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
    )
    foreach ($registryPath in $registryPaths) {
        if (-not (Test-Path $registryPath)) {
            continue
        }
        $runtime = Get-ItemProperty -Path $registryPath -ErrorAction SilentlyContinue
        if ($runtime -and [int]$runtime.Installed -eq 1 -and [int]$runtime.Major -ge 14) {
            return $true
        }
    }
    return $false
}

function Ensure-VisualCppRuntime {
    if (Test-VisualCppRuntime) {
        Write-Host "Microsoft Visual C++ x64 runtime already installed." -ForegroundColor DarkCyan
        return
    }
    if ($SkipPrerequisiteInstall) {
        throw "Microsoft Visual C++ v14 Redistributable (x64) is required. Install $VisualCppRuntimeUrl, then rerun this script."
    }

    Write-Step "Installing Microsoft Visual C++ x64 runtime" -Id vcredist
    if ($DryRun) {
        Write-Host "[dry-run] Download and install $VisualCppRuntimeUrl" -ForegroundColor DarkGray
        return
    }

    $installerPath = Join-Path `
        ([System.IO.Path]::GetTempPath()) `
        ("bibliobrain-vc-redist-" + [Guid]::NewGuid().ToString("N") + ".exe")
    try {
        Save-InstallerDownload -Uri $VisualCppRuntimeUrl -DestinationPath $installerPath -Name "Microsoft Visual C++ runtime"
        $signature = Get-AuthenticodeSignature -FilePath $installerPath
        if ($signature.Status -ne "Valid" -or
            $signature.SignerCertificate.Subject -notmatch "Microsoft Corporation") {
            throw "Downloaded Microsoft Visual C++ runtime has an invalid publisher signature."
        }
        $process = Start-Process `
            -FilePath $installerPath `
            -ArgumentList @("/install", "/passive", "/norestart") `
            -Verb RunAs `
            -Wait `
            -PassThru
        if ($process.ExitCode -notin @(0, 1638, 3010)) {
            throw "Microsoft Visual C++ runtime installer failed with exit code $($process.ExitCode)."
        }
    }
    finally {
        if (Test-Path $installerPath) {
            Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
        }
    }

    if (-not (Test-VisualCppRuntime)) {
        throw "Microsoft Visual C++ runtime installation did not complete. Restart Windows, then rerun this installer."
    }
    Write-Host "Microsoft Visual C++ x64 runtime installed." -ForegroundColor DarkCyan
}

function Install-PythonFromOfficialInstaller {
    Write-Step "Installing Python $PythonVersion from python.org" -Id python
    if ($DryRun) {
        Write-Host "[dry-run] Download and install $PythonInstallerUrl" -ForegroundColor DarkGray
        return
    }

    $installerPath = Join-Path `
        ([System.IO.Path]::GetTempPath()) `
        ("bibliobrain-python-" + [Guid]::NewGuid().ToString("N") + ".exe")
    try {
        Save-InstallerDownload -Uri $PythonInstallerUrl -DestinationPath $installerPath -Name "official Python installer"

        $actualHash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $PythonInstallerSha256) {
            throw "Downloaded Python installer failed SHA-256 verification."
        }
        $signature = Get-AuthenticodeSignature -FilePath $installerPath
        if ($signature.Status -ne "Valid" -or
            $signature.SignerCertificate.Subject -notmatch "Python Software Foundation") {
            throw "Downloaded Python installer has an invalid publisher signature."
        }

        $process = Start-Process `
            -FilePath $installerPath `
            -ArgumentList @(
                "/passive",
                "InstallAllUsers=0",
                "PrependPath=0",
                "AssociateFiles=0",
                "Shortcuts=0",
                "Include_launcher=0",
                "Include_pip=1",
                "Include_test=0",
                "Include_doc=0",
                "Include_tcltk=0"
            ) `
            -Wait `
            -PassThru
        if ($process.ExitCode -notin @(0, 3010)) {
            throw "Python installer failed with exit code $($process.ExitCode)."
        }
    }
    finally {
        if (Test-Path $installerPath) {
            Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Resolve-PythonBootstrap {
    $candidate = Find-PythonBootstrap
    if ($candidate) {
        return $candidate
    }

    if ($SkipPrerequisiteInstall) {
        throw "Python 3.14 or newer is required. Install Python 3.14+, then rerun this script."
    }
    $wingetAvailable = [bool](Get-Command "winget" -ErrorAction SilentlyContinue)
    if ($wingetAvailable) {
        Write-Step "Installing Python 3.14 with winget" -Id python
        try {
            Invoke-External `
                -FilePath "winget" `
                -Arguments @(
                    "install", "--id", "Python.Python.3.14", "--exact",
                    "--accept-package-agreements", "--accept-source-agreements",
                    "--silent", "--disable-interactivity"
                ) `
                -Label "Install Python 3.14"
            if ($DryRun) {
                return @{ FilePath = "py"; PrefixArgs = @("-3.14") }
            }
        }
        catch {
            Write-Host "Winget could not install Python. Trying the official python.org installer..." -ForegroundColor Yellow
        }
    }
    else {
        Write-Host "Winget is unavailable. Using the official python.org installer..." -ForegroundColor Yellow
    }

    $candidate = Find-PythonBootstrap
    if ($candidate) {
        return $candidate
    }

    Install-PythonFromOfficialInstaller
    if ($DryRun) {
        $localPython = Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"
        return @{ FilePath = $localPython; PrefixArgs = @() }
    }

    $candidate = Find-PythonBootstrap
    if ($candidate) {
        return $candidate
    }
    throw "Python 3.14 installation completed but Python is not accessible. Restart Windows, then rerun Install-Bibliobrain.cmd."
}

function Get-GpuSummary {
    if ($script:GpuSummaryCache) {
        return $script:GpuSummaryCache
    }

    $names = @()
    $maxVramMiB = 0
    try {
        $controllers = @(Get-CimInstance Win32_VideoController -ErrorAction Stop)
        foreach ($controller in $controllers) {
            $name = [string]$controller.Name
            if ($name) {
                $names += $name
            }
            # Win32_VideoController.AdapterRAM can be capped at 4 GiB on some
            # drivers. It is still useful as a conservative non-NVIDIA fallback.
            if ($name -notmatch "Microsoft (Basic|Remote)" -and $controller.AdapterRAM) {
                $candidateMiB = [int][math]::Floor(([double]$controller.AdapterRAM) / 1MB)
                $maxVramMiB = [math]::Max($maxVramMiB, $candidateMiB)
            }
        }
    }
    catch {
        $names = @()
    }

    # The WMI AdapterRAM property is only 32-bit and therefore truncates cards
    # larger than 4 GiB. Current Windows display drivers also expose the real
    # 64-bit value in the registry; only accept entries matching an active CIM
    # adapter so stale driver records cannot select an oversized model.
    try {
        $displayDrivers = @(Get-ItemProperty `
                -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Video\*\0000" `
                -ErrorAction SilentlyContinue)
        foreach ($driver in $displayDrivers) {
            $driverName = [string]$driver.DriverDesc
            $memoryBytes = $driver.'HardwareInformation.qwMemorySize'
            if ($driverName -and $driverName -in $names -and $memoryBytes) {
                $candidateMiB = [int][math]::Floor(([double]$memoryBytes) / 1MB)
                $maxVramMiB = [math]::Max($maxVramMiB, $candidateMiB)
            }
        }
    }
    catch {
        # Keep the conservative CIM estimate if the registry is unavailable.
    }

    # Query nvidia-smi even when CIM returned a Remote Display Adapter: remote
    # desktop sessions can otherwise hide a perfectly usable NVIDIA card.
    if (Get-Command "nvidia-smi" -ErrorAction SilentlyContinue) {
        try {
            $nvidiaRows = @(& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null)
            foreach ($row in $nvidiaRows) {
                if ([string]$row -match "^(.+),\s*(\d+)\s*$") {
                    $names += $Matches[1].Trim()
                    $maxVramMiB = [math]::Max($maxVramMiB, [int]$Matches[2])
                }
            }
        }
        catch {
            # Keep the conservative CIM result when nvidia-smi is unavailable.
        }
    }
    # Driver branch and compute capability decide which CUDA build can run.
    # Each field is queried alone: older nvidia-smi builds reject unknown ones.
    $nvidiaDriverVersion = ""
    $computeCaps = @()
    if (Get-Command "nvidia-smi" -ErrorAction SilentlyContinue) {
        try {
            $nvidiaDriverVersion = [string](@(& nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>$null) | Select-Object -First 1)
            $nvidiaDriverVersion = $nvidiaDriverVersion.Trim()
        }
        catch {
            $nvidiaDriverVersion = ""
        }
        try {
            foreach ($row in @(& nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>$null)) {
                $capability = 0.0
                if ([double]::TryParse(([string]$row).Trim(), [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$capability)) {
                    $computeCaps += $capability
                }
            }
        }
        catch {
            $computeCaps = @()
        }
    }
    $names = @($names | Where-Object { $_ -and $_.Trim() } | Select-Object -Unique)
    $hasNvidia = $false
    $hasBlackwell = $false
    foreach ($name in $names) {
        if ($name -match "NVIDIA") {
            $hasNvidia = $true
        }
        if ($name -match "RTX\s*50\d\d|Blackwell") {
            $hasBlackwell = $true
        }
    }
    if (@($computeCaps | Where-Object { $_ -ge 10.0 }).Count -gt 0) {
        $hasBlackwell = $true
    }
    # 0 when nvidia-smi could not tell: the profile list then stays unchanged.
    $maxComputeCap = 0.0
    foreach ($capability in $computeCaps) {
        $maxComputeCap = [math]::Max($maxComputeCap, [double]$capability)
    }
    $script:GpuSummaryCache = [pscustomobject]@{
        Names               = $names
        HasNvidia           = $hasNvidia
        HasBlackwell        = $hasBlackwell
        NvidiaDriverVersion = $nvidiaDriverVersion
        MaxComputeCap       = $maxComputeCap
        MaxVramMiB          = $maxVramMiB
    }
    return $script:GpuSummaryCache
}

function Get-HardwareSummary {
    if ($script:HardwareSummaryCache) {
        return $script:HardwareSummaryCache
    }

    $gpu = Get-GpuSummary
    $totalRamMiB = 0
    $logicalProcessorCount = [Environment]::ProcessorCount
    $physicalCoreCount = 0
    $maxClockMHz = 0
    $cpuNames = @()

    try {
        $computerSystem = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
        if ($computerSystem.TotalPhysicalMemory) {
            $totalRamMiB = [int64][math]::Floor(
                ([double]$computerSystem.TotalPhysicalMemory) / 1MB
            )
        }
        if ($computerSystem.NumberOfLogicalProcessors) {
            $logicalProcessorCount = [int]$computerSystem.NumberOfLogicalProcessors
        }
    }
    catch {
        # Keep the portable processor-count fallback and try the OS RAM value below.
    }

    if ($totalRamMiB -le 0) {
        try {
            $operatingSystem = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
            if ($operatingSystem.TotalVisibleMemorySize) {
                $totalRamMiB = [int64][math]::Floor(
                    ([double]$operatingSystem.TotalVisibleMemorySize) / 1KB
                )
            }
        }
        catch {
            # Unknown RAM keeps the automatic selection on the safest tier.
        }
    }

    try {
        $processors = @(Get-CimInstance Win32_Processor -ErrorAction Stop)
        $reportedLogicalProcessors = 0
        foreach ($processor in $processors) {
            $name = ([string]$processor.Name).Trim()
            if ($name) {
                $cpuNames += $name
            }
            if ($processor.NumberOfCores) {
                $physicalCoreCount += [int]$processor.NumberOfCores
            }
            if ($processor.NumberOfLogicalProcessors) {
                $reportedLogicalProcessors += [int]$processor.NumberOfLogicalProcessors
            }
            if ($processor.MaxClockSpeed) {
                $maxClockMHz = [math]::Max($maxClockMHz, [int]$processor.MaxClockSpeed)
            }
        }
        if ($reportedLogicalProcessors -gt 0) {
            $logicalProcessorCount = $reportedLogicalProcessors
        }
    }
    catch {
        # Core and clock details are informative; selection can use logical processors.
    }

    $script:HardwareSummaryCache = [pscustomobject]@{
        GpuNames              = $gpu.Names
        HasNvidia             = $gpu.HasNvidia
        MaxVramMiB            = $gpu.MaxVramMiB
        TotalRamMiB           = $totalRamMiB
        CpuNames              = @($cpuNames | Select-Object -Unique)
        PhysicalCoreCount     = $physicalCoreCount
        LogicalProcessorCount = $logicalProcessorCount
        MaxClockMHz           = $maxClockMHz
    }
    return $script:HardwareSummaryCache
}

function Test-OllamaUsableGpu {
    param($Hardware)
    # Ollama accelerates NVIDIA and discrete AMD cards; the shared memory that an
    # integrated Intel or "AMD Radeon(TM) Graphics" adapter reports is not VRAM.
    $usableNames = @($Hardware.GpuNames | Where-Object { $_ -match "NVIDIA|Radeon (RX|Pro)" })
    return ($Hardware.MaxVramMiB -gt 0 -and $usableNames.Count -gt 0)
}

function Get-ChatModelFit {
    param($Entry, $Hardware)
    $threads = [int]$Hardware.LogicalProcessorCount
    $cpuFits = (
        $Hardware.TotalRamMiB -ge ($Entry.MinCpuRamGiB * 1024) -and
        $threads -ge $Entry.MinCpuLogicalProcessors
    )
    if (Test-OllamaUsableGpu -Hardware $Hardware) {
        $gpuFits = (
            $Hardware.MaxVramMiB -ge ($Entry.MinGpuVramGiB * 1024) -and
            $Hardware.TotalRamMiB -ge ($Entry.MinGpuRamGiB * 1024) -and
            $threads -ge $Entry.MinGpuLogicalProcessors
        )
        if ($gpuFits) {
            return "GPU"
        }
        # Ollama can split a model between VRAM and RAM, but answers get very slow.
        if ($cpuFits) {
            return "GPU+CPU, slow"
        }
        return "too big"
    }
    if ($cpuFits) {
        return "CPU only, slow"
    }
    return "too big"
}

function Get-RecommendedChatModel {
    param($Hardware)
    $candidates = @($ChatModelCatalog | Where-Object { $_.Status -ne "limited" })
    foreach ($entry in $candidates) {
        if ((Get-ChatModelFit -Entry $entry -Hardware $Hardware) -eq "GPU") {
            return $entry.Model
        }
    }
    # Without a usable GPU the CPU is the only option; with one, a model that
    # does not fit in VRAM is never recommended.
    $hasGpu = Test-OllamaUsableGpu -Hardware $Hardware
    if (-not $hasGpu) {
        foreach ($entry in $candidates) {
            if ((Get-ChatModelFit -Entry $entry -Hardware $Hardware) -eq "CPU only, slow") {
                return $entry.Model
            }
        }
    }
    # Nothing tested fits: the fallback model. On a small graphics card it may
    # spill into RAM (Ollama splits it by itself): slow, but better than no AI.
    $wantedFits = if ($hasGpu) { @("GPU", "GPU+CPU, slow") } else { @("CPU only, slow") }
    foreach ($entry in @($ChatModelCatalog | Where-Object { $_.Fallback })) {
        if ($wantedFits -contains (Get-ChatModelFit -Entry $entry -Hardware $Hardware)) {
            return $entry.Model
        }
    }
    return ""
}

function Show-ChatModelCatalog {
    param(
        $Hardware,
        [string[]]$InstalledModels
    )
    # Unknown memory: no fit can be told, so none is shown.
    $hardwareKnown = $Hardware.TotalRamMiB -gt 0
    $ramGiB = [math]::Round($Hardware.TotalRamMiB / 1024, 1)
    $gpuText = "no GPU usable by Ollama"
    if (Test-OllamaUsableGpu -Hardware $Hardware) {
        $gpuText = "$([math]::Round($Hardware.MaxVramMiB / 1024, 1)) GiB VRAM"
    }
    if ($hardwareKnown) {
        Write-Host "This PC: $ramGiB GiB RAM, $($Hardware.LogicalProcessorCount) CPU threads, $gpuText." -ForegroundColor DarkCyan
    }
    else {
        Write-Host "This PC: configuration unknown." -ForegroundColor DarkCyan
    }
    Write-Host ""

    $format = "  {0,-2} {1,-14} {2,-9} {3,-10} {4,-24} {5,-22} {6}"
    Write-Host ($format -f "#", "Model", "Download", "Status", "Fully on GPU needs", "CPU only needs", "On this PC")
    $recommended = Get-RecommendedChatModel -Hardware $Hardware
    for ($index = 0; $index -lt $ChatModelCatalog.Count; $index++) {
        $entry = $ChatModelCatalog[$index]
        $fit = if ($hardwareKnown) { Get-ChatModelFit -Entry $entry -Hardware $Hardware } else { "unknown" }
        $marks = @()
        if ($entry.Model -eq $recommended) {
            $marks += "recommended"
        }
        if (Test-ModelNameIn -Model $entry.Model -Names $InstalledModels) {
            $marks += "installed"
        }
        $fitText = $fit
        if ($marks.Count -gt 0) {
            $fitText = "$fit ($($marks -join ', '))"
        }
        $color = "Yellow"
        if ($fit -eq "GPU") {
            $color = "Green"
        }
        elseif ($fit -eq "too big") {
            $color = "DarkGray"
        }
        Write-Host ($format -f ($index + 1), $entry.Model, $entry.Download, $entry.Status,
            "$($entry.MinGpuVramGiB) GiB VRAM, $($entry.MinGpuRamGiB) GiB RAM",
            "$($entry.MinCpuRamGiB) GiB RAM, $($entry.MinCpuLogicalProcessors) threads",
            $fitText) -ForegroundColor $color
    }
    Write-Host ""
    Write-Host "validated: no critical failure in the library48 benchmark and a clean quality review." -ForegroundColor DarkGray
    Write-Host "caveats: no critical failure, but the review found weaknesses (granite4.2:8b: occasional calculation slip; qwen3.5:4b: weak structured output)." -ForegroundColor DarkGray
    Write-Host "limited: small models for modest computers; less reliable answers and citations. Only qwen3.5:2b is recommended, when nothing better fits." -ForegroundColor DarkGray
    Write-Host "A model split between GPU and CPU, or running on the CPU only, answers much more slowly." -ForegroundColor DarkGray
}

function Read-LineWithTimeout {
    param(
        [string]$Prompt,
        [int]$TimeoutSeconds
    )
    # Returns $null when nobody can answer: redirected input, -NonInteractive,
    # no console, or no key pressed before the timeout. The timeout keeps an
    # unattended installation from waiting forever.
    try {
        if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) {
            return $null
        }
        if ([Environment]::GetCommandLineArgs() -match "^-noni") {
            return $null
        }
        while ([Console]::KeyAvailable) {
            [void][Console]::ReadKey($true)
        }
        Write-Host "$Prompt " -NoNewline -ForegroundColor Yellow
        $buffer = New-Object System.Text.StringBuilder
        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        while ((Get-Date) -lt $deadline) {
            if (-not [Console]::KeyAvailable) {
                Start-Sleep -Milliseconds 100
                continue
            }
            $key = [Console]::ReadKey($true)
            if ($key.Key -eq [ConsoleKey]::Enter) {
                Write-Host ""
                return $buffer.ToString().Trim()
            }
            if ($key.Key -eq [ConsoleKey]::Backspace) {
                if ($buffer.Length -gt 0) {
                    [void]$buffer.Remove($buffer.Length - 1, 1)
                    Write-Host "`b `b" -NoNewline
                }
            }
            elseif (-not [char]::IsControl($key.KeyChar)) {
                [void]$buffer.Append($key.KeyChar)
                Write-Host $key.KeyChar -NoNewline
            }
            $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        }
        Write-Host ""
        Write-Host "No answer after $TimeoutSeconds s." -ForegroundColor DarkYellow
    }
    catch {
        # No usable console: behave as if nobody answered.
    }
    return $null
}

function Select-LocalModels {
    # Asked before the long steps so the user answers once, then can leave the
    # installation running. Nothing is downloaded without an explicit answer.
    if ($SkipOllamaSetup -or $SkipModelDownloads) {
        return
    }
    Write-Step "Choosing local AI models" -Id models
    $installed = @(Get-OllamaInstalledModelNames)
    $hardware = Get-HardwareSummary

    if ($ChatModel -and $ChatModel -ne "auto") {
        if ($ChatModel -eq "none") {
            Write-Host "No chat model requested (-ChatModel none)." -ForegroundColor DarkCyan
        }
        else {
            $script:SelectedChatModel = $ChatModel
            Write-Host "Chat model requested with -ChatModel: $ChatModel" -ForegroundColor DarkCyan
        }
    }
    else {
        # The same choices as Setup's wizard (packaging/Bibliobrain.iss): a
        # simple installation by default, a custom one on request, and the same
        # warnings. Without an answer nothing is downloaded.
        $existing = @(Get-OllamaChatModelNames -Names $installed)
        if ($existing.Count -gt 0) {
            Write-Host ("AIs already installed on this computer: " + ($existing -join ", ")) -ForegroundColor DarkCyan
        }
        $fallbackModel = ($ChatModelCatalog | Where-Object { $_.Fallback } | Select-Object -First 1).Model
        $custom = $false
        $customDefault = ""
        if ($hardware.TotalRamMiB -le 0) {
            Write-Host "The configuration of this computer could not be found. Which AI do you want to use? If unsure, keep $($fallbackModel): the smallest, it runs on most computers." -ForegroundColor Yellow
            $custom = $true
            $customDefault = $fallbackModel
        }
        else {
            $recommended = Get-RecommendedChatModel -Hardware $hardware
            if ($recommended) {
                $entry = $ChatModelCatalog | Where-Object { $_.Model -eq $recommended } | Select-Object -First 1
                Write-Host "Simple installation (recommended): Bibliobrain downloads the AI that suits this computer, $recommended ($($entry.Download)), and the module that searches your documents." -ForegroundColor DarkCyan
                if ($recommended -eq $fallbackModel) {
                    Write-Host "This computer only runs a very small AI: its answers will be less reliable. A remote provider (an external API) can be connected later in Settings." -ForegroundColor Yellow
                }
                $prompt = "Press Enter to accept, or type C to choose yourself (custom installation):"
            }
            elseif ($existing.Count -eq 0) {
                Write-Host "This computer does not reach the minimum configuration to run an AI (8 GB of memory and 4 processor cores): Bibliobrain will not work properly with an AI on it, so none will be downloaded." -ForegroundColor Yellow
                Write-Host "You can still install it and use a remote provider (an external API) to ask your questions, configured later in Settings. Your PDFs are prepared on this computer either way." -ForegroundColor Yellow
                $prompt = "Press Enter to install anyway, or type C to choose an AI anyway (custom installation):"
            }
            else {
                Write-Host "Bibliobrain will use the AI already installed. No other AI suits this computer." -ForegroundColor DarkCyan
                $prompt = "Press Enter to accept, or type C to choose yourself (custom installation):"
            }
            $answer = Read-LineWithTimeout -Prompt $prompt -TimeoutSeconds $ModelChoiceTimeoutSeconds
            if ($null -eq $answer) {
                # No answer, no download: reported below.
            }
            elseif ($answer -match "^(c|custom|p|perso)$") {
                $custom = $true
                $customDefault = $recommended
            }
            else {
                # Accepting the simple installation also accepts its search module, as in Setup.
                $script:SimpleInstallAccepted = $true
                $script:SelectedChatModel = $recommended
            }
        }

        if ($custom) {
            Show-ChatModelCatalog -Hardware $hardware -InstalledModels $installed
            $defaultText = if ($customDefault) { "Enter for $customDefault" } else { "Enter for none" }
            for ($attempt = 0; $attempt -lt 3; $attempt++) {
                $answer = Read-LineWithTimeout `
                    -Prompt "Chat model to download (1-$($ChatModelCatalog.Count), another Ollama model name, 0 for none, or $($defaultText)):" `
                    -TimeoutSeconds $ModelChoiceTimeoutSeconds
                if ($null -eq $answer) {
                    break
                }
                if ($answer -eq "") {
                    $script:SelectedChatModel = $customDefault
                    break
                }
                if ($answer -in @("0", "none", "n", "no", "non")) {
                    break
                }
                $number = 0
                if ([int]::TryParse($answer, [ref]$number)) {
                    if ($number -ge 1 -and $number -le $ChatModelCatalog.Count) {
                        $script:SelectedChatModel = $ChatModelCatalog[$number - 1].Model
                        break
                    }
                }
                elseif ($answer -match "^[A-Za-z0-9][A-Za-z0-9._/-]*(:[A-Za-z0-9._-]+)?$") {
                    $script:SelectedChatModel = $answer
                    Write-Host "$answer is not in the table: it has not been evaluated for Bibliobrain." -ForegroundColor Yellow
                    break
                }
                Write-Host "Unrecognised answer: $answer" -ForegroundColor Yellow
            }
        }

        if ($script:SelectedChatModel) {
            $entry = $ChatModelCatalog | Where-Object { $_.Model -eq $script:SelectedChatModel } | Select-Object -First 1
            if ($entry -and $hardware.TotalRamMiB -gt 0) {
                $fit = Get-ChatModelFit -Entry $entry -Hardware $hardware
                if ($fit -ne "GPU") {
                    Write-Host "Note: on this PC $($entry.Model) runs $fit." -ForegroundColor Yellow
                }
            }
            Write-Host "Chat model to download: $($script:SelectedChatModel)" -ForegroundColor Green
        }
        else {
            Write-Host "No chat model will be downloaded." -ForegroundColor DarkCyan
        }
    }

    if (Test-ModelNameIn -Model $EmbeddingModel -Names $installed) {
        $script:EmbeddingModelAlreadyInstalled = $true
        Write-Host "Embedding model already installed: $EmbeddingModel" -ForegroundColor DarkCyan
        return
    }
    $sizeText = ""
    if ($EmbeddingModel -eq "embeddinggemma") {
        $sizeText = " (about 0.6 GiB)"
    }
    Write-Host ""
    Write-Host "Bibliobrain also needs an embedding model to index your documents: $EmbeddingModel$sizeText." -ForegroundColor DarkCyan
    if ($EmbeddingDownload -eq "ask" -and $script:SimpleInstallAccepted) {
        $script:DownloadEmbeddingModel = $true
    }
    elseif ($EmbeddingDownload -eq "ask") {
        $answer = Read-LineWithTimeout -Prompt "Download $EmbeddingModel now? [Y/n]" -TimeoutSeconds $ModelChoiceTimeoutSeconds
        $script:DownloadEmbeddingModel = ($null -ne $answer -and $answer -notmatch "^(n|no|non)$")
    }
    else {
        $script:DownloadEmbeddingModel = ($EmbeddingDownload -eq "yes")
    }
    if (-not $script:DownloadEmbeddingModel) {
        Write-Host "$EmbeddingModel will not be downloaded." -ForegroundColor DarkCyan
    }
}

function Get-RequestedProfiles {
    param([string]$RequestedProfile)
    if ($RequestedProfile -ne "auto") {
        return @($RequestedProfile)
    }

    $gpu = Get-GpuSummary
    if ($gpu.Names.Count -gt 0) {
        Write-Host ("Detected GPU(s): " + ($gpu.Names -join " | ")) -ForegroundColor DarkCyan
    }
    else {
        Write-Host "GPU detection unavailable. Falling back to safe defaults." -ForegroundColor Yellow
    }

    if ($gpu.HasNvidia) {
        $driverMajor = 0
        if ([string]$gpu.NvidiaDriverVersion -match "^(\d+)") {
            $driverMajor = [int]$Matches[1]
        }
        $driverIsPreCuda13 = $driverMajor -gt 0 -and $driverMajor -lt $script:Cuda13MinimumDriverMajor
        $computeCap = [double]$gpu.MaxComputeCap
        if ($computeCap -gt 0 -and $computeCap -lt $script:Cuda12MinimumComputeCap) {
            Write-Host ("NVIDIA GPU of compute capability " + $computeCap + " is too old for PyTorch's CUDA builds: using the CPU build.") -ForegroundColor DarkCyan
            return @("cpu")
        }
        if ($driverIsPreCuda13 -and $gpu.HasBlackwell) {
            # Only CUDA 12.8+ builds know Blackwell, and PyTorch 2.14 ships them
            # as cu130 only, which this driver cannot load.
            Write-Host ("NVIDIA RTX 50 series GPU detected with driver " + $gpu.NvidiaDriverVersion + ".") -ForegroundColor Yellow
            Write-Host ("GPU acceleration for this card needs NVIDIA driver " + $script:Cuda13MinimumDriverMajor + " or newer.") -ForegroundColor Yellow
            Write-Host "Update it with the NVIDIA App or from nvidia.com, then run the Bibliobrain repair launcher to switch to the GPU." -ForegroundColor Yellow
            Write-Host "Installing the CPU build of PyTorch for now: Bibliobrain works, the reranker is just slower." -ForegroundColor Yellow
            return @("cpu")
        }
        if ($driverIsPreCuda13) {
            Write-Host ("NVIDIA driver " + $gpu.NvidiaDriverVersion + " predates CUDA 13: using the CUDA 12.6 build of PyTorch.") -ForegroundColor DarkCyan
            Write-Host ("Updating to driver " + $script:Cuda13MinimumDriverMajor + " or newer enables the CUDA 13 build.") -ForegroundColor DarkCyan
            return @("cu126", "cpu")
        }
        if ($computeCap -gt 0 -and $computeCap -lt $script:Cuda13MinimumComputeCap) {
            Write-Host ("NVIDIA GPU of compute capability " + $computeCap + " predates the CUDA 13 build: using the CUDA 12.6 build of PyTorch.") -ForegroundColor DarkCyan
            return @("cu126", "cpu")
        }
        Write-Host "NVIDIA GPU detected. Trying CUDA-enabled PyTorch profiles automatically." -ForegroundColor DarkCyan
        return @("cu130", "cu126", "cpu")
    }

    Write-Host "No NVIDIA GPU detected. Using CPU-only PyTorch." -ForegroundColor DarkCyan
    return @("cpu")
}

function Get-TorchValidationScript {
    return @'
import sys
import torch

expected_version = sys.argv[1]
expects_cuda = sys.argv[2] == "cuda"
version_ok = str(torch.__version__).split('+', 1)[0] == expected_version
cuda_build = bool(torch.version.cuda)
cuda_ready = bool(torch.cuda.is_available())
if cuda_build and cuda_ready:
    # is_available() stays true when the build lacks kernels for this GPU
    # (for example an RTX 50 card on a CUDA 12.6 build): run a real kernel.
    try:
        cuda_ready = float((torch.ones(8, device="cuda") * 2).sum().item()) == 16.0
    except Exception as exc:
        print(f"cuda_kernel_check_failed={exc}")
        cuda_ready = False
ok = version_ok and ((cuda_build and cuda_ready) if expects_cuda else not cuda_build)
print(f"torch={torch.__version__}; cuda_build={torch.version.cuda}; cuda_ready={cuda_ready}")
raise SystemExit(0 if ok else 1)
'@
}

function Test-ExistingTorchProfile {
    param(
        [string]$PythonExe,
        [string]$Profile
    )
    if ($DryRun -or -not (Test-Path $PythonExe)) {
        return $false
    }
    try {
        $spec = $TorchProfiles[$Profile]
        $expectedRuntime = if ($Profile -eq "cpu") { "cpu" } else { "cuda" }
        $script = Get-TorchValidationScript
        $pythonArgs = @(Get-PythonScriptArguments `
                -Script $script `
                -ScriptArguments @($spec.Version, $expectedRuntime))
        & $PythonExe @pythonArgs 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Test-TorchProfile {
    param(
        [string]$PythonExe,
        [string]$Profile
    )
    $spec = $TorchProfiles[$Profile]
    $expectedRuntime = if ($Profile -eq "cpu") { "cpu" } else { "cuda" }
    $pythonArgs = @(Get-PythonScriptArguments `
            -Script (Get-TorchValidationScript) `
            -ScriptArguments @($spec.Version, $expectedRuntime))
    Invoke-External `
        -FilePath $PythonExe `
        -Arguments $pythonArgs `
        -Label "Verify PyTorch profile $Profile"
}

function Test-AppStack {
    param([string]$PythonExe)
    $pythonArgs = @(Get-PythonScriptArguments `
            -Script "import importlib.metadata, sentence_transformers, sentry_sdk, torch; print('sentence-transformers=' + sentence_transformers.__version__); print('torch=' + torch.__version__); print('sentry-sdk=' + importlib.metadata.version('sentry-sdk'))")
    Invoke-External `
        -FilePath $PythonExe `
        -Arguments $pythonArgs `
        -Label "Verify Bibliobrain Python stack"
}

function Install-TorchProfile {
    param(
        [string]$PythonExe,
        [string]$Profile
    )
    if (-not $TorchProfiles.ContainsKey($Profile)) {
        throw "Unknown PyTorch profile: $Profile"
    }
    $spec = $TorchProfiles[$Profile]
    Invoke-External `
        -FilePath $PythonExe `
        -Arguments @(
            "-m", "pip", "install",
            "--upgrade",
            "--force-reinstall",
            "--verbose",
            "--progress-bar", "raw",
            ("torch==" + $spec.Version),
            "--index-url",
            $spec.IndexUrl
        ) `
        -Label "Install PyTorch profile $Profile" `
        -PipProgress `
        -OnPipInstall {
            Write-Step "Unpacking PyTorch profile $Profile" -Id torch_unpack
            Write-Host "PyTorch is downloaded; unpacking and antivirus checks can now take several minutes." -ForegroundColor Yellow
        }
}

function Warmup-RerankerModel {
    param([string]$PythonExe)

    $script = @"
import os

from src import config  # activates configuration.json and the DPAPI secret store

enabled = os.getenv("RERANKER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
if not enabled:
    print("Reranker disabled by RERANKER_ENABLED; skipping model download.")
    raise SystemExit(0)

if not os.getenv("HF_TOKEN", "").strip():
    print("HF_TOKEN is not set; Hugging Face may rate-limit or slow this download.")

from src.rag.reranker import _load_translation_model, get_reranker_status, warmup_reranker

before = get_reranker_status()
print(f"Downloading/preloading reranker: {before['model_name']} ({before['device']})")
after = warmup_reranker()
if not after.get("loaded"):
    raise RuntimeError(after.get("load_error") or "Reranker model failed to load")
print("Reranker model is cached and ready.")

if after.get("device") == "cpu" and after.get("translation_enabled"):
    print("Downloading/preloading local French-to-English query translator.")
    if _load_translation_model() is None:
        raise RuntimeError(get_reranker_status().get("translation_error") or "Translation model failed to load")
    print("Translation model is cached and ready.")
"@

    $pythonArgs = @(Get-PythonScriptArguments -Script $script)
    Invoke-External `
        -FilePath $PythonExe `
        -Arguments $pythonArgs `
        -Label "Preload reranker model"
}

function Find-OllamaExecutable {
    $command = Get-Command "ollama" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $candidates = @()
    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    }
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles "Ollama\ollama.exe"
    }
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    return ""
}

function Invoke-WebRequestDownload {
    param(
        [string]$Uri,
        [string]$DestinationPath,
        [int]$TimeoutSeconds,
        [string]$Label
    )

    $job = $null
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastHeartbeat = Get-Date
    try {
        $job = Start-Job -ArgumentList @($Uri, $DestinationPath, $TimeoutSeconds) -ScriptBlock {
            param($DownloadUri, $DownloadPath, $DownloadTimeoutSeconds)
            $ErrorActionPreference = "Stop"
            # Windows PowerShell 5.1 redraws its progress bar for every block
            # received, which slows a large download down several times over.
            $ProgressPreference = "SilentlyContinue"
            Invoke-WebRequest `
                -Uri $DownloadUri `
                -OutFile $DownloadPath `
                -UseBasicParsing `
                -TimeoutSec $DownloadTimeoutSeconds
        }

        while ($job.State -eq "Running") {
            Wait-Job -Job $job -Timeout 1 | Out-Null
            $now = Get-Date
            if (($now - $lastHeartbeat).TotalSeconds -ge 20) {
                Write-Host "$Label is still running; please keep this window open..." -ForegroundColor DarkGray
                $lastHeartbeat = $now
            }
            if ($now -ge $deadline) {
                Stop-Job -Job $job -ErrorAction SilentlyContinue
                throw "$Label did not finish within $DownloadTimeoutMinutes minutes."
            }
        }

        if ($job.State -eq "Failed") {
            $failure = $job.ChildJobs[0].JobStateInfo.Reason
            if ($failure) {
                throw "$Label failed: $($failure.Message)"
            }
            throw "$Label failed."
        }
        Receive-Job -Job $job -ErrorAction Stop | Out-Null
    }
    finally {
        if ($job) {
            if ($job.State -eq "Running") {
                Stop-Job -Job $job -ErrorAction SilentlyContinue
            }
            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        }
    }
}

function Save-InstallerDownload {
    param(
        [string]$Uri,
        [string]$DestinationPath,
        # How the file is named in the messages, for example "Ollama installer".
        [string]$Name
    )

    # Shared by every prerequisite: a dropped connection on a clean machine
    # must cost a retry, not the whole installation.
    Write-Host "Downloading the $Name. Progress will remain visible here." -ForegroundColor DarkCyan
    $downloadErrors = New-Object System.Collections.Generic.List[string]
    $curl = Get-Command "curl.exe" -ErrorAction SilentlyContinue
    if ($curl) {
        try {
            Remove-Item -LiteralPath $DestinationPath -Force -ErrorAction SilentlyContinue
            Invoke-External `
                -FilePath $curl.Source `
                -Arguments @(
                    "--fail",
                    "--location",
                    "--retry", [string]$DownloadRetryCount,
                    "--retry-delay", "5",
                    "--retry-all-errors",
                    "--retry-max-time", [string]($DownloadTimeoutMinutes * 60),
                    "--connect-timeout", "30",
                    "--speed-limit", "1024",
                    "--speed-time", "180",
                    "--max-time", [string]($DownloadTimeoutMinutes * 60),
                    "--output", $DestinationPath,
                    $Uri
                ) `
                -Label "$Name download"
            if (-not (Test-Path $DestinationPath) -or
                (Get-Item -LiteralPath $DestinationPath).Length -le 0) {
                throw "curl completed without creating a usable file."
            }
            Write-Host "$Name download completed." -ForegroundColor DarkCyan
            return
        }
        catch {
            $null = $downloadErrors.Add("curl.exe: $($_.Exception.Message)")
            Write-Host "The first download method failed; switching to the PowerShell fallback." -ForegroundColor Yellow
        }
    }
    else {
        $null = $downloadErrors.Add("curl.exe: command unavailable")
        Write-Host "curl.exe is unavailable; using the PowerShell downloader." -ForegroundColor Yellow
    }

    for ($attempt = 1; $attempt -le $DownloadRetryCount; $attempt++) {
        try {
            Remove-Item -LiteralPath $DestinationPath -Force -ErrorAction SilentlyContinue
            Write-Host "PowerShell download attempt $attempt/$DownloadRetryCount (timeout: $DownloadTimeoutMinutes minutes)..." -ForegroundColor DarkCyan
            Invoke-WebRequestDownload `
                -Uri $Uri `
                -DestinationPath $DestinationPath `
                -TimeoutSeconds ($DownloadTimeoutMinutes * 60) `
                -Label "PowerShell download attempt $attempt/$DownloadRetryCount"
            if (-not (Test-Path $DestinationPath) -or
                (Get-Item -LiteralPath $DestinationPath).Length -le 0) {
                throw "PowerShell completed without creating a usable file."
            }
            Write-Host "$Name download completed." -ForegroundColor DarkCyan
            return
        }
        catch {
            $null = $downloadErrors.Add("PowerShell attempt ${attempt}: $($_.Exception.Message)")
            Remove-Item -LiteralPath $DestinationPath -Force -ErrorAction SilentlyContinue
            if ($attempt -lt $DownloadRetryCount) {
                Write-Host "Download attempt $attempt failed. Retrying in 5 seconds..." -ForegroundColor Yellow
                Start-Sleep -Seconds 5
            }
        }
    }

    throw "Unable to download the $Name after trying curl.exe and $DownloadRetryCount PowerShell attempts. Check the Internet connection, then rerun the installer. Details: $($downloadErrors -join ' | ')"
}

function Install-OllamaFromOfficialInstaller {
    Write-Host "Using the official signed Ollama installer." -ForegroundColor Yellow
    if ($DryRun) {
        Write-Host "[dry-run] Download and install $OllamaInstallerUrl" -ForegroundColor DarkGray
        return
    }

    $installerPath = Join-Path `
        ([System.IO.Path]::GetTempPath()) `
        ("bibliobrain-ollama-" + [Guid]::NewGuid().ToString("N") + ".exe")
    try {
        Save-InstallerDownload -Uri $OllamaInstallerUrl -DestinationPath $installerPath -Name "Ollama installer"
        $signature = Get-AuthenticodeSignature -FilePath $installerPath
        if ($signature.Status -ne "Valid" -or
            $signature.SignerCertificate.Subject -notmatch "Ollama") {
            throw "Downloaded Ollama installer has an invalid publisher signature."
        }
        Write-Host "Installing Ollama. This normally takes less than a few minutes." -ForegroundColor DarkCyan
        $process = Start-Process `
            -FilePath $installerPath `
            -ArgumentList @("/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART") `
            -PassThru
        $installerDeadline = (Get-Date).AddMinutes($OllamaInstallerTimeoutMinutes)
        $nextHeartbeat = (Get-Date).AddSeconds(15)
        while (-not $process.HasExited) {
            Start-Sleep -Seconds 2
            $process.Refresh()
            if ($process.HasExited) {
                break
            }
            $now = Get-Date
            if ($now -ge $nextHeartbeat) {
                $elapsed = [int][math]::Floor(($now - $process.StartTime).TotalSeconds)
                Write-Host "Ollama installation is still running ($elapsed seconds elapsed)..." -ForegroundColor DarkCyan
                $nextHeartbeat = $now.AddSeconds(15)
            }
            if ($now -ge $installerDeadline) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
                throw "Ollama installer did not finish within $OllamaInstallerTimeoutMinutes minutes. It was stopped instead of waiting indefinitely. Restart Windows, then rerun Bibliobrain installation."
            }
        }
        $process.WaitForExit()
        if ($process.ExitCode -notin @(0, 3010)) {
            throw "Official Ollama installer failed with exit code $($process.ExitCode)."
        }
    }
    finally {
        if (Test-Path $installerPath) {
            Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Update-OllamaForModelCompatibility {
    param([string]$Model)

    if ($SkipPrerequisiteInstall) {
        throw "The installed Ollama version is too old for '$Model'. Update Ollama from https://ollama.com/download, then rerun the installer."
    }

    Write-Step "Updating Ollama for model compatibility" -Id ollama_update
    if ($DryRun) {
        Write-Host "[dry-run] Stop Ollama, install the latest signed release, and restart its API" -ForegroundColor DarkGray
        return
    }

    $ollamaProcesses = @(Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -like "ollama*" })
    foreach ($process in $ollamaProcesses) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $remainingProcesses = @(Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessName -like "ollama*" })
        if ($remainingProcesses.Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 250
    }
    $remainingProcesses = @(Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -like "ollama*" })
    if ($remainingProcesses.Count -gt 0) {
        throw "Ollama could not be stopped for its update. Quit Ollama from the notification area, then rerun the installer."
    }
    Remove-Item -LiteralPath $OllamaPidPath -Force -ErrorAction SilentlyContinue

    Install-OllamaFromOfficialInstaller
    $updatedOllamaExe = Find-OllamaExecutable
    if (-not $updatedOllamaExe) {
        throw "Ollama was updated but its executable could not be located. Restart Windows and rerun the installer."
    }
    $null = Ensure-Ollama
    Write-Host "Ollama was updated successfully. Retrying model download." -ForegroundColor DarkCyan
}

function Test-OllamaReady {
    $response = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:11434/api/version")
        $request.Method = "GET"
        $request.Timeout = 2000
        $request.ReadWriteTimeout = 2000
        $request.Proxy = $null
        $response = $request.GetResponse()
        return ([int]$response.StatusCode -eq 200)
    }
    catch {
        return $false
    }
    finally {
        if ($response) {
            $response.Close()
        }
    }
}

function Ensure-Ollama {
    $ollamaExe = Find-OllamaExecutable
    if (-not $ollamaExe) {
        if ($SkipPrerequisiteInstall) {
            throw "Ollama is required for local mode. Install it from https://ollama.com/ or omit -SkipPrerequisiteInstall."
        }
        Write-Step "Installing Ollama" -Id ollama_install
        $installedWithWinget = $false
        if (Get-Command "winget" -ErrorAction SilentlyContinue) {
            try {
                Invoke-External `
                    -FilePath "winget" `
                    -Arguments @(
                        "install", "--id", "Ollama.Ollama", "--exact",
                        "--accept-package-agreements", "--accept-source-agreements",
                        "--silent", "--disable-interactivity"
                    ) `
                    -Label "Install Ollama"
                $installedWithWinget = $true
            }
            catch {
                Write-Host "winget could not install Ollama: $($_.Exception.Message)" -ForegroundColor Yellow
            }
        }
        if (-not $installedWithWinget) {
            Install-OllamaFromOfficialInstaller
        }
        if ($DryRun) {
            return "ollama"
        }
        $ollamaExe = Find-OllamaExecutable
        if (-not $ollamaExe) {
            throw "Ollama was installed but its executable could not be located. Restart Windows and rerun the installer."
        }
    }

    if (-not (Test-OllamaReady)) {
        Write-Step "Starting Ollama" -Id ollama_start
        if ($DryRun) {
            Write-Host "[dry-run] Start Ollama service" -ForegroundColor DarkGray
            return $ollamaExe
        }

        $staleProcesses = @(Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessName -like "ollama*" })
        if ($staleProcesses.Count -gt 0) {
            Write-Host "Ollama processes are running but the API is unresponsive. Restarting Ollama..." -ForegroundColor Yellow
            foreach ($process in $staleProcesses) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
            Start-Sleep -Seconds 2
        }

        New-Item -ItemType Directory -Path $InstallStateDir -Force | Out-Null
        $serveStamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $serveOutLog = Join-Path $InstallStateDir "ollama-serve-$serveStamp.out.log"
        $serveErrorLog = Join-Path $InstallStateDir "ollama-serve-$serveStamp.err.log"
        $serveProcess = Start-Process `
            -FilePath $ollamaExe `
            -ArgumentList @("serve") `
            -WindowStyle Hidden `
            -RedirectStandardOutput $serveOutLog `
            -RedirectStandardError $serveErrorLog `
            -PassThru
        Set-Content -Path $OllamaPidPath -Value ([string]$serveProcess.Id) -Encoding ASCII
        $ready = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            Start-Sleep -Seconds 1
            if (Test-OllamaReady) {
                $ready = $true
                break
            }
            if ($serveProcess.HasExited) {
                break
            }
        }
        if (-not $ready) {
            Write-Host "Ollama server startup diagnostics:" -ForegroundColor Red
            foreach ($diagnosticPath in @($serveErrorLog, $serveOutLog)) {
                if (Test-Path $diagnosticPath) {
                    Write-Host "--- $diagnosticPath" -ForegroundColor DarkGray
                    Get-Content -LiteralPath $diagnosticPath -Tail 40 -ErrorAction SilentlyContinue |
                        ForEach-Object { Write-Host ([string]$_) -ForegroundColor Red }
                }
            }
            throw "Ollama's API did not become ready. Startup logs were saved under $InstallStateDir."
        }
        Write-Host "Ollama API is ready." -ForegroundColor DarkCyan
    }
    return $ollamaExe
}

function Get-OllamaModelsDirectory {
    # Same lookup as Ollama itself: OLLAMA_MODELS, else ~/.ollama/models. Setup
    # may run with an environment older than the user's latest setting.
    foreach ($candidate in @(
            $env:OLLAMA_MODELS,
            [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", "User"),
            [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", "Machine")
        )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    return (Join-Path $env:USERPROFILE ".ollama\models")
}

function Get-OllamaModelsOnDisk {
    # Ollama is often installed but not running (after a restart, or when it is
    # not started with Windows): its manifests still say what is downloaded.
    #   manifests\<registry>\<namespace>\<model>\<tag>
    $root = Join-Path (Get-OllamaModelsDirectory) "manifests"
    if (-not (Test-Path -LiteralPath $root)) {
        return @()
    }
    $models = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue)) {
        $parts = @($file.FullName.Substring($root.Length).Trim("\") -split "\\")
        if ($parts.Count -lt 4) {
            continue
        }
        $registry = $parts[0]
        $namespace = $parts[1]
        $model = ($parts[2..($parts.Count - 2)] -join "/")
        $tag = $parts[$parts.Count - 1]
        $name = "${model}:$tag"
        if ($registry -ne "registry.ollama.ai") {
            $name = "$registry/$namespace/$name"
        }
        elseif ($namespace -ne "library") {
            $name = "$namespace/$name"
        }
        $size = [double]0
        try {
            $manifest = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
            $size = [double](@($manifest.layers) | Measure-Object -Property size -Sum).Sum
        }
        catch {
            # A manifest being written: list the model without its size.
        }
        $models += [pscustomobject]@{ Name = $name; Size = $size }
    }
    return $models
}

function Get-OllamaInstalledModels {
    # Name and size in bytes of every model Ollama has, from its API when it is
    # running, else from its model folder.
    if ($DryRun) {
        return @()
    }
    $response = $null
    $reader = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:11434/api/tags")
        $request.Method = "GET"
        $request.Timeout = 5000
        $request.ReadWriteTimeout = 5000
        $request.Proxy = $null
        $response = $request.GetResponse()
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        $payload = $reader.ReadToEnd() | ConvertFrom-Json
        if (-not $payload) {
            return @()
        }
        return @($payload.models | ForEach-Object {
                [pscustomobject]@{ Name = ([string]$_.name).Trim(); Size = [double]$_.size }
            } | Where-Object { $_.Name })
    }
    catch {
        return @(Get-OllamaModelsOnDisk)
    }
    finally {
        if ($reader) {
            $reader.Close()
        }
        if ($response) {
            $response.Close()
        }
    }
}

function Get-OllamaInstalledModelNames {
    return @(Get-OllamaInstalledModels | ForEach-Object { $_.Name })
}

function Test-OllamaModelInstalled {
    param([string]$Model)
    return Test-ModelNameIn -Model $Model -Names @(Get-OllamaInstalledModelNames)
}

function Test-ModelNameIn {
    param(
        [string]$Model,
        [string[]]$Names
    )
    $wantedFull = $Model.Trim().ToLowerInvariant()
    $wantedBase = ($Model -split ":", 2)[0].Trim().ToLowerInvariant()
    $requiresExactTag = $Model.Contains(":")
    foreach ($installed in @($Names)) {
        $name = $installed.ToLowerInvariant()
        $matches = if ($requiresExactTag) {
            $name -eq $wantedFull
        }
        else {
            (($name -split ":", 2)[0]) -eq $wantedBase
        }
        if ($matches) {
            return $true
        }
    }
    return $false
}

function Get-OllamaChatModelNames {
    param([string[]]$Names)
    # Same name filter as _is_chat_model_candidate in src/llm_provider.py, so
    # the installer counts exactly the models the application offers for chat.
    return @($Names | Where-Object {
            $base = ($_ -split ":", 2)[0].ToLowerInvariant()
            $base -notmatch "embed|embedding|bge|e5|gte|minilm|nomic|mxbai" -and $base -notmatch "ocr"
        })
}

function Ensure-OllamaModel {
    param(
        [string]$OllamaExe,
        [string]$Model,
        [string]$Label
    )
    if (Test-OllamaModelInstalled -Model $Model) {
        Write-Host "$Label already installed: $Model" -ForegroundColor DarkCyan
        return
    }
    Write-Step "Downloading $Label ($Model)" -Id download -Detail $Model
    try {
        Invoke-OllamaModelPull -Model $Model -Label $Label
    }
    catch {
        $pullFailure = $_
        if ([string]$pullFailure.Exception.Message -notmatch "requires a newer version of Ollama") {
            throw
        }
        Write-Host "The installed Ollama version is too old for '$Model'." -ForegroundColor Yellow
        Update-OllamaForModelCompatibility -Model $Model
        Invoke-OllamaModelPull -Model $Model -Label $Label
    }
}

function Invoke-OllamaModelPull {
    param(
        [string]$Model,
        [string]$Label
    )
    if ($DryRun) {
        Write-Host "[dry-run] Ollama API pull $Model" -ForegroundColor DarkGray
        return
    }

    $request = $null
    $response = $null
    $reader = $null
    $lastStatus = ""
    $lastPercentBucket = -1
    $download = New-DownloadProgress -Label $Label
    try {
        $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:11434/api/pull")
        $request.Method = "POST"
        $request.ContentType = "application/json"
        $request.Timeout = 30000
        $request.ReadWriteTimeout = 300000
        $request.Proxy = $null
        $payload = @{ model = $Model; stream = $true } | ConvertTo-Json -Compress
        $payloadBytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
        $request.ContentLength = $payloadBytes.Length
        $requestStream = $request.GetRequestStream()
        try {
            $requestStream.Write($payloadBytes, 0, $payloadBytes.Length)
        }
        finally {
            $requestStream.Close()
        }

        $response = $request.GetResponse()
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        $completedSuccessfully = $false
        while (-not $reader.EndOfStream) {
            $line = $reader.ReadLine()
            if (-not $line) {
                continue
            }
            $event = $line | ConvertFrom-Json
            if ($event.error) {
                throw "Ollama rejected model '$Model': $($event.error)"
            }

            $status = ([string]$event.status).Trim()
            $percent = $null
            if ($event.total -and [double]$event.total -gt 0 -and $null -ne $event.completed) {
                $percent = [int][math]::Floor((100 * [double]$event.completed) / [double]$event.total)
                Update-DownloadProgress -Tracker $download -Done ([double]$event.completed) -Total ([double]$event.total)
            }
            $percentBucket = if ($null -eq $percent) { -1 } else { [int][math]::Floor($percent / 5) }
            if ($status -and ($status -ne $lastStatus -or $percentBucket -gt $lastPercentBucket)) {
                if ($null -ne $percent) {
                    Write-Host ("{0}: {1} ({2}%)" -f $Label, $status, $percent) -ForegroundColor DarkCyan
                }
                else {
                    Write-Host ("{0}: {1}" -f $Label, $status) -ForegroundColor DarkCyan
                }
                $lastStatus = $status
                $lastPercentBucket = $percentBucket
            }
            if ($status -eq "success") {
                $completedSuccessfully = $true
            }
        }
        if (-not $completedSuccessfully) {
            throw "Ollama ended the download of '$Model' without reporting success."
        }
    }
    catch [System.Net.WebException] {
        $details = $_.Exception.Message
        if ($_.Exception.Response) {
            try {
                $errorReader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $errorBody = $errorReader.ReadToEnd()
                $errorReader.Close()
                if ($errorBody) {
                    $details = "$details Response: $errorBody"
                }
            }
            catch {
            }
        }
        throw "Ollama API could not download '$Model'. $details"
    }
    finally {
        if ($reader) {
            $reader.Close()
        }
        if ($response) {
            $response.Close()
        }
    }
}

function Save-InstallState {
    param([string]$SelectedTorchProfile)
    if ($DryRun) {
        return
    }
    New-Item -ItemType Directory -Path $InstallStateDir -Force | Out-Null
    $payload = [ordered]@{
        installed_at = (Get-Date).ToString("o")
        torch_profile = $SelectedTorchProfile
        embedding_model = $EmbeddingModel
        chat_model = $SelectedChatModel
        port = 7860
    }
    $payload | ConvertTo-Json | Set-Content -Path $InstallStatePath -Encoding UTF8
}

function Stop-InstallLogging {
    if ($script:InstallTranscriptStarted) {
        try {
            Stop-Transcript | Out-Null
        }
        catch {
        }
        $script:InstallTranscriptStarted = $false
    }
    # After the transcript: the next run may then start its own log.
    if ($script:InstallLock) {
        try {
            $script:InstallLock.ReleaseMutex()
            $script:InstallLock.Dispose()
        }
        catch {
        }
        $script:InstallLock = $null
    }
}

function Write-ModelProbe {
    param([string]$Path)
    # Line-oriented on purpose: Setup's script language has no JSON parser.
    #   hardware|<RAM GiB>|<CPU threads>|<VRAM GiB, 0 without a usable GPU>
    #   ollama|<installed 0/1>|<chat models already in Ollama>
    #   model|<name>|<download, e.g. 6.5 GB>|<status>|<gpu|gpucpu|cpu|toobig>|<recommended 0/1>|<installed 0/1>
    #   installed|<name>|<size, e.g. 5.2 GB>   (chat models outside the catalog)
    #   embedding|<name>|<installed 0/1>
    $installedModels = @(Get-OllamaInstalledModels)
    $installed = @($installedModels | ForEach-Object { $_.Name })
    $hardware = Get-HardwareSummary
    $recommended = Get-RecommendedChatModel -Hardware $hardware
    $vramGiB = 0
    if (Test-OllamaUsableGpu -Hardware $hardware) {
        $vramGiB = [math]::Round($hardware.MaxVramMiB / 1024, 1)
    }
    $ramGiB = [math]::Round($hardware.TotalRamMiB / 1024, 0)
    $lines = @(
        ("hardware|{0}|{1}|{2}" -f $ramGiB.ToString([cultureinfo]::InvariantCulture),
            $hardware.LogicalProcessorCount, $vramGiB.ToString([cultureinfo]::InvariantCulture))
    )
    foreach ($entry in $ChatModelCatalog) {
        $fitText = Get-ChatModelFit -Entry $entry -Hardware $hardware
        $fitCode = switch ($fitText) {
            "GPU" { "gpu" }
            "GPU+CPU, slow" { "gpucpu" }
            "CPU only, slow" { "cpu" }
            default { "toobig" }
        }
        # The wizard speaks GB, the unit people know; the catalog keeps GiB.
        $downloadGiB = [double]::Parse(($entry.Download -replace "\s*GiB$", ""), [cultureinfo]::InvariantCulture)
        $downloadText = "{0} GB" -f ([math]::Round($downloadGiB * 1GB / 1e9, 1)).ToString([cultureinfo]::InvariantCulture)
        $lines += ("model|{0}|{1}|{2}|{3}|{4}|{5}" -f $entry.Model, $downloadText, $entry.Status,
            $fitCode, [int]($entry.Model -eq $recommended),
            [int](Test-ModelNameIn -Model $entry.Model -Names $installed))
    }
    # Models already downloaded can be chosen as they are: nothing to download.
    $chatNames = @(
        Get-OllamaChatModelNames -Names $installed | Where-Object { $_ -notmatch "^llamacpp:" }
    )
    $lines += ("ollama|{0}|{1}" -f [int][bool](Find-OllamaExecutable), $chatNames.Count)
    foreach ($model in @($installedModels | Sort-Object Name)) {
        if ($chatNames -notcontains $model.Name) {
            continue
        }
        if ($ChatModelCatalog | Where-Object { Test-ModelNameIn -Model $_.Model -Names @($model.Name) }) {
            continue
        }
        $lines += ("installed|{0}|{1} GB" -f $model.Name,
            ([math]::Round($model.Size / 1e9, 1)).ToString([cultureinfo]::InvariantCulture))
    }
    $lines += ("embedding|{0}|{1}" -f $EmbeddingModel,
        [int](Test-ModelNameIn -Model $EmbeddingModel -Names $installed))
    Set-Content -LiteralPath $Path -Value $lines -Encoding ASCII
}

if ($ProbeModelsFile) {
    try {
        Write-ModelProbe -Path $ProbeModelsFile
    }
    catch {
        # No probe file: Setup falls back to a plain "none / choose later" page.
        exit 1
    }
    exit 0
}

function Remove-DownloadMark {
    # Explorer passes the "downloaded from the Internet" mark (Zone.Identifier)
    # of a ZIP package to every file it extracts, and Windows then warns before
    # each launcher. Only Bibliobrain's own files: .venv is large and ours.
    $skipped = @(".venv", ".git")
    Get-ChildItem -LiteralPath $RepoRoot -Force -ErrorAction SilentlyContinue |
        Where-Object { $skipped -notcontains $_.Name } |
        ForEach-Object {
            if ($_.PSIsContainer) {
                Get-ChildItem -LiteralPath $_.FullName -Recurse -File -Force -ErrorAction SilentlyContinue
            }
            else {
                $_
            }
        } |
        Unblock-File -ErrorAction SilentlyContinue
}

function Get-InstallLockName {
    # One lock per installation folder: two folders install independently.
    $folder = ([System.IO.Path]::GetFullPath($RepoRoot)).TrimEnd('\').ToLowerInvariant()
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($folder))
    }
    finally {
        $sha.Dispose()
    }
    return "Local\Bibliobrain-Install-" + (-join ($hash[0..7] | ForEach-Object { $_.ToString("x2") }))
}

# Two runs on the same folder would have two pips rewriting the same .venv: a
# Setup killed from the Task Manager leaves its hidden install.ps1 running, and
# relaunching Setup would start a second one. The second one stops before it
# changes anything, the log of the first one included. Windows releases the
# lock when the process ends, however it ends.
$InstallBusyExitCode = 3
$InstallLock = New-Object System.Threading.Mutex($false, (Get-InstallLockName))
$installLockAcquired = $false
try {
    $installLockAcquired = $InstallLock.WaitOne(0)
}
catch [System.Threading.AbandonedMutexException] {
    # Its previous owner died without releasing it: the lock is ours now.
    $installLockAcquired = $true
}
if (-not $installLockAcquired) {
    $InstallLock.Dispose()
    $InstallLock = $null
    Write-Step "Another Bibliobrain installation is already running" -Id busy
    Write-Host "Another Bibliobrain installation is still running in $RepoRoot." -ForegroundColor Red
    Write-Host "Wait for it to finish (its progress is in $InstallLogPath), or restart the computer, then run the installation again." -ForegroundColor Yellow
    exit $InstallBusyExitCode
}

trap {
    $failure = $_
    $ErrorActionPreference = "Continue"
    Write-Error -ErrorRecord $failure
    Write-Host ""
    Write-Host "Installation failed. Full log: $InstallLogPath" -ForegroundColor Red
    Stop-InstallLogging
    exit 1
}

Start-Transcript -Path $InstallLogPath -Force | Out-Null
$InstallTranscriptStarted = $true
Write-Host "Installation log: $InstallLogPath" -ForegroundColor DarkCyan

if (-not $DryRun) {
    Remove-DownloadMark
}
Set-Location $RepoRoot

Select-LocalModels

Write-Step "Preparing Bibliobrain installer" -Id prepare
Assert-FreeDiskSpace
$pythonBootstrap = Resolve-PythonBootstrap
Ensure-VisualCppRuntime

if (-not (Test-Path $VenvPython)) {
    Write-Step "Creating virtual environment" -Id venv
    $venvArgs = @($pythonBootstrap.PrefixArgs + @("-m", "venv", ".venv"))
    Invoke-External -FilePath $pythonBootstrap.FilePath -Arguments $venvArgs -Label "Create virtual environment"
}
else {
    Write-Host "Existing virtual environment found at .venv" -ForegroundColor DarkCyan
}

Write-Step "Upgrading pip" -Id venv
Invoke-External -FilePath $VenvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip") -Label "Upgrade pip"

$selectedProfile = $null
$lastProfileError = $null
$profiles = @()

if ($TorchProfile -eq "auto") {
    foreach ($installedProfile in @("cu130", "cu126", "cpu")) {
        # A failed installation may leave the CPU wheel behind after exhausting
        # CUDA fallbacks. Do not let it short-circuit automatic GPU selection
        # unless a previous installation reached Save-InstallState successfully.
        if ($installedProfile -eq "cpu" -and -not (Test-Path $InstallStatePath)) {
            continue
        }
        if (Test-ExistingTorchProfile -PythonExe $VenvPython -Profile $installedProfile) {
            $selectedProfile = $installedProfile
            Write-Host "Keeping working PyTorch profile: $selectedProfile" -ForegroundColor DarkCyan
            break
        }
    }
}
if (-not $selectedProfile) {
    $profiles = Get-RequestedProfiles -RequestedProfile $TorchProfile
}

foreach ($profile in $profiles) {
    try {
        if (Test-ExistingTorchProfile -PythonExe $VenvPython -Profile $profile) {
            Write-Host "Compatible PyTorch profile already installed: $profile" -ForegroundColor DarkCyan
        }
        else {
            Write-Step "Installing PyTorch profile $profile" -Id torch
            Install-TorchProfile -PythonExe $VenvPython -Profile $profile
        }

        Write-Step "Verifying PyTorch profile $profile" -Id torch_check
        Test-TorchProfile -PythonExe $VenvPython -Profile $profile
        $selectedProfile = $profile
        break
    }
    catch {
        $lastProfileError = $_
        Write-Host "Profile $profile failed. Trying next fallback..." -ForegroundColor Yellow
    }
}

if (-not $selectedProfile) {
    throw "Unable to install a working PyTorch profile. Last error: $lastProfileError"
}

Write-Step "Installing Bibliobrain dependencies" -Id deps
Write-Host "This is the longest Python step and can take 5 to 15 minutes on Windows." -ForegroundColor Yellow
Write-Host "Pip can spend several minutes copying files after 'Installing collected packages'. Do not close this window." -ForegroundColor Yellow
if (-not $DryRun) {
    New-Item -ItemType Directory -Path $InstallStateDir -Force | Out-Null
    if (Test-Path $DependencyInstallMarker) {
        Write-Host "A previous dependency installation was interrupted. Resuming it safely..." -ForegroundColor Yellow
    }
    $dependencyAttempt = [ordered]@{
        started_at = (Get-Date).ToString("o")
        phase = "pip-install-requirements"
    }
    $dependencyAttempt | ConvertTo-Json | Set-Content -Path $DependencyInstallMarker -Encoding UTF8
}
Invoke-External `
    -FilePath $VenvPython `
    -Arguments @(
        "-m", "pip", "install",
        "--no-deps",
        "--require-hashes",
        "--verbose",
        "--progress-bar", "raw",
        "-r", "requirements.lock"
    ) `
    -Label "Install Bibliobrain dependencies" `
    -PipProgress
if (-not $DryRun -and (Test-Path $DependencyInstallMarker)) {
    Remove-Item -LiteralPath $DependencyInstallMarker -Force
}

Write-Step "Verifying Bibliobrain dependencies" -Id deps_check
Test-AppStack -PythonExe $VenvPython

if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA is unavailable; the per-user configuration cannot be created."
}
$configurationDir = Join-Path $env:LOCALAPPDATA "bibliobrain"
$configurationTarget = Join-Path $configurationDir "configuration.json"
$legacyMigrationMarker = Join-Path $configurationDir "legacy-env-migrated-v1.json"
$legacyEnvPath = Join-Path $RepoRoot ".env"
$legacyMigrationScheduled = (
    (Test-Path -LiteralPath $legacyEnvPath -PathType Leaf) -and
    -not (Test-Path -LiteralPath $legacyMigrationMarker -PathType Leaf)
)
if ($legacyMigrationScheduled) {
    Write-Step "Migrating the legacy .env into the JSON configuration and encrypted store" -Id config
    if ($DryRun) {
        Write-Host "[dry-run] Import $legacyEnvPath once without deleting it" -ForegroundColor DarkGray
    }
    else {
        Invoke-External `
            -FilePath $VenvPython `
            -Arguments @((Join-Path $RepoRoot "scripts\migrate-env-config.py"), "--env-file", $legacyEnvPath) `
            -Label "Migrate legacy Bibliobrain configuration"
        $migrationRecord = @{
            version = 1
            migrated_at = [DateTimeOffset]::UtcNow.ToString("o")
        } | ConvertTo-Json
        Set-Content -LiteralPath $legacyMigrationMarker -Value $migrationRecord -Encoding UTF8
        Protect-ConfigurationFileAcl -Path $legacyMigrationMarker
    }
}
elseif (Test-Path -LiteralPath $legacyEnvPath -PathType Leaf) {
    Write-Warning "The legacy .env has already been migrated and is ignored. Remove it after verifying Bibliobrain starts correctly."
}

# On a legacy installation, migrate before installing the defaults. Otherwise
# fresh template values could be mistaken for choices already made by the user.
if (-not $legacyMigrationScheduled -and
    -not (Test-Path -LiteralPath $configurationTarget -PathType Leaf)) {
    Write-Step "Creating the per-user configuration" -Id config
    if ($DryRun) {
        Write-Host "[dry-run] Copy configuration.example.json to $configurationTarget" -ForegroundColor DarkGray
    }
    else {
        New-Item -ItemType Directory -Path $configurationDir -Force | Out-Null
        Copy-Item (Join-Path $RepoRoot "configuration.example.json") $configurationTarget
    }
}
Protect-ConfigurationFileAcl -Path $configurationTarget

$ollamaExe = ""
if ($SkipOllamaSetup) {
    Write-Host "Skipping Ollama setup (-SkipOllamaSetup)." -ForegroundColor Yellow
}
else {
    $ollamaExe = Ensure-Ollama
    if ($SkipModelDownloads) {
        Write-Host "Skipping Ollama model downloads (-SkipModelDownloads)." -ForegroundColor Yellow
    }
    else {
        if ($DownloadEmbeddingModel) {
            Ensure-OllamaModel -OllamaExe $ollamaExe -Model $EmbeddingModel -Label "embedding model"
        }
        if ($SelectedChatModel) {
            Ensure-OllamaModel -OllamaExe $ollamaExe -Model $SelectedChatModel -Label "chat model"
        }
    }
}

if ($SkipRerankerWarmup) {
    Write-Host "Skipping reranker preload (-SkipRerankerWarmup)." -ForegroundColor Yellow
}
else {
    Write-Step "Preloading the fast reranker" -Id reranker
    Write-Host "The larger deep-search reranker remains optional and will only download when requested." -ForegroundColor DarkCyan
    try {
        Warmup-RerankerModel -PythonExe $VenvPython
    }
    catch {
        Write-Host "Reranker preload failed. Bibliobrain will retry at startup." -ForegroundColor Yellow
        Write-Host $_ -ForegroundColor Yellow
    }
}

Write-Step "Creating launchers and shortcuts" -Id finish

$icoPath = Join-Path $RepoRoot "static\logo_bibliobrain.ico"
$lnkPath = Join-Path $RepoRoot "Bibliobrain.lnk"
$managerRelativePath = "scripts\bibliobrain-manager.ps1"
$managerFullPath = Join-Path $RepoRoot $managerRelativePath
$launcherActions = [ordered]@{
    "run.bat" = "start"
    "Stop-Bibliobrain.cmd" = "stop"
    "Repair-Bibliobrain.cmd" = "repair"
    "Update-Bibliobrain.cmd" = "update"
    "Backup-Bibliobrain.cmd" = "backup"
    "Restore-Bibliobrain.cmd" = "restore"
    "Uninstall-Bibliobrain.cmd" = "uninstall"
}

if ($DryRun) {
    Write-Host "[dry-run] Create launchers and Bibliobrain shortcuts" -ForegroundColor DarkGray
}
else {
    foreach ($entry in $launcherActions.GetEnumerator()) {
        $launcherContent = @"
@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0$managerRelativePath" -Action $($entry.Value)
if errorlevel 1 pause
"@
        Set-Content -Path (Join-Path $RepoRoot $entry.Key) -Value $launcherContent -Encoding ASCII
    }
    Write-Host "Start, stop, repair, update, backup, restore and uninstall launchers created." -ForegroundColor DarkCyan

    $shell = New-Object -ComObject WScript.Shell
    $shortcutPaths = @($lnkPath)
    try {
        $desktopPath = [Environment]::GetFolderPath("Desktop")
        if ($desktopPath) {
            $shortcutPaths += Join-Path $desktopPath "Bibliobrain.lnk"
        }
    }
    catch {
    }
    if ($SkipShortcuts) {
        # Setup owns the shortcuts (and removes them at uninstall).
        $shortcutPaths = @()
    }
    foreach ($shortcutPath in ($shortcutPaths | Select-Object -Unique)) {
        $shortcut = $shell.CreateShortcut($shortcutPath)
        # Launch PowerShell itself in hidden mode instead of targeting the
        # .bat wrapper, which necessarily creates a visible cmd.exe window.
        $shortcut.TargetPath = (Get-Command "powershell.exe").Source
        # -ErrorDialog: with no console, a failed start would otherwise be silent.
        $shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$managerFullPath`" -Action start -ErrorDialog"
        $shortcut.IconLocation = "$icoPath,0"
        $shortcut.WorkingDirectory = $RepoRoot
        $shortcut.Save()
    }
    Write-Host "Bibliobrain shortcut created in the project and on the Desktop." -ForegroundColor DarkCyan
}

Save-InstallState -SelectedTorchProfile $selectedProfile

Write-Step "Installation complete" -Id done
Write-Host "Selected PyTorch profile: $selectedProfile" -ForegroundColor Green
if ($SkipOllamaSetup -or $SkipModelDownloads) {
    Write-Host "Ollama models: left unchanged." -ForegroundColor DarkCyan
}
else {
    if ($DownloadEmbeddingModel -or $EmbeddingModelAlreadyInstalled) {
        Write-Host "Embedding model: $EmbeddingModel" -ForegroundColor Green
    }
    else {
        Write-Host "Embedding model: not downloaded. Before indexing documents, run: ollama pull $EmbeddingModel" -ForegroundColor Yellow
    }
    if ($SelectedChatModel) {
        Write-Host "Chat model: $SelectedChatModel" -ForegroundColor Green
    }
    else {
        Write-Host "Chat model: none downloaded. To add one later, run: ollama pull <model> (see the model table in README.md), then choose it in Settings." -ForegroundColor Yellow
    }
}
Write-Host "Use the Bibliobrain shortcut on your Desktop to start the application." -ForegroundColor Green
Write-Host "Repair, update, stop and uninstall launchers are available in this folder."
Write-Host ""
Write-Host "If the automatic choice is not ideal, rerun with -TorchProfile cpu|cu126|cu130." -ForegroundColor DarkCyan

if (-not $NoLaunch -and -not $DryRun) {
    Write-Step "Starting Bibliobrain"
    # -ErrorDialog: this window closes with the installation, so the reason of
    # a failed start (for example a busy port 7860) has to stay on screen.
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot $managerRelativePath) -Action start -ErrorDialog
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Bibliobrain is installed but did not start. Use the Desktop shortcut once the problem above is solved."
    }
}

Write-Host "Installation log saved to: $InstallLogPath" -ForegroundColor DarkCyan
Stop-InstallLogging
