# Backups and maintenance

English · [Français](Maintenance.md) · [Guide home](Home-en.md)

The commands below are in Bibliobrain's installation folder. With the Windows
installer, the default is `%LOCALAPPDATA%\Programs\Bibliobrain`: press **Win + R**,
paste that path and press **Enter** to open it. For installation and updates, see
[Installation](Installation-en.md).

## Back up and restore

`Backup-Bibliobrain.cmd` briefly stops the app and archives the configuration,
indexes, settings, histories and writing draft. `Restore-Bibliobrain.cmd` validates
the archive, creates a rollback backup first, restores the files and restarts the app.

Documents outside Bibliobrain's data folder are not included by default. To
include them, run the following PowerShell command from the installation folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bibliobrain-manager.ps1 -Action backup -IncludeExternalDocuments
```

API keys encrypted with Windows DPAPI work after restoration only under the same
Windows account.

## Repair or update

- `Repair-Bibliobrain.cmd` reinstalls missing components in a damaged installation.
- `Update-Bibliobrain.cmd` creates a backup before updating Bibliobrain. It asks you
  to select the new installer or ZIP, depending on your installation method. An
  invalid signature or older version is rejected; an unsigned alpha requires an
  additional confirmation.

Data is separate from the program and is kept during these operations. If an
update fails, run `Repair-Bibliobrain.cmd`.

## Logs

Bibliobrain's data lives in `%LOCALAPPDATA%\bibliobrain`, or the folder selected
with `BIBLIOBRAIN_HOME`. Useful logs are:

| File | Contents |
|---|---|
| `logs\bibliobrain.log` | Application log; rotates across five 5 MiB files |
| `logs\server.out.log`, `logs\server.err.log` | Output from the latest startup |
| `bibliobrain-install.log`, in the installation folder | Details of the latest installation |
| `%TEMP%\bibliobrain-uninstall.log` | Uninstallation details, kept after cleanup |

See [Troubleshooting](Troubleshooting-en.md) to report a problem. Reports are sent
only after an explicit action in the interface. Leaving the reporting DSN empty
automatically uses the official write-only DSN. To disable reports entirely, set
`BIBLIOBRAIN_ERROR_REPORTING_ENABLED` to `false` in `configuration.json` in the
data folder.

## Uninstall

1. Run `Backup-Bibliobrain.cmd` if you want to keep a backup.
2. If you used the Windows installer, open **Windows Settings > Apps**, select
   **Bibliobrain** and choose **Uninstall**. With a ZIP or Git clone, run
   `Uninstall-Bibliobrain.cmd` and confirm.
3. Let cleanup finish. The window shows the current step and progress.

With the Windows installer, the uninstaller removes the application files. With a
ZIP or Git clone, the command removes Bibliobrain's Python environment and
launchers; the source folder remains. Running `Uninstall-Bibliobrain.cmd` in a
Windows installation performs the same runtime cleanup. Use the Windows
uninstaller to also remove the application files and its entry in the apps list.

Your indexes, settings, API keys, histories and drafts are kept in the data folder
by default. Your library documents and Zotero library are not deleted. Python
installed on Windows, Ollama and downloaded models remain installed.

If cleanup fails, read `%TEMP%\bibliobrain-uninstall.log`, close Ollama if the
message asks you to, then run the uninstaller again.
