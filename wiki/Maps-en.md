# Maps

English · [Français](Cartographies.md) · [Guide home](Home-en.md)

The **Maps** tab helps you navigate your corpus and understand where chat answers
come from. Choose between four views at the top of the tab.

<!-- TODO screenshot: From papers view -->

## From papers

Pick one or more papers from your library, then choose **Build the graph** to see
their citation graph (see below) right in the tab.

- **Add a paper** searches the library by title, author or year; **Enter** adds
  the first result.
- Three buttons add the **Library selection**, the **Sources of the last answer**
  or the **Papers from a notebook** in one go.
- Up to 60 papers. **Change papers** goes back to the picker and keeps your choice.
- In the Library, select papers and choose **Citation graph** in the selection
  bar: the tab opens and the graph is built straight away.

This view needs online search through OpenAlex (see below).

## Overview

A map of your whole library by topic, useful for navigating a large corpus.

- **Search for a concept** filters the map.
- **Reset** returns to the overview.
- **Add** creates a notebook from the displayed papers.
- After adding many papers, use **⋯ (More actions) > Rebuild map** to rebuild it.

This is a navigation aid, rather than a graph of facts extracted by AI.

## Last answer

This view shows the chat question, retrieved papers and passages used to answer
it, including pages and excerpts. **RAG graph** under a chat answer opens it
directly. It is the easiest way to check what supports an answer.

## Author or lab

*(beta)* Search for an author or laboratory by name, ORCID or ROR, select the
correct identity, then choose **Build graph** to see a citation graph of their
publications.

## Citation graphs

You can open a citation graph for a paper from the Library, for a notebook, for
papers you pick (**From papers** view), or for an author or laboratory. These graphs use OpenAlex, so online search must be
allowed. Time runs from left to right:

- **One paper**: its references are on the left (**← What it cites**), the paper is
  in the centre, and works citing it are on the right (**Who cites it →**).
  Position indicates the year; point size indicates the total citation count.
- **Several papers** (picked papers, notebook or author): three bands show references, corpus
  papers and citing works on a shared timeline. Point size indicates the number
  of links in this graph.
- Filters at the top hide references, citing works or internal links. Use
  **Author or title…** to find a point.
- Click a point (**Cited reference**, **Citing work**…) to see its abstract and
  actions: **Add to notebook**, **View in library**, **Explore its citations**,
  **Search in Discovery**, or open the publisher's page.

A note identifies works without a date, which cannot be placed on the timeline,
and sources without a DOI, which are absent from the graph.
