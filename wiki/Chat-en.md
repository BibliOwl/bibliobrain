# Chat

English · [Français](Chat.md) · [Guide home](Home-en.md)

Use **Chat** to ask questions about your documents in plain language. Answers are
grounded in **passages retrieved** from your library, rather than relying solely
on the AI model's training.

<!-- TODO screenshot: answer with citations and the sources panel open -->

## Ask a question

Type your question and press **Enter**. For example:

- “What do my papers say about the effect of sedentary behaviour on memory?”
- “Which methods are used to measure attention in this corpus?”
- “What are the limitations of studies more than ten years old?”

If no paper answers the question, Bibliobrain says so instead of making one up.

The buttons beside the send arrow choose the search depth: **Auto** suits most
questions, **Fast** answers in a few seconds and **Deep** searches more widely for
difficult questions.

## Read an answer

- **Citations** in the text are clickable: they open the paper in the Library.
- **View sources** opens a side panel with the selected passages and their pages.
  A passage's similarity score does not mean it proves the answer: read the excerpt.
- **RAG graph** under an answer opens **Maps > Last answer**, showing which
  papers and passages were used (see [Maps](Maps-en.md)).
- Each answer has a **Copy** button.
- Models that expose their reasoning may display a *Thinking* block.

## Restrict the sources

In an existing conversation, open **Options > Filter by paper**, search for a
title (a few words are enough), an author or a year, then select the paper. The next answers will use
that paper without starting a new conversation. Only indexed papers included in
RAG are offered. Search ignores case and accents. The active filter appears above
the message box; click **×** to remove it or select another paper to replace it.

If Zotero is connected, **Options > Zotero filters**, below the message field,
limits chat to selected **tags** or **collections**. The panel's reset button
removes these filters.

For a question about a few specific papers, select them in the
[Library](Library-en.md), then choose **Ask in chat**. For focused work, create a
[Notebook](Notebooks-en.md): its chat searches only its sources.

## Help with Bibliobrain

You can also ask how to use the app: “How do I connect Zotero?”, “How do I create a
notebook?” or “How do I use Bibliobrain?”. To request help explicitly, begin with
`/help` or `/aide`, followed by your question.

The assistant consults relevant pages from the wiki bundled with the app and adds
links to its answer. This help works without indexed papers. The links open the
online wiki; the assistant reads the bundled excerpts locally.

Documentation is loaded on the first help request and then kept in memory.
Questions about your papers do not load this documentation or add a model call
for this feature. Help uses the AI service selected in Settings, just like other
chat answers.

## Quick commands

You can write naturally (“write a literature review on…”) or start your message
with a command:

| Command | What it does |
|---|---|
| `/papers …` or `/liste …` | List all papers in your library on a topic, with a short summary of each |
| `/review …` or `/revue …` | Literature review: overview, timeline, schools of thought, disagreements and synthesis |
| `/compare …` | Compare papers: methods, hypotheses, findings and limitations |
| `/evidence …` or `/matrice …` | Table of claims with their supporting evidence and limitations |
| `/factcheck …` or `/verify …` | Check a passage against your library, claim by claim |
| `/biblio …` | Bibliography of sources already cited in the conversation |
| `/origin …` or `/origine …` | Trace whom your library's papers credit with an idea by following their citations |
| `/md …` or `/latex …` | Export an answer or the conversation as Markdown or LaTeX |
| `/carnet …` | Create a notebook from the sources in the latest answer |
| `/discover …` | Search online for new papers (see [Discovery](Discovery-en.md)) |
| `/rag …` | Force a standard answer grounded in your library |

In an empty chat, suggestions (**Find**, **Synthesize**, **Compare**, **Cite**) fill
in these requests. **More suggestions**, or **Options** below the message field,
offers more: paper lists, reviews, evidence, bibliographies, Markdown and others.

## Trace the original source of an idea

Use `/origin` followed by an idea, or ask a question such as “Who introduced the
critical state model?”. Bibliobrain finds the paper in your library that discusses
it most, reads the relevant passage and checks which references it credits. If
one of those references is also in your library, Bibliobrain reads it in turn,
following the chain for a few levels. The answer gives the citation chain and the
**oldest credited source found**. Each paper read appears in Sources with its passage.

To start from a specific paper, restrict the question to that paper first. This
process runs on your computer. The result is not proof of originality: an idea
may predate the cited sources, and small models make attribution errors more
often. Always check the passages.

## Online search from chat

If your message looks like a paper search (“find papers on…”), Bibliobrain can
start an online search, **only if you have allowed it** in Settings. It then asks
whether the search should stay local or use the web.

## Conversations

Your conversations appear under **Chat** in the sidebar. **All conversations**
shows the full list with search. The **+** button beside **Chat** (**New chat**)
starts a fresh conversation.
