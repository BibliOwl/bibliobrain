# Privacy

English · [Français](Confidentialite.md) · [Guide home](Home-en.md)

**By default, your documents and their index stay on your computer.** Here is
what changes when you enable optional features.

## With the local setup

- Your PDFs, their text and their index stay on your computer.
- Chat questions and answers stay local while the AI assistant is set to
  **On this computer**.
- Bibliobrain reads Zotero's database without modifying it. A PDF dropped into
  Bibliobrain is handed to Zotero itself, which creates its record.

## What can leave your computer when enabled

| Option | What is sent | Where to enable it |
|---|---|---|
| **Online service** (remote AI) | Your questions and the excerpts needed to answer them, to your chosen provider | Settings > AI assistant |
| **Another computer nearby** | The same information, to that computer on your network | Settings > AI assistant |
| **Allow searching the web** | Keywords, titles and DOIs to OpenAlex, Crossref, Semantic Scholar, arXiv, HAL and others; never your PDFs | Settings > Finding articles online |
| **Check for updates daily** | One daily request to GitHub's release information, with no identifier or document information; GitHub sees your IP address | Settings > Expert settings > Diagnostics |
| Bug report | Your description, your contact email if you give one and the Bibliobrain version; diagnostics, the last 20 chat messages and a screenshot **only if you tick them**. The screen lists exactly what will be attached. The computer name is not sent | **Report a bug** button |

Bug reports go to a server run by the project's maintainer. Who is responsible for them,
how long they are kept and how to have them deleted: see the
[privacy policy](../PRIVACY.md).

The **Author or lab** map sends the searched name or identifier to OpenAlex.
If you configure remote indexing or move scan and figure processing to another
computer, document excerpts or page images are sent to that service. See
[Settings](Settings-en.md#ai-assistant).

## Your keys and data

- API keys are encrypted for your Windows account.
- Logs in `%LOCALAPPDATA%\bibliobrain\logs` contain neither keys nor full
  addresses. You can attach them to a report.
- By default, Bibliobrain listens only on your computer.

## Answer reliability

An AI answer can be wrong or incomplete. Bibliobrain checks citations against
your documents' actual text and removes those that are absent, but you remain
responsible for reading and checking the cited passage.
