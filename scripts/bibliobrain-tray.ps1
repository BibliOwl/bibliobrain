param(
    [string]$BaseUrl = "http://localhost:7860",
    [string]$HealthUrl = "http://127.0.0.1:7860/api/capabilities",
    [string]$LogDir = "",
    [string]$IconPath = "",
    [int]$ServerPid = 0
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, "Local\BibliobrainTray", [ref]$createdNew)
if (-not $createdNew) {
    $mutex.Dispose()
    exit 0
}

$applicationContext = New-Object System.Windows.Forms.ApplicationContext
$notifyIcon = New-Object System.Windows.Forms.NotifyIcon
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$timer = New-Object System.Windows.Forms.Timer
$ownedIcon = $null
$state = @{
    FailedChecks = 0
    ShuttingDown = $false
}
# Without a server PID, the HTTP probe is the only liveness signal. A busy server
# (long GPU reranking, resume from sleep) can miss several probes in a row, so the
# icon is only removed after about a minute of consecutive failures.
$maxFailedHealthChecks = 30
$serverProcess = $null
if ($ServerPid -gt 0) {
    try {
        $serverProcess = [System.Diagnostics.Process]::GetProcessById($ServerPid)
    }
    catch {
        # The server is already gone or the PID is stale: fall back to HTTP probes.
        $serverProcess = $null
    }
}

function Open-Bibliobrain {
    try {
        Start-Process -FilePath $BaseUrl | Out-Null
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show(
            ("Impossible d'ouvrir Bibliobrain dans le navigateur par d{0}faut." -f [char]0x00E9),
            "Bibliobrain",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        ) | Out-Null
    }
}

try {
    if ($IconPath -and (Test-Path -LiteralPath $IconPath -PathType Leaf)) {
        $ownedIcon = New-Object System.Drawing.Icon($IconPath)
        $notifyIcon.Icon = $ownedIcon
    }
    else {
        $notifyIcon.Icon = [System.Drawing.SystemIcons]::Application
    }

    $openItem = $menu.Items.Add("Ouvrir Bibliobrain")
    $copyItem = $menu.Items.Add("Copier l'adresse locale")
    $logsItem = $menu.Items.Add("Ouvrir le dossier des journaux")
    [void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
    $statusItem = $menu.Items.Add(("Bibliobrain est en cours d'ex{0}cution" -f [char]0x00E9))
    $statusItem.Enabled = $false
    [void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
    $stopItem = $menu.Items.Add(("Arr{0}ter Bibliobrain" -f [char]0x00EA))

    $openItem.Add_Click({ Open-Bibliobrain })
    $copyItem.Add_Click({
        try {
            [System.Windows.Forms.Clipboard]::SetText($BaseUrl)
        }
        catch {
            [System.Windows.Forms.MessageBox]::Show(
                "Impossible de copier l'adresse locale.",
                "Bibliobrain",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Warning
            ) | Out-Null
        }
    })
    $logsItem.Add_Click({
        try {
            if (-not $LogDir) {
                throw "The log directory is unavailable."
            }
            New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
            Start-Process -FilePath $LogDir | Out-Null
        }
        catch {
            [System.Windows.Forms.MessageBox]::Show(
                "Impossible d'ouvrir le dossier des journaux.",
                "Bibliobrain",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Warning
            ) | Out-Null
        }
    })
    $stopItem.Add_Click({
        if ($state.ShuttingDown) {
            return
        }
        $state.ShuttingDown = $true
        $stopItem.Enabled = $false
        $statusItem.Text = ("Arr{0}t de Bibliobrain en cours..." -f [char]0x00EA)
        try {
            Invoke-WebRequest `
                -UseBasicParsing `
                -Method Post `
                -Uri "$BaseUrl/api/app/shutdown" `
                -TimeoutSec 3 | Out-Null
        }
        catch {
            # The shutdown request can close the server before a response is read.
        }
    })

    $notifyIcon.ContextMenuStrip = $menu
    $notifyIcon.Text = ("Bibliobrain - en cours d'ex{0}cution" -f [char]0x00E9)
    $notifyIcon.Add_DoubleClick({ Open-Bibliobrain })
    $notifyIcon.Visible = $true

    $timer.Interval = 2000
    $timer.Add_Tick({
        if ($null -ne $serverProcess) {
            # Process exit is authoritative and never depends on server load.
            $serverProcess.Refresh()
            if ($serverProcess.HasExited) {
                $applicationContext.ExitThread()
            }
            return
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                $state.FailedChecks = 0
                return
            }
        }
        catch {
            # Tolerated until $maxFailedHealthChecks consecutive failures.
        }
        $state.FailedChecks++
        if ($state.FailedChecks -ge $maxFailedHealthChecks) {
            $applicationContext.ExitThread()
        }
    })
    $timer.Start()

    $notifyIcon.ShowBalloonTip(
        5000,
        "Bibliobrain",
        ("Bibliobrain fonctionne en arri{0}re-plan. Double-cliquez pour l'ouvrir." -f [char]0x00E8),
        [System.Windows.Forms.ToolTipIcon]::Info
    )
    [System.Windows.Forms.Application]::Run($applicationContext)
}
finally {
    $timer.Stop()
    $notifyIcon.Visible = $false
    $timer.Dispose()
    $menu.Dispose()
    $notifyIcon.Dispose()
    $applicationContext.Dispose()
    if ($null -ne $ownedIcon) {
        $ownedIcon.Dispose()
    }
    if ($null -ne $serverProcess) {
        $serverProcess.Dispose()
    }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
