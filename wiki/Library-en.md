# Library

English · [Français](Bibliotheque.md) · [Guide home](Home-en.md)

The **Library** tab lists your documents. With Zotero or a BibTeX file, it also
shows metadata: authors, year, journal, DOI, tags and collections.

<!-- TODO screenshot: paper list and detail panel -->

## Add papers

There are three ways to add PDFs or other documents after initial preparation:

- **Drag and drop** files onto the Bibliobrain window.
- Use **⋯ (More actions) > Add files…** at the top of the Library.
- **Copy them into your PDF folder**, or **add them to Zotero** if you use its library.

Dropped files are copied to your PDF folder. With a Zotero library, only PDFs are
accepted: Bibliobrain hands them to Zotero, which retrieves their bibliographic record.

**Next, prepare them.** When you return to the Bibliobrain window, the indicator
at the bottom of the sidebar shows **N new PDFs**: click **Add** to read them. For
changed or removed PDFs, the button is called **Update**. Only affected documents
are processed, not the whole library.

## Browse

- **Search**: instantly filter by title, author, tag or collection.
- **Filter**: show a document type, Zotero collection, date range, papers with or
  without an analysis, and more.
- **Sort**: click column headers. Use **Columns** to choose which ones are visible.
- Select several papers to **Ask in chat** or **Add to notebook**.
- **⋯ (More actions) > Export the papers shown** exports Markdown or BibTeX.

## Open a paper

Click a paper to open a panel with three views:

| View | Contents |
|---|---|
| **Overview** | Full metadata and a **Citation graph** card showing citing and cited works (see [Maps](Maps-en.md)) |
| **Summary** | Choose an **Analysis type** (short or full summary, critical analysis, methods, conclusions), then **Generate analysis** |
| **Cite** | **Copy APA** or **Copy BibTeX** to paste the reference elsewhere |

**Read here** displays the PDF inside Bibliobrain. The paper's **⋯** menu offers
**Open PDF**, **Show in Zotero**, **Add to notebook** and **Citation graph**.

Analyses are **saved** rather than recalculated each time you open a paper.
**Edit prompt** in the Summary view changes the instructions for an analysis type.
These instructions are also available in **Expert settings > Analysis instructions**
(see [Settings](Settings-en.md)).

## Ask about a single paper

**Ask about this paper** limits chat to that PDF without creating a notebook.

## Clean up documents

When extracted PDF text is damaged (detached accents, ligatures) or metadata looks
suspicious (strange titles, likely duplicates), **Documents clean-up** in expert
settings helps you identify the problems.

## Without Zotero

Bibliobrain also works without Zotero. Missing metadata can be completed
automatically through Crossref, then OpenAlex, if you allow online search. Only
titles and DOIs are sent, never PDF contents.
