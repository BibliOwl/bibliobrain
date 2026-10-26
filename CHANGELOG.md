# Changelog

All notable changes to Bibliobrain are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[SemVer](https://semver.org/).

The current version is defined once, in the `VERSION` file at the root of the repository.
It is read by `src/version.py`, `packaging/build-installer.ps1` and
`packaging/Bibliobrain.iss`. A release consists of:

1. running `python scripts/release_tools.py prepare X.Y.Z`, which updates `VERSION` and
   turns the "Unreleased" section below into a dated section;
2. committing, then creating and pushing an annotated tag `vX.Y.Z`;
3. on GitHub, `.github/workflows/release.yml` builds the installer, the ZIP package and
   `latest.json` and publishes the release, which the update check then offers. Elsewhere,
   `packaging/build-installer.ps1 -ReleaseBaseUrl <address of the release files>` produces
   the three files to upload by hand.

## [Unreleased]

## [0.9.0] - 2026-10-26

### Added

- Cartographies: a new "From papers" view, open by default. Pick one or more library
  papers (by searching the library, or in one go from the library selection, the sources
  of the last answer or a notebook, up to 60), and the citation graph used by the paper
  sheet and notebooks is drawn in the screen.
- Library: a "Citation graph" button in the selection bar builds that graph for the
  selected papers.

### Changed

- Cartographies: the "Themes" view is now called "Overview".
- Light performance profile: animations, transitions, shadows and blur are switched off
  entirely, and a streamed answer only re-renders the paragraph being written, so
  finished paragraphs (and any text selected in them) stay in place.
- Updates now follow the way Bibliobrain was installed. With the installer or the ZIP
  package, the new release is downloaded and verified in the background once daily checks
  are on, so the click only installs it. ZIP installs also get the manual steps as a
  fallback: a button to download the new ZIP and one to open the folder holding
  `Update-Bibliobrain.cmd`. With a Git clone, an "Update (git pull)" button is always
  offered; it is refused while tracked files have local changes, and
  `Update-Bibliobrain.cmd` refuses too.

### Removed

- Cartographies: the "Who cites whom" view (citations between library papers) and its
  `/api/library/internal-citation-graph` route.

### Fixed

- Chat: an answer cut short is no longer saved as a complete one. A stream from LM Studio
  or a remote provider that closed without its final chunk, an error after some text, and
  Stop, a context switch or leaving the page during an answer now save the text with an
  "incomplete" or "interrupted" notice.
- Literature review: Stop during the synthesis left the conversation with only the
  question while the model kept generating. The synthesis now stops and the review is
  saved with an "interrupted" notice.
- Chat: a citation labelled "Page 126" opened the PDF at page 125. It now opens at the
  page its card shows, older indexes included.
- Zotero: a change made in Zotero (an edited title, for example) stayed invisible until
  Zotero wrote it to its main database file. Changes still held in its journal are now
  read.
- Accessibility: while an answer is generated, screen readers announce the chat button as
  "Stop generation" (tooltip "Stop") instead of "Send message".

## [0.8.4] - 2026-10-25

### Fixed

- Chat: "Que dit Durand 2015 sur…" ("What does Durand 2015 say about…") was not recognised
  as a question about a library document (only "Durand (2015)" was). The name and year are
  now looked up in the library and the search is limited to that document. A broad question
  about one document ("what does it say", "summarise"…) gets its introduction, chapter
  openings and conclusion instead of neighbouring passages.
- Chat: chemical element names, formulas and acronyms in the answer are checked against the
  passages, like figures. An element missing from the sources ("iodine" for iron-based
  superconductors) is corrected by a model rewrite, otherwise flagged under the answer
  ("Terms to check"). The prompt also asks the model to keep technical names as the sources
  write them.
- Chat: every passage sent to the model is saved with the answer, not only the ones it
  cites, so an error can be traced back to its source.
- Discovery: "Add to a notebook" could not create a notebook. The dialog now offers
  "+ New notebook", named after the search, which receives the selected articles directly
  (also from the cartography). Only library articles enter a notebook, with their PDF,
  matched by DOI then by title; the others are listed with a link to the publisher, to get
  their PDF first.
- Notebooks: each source shows what the chat reads (the whole library PDF, or text added by
  hand), with "Read here" and "Library" for PDFs.
- Notebook chat: the suggested questions did not respond to clicks.
- Sidebar: an interrupted scan reading (OCR) hid a running library update, shrunk to "+1".
  The running task now keeps the status pill; the OCR stays in "+N" and in the Activity
  card, with its Resume button, and takes the pill back when nothing else is running.
- Library: a PDF with one page refused by pypdf (a large vector figure) was not indexed at
  all and came back as "new" on every refresh. It is read page by page: only the refused
  page is missing.
- Library: a file that became unreadable lost its already indexed passages and vanished
  from the answers. They are kept, the file is retried when it changes or after a
  Bibliobrain update, and the library card lists unreadable files with the reason.
- Zotero: PDFs without a parent record (standalone attachments) were not indexed, except
  those dropped through Bibliobrain. They now are, titled after the file name; a Settings
  option skips them ("Index PDFs without a Zotero record").
- Zotero: while Zotero was writing to its database (sync, import), the library could come
  back empty or fail. The read is retried from a copy; if it still fails, a message says
  Zotero is busy.
- Startup: Bibliobrain refused to start when port 7860 was already taken (for example by
  Stable Diffusion WebUI). It now takes a free port between 7861 and 7870 and remembers it.
- Startup: a relative path in the configuration (such as `./chroma_db_zotero`) was read from
  the folder Bibliobrain was started from; started from a shortcut, it then created an
  empty index. These paths are now read from the application folder.
- Reliability: custom prompts, caches and the list of dropped PDFs are written without risk
  of a truncated file, even when an antivirus holds the file. An update checks the free
  disk space before downloading and no longer leaves a partial package behind. A stopped
  literature review keeps what it had already written.

## [0.8.3] - 2026-10-24

### Fixed

- Chat: the input box stayed on one line with a long question. It now grows with the text,
  then scrolls beyond about 30% of the screen height.
- Settings › My documents: the "1 item to fix" badge did not say what to fix and clicking it
  did nothing. The item is shown under the title, with its button ("Prepare"…), and the
  badge leads to it. The text says whether the index only needs reloading or the indexing
  model has changed.
- Setup assistant: when the PDF folder is already configured (for example after changing AI
  provider), it no longer shows the welcome screen or the "Where are your PDFs?" question
  again. It goes straight to preparation and says the folder and settings are kept.
- Chat: the conversation now scrolls behind the input box, which floats above it (like
  ChatGPT or Claude), instead of being cut off sharply above it.
- Discovery › Compare: long article titles overflowed into the neighbouring table columns.
- New notebook: with Zotero, the two choice cards were stretched vertically.
- New notebook: from an open notebook, the creation dialog stayed invisible and blocked the
  whole interface.
- Chat and notebooks: automatic figure reading no longer starts for a question that mentions
  no figure, chart or table. Before, the LLM was consulted on every question and small
  models often asked for a useless reading, which unloads the chat model and slows the
  answer. The "Read figures" button still forces the reading.

### Security

- filelock moves to 4.2.4 (three vulnerabilities fixed), OpenTelemetry to 1.45.1 and
  setuptools to 84.2.1. filelock protects the Hugging Face model cache.

## [0.8.2] - 2026-10-22

### Fixed

- In-app update: it refused every installer with "The selected executable is not
  identified as a Bibliobrain installer". Inno Setup pads the exe's product name with
  spaces, which the check did not strip. An installation of 0.8.1 or earlier must still run
  the 0.8.2 installer by hand once.

### Security

- PDF download from article discovery: the connection goes to the public address already
  checked, which stops DNS rebinding from targeting a local address.
- Chat export, bug reports and citation graph: regular expressions with quadratic cost on
  some texts were replaced by linear processing.

## [0.8.1] - 2026-10-22

### Added

- Moving to another computer: index export and import are in Settings > Documents ("Move
  to another computer"). The export file also contains the indexed PDFs (a box to untick to
  leave them out, with their size shown and a FAT32 warning above 4 GB) and each document's
  record (title, authors, tags, collections, notes, annotations). On import, a short plan
  shows only what needs a decision; a single button downloads the missing embedding model
  and installs the index. Missing PDFs are copied without ever overwriting an existing file
  (into "Documents/Bibliobrain PDF" if no folder is chosen). If the computer has neither
  Zotero nor BibTeX, the imported records serve as the source and nothing is re-indexed. A
  progress bar follows the model download, the export, the copy and the installation. The
  first-run assistant offers "import its index" at the end of step 1.

### Fixed

- Reload preview impossible (error 500): a single unreadable .pptx, or one containing a
  large embedded object (pasted spreadsheet, video), made the preview, metadata and
  duplicate detection fail. Only the parts read by the parsers now count towards the size
  limit; embedded objects, fonts, images and thumbnails are ignored. An unreadable or
  oversized document no longer blocks the library scan.
- Zotero with ZotMoov and subfolders: loading Zotero no longer replaces the chosen PDF
  folder; detection reads `prefs.js` (custom data folder, linked-files folder suggested when
  most PDFs are linked); documents are matched to their record by path first, by file name
  only if it is unique; two "article.pdf" files from different folders no longer merge
  (they are read again once). Settings: "Back to the default Zotero" and a count of the
  documents found, subfolders included.
- Chat: "Find the articles written by <first name>" also finds entries with initials only
  ("Moreau, C.") when the library shows the same name with the full first name. "Only 3?"
  or "seulement 3 ?" extend the previous list instead of starting a new search, and a list
  taken from the metadata no longer claims to come from the index.
- Chat: "What are the main themes of my library?" and overview questions without a precise
  subject rely on the Cartographies: the library's 8 largest groups, each with a text
  passage from a representative article. Before, the search returned tables of contents and
  indexes, and small models answered that the themes could not be found or listed random
  titles. `qwen3.5` models are now told to group into 2 to 4 themes; "test" or "thanks" no
  longer re-run the previous overview.
- Moving to another computer, full disk: the import adds up everything that goes on the
  same disk (PDFs and index) before writing anything, instead of answering "Internal
  transfer error" halfway. If the disk fills up anyway, the message says the index was not
  replaced. The export refuses a destination that is too small.
- Update with the ZIP package: a window announces that `Update-Bibliobrain.cmd` has
  finished and offers to open Bibliobrain; during the update, the shortcut no longer starts
  it.
- Discovery: opening the model list made the search field disappear and the list was cut
  off by its frame. It is now shown in full.

### Security

- Pillow moves to 13.0.0 (eight vulnerabilities fixed) and certifi to 2026.10.19. Pillow
  reads the images extracted from PDFs.
- pypdf moves to 6.21.2 (five vulnerabilities fixed). pypdf reads the text of the library's
  PDFs.

## [0.8.0] - 2026-10-20

First public release.

### Added

- Chat: help on the software from the wiki shipped with the application. `/aide` or `/help`
  asks for it explicitly; without a command, only short questions about Bibliobrain itself
  go there ("How do I create a notebook?", "Where are my PDFs stored?", "Zotero is not
  detected"). A question that names a subject stays a question about the articles. Help
  answers have no sources and point to the wiki pages.
- An installer anyone can follow: a first page offers the "simple installation" (default),
  which downloads the AI recommended for the computer and the search module and says so
  clearly, or the "custom installation". The texts no longer use jargon (GiB, VRAM,
  runtime, PyTorch, reranking) and sizes are in GB.
- Installer: three lighter chat models (`gemma4:e2b`, `qwen3.5:2b`, `qwen3.5:0.8b`), marked
  "reduced quality" and never recommended. The models page says what is already installed
  (Ollama missing, no model, or the number of chat models present), lists these models so
  they can be picked without downloading anything, and also finds them when Ollama is not
  running (by reading the models folder). The list scrolls when there are many models.
- Installation: each release also publishes `Bibliobrain-Windows.zip`, the same files as the
  installer without any executable, for PCs whose antivirus quarantines the recent,
  unsigned `Setup.exe`. Extract it, then run `Install-Bibliobrain.cmd`. An installation made
  this way carries a `ZIP-PACKAGE.txt` file and updates with the next ZIP (checked by SHA-256
  from the application, or selected in `Update-Bibliobrain.cmd`), never with the installer.
  At the start of the installation, `install.ps1` removes the "downloaded from the
  Internet" mark from the extracted files.
- Chat: a question about the previous answer works on its sources. After a list of
  articles, "Sort these articles by approach", "Extract the topics and group them" or
  "Write an overall summary" answer from all the listed articles (title, authors, year,
  excerpt), with their original [Sx] labels, instead of starting a new six-passage search
  while the history only showed the model the first 8 sources. If everything does not fit
  in the model's window, the excerpts go first, then the answer says how many sources were
  left out. An export or a bibliography of "this list" keeps its tool.
- Faster chat with Ollama: the chat model keeps a single context window for all its calls,
  the largest the video memory allows next to the reranker and the embedding model
  (measured once per model on first launch). The tool planner, the automatic title, a long
  question or the Stop button no longer cause a model reload (about 3 s each). Models stay
  loaded for 30 minutes (`OLLAMA_KEEP_ALIVE_SECONDS`) and are preloaded when the
  application opens and when the model changes. The debug panel shows reloads, prompt size
  and generation speed.
- Chat: answers from qwen3.5 models are streamed, like those of other models, instead of
  appearing in one block once finished (first word in about 0.2 s instead of 1 to 1.5 s
  with qwen3.5:4b). Their citation labels are repaired while they are displayed, and the
  Stop button interrupts them without unloading the model.
- Chat: the figures in an answer are checked against the retrieved passages, with no extra
  model call. A value absent from every passage is flagged at the end of the answer
  ("Values to check"), and labels piled up after the last sentence are removed. Mostly
  useful with small local models.
- Installer without a terminal: `Bibliobrain-Setup.exe` asks all its questions in standard
  pages (Next, Next…), in French or English depending on Windows. An "AI models" page
  analyses the computer (RAM, video memory, models already in Ollama), preselects the
  recommended chat model and offers EmbeddingGemma. Components install in the background:
  the progress page shows the current step and the elapsed time. At the end, a "Launch
  Bibliobrain" box replaces starting from the console. Shortcuts (Start menu, optional
  Desktop) are created and removed by the installer. Silent installation:
  `/CHATMODEL=<model|none>` and `/EMBEDDING=1`. `install.ps1` can still be run by hand and
  accepts `-EmbeddingDownload ask|yes|no`.
- Update check, once a day: an anonymous request for a `latest.json` file published with
  each release (no identifier, versions compared locally). Nothing is sent before the user
  agrees: the startup assistant asks, and a single card asks existing installations.
  Setting and "Check now" button in Settings › Overview; the address can be replaced with
  `BIBLIOBRAIN_UPDATE_MANIFEST_URL`. New routes `/api/update/status`, `/api/update/check`
  and `/api/update/start`.
- One-click update: the installer is downloaded then checked by SHA-256 before
  `bibliobrain-manager.ps1 -Action update -FromApp`, which backs up the data (two
  `Bibliobrain-Backup-BeforeUpdate-*` backups kept), installs silently and restarts the
  server; the page reloads by itself. Reserved for the server started by its shortcut,
  never for a development server.
- Release tools: `scripts/release_tools.py` (`prepare`, `manifest`),
  `build-installer.ps1 -ReleaseBaseUrl` and the GitHub workflow `release.yml` triggered by a
  `v*` tag.

- `VERSION` becomes the single source of the application version; the interface shows the
  version returned by `/api/capabilities` instead of a hard-coded value.
- Application logging in `%LOCALAPPDATA%\bibliobrain\logs\bibliobrain.log` with rotation,
  level set by `BIBLIOBRAIN_LOG_LEVEL`.
- Test coverage measured with `pytest-cov` locally and in CI.
- Static analysis of the JavaScript with ESLint (configuration `eslint.config.js`).
- This CHANGELOG file.
- Writing: "Include sentences already cited" option, to find additional sources for them.
  Each analysed sentence is validated with the two sentences before it and the one after
  it, including around a selection or a re-run sentence; an anaphoric sentence ("These
  currents…") is searched together with the previous sentence.

- Query strings redacted in the Uvicorn access logs.
- Settings: "Fix this" makes scanned PDFs searchable from one card. It explains the three
  steps and the available memory, then chains installing the OCR model, enabling it and
  starting it; each blocker (Ollama stopped, download failed, indexing in progress, library
  not loaded) has its own card with what to do. New route `/api/ocr/inventory`, which
  counts the scanned pages without an OCR model, and stable error codes (`code`) on
  `/api/ocr/start`.
- EmbeddingGemma index (v1 and v2): questions and passages are embedded with the task
  prefixes recommended by Google. Existing indexes stay in plain text and switch to the
  prefixes at their next full rebuild (`embedding_prompts` field of the manifest).

### Changed

- Installer: the file carries its version in its name (`Bibliobrain-Setup-X.Y.Z.exe`), so
  you can tell which version a downloaded file installs.
- Installer: during large downloads (PyTorch, components, AI models), the installation page
  shows the amount downloaded and the estimated time left, with a real progress bar; the
  PyTorch unpacking that follows is announced as a separate step. The computer no longer
  goes to sleep during the installation.
- Installer: an NVIDIA card too old for the CUDA 13 build of PyTorch (before the Turing
  generation, for example a GeForce MX130) gets the CUDA 12.6 build directly, instead of
  downloading 2 GB for nothing; before Maxwell, the build without a graphics card.
- Settings: the update check and the "Check now" button move to Expert settings ›
  Diagnostics ("Version and updates" section). The "Update available" button in the
  sidebar leads straight there.
- Complete article lists. "List all my articles", "Which articles do I have?" or "How many
  articles in my library?" list the whole indexed library, once per article even when
  there are several copies of the PDF, newest first ("… from 2024" filters the year),
  without calling the model: the list used to find 0 articles. A list on a subject is no
  longer limited to 12 articles drawn from 32 passages: every article with a passage that
  stands out for the subject is scored by the reranker, then listed in two groups, "deal
  with" and "may be related". An absent subject lists nothing. A subject in French is
  translated by the chat model using the vocabulary of the library's titles. Without a
  reranker, the list checked by the model is still used.
- More accurate and faster chat search: a question is no longer mixed with the previous one
  unless it follows on from it ("And in that case?", "What are its limits?"); after a change
  of subject, 9 papers out of 10 are found instead of 6. The two copies of the same PDF no
  longer take two places among the candidates, so an answer again relies on 6 distinct
  passages. A question in French is searched in both languages then ranked once, and its
  translation happens during the search. The lexical score no longer recomputes word
  similarity for every passage of the same article. Articles found 75 → 79 out of 84;
  median search time 854 → 341 ms (question in French 1.3 → 0.9 s).
- Generation: the recommended repetition penalty goes from 1.2 to 1.0. It also penalised the
  figures and citation labels the answer must copy: on 54 test cases with three local
  models, 1.0 is never worse, qwen3.5:4b invents fewer values and answers twice as briefly.
  A 1.2 value saved automatically by the interface is replaced with the new one; a value
  chosen in the Settings is kept.
- Installer: assistant texts rewritten, in French and English. The "Before installing" page
  separates what is installed by default, what is offered and what will only be downloaded
  later on request, and gives the disk space to plan for. Installation steps are shown in
  the assistant's language instead of the script's English text. The licence acceptance
  page is gone: the AGPL requires no acceptance to use the software, and its text is still
  shipped in the installation folder.
- Hardened document reading: a size cap (768 MB, `BIBLIOBRAIN_MAX_DOCUMENT_MB`) and a
  per-format signature check before any parser, and text extraction in a child process with
  a time limit and a memory cap (can be turned off with `BIBLIOBRAIN_DOCUMENT_ISOLATION=0`).
  A booby-trapped file can no longer hang or crash the server; XML entity declarations are
  refused in Office/ODF/EPUB archives.
- Settings reorganised: an Overview (one status line per area, a button where an action is
  expected) and four areas with simple names: My documents, AI assistant, Find articles
  online, Appearance and language. Technical settings move to Expert settings, opened from
  the header, with a warning banner, each setting's default value and a Reset button per
  setting. The old tab names are still accepted.

- PyTorch moves to 2.14.0, which allows setuptools to move to 84.0.0 and removes the audit
  exception `PYSEC-2026-3447`. Since PyTorch no longer publishes a cu128 build, the
  installer tries cu130, then cu126, then the CPU. It reads the NVIDIA driver version: an
  RTX 50 card whose driver predates the 580 branch gets the CPU build and a message asking
  to update the driver. The GPU check now runs a real computation, which detects a PyTorch
  build that does not support the card.
- The managed server's runtime state (PID, logs) moves from the installation folder to
  `%LOCALAPPDATA%\bibliobrain\runtime` and `...\logs`. The old `.bibliobrain` folder is
  migrated automatically on first start.
- Silent exceptions (`except: pass`) are now logged at debug level.
- The `utc_now_iso` and `env_bool` helpers are centralised in `src/common.py`.
- The HTTP middlewares and the rate limiter are moved out of `app_api.py` into
  `src/http_security.py`.
- The installer no longer downloads any model without asking. From the start, before the
  long steps, it shows the table of chat models (`qwen3.5:9b` validated, `granite4.2:8b`
  and `qwen3.5:4b` with reservations) with, for each, how it runs on the machine (GPU,
  slow GPU+CPU, CPU only or too large), then asks which one to download; it also asks for
  `embeddinggemma`. Without an answer, nothing is downloaded. The automatic selection of
  `qwen3.5:9b` and the installation stopping on a machine that is too modest are gone.
- Discovery: result cards show the relevance estimated by the model (out of 100) without
  opening the details, a clickable DOI, and nothing when the year or DOI is missing.
  Without an open-access PDF, "Open at the publisher" becomes the main action instead of a
  greyed-out download button. Card labels are translated into English. The history only
  shows the date and the number of results (model, duration and sources in a tooltip). The
  "External disabled" pill is gone in favour of the single "Enable" banner.
- Discovery, search form: the "Keyword search" mode becomes "Describe a topic", since the
  search accepts sentences. The mode drop-down is replaced by two buttons, the number of
  results fits on the same line, and "Run the search" moves to the bottom-right corner of
  the input area. The placeholder text and the help adapt to notebook mode.

### Fixed

- Simple installation: on a computer where no recommended model fits (graphics card under
  5 GB, or under 12 GB of RAM without a graphics card), it downloaded no chat model, and the
  startup assistant stayed stuck on "no chat model is installed". It now takes
  `qwen3.5:2b`, including on a small graphics card (under 3 GB) where it spills into RAM as
  Ollama does by default. The application then warns once that this very small model can
  give wrong answers, and offers to connect an external API. It does not pick it by default
  when another model is installed. Below the minimum configuration (8 GB of memory and 4
  cores), the simple installation says so before installing without AI, and offers a
  remote provider (Cancel goes back to choosing the installation type). When the computer
  could not be analysed, it asks which AI to use instead of taking none, with `qwen3.5:2b`
  ticked.
- ZIP package: `Install-Bibliobrain.cmd` offers the same choices as the installer. Enter
  accepts the simple installation (the recommended AI and the search module), `C` opens the
  custom choice, with the same warnings. Without an answer, nothing is downloaded.
- Preparing PDFs without a chat model: it needs none (only the indexing model is used), but
  the startup assistant refused to start it. It now runs, with a warning and the command to
  install one; the chat then says the documents are ready and how to add a model or an
  external API.
- Startup assistant: "Ollama responds, but no chat model is installed" was also shown when
  the indexing model did not work or the model list could not be read. Each case now has
  its own message, with the details of the indexing model's error.
- Installer: run again while a previous installation was still running in the background
  (for example after closing the installer from the Task Manager), it started a second
  installation in the same folder. It now stops without changing anything and explains
  that you must wait for the first one to finish or restart the computer.
- Uninstall: started normally (`unins000.exe`, or Windows "Installed apps"), it stopped right
  after the confirmation on "Runtime error: Could not call proc" and removed nothing. The
  script wrote to the progress window before it existed; only the silent uninstall worked.
- First launch: the assistant prefilled the PDF folder with Bibliobrain's internal data
  folder, which does not exist yet on a fresh installation; "Continue" answered "folder not
  found". The field stays empty until a folder is chosen or used (`data_path_configured`).
- Folder and file pickers (Windows): the window could never appear and the button spin
  forever. They now use the Explorer dialog from a small Python process
  (`src/native_dialog_worker.py`) instead of a hidden PowerShell and the old "Browse for
  folder" tree; if no window is visible after 20 seconds, the interface says so and offers
  to type the path. Without a valid starting folder, the picker opens where the user was
  last time rather than in the installation folder.
- Chat: a message with no usable content (empty, punctuation or emojis only) gets a request
  for clarification before any RAG initialisation; short scientific expressions ("77 K",
  "∇", "α") are still accepted.
- Chat: a source carrying an explicit label no longer allows a different citation inferred
  from its position (a passage labelled S9 also validated `[S1]`), and the citation guide
  no longer presents S9 as a forbidden label.
- Installer: an update left in place the modules removed or renamed by the new version,
  which Python could still import; `src` and `static` are now emptied before the copy
  (`[InstallDelete]`).
- Installer: a failure of `install.ps1` (connection lost, disk full) went unnoticed, the
  assistant announcing a successful installation, and the one-click update too.
  `Bibliobrain-Setup.exe` now runs the script itself, shows the failure with the log path
  and returns exit code 20; the manager then offers `Repair-Bibliobrain.cmd`.
- One-click update: the console asked the chat model question again and waited up to five
  minutes. An update no longer touches Ollama or its models (`-SkipOllamaSetup`), for a
  package as for a Git clone.
- Update: a beta version (`0.8.0-beta.1`) was refused by `Update-Bibliobrain.cmd` and by the
  one-click update, and could not be replaced by the final version. The manager compares
  versions like the application.
- Installer: running `Bibliobrain-Setup.exe` again while Bibliobrain was running replaced
  the code under the server; it is now stopped before the copy.
- Uninstall: the program folder stayed in place because of `__pycache__` and
  `bibliobrain-install.log`; they are deleted.
- Startup: if port 7860 is taken by another program, the message names it instead of
  announcing a timeout; a server still loading is waited for (up to 3 minutes) instead of
  being doubled by a second one; the Desktop shortcut, which has no console, shows the
  error in a window.
- Installer: Python and the Visual C++ runtime download with the same retries as Ollama
  (`curl` then PowerShell, three attempts) and without the progress bar that slowed down
  PowerShell 5.1; free disk space is checked before a first installation; the stray
  `System.Management.Automation.RemoteException` lines are gone from the console. The
  assistant no longer announces a chat model "chosen automatically".
- Release: `release.yml` runs the package test (`test-installer-payload.ps1`) before
  publishing; this test also checks the arguments and exit code of `install.ps1`.
- Ollama models without a dedicated profile (for example `granite4.2`, `ornith-1.5`)
  reasoned even when thinking was off, because the `think` field was not sent: empty or
  very slow answers. `think=false` is now explicit, and the draft shown before an orphan
  `</think>` tag is removed from the visible answer.

- Installation impossible on a managed computer (domain account without
  `SeSecurityPrivilege`): `install.ps1` failed at the very end of the installation on
  `Set-Acl` (`PrivilegeNotHeldException`), which tries to rewrite the SACL. Hardening
  `configuration.json` now goes through `icacls` then the .NET API, which only change the
  DACL, and a failure becomes a warning instead of stopping the installation.
- Writing, narrow window: the citation search results were cut off with no way to scroll,
  and between ~820 and 980 px wide the side panel was squashed to a few pixels. When editor
  and panel are stacked, the area scrolls and a splitter adjusts the editor's height
  (double-click to go back to the default size).
- Discovery, narrow window: between ~820 and 1080 px with the sidebar collapsed, a large
  gap separated the history from the search. Stacking now follows the panel's actual
  width. On mobile, the history drawer (Discovery and Chat) was shown as a 100 px
  horizontal strip: it is a vertical list again. History collapsed around 1100 px: its two
  buttons no longer overflow the bar.

[Unreleased]: https://github.com/bibliOwl/bibliobrain/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/bibliOwl/bibliobrain/compare/v0.8.4...v0.9.0
[0.8.4]: https://github.com/bibliOwl/bibliobrain/compare/v0.8.3...v0.8.4
[0.8.3]: https://github.com/bibliOwl/bibliobrain/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/bibliOwl/bibliobrain/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/bibliOwl/bibliobrain/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/bibliOwl/bibliobrain/releases/tag/v0.8.0
