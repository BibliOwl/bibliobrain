# Troubleshooting

English · [Français](Depannage.md) · [Guide home](Home-en.md)

## The app does not open

- Double-click the shortcut again and wait up to a minute.
- Try `http://localhost:7860` in your browser.
- Run `Repair-Bibliobrain.cmd` to reinstall missing components. Your data is
  separate from the program, under `%LOCALAPPDATA%\bibliobrain`.
- If that does not help, read `%LOCALAPPDATA%\bibliobrain\logs\bibliobrain.log`.

With the Windows installer, maintenance scripts are in
`%LOCALAPPDATA%\Programs\Bibliobrain` by default. Press **Win + R** and paste that
path to open the folder.

## Antivirus blocks the installer

Use the `Bibliobrain-Windows.zip` package from the same release (see
[Installation](Installation-en.md#if-your-antivirus-blocks-the-installer)).

## The indicator never reaches “Ready”

- Preparing a large library can take a long time on the first run. Work continues
  in the background: click the indicator to see its progress.
- Check that Ollama is running. If the indicator's details show **AI engine
  stopped**, click **Start**.
- In **Settings > Overview**, click the button on the incomplete row
  (**Prepare**, **Configure** or **Fix**). Bibliobrain explains what is missing
  and which command to run.

## My new PDFs do not appear

Bibliobrain does not read new files automatically. Return to its window: the
indicator shows **N new PDFs**. Click **Add** (see [Library](Library-en.md#add-papers)).

## Chat says it found nothing, or answers the wrong question

- Wait for the indicator to show **Ready**.
- Rephrase your question using terms present in your papers.
- Remove chat tag or collection filters in **Options > Zotero filters**.
- Open **Maps > Last answer** to see what was retrieved.
- A model that is too small gives weaker answers: try a larger one (see
  [Installation](Installation-en.md)).

## Answers are very slow

The chat model is probably too large for your graphics card's memory and is
partly running on the CPU. Choose a lighter model in **Settings > AI assistant**,
or use **Fast** beside the chat send arrow.

## Some PDFs never appear in searches

They are probably **scanned PDFs**, containing photographs of pages. Open
**Settings > My documents > Read scanned pages**.

## Discovery finds nothing

Check that **Allow searching the web** is enabled in **Settings > Finding articles online**.
When online search is disabled, Discovery explains this and offers **Allow
online search**.

## The server keeps running after closing the browser

If the browser closed unexpectedly, use `Stop-Bibliobrain.cmd`.

## Report a problem

Attach `logs\bibliobrain.log`: it contains neither keys nor passwords. The app's
**Report a bug** button shows exactly what will be sent before you submit. You
can also [open an issue](https://github.com/bibliOwl/bibliobrain/issues).

## Backup and restore

`Backup-Bibliobrain.cmd` archives the configuration, indexes, settings, histories
and draft. `Restore-Bibliobrain.cmd` restores them, creating a rollback backup
first. Encrypted API keys work only under the same Windows account. See
[Backups and maintenance](Maintenance-en.md) for backup and log details.
