[CmdletBinding()]
param(
    [string]$IsccPath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
$InnoScript = Join-Path $PSScriptRoot "Bibliobrain.iss"
$Version = (Get-Content -LiteralPath (Join-Path $RepoRoot "VERSION") -Raw).Trim()
$Artifact = Join-Path $RepoRoot "dist\Bibliobrain-Setup-Smoke.exe"

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
    throw "Inno Setup 6 (ISCC.exe) is required for the installer payload smoke test."
}

$systemTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd("\")
$evidenceRoot = Join-Path $systemTempRoot ("bibliobrain-installer-smoke-" + [guid]::NewGuid().ToString("N"))
$resolvedEvidenceRoot = [System.IO.Path]::GetFullPath($evidenceRoot)
$expectedPrefix = $systemTempRoot + [System.IO.Path]::DirectorySeparatorChar
if (-not $resolvedEvidenceRoot.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use a smoke-test directory outside the system temporary directory: $resolvedEvidenceRoot"
}

$installRoot = Join-Path $resolvedEvidenceRoot "installed"
$installLog = Join-Path $resolvedEvidenceRoot "install.log"
$uninstallLog = Join-Path $resolvedEvidenceRoot "uninstall.log"
$passed = $false
$result = $null

try {
    New-Item -ItemType Directory -Path $resolvedEvidenceRoot | Out-Null

    $compileArguments = @(
        "/DMyAppVersion=$Version"
        "/DSourceRoot=$RepoRoot"
        "/DPackagingSmoke=1"
        $InnoScript
    )
    & $IsccPath @compileArguments
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Artifact)) {
        throw "Packaging smoke installer compilation failed."
    }

    $setupArguments = @(
        "/VERYSILENT"
        "/SUPPRESSMSGBOXES"
        "/NORESTART"
        "/CURRENTUSER"
        "/DIR=$installRoot"
        "/LOG=$installLog"
    )
    $setupOptions = @{
        FilePath = $Artifact
        ArgumentList = $setupArguments
        Wait = $true
        PassThru = $true
        WindowStyle = "Hidden"
    }
    $setup = Start-Process @setupOptions
    if ($setup.ExitCode -ne 0) {
        throw "Silent smoke installation failed with exit code $($setup.ExitCode)."
    }

    $requiredPayload = @(
        "app_api.py"
        "VERSION"
        "install.ps1"
        "LICENSE"
        "THIRD_PARTY_NOTICES.md"
        "THIRD_PARTY_LICENSES\Inter-OFL-1.1.txt"
        "THIRD_PARTY_LICENSES\JetBrainsMono-OFL-1.1.txt"
        "src\version.py"
        "wiki\Home.md"
        "static\index.html"
        "scripts\bibliobrain-manager.ps1"
    )
    $missingPayload = @(
        $requiredPayload | Where-Object {
            -not (Test-Path -LiteralPath (Join-Path $installRoot $_) -PathType Leaf)
        }
    )
    if ($missingPayload.Count -gt 0) {
        throw "Installed payload is missing required files: $($missingPayload -join ', ')"
    }

    $forbiddenPayload = @(
        "docs"
        "BETA_TESTER_GUIDE.md"
        ".git"
        "tests"
        "data"
        "storage"
        "bibliobrain-install.log"
    )
    $unexpectedPayload = @(
        $forbiddenPayload | Where-Object {
            Test-Path -LiteralPath (Join-Path $installRoot $_)
        }
    )
    if ($unexpectedPayload.Count -gt 0) {
        throw "Installed payload contains forbidden development or user-data paths: $($unexpectedPayload -join ', ')"
    }

    $installedVersion = (Get-Content -LiteralPath (Join-Path $installRoot "VERSION") -Raw).Trim()
    if ($installedVersion -ne $Version) {
        throw "Installed VERSION '$installedVersion' does not match expected version '$Version'."
    }

    $repairTarget = Join-Path $installRoot "app_api.py"
    $expectedRepairHash = (Get-FileHash -LiteralPath (Join-Path $RepoRoot "app_api.py") -Algorithm SHA256).Hash
    Set-Content -LiteralPath $repairTarget -Value "# intentionally damaged by the packaging smoke test"
    $repairSetup = Start-Process @setupOptions
    if ($repairSetup.ExitCode -ne 0) {
        throw "Silent smoke repair failed with exit code $($repairSetup.ExitCode)."
    }
    $repairedHash = (Get-FileHash -LiteralPath $repairTarget -Algorithm SHA256).Hash
    if ($repairedHash -ne $expectedRepairHash) {
        throw "Reinstalling the package did not restore the damaged application file."
    }

    # A prerelease suffix (0.8.1-beta.1) is not valid for [version]: keep the numeric core.
    $parsedVersion = [version]($Version -replace '[-+].*$', '')
    $nextBuild = [math]::Max(0, $parsedVersion.Build) + 1
    $nextVersion = [version]::new($parsedVersion.Major, $parsedVersion.Minor, $nextBuild).ToString()
    $nextVersionFile = Join-Path $resolvedEvidenceRoot "VERSION.next"
    Set-Content -LiteralPath $nextVersionFile -Value $nextVersion -Encoding ASCII -NoNewline
    $upgradeCompileArguments = @(
        "/DMyAppVersion=$nextVersion"
        "/DPayloadVersionFile=$nextVersionFile"
        "/DSourceRoot=$RepoRoot"
        "/DPackagingSmoke=1"
        $InnoScript
    )
    & $IsccPath @upgradeCompileArguments
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Artifact)) {
        throw "Packaging smoke upgrade installer compilation failed."
    }
    $upgradeSetup = Start-Process @setupOptions
    if ($upgradeSetup.ExitCode -ne 0) {
        throw "Silent package upgrade failed with exit code $($upgradeSetup.ExitCode)."
    }
    $upgradedVersion = (Get-Content -LiteralPath (Join-Path $installRoot "VERSION") -Raw).Trim()
    if ($upgradedVersion -ne $nextVersion) {
        throw "Package upgrade left VERSION '$upgradedVersion' instead of '$nextVersion'."
    }

    # The real install.ps1 downloads gigabytes, so a stub stands in for it: what
    # is checked here is how Setup starts it and what Setup does with its result.
    $stubScript = Join-Path $resolvedEvidenceRoot "install-stub.ps1"
    $stubRecord = Join-Path $resolvedEvidenceRoot "install-stub-arguments.txt"
    Set-Content -LiteralPath $stubScript -Encoding ASCII -Value @'
Set-Content -LiteralPath $env:BIBLIOBRAIN_SMOKE_RECORD -Value ("arguments:" + ($args -join " ")) -Encoding ASCII
# Setup shows this step name and keeps its window responsive while the script runs.
$progressIndex = [array]::IndexOf($args, "-ProgressFile")
if ($progressIndex -ge 0) {
    Set-Content -LiteralPath $args[$progressIndex + 1] -Value "Stub step" -Encoding ASCII
}
Start-Sleep -Seconds 1
exit [int]$env:BIBLIOBRAIN_SMOKE_EXIT_CODE
'@
    $scriptCompileArguments = @(
        "/DMyAppVersion=$nextVersion"
        "/DPayloadVersionFile=$nextVersionFile"
        "/DSourceRoot=$RepoRoot"
        "/DPackagingSmoke=1"
        "/DSmokeInstallScript=$stubScript"
        $InnoScript
    )
    & $IsccPath @scriptCompileArguments
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Artifact)) {
        throw "Packaging smoke installer with the install script stub failed to compile."
    }
    $updateModeOptions = $setupOptions.Clone()
    $updateModeOptions.ArgumentList = $setupArguments + @("/UPDATEMODE=1")
    $env:BIBLIOBRAIN_SMOKE_RECORD = $stubRecord
    try {
        $env:BIBLIOBRAIN_SMOKE_EXIT_CODE = "0"
        $scriptSetup = Start-Process @setupOptions
        if ($scriptSetup.ExitCode -ne 0) {
            throw "Setup returned $($scriptSetup.ExitCode) although the install script succeeded."
        }
        $recordedArguments = (Get-Content -LiteralPath $stubRecord -Raw).Trim()
        # Nothing may prompt: install.ps1 runs without a console, so Setup answers
        # for it. A silent installation downloads no model unless it is asked to.
        if ($recordedArguments -notmatch "^arguments:-ProgressFile \S+ -SkipShortcuts -NoLaunch -ChatModel none -EmbeddingDownload no$") {
            throw "A first silent installation must answer every question of install.ps1, got '$recordedArguments'."
        }

        $modelOptions = $setupOptions.Clone()
        $modelOptions.ArgumentList = $setupArguments + @("/CHATMODEL=qwen3.5:9b", "/EMBEDDING=1")
        $scriptSetup = Start-Process @modelOptions
        if ($scriptSetup.ExitCode -ne 0) {
            throw "Setup returned $($scriptSetup.ExitCode) with explicit model choices although the install script succeeded."
        }
        $recordedArguments = (Get-Content -LiteralPath $stubRecord -Raw).Trim()
        if ($recordedArguments -notmatch "-ChatModel qwen3\.5:9b -EmbeddingDownload yes$") {
            throw "/CHATMODEL and /EMBEDDING must reach install.ps1, got '$recordedArguments'."
        }

        $scriptSetup = Start-Process @updateModeOptions
        if ($scriptSetup.ExitCode -ne 0) {
            throw "Setup returned $($scriptSetup.ExitCode) in update mode although the install script succeeded."
        }
        $recordedArguments = (Get-Content -LiteralPath $stubRecord -Raw).Trim()
        if ($recordedArguments -notmatch "^arguments:-ProgressFile \S+ -SkipShortcuts -NoLaunch -SkipOllamaSetup$") {
            throw "The in-app update must not launch a browser tab nor ask about models, got '$recordedArguments'."
        }

        $env:BIBLIOBRAIN_SMOKE_EXIT_CODE = "1"
        $scriptSetup = Start-Process @updateModeOptions
        if ($scriptSetup.ExitCode -ne 20) {
            throw "Setup returned $($scriptSetup.ExitCode) instead of 20 although the install script failed."
        }
    }
    finally {
        Remove-Item Env:\BIBLIOBRAIN_SMOKE_RECORD, Env:\BIBLIOBRAIN_SMOKE_EXIT_CODE -ErrorAction SilentlyContinue
    }

    # What a real installation leaves behind and the uninstaller must still remove.
    $bytecodeFolder = Join-Path $installRoot "src\__pycache__"
    New-Item -ItemType Directory -Path $bytecodeFolder -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $bytecodeFolder "version.cpython-314.pyc") -Value "smoke"
    Set-Content -LiteralPath (Join-Path $installRoot "bibliobrain-install.log") -Value "smoke"

    $uninstaller = Join-Path $installRoot "unins000.exe"
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
        throw "The smoke installation did not create an uninstaller."
    }
    $payloadFileCount = @(Get-ChildItem -LiteralPath $installRoot -Recurse -File).Count
    $artifactInfo = Get-Item -LiteralPath $Artifact
    $artifactHash = (Get-FileHash -LiteralPath $Artifact -Algorithm SHA256).Hash

    $uninstallArguments = @(
        "/VERYSILENT"
        "/SUPPRESSMSGBOXES"
        "/NORESTART"
        "/LOG=$uninstallLog"
    )
    $uninstallOptions = @{
        FilePath = $uninstaller
        ArgumentList = $uninstallArguments
        Wait = $true
        PassThru = $true
        WindowStyle = "Hidden"
    }
    $uninstall = Start-Process @uninstallOptions
    if ($uninstall.ExitCode -ne 0) {
        throw "Silent smoke uninstallation failed with exit code $($uninstall.ExitCode)."
    }
    if (Test-Path -LiteralPath $installRoot) {
        throw "Smoke uninstallation left the installation directory behind: $installRoot"
    }

    $result = [ordered]@{
        Installer = $artifactInfo.FullName
        Version = $Version
        Bytes = $artifactInfo.Length
        SHA256 = $artifactHash
        PayloadFileCount = $payloadFileCount
        InstallPassed = $true
        RepairPassed = $true
        UpdateFrom = $Version
        UpdateTo = $nextVersion
        UpdatePassed = $true
        InstallScriptResultPassed = $true
        UninstallPassed = $true
    }
    $passed = $true
}
catch {
    Write-Host "Installer smoke-test evidence retained at: $resolvedEvidenceRoot"
    throw
}
finally {
    if ($passed) {
        if (Test-Path -LiteralPath $resolvedEvidenceRoot) {
            Remove-Item -LiteralPath $resolvedEvidenceRoot -Recurse -Force
        }
        if (Test-Path -LiteralPath $Artifact) {
            Remove-Item -LiteralPath $Artifact -Force
        }
    }
}

$result | ConvertTo-Json
