# Settings

English · [Français](Parametres.md) · [Guide home](Home-en.md)

The **Settings** button at the bottom of the sidebar opens settings, starting
with an **Overview**: one row per area, showing its status. Click a row to change
it. An incomplete row offers **Configure**, **Prepare** or **Fix**. Search at the
top of the panel covers all settings.

## Everyday settings

### My documents

- Change your **PDF folder** with **Change…**.
- Connect **Zotero**: choose **Use my Zotero library**, then **Detect Zotero** for
  automatic detection, or choose a BibTeX file.
- **Read scanned pages** makes PDFs containing photographs of pages searchable
  (see [First launch](First-launch-en.md)).

### AI assistant

**Where should the AI run?**

- **On this computer**: nothing leaves your PC.
- **Another computer nearby**: enter the address of a machine on your network.
- **Online service**: enter an OpenAI-compatible API's address and key, then
  choose **Save and connect**.

For the first two options, choose the **Software that runs the models**: Ollama
or LM Studio. On another computer, Ollama must accept network connections
(`OLLAMA_HOST=0.0.0.0`), and LM Studio needs “Serve on Local Network” enabled.

This choice applies only to answers. Document indexing, reading scanned pages and
figure analysis continue to use Ollama on this computer. To move those tools,
open **Expert settings > Indexing & figures**, under “Where the index is
computed” and “Where these tools run”. Scans and figures can use Ollama on another
computer: page images are then sent to it, and the models must be installed there.

Then choose **Which AI model answers?** and use **Run a quick test** to check the
connection.

### Finding articles online

- **Allow searching the web** is required for [Discovery](Discovery-en.md), completing
  missing metadata and citation graphs.
- Choose sources and optionally enter an Unpaywall email address.

### Look & language

Choose Français or English, and a **Dark**, **Light** or **System** theme.
**Reset panels** restores panels to their original widths.

**Take the tutorial**, at the bottom of Overview, guides your first question.

## Expert settings

**Expert settings** in the header opens technical settings. Use **← Everyday
settings** to return. A banner reminds you that these settings can reduce answer
quality; each value has a **Reset** button.

| Section | Purpose |
|---|---|
| Answer generation | Context window, temperature and *thinking* mode |
| Search quality | Number of passages, similarity threshold and reranker model |
| Indexing & figures | Embedding model, where the index is computed, figure analysis, OCR, where these tools run and index transfer |
| Documents clean-up | Poorly extracted text and suspicious metadata |
| Analysis instructions | Custom prompts for each analysis type |
| Keys & connections | API keys for OpenAlex, Semantic Scholar, Ollama, Hugging Face and others |
| Diagnostics | **Version and updates**, **Open the assistant**, **Replay the tab tour**, debug mode, maintenance and **Quit Bibliobrain** |

## API keys

Enter keys in the interface, never in a file. On Windows, keys are encrypted for
your account and are never sent back to the browser. Leaving a field empty keeps
the current value; **Clear** removes it.

| Key | Purpose |
|---|---|
| OpenAlex | Search and more reliable maps; required for Trends (free) |
| Semantic Scholar | Dedicated quota for discovery |
| Online LLM | Only if you choose **Online service**; enter it under **AI assistant** |
| Ollama | Only for a protected Ollama server |
| Hugging Face | Download quotas for reranker models |
