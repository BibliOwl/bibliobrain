<a id="readme-top"></a>

<div align="center">
  <img src="static/logo_bibliobrain_1024.png" alt="Bibliobrain logo" width="140" height="140">

  <h3>Bibliobrain</h3>

  <p>
    <strong>Interact with your bibliography using AI.</strong>
    <br />
    A local, private research assistant: ask questions about your papers, explore and extend your references, and get answers with citations you can check.
  </p>

  <p>
    <strong><a href="https://www.bibliobrain.org/">Official website</a></strong>
    &nbsp;·&nbsp; <a href="https://github.com/bibliOwl/bibliobrain/releases/latest">Download</a>
    &nbsp;·&nbsp; <a href="wiki/Home-en.md">English wiki</a>
  </p>

  <p>
    English · <a href="README.fr.md">Français</a>
  </p>
</div>

<hr />

> **Alpha — under active development.** Features and the interface change quickly and bugs are likely. Keep your original documents, and check AI answers against the cited sources.

## What it does

Bibliobrain lets you **interact with your bibliography using AI**. It reads your PDFs and other documents (and your Zotero metadata, if you use Zotero), indexes them on your computer, and lets you question them, map them, extend them with new papers and cite them in your writing, all in plain language and with every answer traced back to its sources.

- **Ask your library.** Chat with your documents and open the exact passages cited in each answer. A sources panel and a provenance graph show what the answer was based on.
- **Work with or without Zotero.** Point Bibliobrain at a Zotero library or at a plain folder. Supported formats: PDF, DOCX, ODT, EPUB, PPTX and text files (`.txt`, `.md`, `.rst`, `.tex`, `.csv`, `.tsv`). Every format keeps its natural citation unit (page, section, chapter or slide).
- **Organise.** Group sources into thematic workspaces; question a single paper straight from the library.
- **Explore.** Library map and citation graphs (OpenAlex), plus a Discovery module to find and fetch papers from Semantic Scholar, OpenAlex, arXiv, HAL and Crossref.
- **Write.** A drafting tab suggests references for a selected passage and reviews your draft section by section.
- **Read scans and figures (optional).** Local OCR for scanned pages and a vision model to describe figures and tables.

The interface is available in English and French. Slash commands such as `/papers`, `/liste` and `/review` produce paper lists and structured literature reviews from the chat.

## Privacy

With the default local setup (Ollama + ChromaDB), your documents and their search index never leave your computer.

Some features are optional and talk to the Internet **only when you enable them**:

| Feature | What is sent |
| --- | --- |
| Remote AI provider (OpenAI-compatible) | Your questions and the document excerpts needed to answer them, to the provider you configured |
| Paper search (Discovery, OpenAlex, Semantic Scholar…) | Your search queries, once web search is enabled in the settings |
| Bug report | Your description, your contact email if you give one, the app version, plus only what you tick on the report screen: diagnostics, the last 20 chat messages, a screenshot. Nothing is sent without a click |
| Update check | If you accept it (asked at first launch, changeable in Settings › Expert settings › Diagnostics): one request per day to GitHub for the `latest.json` file of the latest release. Bibliobrain sends no identifier and no version; GitHub sees your IP address. Nothing is installed without your click |

Bug reports go to a server run by the maintainer; [PRIVACY.md](PRIVACY.md) says who is responsible, how long reports are kept and how to have them deleted.

Connecting Zotero never writes into its database or storage folder; a document you drop into Bibliobrain is handed to Zotero itself through its local connector. API keys are encrypted with Windows DPAPI and are never sent back to the browser. See [SECURITY.md](SECURITY.md) and the [settings guide](wiki/Settings-en.md).

## Installation

Bibliobrain runs on **Windows 10 or 11 (64-bit)**. An Internet connection is needed during setup. Python 3.14 and [Ollama](https://ollama.com) are installed for you if they are missing. Allow several GB of disk space for the Python dependencies (PyTorch) plus the size of the models you choose below.

### Option A — installer (recommended)

1. Download `Bibliobrain-Setup-X.Y.Z.exe` (X.Y.Z is the version number) from the [latest release](https://github.com/bibliOwl/bibliobrain/releases/latest).
2. Run it. The alpha builds are **not code-signed**, so Windows SmartScreen may warn you.
3. Follow the prompts: setup prepares the local components and asks which AI models to download.

By default, the installer places the application files and maintenance scripts (`Update-Bibliobrain.cmd`, `Backup-Bibliobrain.cmd`, `Restore-Bibliobrain.cmd` and `Uninstall-Bibliobrain.cmd`) in `%LOCALAPPDATA%\Programs\Bibliobrain` (usually `C:\Users\<username>\AppData\Local\Programs\Bibliobrain`). To open this folder, press **Win + R**, paste `%LOCALAPPDATA%\Programs\Bibliobrain` and press **Enter**. User data is stored separately in `%LOCALAPPDATA%\bibliobrain`.

### Option B — ZIP package (if your antivirus blocks the installer)

Some antivirus software (Symantec, for example) quarantines `Bibliobrain-Setup-X.Y.Z.exe` because it is new and unsigned. The ZIP package contains the same files without any executable.

1. Download `Bibliobrain-Windows.zip` from the [latest release](https://github.com/bibliOwl/bibliobrain/releases/latest).
2. Right-click the ZIP → **Properties** → tick **Unblock** → **OK**. Windows then stops warning before each launcher.
3. Extract it to a folder you will keep, for example `Documents`: you get a `Bibliobrain` folder.
4. Double-click `Install-Bibliobrain.cmd` in that folder and answer the questions in the window.

This installation updates from the next `Bibliobrain-Windows.zip`, never from the installer.

### Option C — Git clone

Install [Git](https://git-scm.com), then in PowerShell:

```powershell
git clone https://github.com/bibliOwl/bibliobrain.git
cd bibliobrain
.\Install-Bibliobrain.cmd
```

Use `git clone` rather than GitHub's "Download ZIP" (not the ZIP package above): updates need the `.git` folder.

### First launch

Open the **Bibliobrain** shortcut (Desktop or project folder). The server starts in the background and your browser opens on <http://localhost:7860>. A short two-step assistant asks for your documents folder and prepares the index; Ollama and the indexing model are checked for you. Closing the last Bibliobrain tab stops the server.

### Updating

Run **`Update-Bibliobrain.cmd`**. With a Git clone it pulls the new version; with the ZIP package it asks you to select the new `Bibliobrain-Windows.zip`, checks its version and takes a backup first; with the installer it asks you to select the new `Bibliobrain-Setup-X.Y.Z.exe`, checks its identity, version and signature, and takes a backup first. `Backup-Bibliobrain.cmd`, `Restore-Bibliobrain.cmd` and `Uninstall-Bibliobrain.cmd` sit next to it. User data lives in `%LOCALAPPDATA%\bibliobrain`, separate from the code, and is kept on uninstall.

For backups, logs and uninstallation, see the [maintenance guide](wiki/Maintenance-en.md). API keys are configured in [settings](wiki/Settings-en.md).

## Recommended models

Setup downloads nothing without asking. At the start it shows the table below, tells you how each model would run on your machine (fully on GPU, GPU + CPU, CPU only, or too large), then asks which one to download. Pressing Enter downloads none. It then asks separately about `embeddinggemma`, which is required to index documents.

| # | Model | Download | Status | Fully on GPU | CPU only |
| --- | --- | --- | --- | --- | --- |
| 1 | `qwen3.5:9b` | 6.1 GiB | validated | 8 GiB VRAM, 16 GiB RAM, 6 threads | 24 GiB RAM, 8 threads |
| 2 | `granite4.2:8b` | 5.0 GiB | with reservations | 6 GiB VRAM, 12 GiB RAM, 4 threads | 16 GiB RAM, 8 threads |
| 3 | `qwen3.5:4b` | 3.2 GiB | with reservations | 5 GiB VRAM, 8 GiB RAM, 4 threads | 12 GiB RAM, 6 threads |
| 4 | `gemma4:e2b` | 6.7 GiB | reduced quality | 8 GiB VRAM, 12 GiB RAM, 4 threads | 16 GiB RAM, 6 threads |
| 5 | `qwen3.5:2b` | 2.5 GiB | reduced quality | 3 GiB VRAM, 6 GiB RAM, 4 threads | 8 GiB RAM, 4 threads |
| 6 | `qwen3.5:0.8b` | 1.2 GiB | reduced quality | 2 GiB VRAM, 4 GiB RAM, 2 threads | 4 GiB RAM, 2 threads |

- **validated**: no critical failure on the in-house `library48` document benchmark and no blocking defect in a qualitative review.
- **with reservations**: no critical failure, but the review found weaknesses (`granite4.2:8b`: a miscalculated percentage; `qwen3.5:4b`: weak structured output).
- **reduced quality**: small models for modest computers. They failed `library48` (`gemma4:e2b`: five critical failures) or were not run through it; answers and citations are less reliable, and Setup never recommends them, except `qwen3.5:2b` when nothing better fits the computer (the app then warns that answers may be poor).

The installer also lists the chat models already in Ollama: choosing one of them downloads nothing. A model that does not fit in VRAM is split between GPU and CPU by Ollama: it works but answers much more slowly. The thresholds for every model but `qwen3.5:9b` are estimated from their size, not measured on every configuration. For unattended installs, `-ChatModel <name>` downloads a given model, `-ChatModel none` none, and `-SkipModelDownloads` skips every question.

You can also use any model served by Ollama, or any OpenAI-compatible endpoint (Settings › AI assistant).

`embeddinggemma` is the recommended **embedding model** (the one that indexes your documents; it did best in our tests, notably on French text), but it is not the only choice: other Ollama embedding models can be selected in the settings ("Indexing model"). Bibliobrain keeps a separate index for each embedding model, so you can switch back and forth without rebuilding. Remote embeddings from an OpenAI-compatible API are also available, but disabled by default because your document excerpts would then be sent to that API.

A local reranker re-orders the best passages after the vector search. It is chosen automatically (Qwen3-Reranker 0.6B on a CUDA GPU with at least 3 GiB of free VRAM, otherwise a small CPU cross-encoder) and can be changed in expert settings. Only models with pinned Safetensors weights, `trust_remote_code=False` and an Apache-2.0 or MIT licence are offered.

Model weights are not part of Bibliobrain and are not covered by its licence. Each model is downloaded under its own terms: `embeddinggemma`, for instance, is distributed under Google's Gemma Terms of Use. Check the licence on the model's Ollama page, or with `ollama show <model> --license`, before relying on a model, in particular for commercial use.

## Limits and feedback

Bibliobrain is a research aid. Answers can be wrong even when they carry citations, and quality depends on your hardware, the model you pick and the quality of your documents (scans especially). Windows is the only supported platform for now.

To report a problem, use the in-app bug report: describe what you did, what you expected and what happened, and review the attachments before sending (avoid confidential documents). You can also open an [issue](https://github.com/bibliOwl/bibliobrain/issues). Security problems: see [SECURITY.md](SECURITY.md).

## Documentation

Read the wiki in [English](wiki/Home-en.md) or [French](wiki/Home.md).

- [User guide](wiki/Home-en.md)
- [Installation and updates](wiki/Installation-en.md)
- [Backups, maintenance and uninstallation](wiki/Maintenance-en.md)
- [Changelog](CHANGELOG.md)

## Maintainer

Team roles: the project has a single maintainer, [bibliOwl](https://github.com/bibliOwl), who is committer, reviewer and approver. Contributions from others are reviewed and merged by the maintainer (see [CONTRIBUTING.md](CONTRIBUTING.md)).

Privacy: see the [privacy policy](PRIVACY.md). Bibliobrain only sends data off your computer for the features described there, and the update check and bug reports need your consent.

## Built with

FastAPI and Uvicorn on the backend; LangChain and ChromaDB for retrieval; Ollama or any OpenAI-compatible endpoint for models (official `openai` client with `instructor` for structured output); a plain HTML/CSS/JS frontend.

## License and contributing

Copyright (C) 2026 bibliOwl. Bibliobrain is released under the **GNU AGPL v3 only** (`AGPL-3.0-only`): see [LICENSE](LICENSE). It comes with no warranty. The Bibliobrain name and logo are not licensed under the AGPL: see [TRADEMARKS.md](TRADEMARKS.md). Third-party components keep their own licences: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Contributions are welcome: read [CONTRIBUTING.md](CONTRIBUTING.md). Non-trivial contributions require accepting the [CLA](CLA.md), which is non-exclusive and keeps open the possibility of dual open-source and commercial licensing.

<p align="right"><a href="#readme-top">Back to top</a></p>
