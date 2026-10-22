from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8-sig")


def _read_private_doc(relative_path: str) -> str:
    """Maintainer documents kept out of the public repository (see .gitignore)."""
    path = ROOT / relative_path
    if not path.exists():
        pytest.skip(f"{relative_path} is a private maintainer document")
    return path.read_text(encoding="utf-8-sig")


def test_frontend_tooling_has_an_integrity_locked_dependency_graph() -> None:
    package = json.loads(_read("package.json"))
    lock = json.loads(_read("package-lock.json"))
    packages = lock["packages"]

    assert lock["lockfileVersion"] == 3
    assert packages[""]["devDependencies"] == package["devDependencies"]
    assert len(packages) > 1
    for path, metadata in packages.items():
        if not path:
            continue
        assert metadata["integrity"].startswith("sha512-"), path


def test_recommended_local_models_are_consistent() -> None:
    installer = _read("install.ps1")
    configuration_example = _read("configuration.example.json")
    provider = _read("src/llm_provider.py")
    readme = _read("README.md")
    readme_fr = _read("README.fr.md")

    assert '[string]$ChatModel = "auto"' in installer
    expected_models = (
        "qwen3.5:9b",
        "granite4.2:8b",
        "qwen3.5:4b",
    )
    for model in expected_models:
        assert f'Model                   = "{model}"' in installer
        assert f"`{model}`" in readme
    # Small models are offered for modest computers. Only qwen3.5:2b may be
    # recommended, as the fallback when nothing better fits.
    for model in ("gemma4:e2b", "qwen3.5:2b", "qwen3.5:0.8b"):
        assert f'Model                   = "{model}"' in installer
        assert f"`{model}`" in readme
        assert f"`{model}`" in readme_fr
    assert '$_.Status -ne "limited"' in installer
    assert installer.count("Fallback                = $true") == 1
    before_fallback = installer.split("Fallback                = $true")[0]
    fallback_entry = before_fallback.rsplit("[pscustomobject]@{", 1)[1]
    assert 'Model                   = "qwen3.5:2b"' in fallback_entry
    # No chat model is chosen or downloaded without an explicit answer.
    assert "$ChatModelCatalog = @(" in installer
    assert "AutomaticChatModelTiers" not in installer
    assert "function Select-LocalModels" in installer
    assert "function Show-ChatModelCatalog" in installer
    assert "function Read-LineWithTimeout" in installer
    assert "[Console]::IsInputRedirected" in installer
    assert "Set-Location $RepoRoot\n\nSelect-LocalModels" in installer.replace("\r\n", "\n")
    assert "if ($DownloadEmbeddingModel) {" in installer
    assert "if ($SelectedChatModel) {" in installer
    assert '"GPU+CPU, slow"' in installer
    assert "lfm2.5:8b" not in installer
    assert "MinGpuVramGiB" in installer
    assert "MinGpuRamGiB" in installer
    assert "MinGpuLogicalProcessors" in installer
    assert "MinCpuRamGiB" in installer
    assert "MinCpuLogicalProcessors" in installer
    assert "function Get-HardwareSummary" in installer
    assert "Win32_ComputerSystem" in installer
    assert "Win32_OperatingSystem" in installer
    assert "Win32_Processor" in installer
    assert "function Get-ChatModelFit" in installer
    assert "function Get-RecommendedChatModel" in installer
    assert "--query-gpu=name,memory.total" in installer
    assert "HardwareInformation.qwMemorySize" in installer
    assert '[string]$EmbeddingModel = "embeddinggemma"' in installer
    assert '"OLLAMA_EMBEDDING_MODEL": "embeddinggemma"' in configuration_example
    assert 'DEFAULT_OLLAMA_EMBEDDING_MODEL = "embeddinggemma"' in provider
    for excluded_model in (
        "granite4.2:3b",
        "ministral-3:3b",
        "gemma4:e4b",
        "granite4.1:8b",
        "ornith:9b",
        "gemma4:12b",
        "qwen3.6:35b",
    ):
        assert f'Model                   = "{excluded_model}"' not in installer
    assert "8 Gio VRAM, 16 Gio RAM, 6 threads" in readme_fr
    assert "24 Gio RAM, 8 threads" in readme_fr
    assert "model-release-validation.md" in installer
    assert "No release-validated automatic chat model fits this computer" not in installer
    assert "L'installateur ne télécharge aucun modèle sans le demander" in readme_fr


_RECOMMENDATION_PROBE = r"""
param([string]$InstallScript)
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($InstallScript, [ref]$tokens, [ref]$errors)
foreach ($name in "Test-OllamaUsableGpu", "Get-ChatModelFit", "Get-RecommendedChatModel") {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    . ([scriptblock]::Create($fn.Extent.Text))
}
$catalog = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$ChatModelCatalog' }, $true)
. ([scriptblock]::Create($catalog.Extent.Text))
function Hw($ramGiB, $threads, $vramGiB, $gpu) {
    [pscustomobject]@{ TotalRamMiB = $ramGiB * 1024; LogicalProcessorCount = $threads; MaxVramMiB = $vramGiB * 1024; GpuNames = @($gpu) }
}
@(
    (Hw 32 16 12 "NVIDIA GeForce RTX 4070"),
    (Hw 16 8 4 "NVIDIA GeForce RTX 3050"),
    (Hw 16 8 2 "NVIDIA GeForce MX250"),
    (Hw 16 8 0 "Intel(R) Iris(R) Xe Graphics"),
    (Hw 8 4 0 "Intel(R) UHD Graphics"),
    (Hw 4 4 0 "Intel(R) UHD Graphics")
) | ForEach-Object { "model=" + (Get-RecommendedChatModel -Hardware $_) }
"""


@pytest.mark.skipif(os.name != "nt", reason="install.ps1 runs on Windows only")
def test_fallback_chat_model_is_recommended_only_when_nothing_better_fits(tmp_path: Path) -> None:
    probe = tmp_path / "recommend.ps1"
    probe.write_text(_RECOMMENDATION_PROBE, encoding="utf-8-sig")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-NonInteractive",
            "-File",
            str(probe),
            "-InstallScript",
            str(ROOT / "install.ps1"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    lines = result.stdout.splitlines()
    models = [line.removeprefix("model=") for line in lines if line.startswith("model=")]
    assert models == [
        "qwen3.5:9b",
        # A 4 GiB card holds only the fallback, never a tested model split with the CPU.
        "qwen3.5:2b",
        # Too small even for the fallback: it spills into RAM rather than no AI.
        "qwen3.5:2b",
        "granite4.2:8b",
        "qwen3.5:2b",
        "",
    ]


_CONSOLE_CHOICE_PROBE = r"""
param([string]$InstallScript)
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($InstallScript, [ref]$tokens, [ref]$errors)
foreach ($name in "Test-OllamaUsableGpu", "Get-ChatModelFit", "Get-RecommendedChatModel", "Show-ChatModelCatalog", "Select-LocalModels", "Test-ModelNameIn") {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    . ([scriptblock]::Create($fn.Extent.Text))
}
$catalog = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$ChatModelCatalog' }, $true)
. ([scriptblock]::Create($catalog.Extent.Text))
function Write-Step { }
function Get-OllamaInstalledModelNames { @() }
function Get-OllamaChatModelNames { param([string[]]$Names) @() }
function Get-HardwareSummary { $script:FakeHardware }
function Read-LineWithTimeout { param([string]$Prompt, [int]$TimeoutSeconds)
    if ($script:FakeAnswers.Count -eq 0) { return $null }
    $next = $script:FakeAnswers[0]; $script:FakeAnswers = @($script:FakeAnswers | Select-Object -Skip 1)
    if ($next -eq "<none>") { return $null }
    return $next
}
function Hw($ramGiB, $threads, $vramGiB, $gpu) {
    [pscustomobject]@{ TotalRamMiB = $ramGiB * 1024; LogicalProcessorCount = $threads; MaxVramMiB = $vramGiB * 1024; GpuNames = @($gpu) }
}
$ChatModel = "auto"; $SkipOllamaSetup = $false; $SkipModelDownloads = $false
$EmbeddingModel = "embeddinggemma"; $EmbeddingDownload = "ask"; $ModelChoiceTimeoutSeconds = 1
@(
    @{ Hw = (Hw 16 8 0 "Intel(R) Iris(R) Xe Graphics"); Answers = @("") },
    @{ Hw = (Hw 16 8 2 "NVIDIA GeForce MX250"); Answers = @("") },
    @{ Hw = (Hw 4 4 0 "Intel(R) UHD Graphics"); Answers = @("") },
    @{ Hw = (Hw 0 4 0 ""); Answers = @("", "") },
    @{ Hw = (Hw 16 8 0 "Intel(R) Iris(R) Xe Graphics"); Answers = @("<none>", "<none>") }
) | ForEach-Object {
    $script:FakeHardware = $_.Hw; $script:FakeAnswers = $_.Answers
    $script:SelectedChatModel = ""; $script:DownloadEmbeddingModel = $false; $script:SimpleInstallAccepted = $false
    Select-LocalModels *> $null
    "choice=$($script:SelectedChatModel)|$($script:DownloadEmbeddingModel)"
}
"""


@pytest.mark.skipif(os.name != "nt", reason="install.ps1 runs on Windows only")
def test_console_installation_offers_the_wizard_choices(tmp_path: Path) -> None:
    # The ZIP package runs install.ps1 in a console: Enter is the simple
    # installation, as in Setup, and no answer downloads nothing.
    probe = tmp_path / "console.ps1"
    probe.write_text(_CONSOLE_CHOICE_PROBE, encoding="utf-8-sig")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-NonInteractive",
            "-File",
            str(probe),
            "-InstallScript",
            str(ROOT / "install.ps1"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    lines = result.stdout.splitlines()
    choices = [line.removeprefix("choice=") for line in lines if line.startswith("choice=")]
    assert choices == [
        "granite4.2:8b|True",
        # A small graphics card still gets the fallback model.
        "qwen3.5:2b|True",
        # Below the minimum: no AI, but the search module of the simple installation.
        "|True",
        # Unknown computer: the question defaults to the fallback model.
        "qwen3.5:2b|True",
        "|False",
    ]


def test_windows_installer_is_idempotent_and_checks_cuda_runtime() -> None:
    installer = _read("install.ps1")

    assert "function Test-ExistingTorchProfile" in installer
    assert "function Get-PythonScriptArguments" in installer
    assert "[Convert]::ToBase64String" in installer
    assert "function Test-VisualCppRuntime" in installer
    assert "function Ensure-VisualCppRuntime" in installer
    assert "https://aka.ms/vc14/vc_redist.x64.exe" in installer
    assert "Get-AuthenticodeSignature" in installer
    assert "Microsoft Corporation" in installer
    assert 'Ensure-VisualCppRuntime' in installer
    assert "function Install-PythonFromOfficialInstaller" in installer
    assert "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" in installer
    assert '$PythonVersion = "3.14.6"' in installer
    assert "14b3e9a710a3fcf0bd9b55ab6b60412bd91227563f813fc49040cabc0209e0bd" in installer
    assert 'SignerCertificate.Subject -notmatch "Python Software Foundation"' in installer
    assert "Winget is unavailable. Using the official python.org installer" in installer
    assert "expected_version = sys.argv[1]" in installer
    assert 'expects_cuda = sys.argv[2] == "cuda"' in installer
    assert "torch.cuda.is_available()" in installer
    assert "Compatible PyTorch profile already installed" in installer
    assert "cu118" not in installer
    assert "-not (Test-Path $InstallStatePath)" in installer
    assert "cu118" not in _read("README.md")
    assert "Ensure-OllamaModel" in installer
    assert "Test-OllamaModelInstalled" in installer
    # Models already in Ollama are listed with the same filter as the application.
    assert "function Get-OllamaChatModelNames" in installer
    assert '"embed|embedding|bge|e5|gte|minilm|nomic|mxbai"' in installer
    assert "function Install-OllamaFromOfficialInstaller" in installer
    assert "function Save-InstallerDownload" in installer
    assert "function Invoke-WebRequestDownload" in installer
    assert "function Update-OllamaForModelCompatibility" in installer
    assert "https://ollama.com/download/OllamaSetup.exe" in installer
    assert "$DownloadTimeoutMinutes = 30" in installer
    assert "$DownloadRetryCount = 3" in installer
    assert 'Get-Command "curl.exe"' in installer
    assert '"--retry-all-errors"' in installer
    assert '"--speed-time", "180"' in installer
    assert "-TimeoutSec $DownloadTimeoutSeconds" in installer
    assert "PowerShell download attempt $attempt/$DownloadRetryCount" in installer
    assert "$Name download completed." in installer
    # Python, the Visual C++ runtime and Ollama share the retrying download: the
    # only bare Invoke-WebRequest left is its PowerShell fallback.
    for prerequisite_url in ("$OllamaInstallerUrl", "$PythonInstallerUrl", "$VisualCppRuntimeUrl"):
        assert f"Save-InstallerDownload -Uri {prerequisite_url} " in installer
    assert installer.count("Invoke-WebRequest `") == 1
    assert installer.count('$ProgressPreference = "SilentlyContinue"') == 2
    assert "function Assert-FreeDiskSpace" in installer
    assert (
        'Write-Step "Preparing Bibliobrain installer" -Id prepare\nAssert-FreeDiskSpace'
        in installer.replace("\r\n", "\n")
    )
    assert "Not enough free disk space on drive" in installer
    # An update leaves Ollama alone and must not claim that nothing was downloaded.
    assert "if ($SkipOllamaSetup -or $SkipModelDownloads) {\n    Write-Host \"Ollama models: left unchanged.\"" in (
        installer.replace("\r\n", "\n")
    )
    assert "$OllamaInstallerTimeoutMinutes = 10" in installer
    assert "winget could not install Ollama" in installer
    assert "Downloaded Ollama installer has an invalid publisher signature" in installer
    assert '"/SUPPRESSMSGBOXES", "/NORESTART"' in installer
    assert "Ollama installation is still running" in installer
    assert "instead of waiting indefinitely" in installer
    assert "requires a newer version of Ollama" in installer
    assert "Ollama was updated successfully. Retrying model download." in installer
    assert "function Invoke-External" in installer
    assert "Start-Job" in installer
    assert "BibliobrainNativeResult" in installer
    assert "2>&1" in installer
    assert "is still running; please keep this window open" in installer
    assert '@pythonArgs 2>$null | Out-Null' in installer
    assert "PyTorch is downloaded; unpacking and antivirus checks" in installer
    assert 'HttpWebRequest]::Create("http://127.0.0.1:11434/api/version")' in installer
    assert "function Invoke-OllamaModelPull" in installer
    assert 'HttpWebRequest]::Create("http://127.0.0.1:11434/api/pull")' in installer
    assert "Ollama rejected model" in installer
    assert "percent / 5" in installer
    assert "Ollama processes are running but the API is unresponsive" in installer
    assert "ollama-serve-$serveStamp.err.log" in installer
    assert "Ollama server startup diagnostics" in installer
    assert '$OllamaPidPath = Join-Path $InstallStateDir "ollama.pid"' in installer
    assert "Set-Content -Path $OllamaPidPath" in installer
    assert "importlib.metadata, sentence_transformers, sentry_sdk, torch" in installer
    assert "importlib.metadata.version('sentry-sdk')" in installer
    assert "dependency-installing.json" in installer
    assert "A previous dependency installation was interrupted" in installer
    assert '"--verbose",' in installer
    # pip's raw progress feeds the download estimate shown by Setup.
    assert '"--progress-bar", "raw"' in installer
    assert '"--progress-bar", "on"' not in installer
    assert "^Progress (\\d+) of (\\d+)$" in installer
    assert installer.count("-PipProgress") == 2
    assert "Update-DownloadProgress -Tracker $download -Done ([double]$event.completed)" in installer
    assert "can take 5 to 15 minutes on Windows" in installer


def test_cuda13_is_not_tried_on_gpus_it_has_no_kernels_for() -> None:
    installer = _read("install.ps1")
    profiles = installer.split("function Get-RequestedProfiles", 1)[1].split("\nfunction ", 1)[0]

    assert "$Cuda13MinimumComputeCap = 7.5" in installer
    assert "$Cuda12MinimumComputeCap = 5.0" in installer
    assert "MaxComputeCap       = $maxComputeCap" in installer
    # An unknown capability (0) keeps the full fallback list.
    assert "$computeCap -gt 0 -and $computeCap -lt $script:Cuda13MinimumComputeCap" in profiles
    assert profiles.index("Cuda12MinimumComputeCap") < profiles.index(
        'return @("cu130", "cu126", "cpu")'
    )


def test_setup_shows_download_estimate_and_keeps_the_computer_awake() -> None:
    inno = _read("packaging/Bibliobrain.iss")
    run_script = inno.split("procedure RunInstallScript", 1)[1].split("\nprocedure ", 1)[0]

    assert "SetThreadExecutionState(ES_CONTINUOUS or ES_SYSTEM_REQUIRED);" in run_script
    assert "SetThreadExecutionState(ES_CONTINUOUS);" in run_script.split("finally", 1)[1]
    assert "LastDownloadEta := StrToIntDef(Field(Line, 5), -1);" in inno
    assert "fr.InstallDownload=%1 sur %2 téléchargés, encore environ %3" in inno
    assert "InstallStep'" not in inno


def test_legacy_configuration_is_migrated_before_fresh_defaults_are_created() -> None:
    installer = _read("install.ps1")

    migration = installer.index('Write-Step "Migrating the legacy .env')
    defaults = installer.index('Write-Step "Creating the per-user configuration"')

    assert migration < defaults
    assert "if (-not $legacyMigrationScheduled -and" in installer


def test_double_click_entrypoint_and_maintenance_manager_exist() -> None:
    entrypoint = _read("Install-Bibliobrain.cmd")
    installer = _read("install.ps1")
    manager = _read("scripts/bibliobrain-manager.ps1")
    tray = _read("scripts/bibliobrain-tray.ps1")

    assert "install.ps1" in entrypoint
    assert "bibliobrain-install.log" in entrypoint
    assert "bibliobrain-install.log" in installer
    assert "Start-Transcript" in installer
    assert "Stop-InstallLogging" in installer
    assert "Installation failed. Full log:" in installer
    assert 'ValidateSet("start", "stop", "repair", "update", "backup", "restore", "uninstall")' in manager
    assert "/api/capabilities" in manager
    assert "function Invoke-GitFastForwardPull" in manager
    assert "function Test-GitAuthenticationFailure" in manager
    git_pull_function = manager[
        manager.index("function Invoke-GitFastForwardPull") : manager.index(
            "function Test-GitAuthenticationFailure"
        )
    ]
    assert '$ErrorActionPreference = "Continue"' in git_pull_function
    assert "Git writes normal fetch progress to stderr" in git_pull_function
    assert "function Test-GitPermanentFailure" in manager
    assert "function Invoke-GitFastForwardPullWithRetry" in manager
    assert "$retryDelaysSeconds = @(2, 5, 10)" in manager
    assert "for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++)" in manager
    assert "Test-GitPermanentFailure -Output $pullResult.Output" in manager
    assert "Git could not reach the remote repository" in manager
    assert "The saved Git credentials were rejected" in manager
    assert "function Backup-Bibliobrain" in manager
    assert "function Restore-Bibliobrain" in manager
    assert "function Update-PackagedBibliobrain" in manager
    assert '([string]$file.VersionInfo.ProductName).Trim() -ne "Bibliobrain"' in manager
    assert "Get-AuthenticodeSignature" in manager
    assert "Refusing to replace Bibliobrain" in manager
    assert "Backup-Bibliobrain -Destination $BackupPath" in manager
    assert "Start-Sleep -Milliseconds 750" in manager
    assert '"backup" { Backup-Bibliobrain' in manager
    assert '"restore" { Restore-Bibliobrain' in manager
    assert '"Backup-Bibliobrain.cmd" = "backup"' in installer
    assert '"Restore-Bibliobrain.cmd" = "restore"' in installer
    assert "Invoke-GitFastForwardPullWithRetry -AllowDifferentOwner:$allowDifferentOwner" in manager
    assert 'Replace("\\", "/")' in manager
    assert '@("-c", "safe.directory=$safeRepoRoot")' in manager
    assert "detected dubious ownership" in manager
    assert "never modify global config" in manager
    assert "User data will be kept" in manager
    assert "function Write-UninstallProgress" in manager
    assert "function Start-UninstallLogging" in manager
    assert "function Stop-UninstallLogging" in manager
    assert "function Invoke-UninstallWithLogging" in manager
    assert '"bibliobrain-uninstall.log"' in manager
    assert '"uninstall" { Invoke-UninstallWithLogging }' in manager
    assert 'Write-Progress -Activity "Uninstalling Bibliobrain"' in manager
    assert "Removing the Python environment (this can take a few minutes)" in manager
    assert "if ($NativeInstaller)" in manager
    assert "function Stop-ManagedOllama" in manager
    assert "function Remove-RuntimeTarget" in manager
    assert "Managed Ollama server stopped" in manager
    assert "ollama-serve-*.err.log" in manager
    assert "function Start-BibliobrainTray" in manager
    assert '"bibliobrain-tray.ps1"' in manager
    assert '"-Sta"' in manager
    assert "-WindowStyle Hidden" in manager
    assert "Start-BibliobrainTray" in manager
    assert "System.Windows.Forms.NotifyIcon" in tray
    assert '"Local\\BibliobrainTray"' in tray
    assert 'Items.Add("Ouvrir Bibliobrain")' in tray
    assert 'Items.Add("Copier l\'adresse locale")' in tray
    assert 'Items.Add("Ouvrir le dossier des journaux")' in tray
    assert 'Items.Add(("Arr{0}ter Bibliobrain" -f [char]0x00EA))' in tray
    assert "/api/app/shutdown" in tray
    assert "$HealthUrl" in tray
    assert "ShowBalloonTip" in tray
    # The icon follows the server process; a slow HTTP probe must not remove it.
    assert '"-ServerPid"' in manager
    assert "[int]$ServerPid = 0" in tray
    assert "$serverProcess.HasExited" in tray
    assert "$maxFailedHealthChecks = 30" in tray
    assert "$state.FailedChecks -ge 2" not in tray
    assert '$shortcut.TargetPath = (Get-Command "powershell.exe").Source' in installer
    assert "-WindowStyle Hidden" in installer
    assert "$shortcut.TargetPath = $batFullPath" not in installer
    # The shortcut has no console: a failed start is shown in a dialog, a server
    # still loading is waited for, and a foreign program on the port is named.
    assert '-Action start -ErrorDialog"' in installer
    assert "[switch]$ErrorDialog" in manager
    assert "function Show-StartFailure" in manager
    assert "function Get-ManagedServerProcess" in manager
    assert "function Get-PortOwnerDescription" in manager
    assert "port 7860 is already used by $portOwner" in manager
    assert "$StartupTimeoutSeconds = 180" in manager
    assert "Wait-Process -Id $serverPid -Timeout 10" in manager


def test_update_and_recovery_documentation_covers_package_and_browser_state() -> None:
    operations = _read("wiki/Maintenance.md")
    assert "signature invalide" in operations
    recovery = _read_private_doc("docs/1-INSTALLATION/update-and-recovery.md")

    assert "Update-Bibliobrain.cmd" in recovery
    assert "Backup-Bibliobrain.cmd" in recovery
    assert "Restore-Bibliobrain.cmd" in recovery
    assert "drafting_document.json" in recovery
    assert "IncludeExternalDocuments" in recovery
    assert "pre-restore-" in recovery
    assert "DPAPI" in recovery


def test_beta_guide_covers_onboarding_limits_and_privacy() -> None:
    guide = _read_private_doc("docs/1-INSTALLATION/beta-tester-guide.md")
    model_validation = _read_private_doc("docs/5-DEVELOPMENT/model-release-validation.md")

    for expected in (
        "Matériel couvert",
        "25 Gio libres",
        "35 Gio libres",
        "Update-Bibliobrain.cmd",
        "Backup-Bibliobrain.cmd",
        "Restore-Bibliobrain.cmd",
        "Limites connues",
        "fournisseur distant",
        "vingt derniers messages",
        "capture d’écran",
    ):
        assert expected in guide
    assert "qwen3.5:9b" in model_validation
    assert "qwen3.6:35b" in model_validation
    assert "une hallucination critique" in model_validation
    assert "validation humaine indépendante" in model_validation


def test_windows_shortcut_icon_is_square_and_multiresolution() -> None:
    icon = (ROOT / "static" / "logo_bibliobrain.ico").read_bytes()
    reserved, image_type, image_count = struct.unpack_from("<HHH", icon)

    assert reserved == 0
    assert image_type == 1
    assert image_count >= 6

    dimensions = set()
    for index in range(image_count):
        width, height = struct.unpack_from("<BB", icon, 6 + index * 16)
        dimensions.add((width or 256, height or 256))

    assert all(width == height for width, height in dimensions)
    assert {16, 32, 48, 64, 128, 256} <= {width for width, _ in dimensions}


def test_native_windows_installer_preserves_user_data_and_supports_signing() -> None:
    inno = _read("packaging/Bibliobrain.iss")
    builder = _read("packaging/build-installer.ps1")

    assert "Bibliobrain-Setup" in inno
    assert "{localappdata}\\Programs\\Bibliobrain" in inno
    assert 'Name: "{app}\\.venv"' in inno
    assert 'Name: "{localappdata}\\bibliobrain"' not in inno
    assert "-NativeInstaller" in inno
    assert "procedure InitializeUninstallProgressForm" in inno
    assert "procedure CurUninstallStepChanged" in inno
    # The progress form does not exist in a silent uninstall, nor at the first
    # step of an interactive one: using it there aborts the uninstall with
    # "Runtime error: Could not call proc".
    assert "if UninstallSilent or (UninstallProgressForm = nil) then" in inno
    assert "usAppMutexCheck" not in inno.replace("at usAppMutexCheck, the first step", "")
    assert "Uninstall complete." in inno
    assert "This is the longest step" in inno
    assert "procedure InitializeWizard" in inno
    assert "CreateOutputMsgMemoPage" in inno
    assert "What Bibliobrain will install" in inno
    # Plain words for anyone: what is installed, without the technical names.
    assert "Python, Ollama (the program that runs the AI) and a few Windows components" in inno
    assert "The module that lets Bibliobrain search your documents" in inno
    assert "a small module that sorts search results" in inno
    assert "Reading scanned documents, and the large AI for in-depth research" in inno
    assert "Bibliobrain asks for your permission before downloading them" in inno
    for jargon in ("runtime", "PyTorch, matched", "reranking model", "logical cores", "GiB"):
        wizard_texts = inno.split("[CustomMessages]", 1)[1].split("[Code]", 1)[0]
        assert jargon not in wizard_texts, jargon
    assert "waituntilterminated runhidden" not in inno.split("[UninstallRun]", 1)[0]
    # The wizard asks before downloading a model; it must not promise otherwise.
    # The reranker is downloaded without a question: the promise names the two
    # models the wizard asks about, not "no model".
    assert "chosen automatically" not in inno
    assert "Neither of them is downloaded without your consent" in inno
    assert "no model is downloaded without" not in inno
    # Every message exists in both languages: a missing French one would show
    # English text in a French wizard.
    messages = inno.split("[CustomMessages]", 1)[1].split("[Code]", 1)[0]
    english = set(re.findall(r"^en\.(\w+)=", messages, flags=re.MULTILINE))
    french = set(re.findall(r"^fr\.(\w+)=", messages, flags=re.MULTILINE))
    assert english == french
    # install.ps1 names its steps by id; Setup owns their translation.
    step_ids = set(re.findall(r'Write-Step "[^"\n]+" -Id (\w+)', _read("install.ps1")))
    assert step_ids
    assert {f"Step_{step_id}" for step_id in step_ids} <= english
    # Setup starts install.ps1 itself so that a failure is shown and returned,
    # without any console: every answer is passed as an argument.
    assert "procedure RunInstallScript" in inno
    assert "CREATE_NO_WINDOW" in inno
    assert "-NonInteractive" in inno
    assert "SW_SHOW" not in inno
    assert "-SkipShortcuts -NoLaunch" in inno
    assert "-EmbeddingDownload" in inno
    run_section = inno.split("[Run]", 1)[1].split("[UninstallRun]", 1)[0]
    assert "install.ps1" not in run_section
    assert "bibliobrain-manager.ps1" in run_section
    assert "postinstall nowait skipifsilent runhidden" in run_section
    assert "InstallScriptFailedExitCode = 20" in inno
    assert "ExitProcess(InstallScriptFailedExitCode)" in inno
    assert "Bibliobrain is not ready yet" in inno
    # The model choice is a wizard page fed by the script, never a terminal prompt.
    assert "CreateCustomPage(wpSelectTasks" in inno
    assert "-ProbeModelsFile" in inno
    assert 'DestName: "install-probe.ps1"; Flags: dontcopy' in inno
    assert "{param:CHATMODEL|none}" in inno
    assert "{param:EMBEDDING|0}" in inno
    assert 'Name: "fr"; MessagesFile: "compiler:Languages\\French.isl"' in inno
    assert "fr.ModelsTitle=Modèles d'IA" in inno
    # One scrollable radio set (a group heading would start a second one), and
    # the chat models already in Ollama are offered without a download.
    assert "TNewCheckListBox.Create(ModelsPage)" in inno
    # Next, Next, Install works for a novice: the simple installation is the
    # default and says what it downloads; silent installs keep their parameters.
    assert "CreateInputOptionPage(wpWelcome" in inno
    assert "ModePage.Values[0] := True;" in inno
    assert "By choosing it, you agree to these downloads" in inno
    assert "IsEasyMode and not ModelsPageBuilt and not WizardSilent" in inno
    assert "EmbeddingCheck.Checked := True;" in inno
    # Inno's 4-pixel check-box margin does not follow the display scale.
    for check_list in ("ModelList", "ModePage.CheckListBox", "WizardForm.TasksList"):
        assert f"{check_list}.Offset := ScaleX(4);" in inno
    assert "AddGroup(" not in inno
    assert "'installed'" in inno
    installer_script = _read("install.ps1")
    assert 'installed|{0}|{1} GB' in installer_script
    assert "function Get-OllamaModelsOnDisk" in installer_script
    assert '[ValidateSet("ask", "yes", "no")]' in installer_script
    assert "[string]$EmbeddingDownload" in installer_script
    assert "[string]$ProgressFile" in installer_script
    assert "[string]$ProbeModelsFile" in installer_script
    assert "[switch]$SkipShortcuts" in installer_script
    assert 'if ($EmbeddingDownload -eq "ask")' in installer_script
    # The probe answers before the transcript starts and before anything is installed.
    assert installer_script.index("if ($ProbeModelsFile)") < installer_script.index(
        "Start-Transcript"
    )
    # A running server is stopped before its files are replaced.
    assert "function PrepareToInstall" in inno
    assert '" -Action stop\'' in inno
    uninstall_delete = inno.split("[UninstallDelete]", 1)[1].split("[Code]", 1)[0]
    for leftover in ("src", "scripts", "__pycache__", "bibliobrain-install.log"):
        assert f'Name: "{{app}}\\{leftover}"' in uninstall_delete
    assert 'Source: "..\\*"' not in inno
    assert 'Source: "{#SourceRoot}\\src\\*"' in inno
    assert 'Source: "{#SourceRoot}\\static\\*"' in inno
    assert 'Source: "{#SourceRoot}\\requirements.lock"' in inno
    assert 'Source: "{#PayloadVersionFile}"' in inno
    assert 'DestName: "VERSION"' in inno
    # The AGPL asks for no acceptance to run the program: no licence page, but
    # its text ships with the application and the summary page points to it.
    assert "LicenseFile=" not in inno
    assert "free software under the GNU AGPL v3" in inno
    assert 'Source: "{#SourceRoot}\\LICENSE"' in inno
    assert 'Source: "{#SourceRoot}\\wiki\\*"' in inno
    assert 'Source: "{#SourceRoot}\\docs\\' not in inno
    assert 'DestName: "BETA_TESTER_GUIDE.md"' not in inno
    assert 'Source: "{#SourceRoot}\\THIRD_PARTY_NOTICES.md"' in inno
    assert 'Source: "{#SourceRoot}\\THIRD_PARTY_LICENSES\\*"' in inno
    assert 'DestDir: "{app}\\THIRD_PARTY_LICENSES"' in inno
    assert "#ifndef PackagingSmoke" in inno
    assert "Bibliobrain Installer Smoke Test" in inno
    assert "archive --format=zip" in builder
    assert "status --porcelain --untracked-files=all" in builder
    assert "/DSourceRoot=$sourceRoot" in builder
    assert '$requiredLegalPaths = @(' in builder
    assert '"LICENSE"' in builder
    assert '"THIRD_PARTY_NOTICES.md"' in builder
    assert '"THIRD_PARTY_LICENSES"' in builder
    assert "Clean source export is missing required legal material" in builder
    assert 'Get-Command "ISCC.exe"' in builder
    assert '"Programs\\Inno Setup 6\\ISCC.exe"' in builder
    assert "CertificateThumbprint" in builder
    assert "signtool.exe" in builder


def test_installer_pins_safe_starlette_and_protects_configuration_acl() -> None:
    requirements = _read("requirements.txt")
    installer = _read("install.ps1")

    assert "starlette==1.3.1" in requirements
    assert "function Protect-ConfigurationFileAcl" in installer
    assert "[System.Security.AccessControl.FileSecurity]::new()" in installer
    assert "Get-Acl -LiteralPath $Path" not in installer
    assert "$acl.SetAccessRuleProtection($true, $false)" in installer
    assert 'SecurityIdentifier]::new("S-1-5-18")' in installer
    assert 'SecurityIdentifier]::new("S-1-5-32-544")' in installer
    assert "Protect-ConfigurationFileAcl -Path $configurationTarget" in installer
    # Set-Acl rewrites the SACL and needs SeSecurityPrivilege, which a managed
    # domain account lacks; the DACL-only methods must be used and a failure
    # must warn instead of aborting the installation.
    code_lines = [line for line in installer.splitlines() if not line.lstrip().startswith("#")]
    assert not any("Set-Acl" in line for line in code_lines)
    assert "function Grant-FileDaclOnly" in installer
    assert '"/inheritance:r", "/grant:r"' in installer
    assert "function Grant-FileDaclViaDotNet" in installer
    assert "$file.SetAccessControl($acl)" in installer
    acl_body = installer.split("function Protect-ConfigurationFileAcl", 1)[1].split(
        "function Format-Command", 1
    )[0]
    assert "Write-Warning" in acl_body
    assert re.search(r"throw", acl_body) is None
    assert ".env.example" not in installer


def test_clean_machine_installer_plan_covers_release_workflow() -> None:
    plan = _read_private_doc("packaging/CLEAN_MACHINE_TEST_PLAN.md")

    for requirement in (
        "Profil CPU",
        "Profil NVIDIA",
        "premier lancement",
        "préparation",
        "citation",
        "redémarrage",
        "réparation",
        "désinstallation",
        "réinstaller",
        "THIRD_PARTY_NOTICES.md",
        "torch.cuda.is_available()",
    ):
        assert requirement.casefold() in plan.casefold()


def test_installer_payload_smoke_test_is_isolated_and_checks_legal_files() -> None:
    smoke_test = _read("packaging/test-installer-payload.ps1")

    assert '"/DPackagingSmoke=1"' in smoke_test
    assert "bibliobrain-installer-smoke-" in smoke_test
    assert '"LICENSE"' in smoke_test
    assert '"BETA_TESTER_GUIDE.md"' in smoke_test
    assert '"THIRD_PARTY_NOTICES.md"' in smoke_test
    assert '"THIRD_PARTY_LICENSES\\Inter-OFL-1.1.txt"' in smoke_test
    assert '"THIRD_PARTY_LICENSES\\JetBrainsMono-OFL-1.1.txt"' in smoke_test
    assert '"tests"' in smoke_test
    assert "InstallPassed = $true" in smoke_test
    assert "RepairPassed = $true" in smoke_test
    assert '"/DPayloadVersionFile=$nextVersionFile"' in smoke_test
    assert "UpdatePassed = $true" in smoke_test
    assert '"/DSmokeInstallScript=$stubScript"' in smoke_test
    assert "-SkipShortcuts -NoLaunch -SkipOllamaSetup" in smoke_test
    assert "-ChatModel none -EmbeddingDownload no" in smoke_test
    assert '"/CHATMODEL=qwen3.5:9b"' in smoke_test
    assert "$scriptSetup.ExitCode -ne 20" in smoke_test
    assert "InstallScriptResultPassed = $true" in smoke_test
    assert '"src\\__pycache__"' in smoke_test
    assert "UninstallPassed = $true" in smoke_test


def test_runtime_dependencies_are_hash_locked_without_overriding_torch_profile() -> None:
    requirements = _read("requirements.txt")
    lockfile = _read("requirements.lock")
    installer = _read("install.ps1")

    assert "torch==2.14.0" in requirements
    assert "uv pip compile requirements.txt" in lockfile.splitlines()[1]
    assert lockfile.count("--hash=sha256:") > 100
    assert "\ntorch==" not in lockfile
    assert "# torch" in lockfile
    assert '"--no-deps",' in installer
    assert '"--require-hashes",' in installer
    assert '"-r", "requirements.lock"' in installer


def test_gitea_ci_mirrors_the_github_workflow() -> None:
    gitea = ROOT / ".gitea/workflows/ci.yml"
    if not gitea.exists():
        pytest.skip(".gitea/workflows/ci.yml is not part of the public repository")
    mirrored = gitea.read_text(encoding="utf-8-sig").replace(
        "secrets.GITEA_TOKEN", "secrets.GITHUB_TOKEN"
    )

    assert mirrored == _read(".github/workflows/ci.yml")


def test_ci_enforces_quality_tests_and_security_scans() -> None:
    workflow = _read(".github/workflows/ci.yml")
    development_requirements = _read("requirements-dev.txt")

    assert "runs-on: windows-latest" in workflow
    assert "python-version: \"3.14\"" in workflow
    assert "python -m ruff check ." in workflow
    assert "python -m mypy" in workflow
    assert "continue-on-error: true" not in workflow
    assert "python -m pytest -q" in workflow
    assert "python -m pip install --no-deps --require-hashes -r requirements.lock" in workflow
    assert "uv pip compile requirements.txt --generate-hashes" in workflow
    assert "git diff --exit-code -- requirements.lock" in workflow
    assert "python -m pip_audit -r requirements.lock --disable-pip" in workflow
    for advisory in (
        "PYSEC-2026-311",
        "PYSEC-2026-3813",
        "PYSEC-2026-3814",
        "PYSEC-2026-3815",
    ):
        assert f"--ignore-vuln {advisory}" in workflow
    # setuptools is fixed since torch 2.14 lifted its setuptools<82 cap.
    assert "PYSEC-2026-3447" not in workflow
    assert "SECURITY.md" in workflow
    assert "gitleaks/gitleaks-action@v2" in workflow
    assert "ENABLE_EXTERNAL_SECURITY_SCANS" not in workflow
    assert "npm ci --ignore-scripts --no-audit --no-fund" in workflow
    assert "npm audit --audit-level=high --ignore-scripts" in workflow
    assert "npm run test:browser" in workflow
    assert "pip-audit==2.10.1" in development_requirements
    assert "uv==0.13.4" in development_requirements


def test_public_bugsink_reporting_is_installed_and_configurable() -> None:
    requirements = _read("requirements.txt")
    secret_store = _read("src/api_secrets.py")
    configuration_example = _read("configuration.example.json")
    bug_reports = _read("src/bug_reports.py")
    html = _read("static/index.html")
    javascript = read_app_javascript()
    installer = _read("packaging/Bibliobrain.iss")
    builder = _read("packaging/build-installer.ps1")

    assert "sentry-sdk[fastapi]==2.66.0" in requirements
    assert '"environment": "BIBLIOBRAIN_BUGSINK_DSN"' in secret_store
    assert '"BIBLIOBRAIN_ERROR_REPORTING_ENABLED": true' in configuration_example
    assert "BIBLIOBRAIN_BUG_REPORT_TOKEN" not in configuration_example
    assert "_DEFAULT_BUGSINK_DSN" in bug_reports
    assert "send_default_pii=False" in bug_reports
    assert "traces_sample_rate=0.0" in bug_reports
    version = _read("VERSION").strip()
    # Same grammar as build-installer.ps1: prereleases (0.9.0-beta.1) are valid too.
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?", version)
    assert "BIBLIOBRAIN_VERSION" not in configuration_example
    assert 'id="app-version-badge"' in html
    assert re.search(r"v0\.\d+\.\d+", html) is None
    assert 'FileOpen(AddBackslash(SourceRoot) + "VERSION")' in installer
    assert 'Source: "{#PayloadVersionFile}"' in installer
    assert '[string]$Version = "",' in builder
    assert 'Join-Path $RepoRoot "VERSION"' in builder
    assert 'id="bug-report-include-chat"' in html
    assert 'id="bug-report-capture-screenshot"' in html
    assert 'id="bug-report-choose-screenshot"' in html
    assert 'id="bug-report-screenshot-file"' in html
    assert "navigator.mediaDevices.getDisplayMedia" in javascript
    assert "window.isSecureContext === false" in javascript
    assert "event.clipboardData?.items" in javascript
    assert "_attachBugReportImageFile" in javascript
    maintenance = " ".join(_read("wiki/Maintenance.md").split())
    assert "Une valeur vide active automatiquement le DSN officiel" in maintenance


def test_onboarding_has_two_steps_and_leaves_ocr_to_settings() -> None:
    html = _read("static/index.html")
    css = _read("static/css/settings.css")
    javascript = read_app_javascript()
    modal = html[html.index('id="startup-help-modal"') : html.index('src="/static/js/startup-preopen.js')]

    assert "const STARTUP_GUIDE_STEPS = ['library', 'build'];" in javascript
    assert modal.count('data-startup-step="') == 2
    assert "data-startup-step-btn" not in html
    assert "startup-help-card" not in html
    assert 'id="startup-help-close"' not in html
    assert 'id="startup-back-btn"' not in html
    assert 'id="startup-help-dont-show"' not in html
    assert ".startup-onboarding-steps" not in css

    # OCR and figure reading stay available, but only in Settings.
    assert 'id="startup-install-ovis-btn"' not in html
    assert 'id="startup-install-glm-btn"' not in html
    assert "async function saveStartupEnrichment()" not in javascript
    assert 'id="ocr-install"' in html
    assert "fetch('/api/ocr/install'" in _read("static/js/app/ocr.js")
    assert "fetch('/api/vision-models/install'" in _read("static/js/settings.js")

    assert "async function prepareStartupBuildStep()" in javascript
    assert "state.providerInfo?.required_embedding_model || d.required_embedding_model" in javascript
    assert "const installedConfigured = configured" in javascript
    assert "embeddingReady = await saveStartupEmbeddingModel();" not in javascript
    assert 'id="startup-build-problem" class="startup-build-problem" hidden' in modal

    # Metadata sources are tucked behind one disclosure; Zotero is detected quietly.
    assert '<details id="startup-metadata-details"' in modal
    assert html.count('name="startup-metadata-source"') == 3
    assert 'value="bibtex"' in html
    assert 'value="zotero"' in html
    assert 'value="none"' in html
    assert ".startup-source-choices" in css
    assert "function setStartupMetadataSource(source" in javascript
    assert "function selectAndDetectStartupZotero()" in javascript
    static_bindings = _read("static/js/static-event-bindings.js")
    assert "selectAndDetectStartupZotero()" in static_bindings
    assert "openStartupAssistant()" in static_bindings
    assert "fetch('/api/zotero-detect')" in javascript
    assert "async function autoDetectStartupZotero()" in javascript
    assert "metadataSource === 'none' ? ''" in javascript


def test_onboarding_is_accessible_and_state_driven() -> None:
    html = _read("static/index.html")
    javascript = read_app_javascript()
    translations = _read("static/js/i18n.js")
    modal = html[html.index('id="startup-help-modal"') : html.index('src="/static/js/startup-preopen.js')]
    actions = modal[modal.index('id="startup-help-actions"') :]

    assert 'role="dialog" aria-modal="true"' in modal
    assert 'aria-describedby="startup-help-subtitle"' in modal
    assert modal.count('aria-live="polite"') >= 4
    assert 'role="progressbar"' in modal
    assert actions.count("<button") == 2
    assert 'id="startup-next-btn"' in actions
    assert 'id="startup-later-btn"' in actions
    assert 'id="startup-build-btn"' not in html
    assert "function _handleStartupHelpKeydown(event)" in javascript
    assert "function updateStartupGuideControls()" in javascript
    assert "_ti('startup.stepCounter'" in javascript
    assert "function setStartupBuildProgress(percent)" in javascript
    assert "function describeBackendConnectionError(" in javascript
    assert "'app.backendDisconnected'" in translations
    assert "startupGuideChecksReady" in javascript
    assert "function normalizeUserPathInput(value)" in javascript
    assert "function inferStartupMetadataSource(path)" in javascript
    assert "if (!r.ok || d.ok === false)" in javascript
    for key in (
        "startup.stepCounter",
        "startup.libraryQuestion",
        "startup.metadataPathRequired",
        "startup.exploreDuringBuild",
        "startup.buildContinuesToast",
        "startup.completedToast",
    ):
        assert f"'{key}'" in translations

    # Leaving the assistant never interrupts preparation or asks for confirmation.
    request_hide = javascript[
        javascript.index("function requestHideStartupHelp()")
        : javascript.index("function _handleStartupHelpKeydown(event)")
    ]
    assert "window.confirm" not in request_hide
    assert "startup.buildContinuesToast" in request_hide


def test_onboarding_popup_is_requested_before_application_boot() -> None:
    html = _read("static/index.html")
    javascript = read_app_javascript()
    preopen_javascript = _read("static/js/startup-preopen.js")

    popup_request = "void showStartupHelpOnPageLoad();"
    assert popup_request in javascript
    assert javascript.index(popup_request) < javascript.index("async function bootApplicationAuto()")
    assert "startupHelpAutoChecked: false" in javascript
    preopen_script = "function preopenStartupGuide()"
    assert preopen_script in preopen_javascript
    assert html.index('src="/static/js/startup-preopen.js') < html.index('src="/static/js/app.js')
    assert "modal.classList.add('open');" in preopen_javascript
    assert html.index('id="startup-boot-splash"') < html.index('id="mobile-nav-toggle"')
    assert 'id="startup-boot-critical"' in html
    assert "function hideStartupBootSplash()" in javascript
    assert javascript.count("hideStartupBootSplash();") >= 2
    automatic_boot = javascript[javascript.index("async function bootApplicationAuto()") :]
    assert "hideStartupBootSplash();" in automatic_boot
    popup_function = javascript[
        javascript.index("async function showStartupHelpOnPageLoad()")
        :javascript.index("async function persistStartupOnboardingDone()")
    ]
    assert popup_function.index("showStartupHelp(true);") < popup_function.index(
        "await fetchStartupStatus()"
    )
    assert "if (!state.startupHelpAutoChecked) void showStartupHelpOnPageLoad();" in javascript
    # The pre-opened guide closes again once the backend says setup is done.
    assert popup_function.index("await fetchStartupStatus()") < popup_function.index(
        "if (!isStartupGuideNeeded(snapshot))"
    )
    assert "hideStartupHelp({ restoreFocus: false });" in popup_function


@pytest.mark.skipif(os.name != "nt", reason="PowerShell syntax validation is Windows-only")
@pytest.mark.parametrize(
    "relative_path",
    [
        "install.ps1",
        "scripts/bibliobrain-manager.ps1",
        "scripts/bibliobrain-tray.ps1",
        "packaging/build-installer.ps1",
        "packaging/test-installer-payload.ps1",
        "packaging/test-installer-legacy-upgrade.ps1",
    ],
)
def test_powershell_scripts_parse(relative_path: str) -> None:
    path = ROOT / relative_path
    command = (
        "$errors = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _probe_embedding_model(local_app_data: Path, existing_install: Path, *extra: str) -> str:
    probe = local_app_data.parent / "probe.txt"
    # A copy, as Setup runs it (install-probe.ps1 in its temporary folder):
    # install.ps1 also reads the .env next to itself, and the checkout's own
    # developer .env would otherwise decide the answer.
    setup_dir = local_app_data.parent / "setup"
    setup_dir.mkdir(exist_ok=True)
    probe_script = setup_dir / "install-probe.ps1"
    shutil.copyfile(ROOT / "install.ps1", probe_script)
    environment = {**os.environ, "LOCALAPPDATA": str(local_app_data)}
    environment.pop("BIBLIOBRAIN_HOME", None)
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-NonInteractive",
            "-File",
            str(probe_script),
            "-ProbeModelsFile",
            str(probe),
            "-ExistingInstallDir",
            str(existing_install),
            *extra,
        ],
        cwd=setup_dir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    lines = probe.read_text(encoding="ascii").splitlines()
    return next(line.split("|")[1] for line in lines if line.startswith("embedding|"))


@pytest.mark.skipif(os.name != "nt", reason="install.ps1 runs on Windows only")
def test_an_update_keeps_the_embedding_model_of_the_existing_index(
    workspace_tmp_dir: Path,
) -> None:
    # An index built with another model would gain nothing from embeddinggemma.
    local_app_data = workspace_tmp_dir / "LocalAppData"
    app_home = local_app_data / "bibliobrain"
    existing_install = workspace_tmp_dir / "Bibliobrain"
    (app_home / "storage").mkdir(parents=True)
    existing_install.mkdir()

    assert _probe_embedding_model(local_app_data, existing_install) == "embeddinggemma"

    (existing_install / ".env").write_text(
        "OLLAMA_EMBEDDING_MODEL=nomic-embed-text-v2-moe\n", encoding="utf-8"
    )
    assert _probe_embedding_model(local_app_data, existing_install) == "nomic-embed-text-v2-moe"

    # Once migrated, the application ignores the .env.
    (app_home / "legacy-env-migrated-v1.json").write_text('{"version": 1}', encoding="utf-8")
    assert _probe_embedding_model(local_app_data, existing_install) == "embeddinggemma"

    (app_home / "configuration.json").write_text(
        json.dumps({"version": 1, "settings": {"OLLAMA_EMBEDDING_MODEL": "nomic-embed-text-v2-moe"}}),
        encoding="utf-8",
    )
    assert _probe_embedding_model(local_app_data, existing_install) == "nomic-embed-text-v2-moe"

    # The model chosen in Settings comes first, as in src/llm_provider.py.
    (app_home / "storage" / "provider_state.json").write_text(
        json.dumps({"ollama_embedding_model": "qwen3-embedding:0.6b"}), encoding="utf-8"
    )
    assert _probe_embedding_model(local_app_data, existing_install) == "qwen3-embedding:0.6b"
    assert (
        _probe_embedding_model(local_app_data, existing_install, "-EmbeddingModel", "embeddinggemma")
        == "embeddinggemma"
    )


def test_a_second_installation_run_waits_for_the_first() -> None:
    installer = _read("install.ps1")
    inno = _read("packaging/Bibliobrain.iss")

    # The lock is taken before the log starts (a second run would overwrite
    # the first one's log) and after the probe, which Setup runs meanwhile.
    lock = installer.index("$InstallLock.WaitOne(0)")
    assert installer.index("if ($ProbeModelsFile) {") < lock
    assert lock < installer.index("Start-Transcript -Path $InstallLogPath")
    assert "catch [System.Threading.AbandonedMutexException]" in installer
    assert "$InstallBusyExitCode = 3" in installer
    stop_logging = installer.split("function Stop-InstallLogging", 1)[1].split("\nfunction ", 1)[0]
    assert stop_logging.index("Stop-Transcript") < stop_logging.index("ReleaseMutex()")
    assert "InstallScriptBusyExitCode = 3;" in inno
    assert "CustomMessage('InstallBusyBox')" in inno


def _install_lock_name(folder: Path) -> str:
    # Mirrors Get-InstallLockName in install.ps1.
    normalized = os.path.abspath(folder).rstrip("\\").lower()
    return "Local\\Bibliobrain-Install-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@pytest.mark.skipif(os.name != "nt", reason="install.ps1 runs on Windows only")
def test_install_script_stops_while_another_run_holds_its_folder(
    workspace_tmp_dir: Path,
) -> None:
    import ctypes

    install_dir = workspace_tmp_dir / "Bibliobrain"
    install_dir.mkdir()
    script = install_dir / "install.ps1"
    shutil.copyfile(ROOT / "install.ps1", script)
    progress = workspace_tmp_dir / "progress.txt"
    environment = {**os.environ, "LOCALAPPDATA": str(workspace_tmp_dir / "LocalAppData")}
    environment.pop("BIBLIOBRAIN_HOME", None)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateMutexW(None, True, _install_lock_name(install_dir))
    assert handle
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-NonInteractive",
                "-File",
                str(script),
                "-ProgressFile",
                str(progress),
                # Should the lock ever be missed, nothing real may happen.
                "-DryRun",
                "-ChatModel",
                "none",
                "-EmbeddingDownload",
                "no",
                "-SkipPrerequisiteInstall",
                "-SkipOllamaSetup",
                "-SkipModelDownloads",
                "-SkipShortcuts",
                "-SkipRerankerWarmup",
                "-NoLaunch",
            ],
            cwd=install_dir,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    finally:
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)

    assert result.returncode == 3, result.stdout + result.stderr
    assert "Another Bibliobrain installation is still running" in result.stdout
    assert progress.read_text(encoding="ascii").startswith("busy||")
    # The running installation keeps its log.
    assert not (install_dir / "bibliobrain-install.log").exists()


def test_the_installer_file_name_carries_its_version() -> None:
    inno = _read("packaging/Bibliobrain.iss")
    build = _read("packaging/build-installer.ps1")
    workflow = _read(".github/workflows/release.yml")

    # Inno names the file, the build script finds it, the release uploads it.
    assert '#define InstallerOutputBaseFilename "Bibliobrain-Setup-" + MyAppVersion' in inno
    assert '"dist\\Bibliobrain-Setup-$Version.exe"' in build
    assert "\"dist/Bibliobrain-Setup-$($tag.TrimStart('v')).exe\"" in workflow
    assert "dist/Bibliobrain-Setup.exe" not in workflow
