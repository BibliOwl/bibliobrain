# Installation

English · [Français](Installation.md) · [Guide home](Home-en.md)

Bibliobrain runs on **Windows 10 or 11**. It opens in your browser at
`http://localhost:7860`.

## What you need

- Windows 10 or 11 and an Internet connection during installation.
- Enough disk space: setup downloads AI models that take several GB.
- An NVIDIA graphics card is helpful, but optional.

Python and Ollama (the program that runs AI models on your computer) are installed
automatically if missing.

## Install

1. Download `Bibliobrain-Setup-X.Y.Z.exe` (X.Y.Z is the version number) from the
   [latest release](https://github.com/bibliOwl/bibliobrain/releases/latest).
2. Run it. Alpha versions are unsigned, so Windows SmartScreen may show a warning.
3. Choose the installation type:
   - **Simple installation** (recommended, the default): Bibliobrain installs the
     components it needs, including an AI model suited to your computer and the
     document search component.
   - **Custom installation**: choose the folder, AI model to download and shortcuts.
4. Let setup finish. Downloads account for most of the wait. At the end, the
   **Launch Bibliobrain** checkbox opens the app in your browser.

The default application folder is `%LOCALAPPDATA%\Programs\Bibliobrain`, usually
`C:\Users\<username>\AppData\Local\Programs\Bibliobrain`. It contains the program
and maintenance scripts. Press **Win + R**, paste `%LOCALAPPDATA%\Programs\Bibliobrain`
and press **Enter** to open it.

If installation fails, run the installer again: it resumes where it stopped. If
it fails again, attach `bibliobrain-install.log` from the installation folder to
your report.

### If your antivirus blocks the installer

Some antivirus software quarantines `Bibliobrain-Setup-X.Y.Z.exe` because it is new
and unsigned. Each release also provides `Bibliobrain-Windows.zip`, containing
the same files without an executable:

1. Download `Bibliobrain-Windows.zip` from the same release page.
2. Right-click the ZIP > **Properties** > tick **Unblock** > **OK**.
3. Extract it to a folder you will keep, such as `Documents`.
4. Double-click **`Install-Bibliobrain.cmd`** in that folder and answer the questions
   in the window.

A ZIP installation updates using the next ZIP package, never the installer.

## Choose a chat model

The chat model is the AI that writes answers. Larger models generally give better
answers but need more memory. Setup checks your computer, selects a suitable model
and indicates how quickly each model would run on your machine.

| Model | Size | Suggested memory |
|---|---|---|
| `qwen3.5:9b` (validated) | 6 GB | 8 GB VRAM, or 24 GB RAM without a GPU |
| `granite4.2:8b` | 5 GB | 6 GB VRAM, or 16 GB RAM without a GPU |
| `qwen3.5:4b` (lighter) | 3 GB | 5 GB VRAM, or 12 GB RAM without a GPU |

For modest computers, setup also offers smaller models marked **less reliable**.
They run, but make more errors in answers and citations. See the
[README](../README.md#recommended-models) for the current validation status and
recommendation rules.

A model that is too large for your graphics card's memory can still run, but much
more slowly. Models already installed in Ollama are listed: selecting one downloads
nothing. You can change models later in **Settings > AI assistant**.

## Start and stop

- **Start**: double-click the **Bibliobrain** shortcut in the Start menu, on the
  Desktop or in the application folder. Your browser opens after a few seconds.
- **Stop**: close the last Bibliobrain tab, or use **Quit Bibliobrain** at the bottom
  of the sidebar. `Stop-Bibliobrain.cmd` stops the server if needed.

## Update

On first launch, Bibliobrain asks whether it may check for a new version once a day
(**Check daily** or **No thanks**). No request is sent until you agree, and nothing
is installed without your click.

- When a release is available, **Update available** appears in the sidebar. It opens
  **Settings > Expert settings > Diagnostics**, under **Version and updates**.
  **Update to…** downloads the release, checks it, backs up your data, installs it
  and restarts Bibliobrain. This works when you started Bibliobrain from its shortcut.
- The same section contains **Check for updates daily** and **Check now**.
- To update outside the app, run `Update-Bibliobrain.cmd`. It creates a backup
  first, then asks you to select the new `Bibliobrain-Setup-X.Y.Z.exe`, or the new
  `Bibliobrain-Windows.zip` if you installed from a ZIP.

## Maintenance commands

These files are in the installation folder:

| File | Purpose |
|---|---|
| `Update-Bibliobrain.cmd` | Update, with a backup first |
| `Repair-Bibliobrain.cmd` | Repair a damaged installation |
| `Backup-Bibliobrain.cmd` | Back up settings, indexes, histories and your draft |
| `Restore-Bibliobrain.cmd` | Restore a backup |
| `Uninstall-Bibliobrain.cmd` | Uninstall, keeping user data by default |

Your data (indexes, settings, histories and draft) lives in
`%LOCALAPPDATA%\bibliobrain`, separate from the program. See
[Backups and maintenance](Maintenance-en.md) for backups, logs and uninstallation.

## Uninstall

If you used the Windows installer, open **Windows Settings > Apps**, select
**Bibliobrain** and choose **Uninstall**. With a ZIP or Git clone, run
`Uninstall-Bibliobrain.cmd` in the installation folder. Your data is kept by
default. See [Backups and maintenance](Maintenance-en.md) for details and the
uninstallation log.

