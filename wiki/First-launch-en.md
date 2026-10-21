# First launch

English · [Français](Premier-lancement.md) · [Guide home](Home-en.md)

On first launch, a **two-step** wizard prepares Bibliobrain. This can take a few
minutes to a few hours depending on your library's size. Full preparation happens
once; afterwards, only new documents need to be read.

<!-- TODO screenshot: setup wizard, step 1 -->

## Step 1: where are your PDFs?

Choose the folder containing your papers. Subfolders are included.

- **If Zotero is installed**, Bibliobrain detects it and suggests its PDF folder.
- **Other metadata source** lets you choose Zotero, a BibTeX file or no source.
  Without a metadata source, Bibliobrain uses information found in the files.

Supported formats: PDF, DOCX, ODT, EPUB, PPTX and text files (`.txt`, `.md`, `.rst`,
`.tex`, `.csv`, `.tsv`).

**Bibliobrain already set up on another computer?** Export its index there (Settings >
My documents > **Move to another computer**), then click **Import its index** at the
bottom of step 1 here. If the file holds your PDFs, no need to choose a folder: they are
copied into **Documents\Bibliobrain PDF**. The import replaces step 2.

## Step 2: prepare your papers

Bibliobrain checks that Ollama and the document search component are ready, then
reads your documents. If everything is installed, there is nothing to configure.
If something goes wrong, the wizard shows the command to run.

- **Explore while it prepares** closes the window without stopping the work.
- **Later** closes the wizard. It reappears on the next launch until preparation
  succeeds.

## Check whether it is ready

An indicator at the bottom of the sidebar shows your library's status:

| Status | Meaning |
|---|---|
| Not initialised | Your papers have not been prepared yet |
| Updating library | Preparation is in progress, with a percentage |
| Ready · N papers | Chat, analyses and writing are available |

Wait for **Ready**: answers would be incomplete before then. Click the indicator
to view detailed activity.

## Next: ask your first question

Open **Chat** and ask about your papers (see [Chat](Chat-en.md)). For guidance,
click **Take the tutorial** in **Settings > Overview**: it walks you through a real
question and checking a source. The **Getting started** list in the sidebar
suggests the next steps: check a source, insert a citation and create a notebook.

To add papers later, see [Library](Library-en.md#add-papers).

## What about scanned PDFs?

A scanned PDF is a photograph of pages: document search cannot read it directly.
Reading scans is not part of initial setup. When you want to make them searchable,
open **Settings > My documents > Read scanned pages**. The tool first counts the
affected pages without downloading anything, then guides you through the process
(a one-off 1.1 GB download; processing runs on your computer).

## Run the wizard again

At any time, open **Settings > Expert settings > Diagnostics > Open the assistant**.
In **Settings > Overview**, an incomplete row also shows **Configure**, **Prepare**
or **Fix**. To repeat the tab tour, use **Diagnostics > Replay the tab tour**.
