# Discovery

English · [Français](Decouverte.md) · [Guide home](Home-en.md)

The **Discovery** tab searches online for **new** papers and helps you explore
them. Sources include Semantic Scholar, OpenAlex, arXiv, HAL and Crossref, with
Unpaywall for open-access versions.

> **Before you start.** Enable **Allow searching the web** in **Settings > Finding articles
> online**, or click **Allow online search** when Discovery says online search is
> disabled. Only your query (keywords, titles, DOIs) is sent; your PDFs stay on
> your computer.

<!-- TODO screenshot: search results -->

## Find papers

Choose a starting point:

- **Describe a topic**: a few sentences, an abstract, keywords, authors or a DOI.
- **From a notebook**: choose the **Source notebook**. Its papers and notes are
  enough; additional instructions are optional.

Adjust the number of **Results** if needed (1 to 50), then click **Search**. Each
result explains why it matches your search.

## Sort and compare results

- Filter by **Year**, keep **Open access PDF**, or distinguish papers **Already
  present** in your library from those **Not found**.
- **Sort by** relevance, newest first or oldest first.
- Select up to 4 papers and choose **Compare**: a table extracts information from
  their abstracts (methods, findings…), with a supporting passage for each cell.
- **Find similar articles** starts a search from a result. **Add to a notebook…**
  saves it to a notebook, using its title and abstract only.

## Return to a search

Your searches appear under **Discovery** in the sidebar. **All searches** shows
them all. You can rerun, rename or delete a search; rerunning highlights **new**
papers since the previous search. In chat, `/discover …` does the same thing
(see [Chat](Chat-en.md)).

## Trends

The **Trends** view counts publications indexed by OpenAlex year by year for one
to three topics. Enter the **Topics to compare**, date range and publication
types, then choose **Analyze trends**. **Export CSV** downloads the figures.
This view requires an OpenAlex key (see below).

## Origins

The **Origins** view explores where an idea came from. Enter a concept, preferably
in English, the language of the bibliographic databases, then choose **Trace the origins**:

- **Roots of the topic**: among roughly 200 OpenAlex publications on the concept, find
  older references cited by many of them. A timeline shows the year and share of
  publications citing each reference. **Oldest root** identifies the earliest
  work cited by at least 10% of them. Only the concept is sent; web access and an
  OpenAlex key are required.
- **In your library**: the same citation chain as `/origin` in
  [Chat](Chat-en.md), built on your computer from your papers.

A frequently cited root is a likely foundational work, not proof of priority.
For a specific sentence in your text, **Original source?** in
[Writing](Writing-en.md) is more reliable.

## API keys (optional)

OpenAlex and Semantic Scholar work better with a free key. An OpenAlex key is
required for **Trends** and the topic roots in **Origins**. Enter keys in
**Settings > Expert settings > Keys & connections**. See [Settings](Settings-en.md).
