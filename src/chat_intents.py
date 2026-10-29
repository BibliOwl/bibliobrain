"""Lightweight intent detection for chat-triggered RAG modes."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

ChatMode = Literal[
    "rag",
    "app_help",
    "literature_review",
    "paper_list",
    "document_export",
    "article_discovery",
    "paper_compare",
    "evidence_matrix",
    "bibliography",
    "fact_check",
    "origin_trace",
    "notebook_create",
    "drafting",
]
ExportFormat = Literal["markdown", "latex"]
ExportScope = Literal["previous", "chat"]


@dataclass(frozen=True)
class ChatIntent:
    mode: ChatMode
    query: str
    explicit: bool = False
    export_format: ExportFormat | None = None
    export_scope: ExportScope = "previous"


_COMMAND_RE = re.compile(
    r"^\s*/(?P<cmd>rag|revue|review|liste|papers?|discover|discovery|articles?|"
    r"md|markdown|tex|latex|compare|comparaison|evidence|preuves?|matrix|matrice|"
    r"bib|biblio|bibliographie|bibliography|factcheck|fact-check|verify|verifie|"
    r"notebook|carnet|draft|redige|help|aide|origine|origin)\b[:\s-]*(?P<rest>.*)$",
    re.I | re.S,
)

_LITERATURE_REVIEW_PATTERNS = (
    "revue de litterature",
    "literature review",
    "etat de l art",
    "state of the art",
    "synthese bibliographique",
    "bibliographic synthesis",
)

_PAPER_NOUN_RE = re.compile(
    r"\b(?:article|articles|papier|papiers|paper|papers|publication|publications|"
    r"etude|etudes|study|studies)\b"
)
_LIST_VERB_RE = re.compile(
    r"\b(?:liste|lister|list|show|donne|dresse|trouve|cherche|find|recense|cite|citer)\b"
)
_EXHAUSTIVE_RE = re.compile(
    r"\b(?:tout|tous|toutes|all|every|exhaustif|exhaustive|complete|complet|complets|completes)\b"
)
_EXPORT_VERB_RE = re.compile(
    r"\b(?:cree|creer|create|genere|generer|generate|exporte|exporter|export|"
    r"sauvegarde|sauvegarder|save|mets|mettre|transforme|transformer|convertis|convertir)\b"
)
_DISCOVERY_VERB_RE = re.compile(
    r"\b(?:cherche|cherches|chercher|recherche|rechercher|trouve|trouver|find|search|"
    r"show|montre|donne|recense|identify|identifie|suggest|suggeste|recommend|recommendations?)\b"
)
_DISCOVERY_RELATION_RE = re.compile(
    r"\b(?:sur|au\s+sujet\s+de|en\s+rapport\s+avec|lies?\s+a|li(?:e|es|ee|ees)\s+a|"
    r"related\s+to|about|on|regarding|qui\s+parlent\s+de|pour\s+ce\s+sujet)\b"
)
_DISCOVERY_REFERENTIAL_RE = re.compile(
    r"\b(?:ce\s+sujet|ce\s+theme|cette\s+question|cette\s+discussion|this\s+topic|this\s+subject)\b"
)
_DISCOVERY_NEW_RE = re.compile(
    r"\b(?:nouveaux|nouvelles|nouvel|nouvelle|autres|recents?|recentes?|"
    r"new|other|more|recent|latest|additional|externes?|external|web|internet)\b"
)
_WHICH_PAPERS_RE = re.compile(
    r"^(?:(?:ok|et|and|alors|so|bon)\s+)*(?:quels?|quelles?|which|what)\s+"
    r"(?:sont\s+les\s+|are\s+the\s+|of\s+my\s+|de\s+mes\s+)?"
    r"(?:articles?|papiers?|papers?|publications?|etudes?|studies)\s+"
    r"(?:(?:qui|that|do|does|in\s+my\s+library|de\s+ma\s+bibliotheque)\s+)*"
    r"(?:parlent|parle|traitent|traite|mentionnent|mentionne|abordent|aborde|concernent|"
    r"portent|talk|talks|discuss|discusses|mention|mentions|cover|covers|deal|deals|"
    r"address|addresses|are\s+about|sont\s+sur|about|on|sur)\b"
)
# "Quels articles ai-je ?", "Combien d'articles dans ma bibliothèque ?",
# "Montre ma bibliothèque": the library itself, which plain RAG answered from
# six passages.
_LIBRARY_CATALOGUE_RE = re.compile(
    r"\b(?:quels?|quelles?|which|what)\s+(?:sont\s+les\s+|are\s+the\s+)?"
    r"(?:articles?|papiers?|papers?|publications?|documents?|pdfs?)\s+"
    r"(?:ai[\s-]je|j\s?ai|do\s+i\s+have|i\s+have|y\s+a[\s-]t[\s-]il|sont\s+dans|are\s+in|"
    r"dans\s+ma\s+bibliotheque|in\s+my\s+library)\b|"
    r"\bcombien\s+d\s*(?:articles?|papiers?|publications?|documents?|pdfs?)\b|"
    r"\bhow\s+many\s+(?:papers|articles|publications|documents|pdfs)\b|"
    r"\b(?:montre|affiche|liste|show|list|display)(?:[\s-]+moi|\s+me)?\s+"
    r"(?:toute\s+|all\s+of\s+|the\s+whole\s+)?(?:ma|my)\s+(?:bibliotheque|library)\b"
)
_DRAFTING_RE = re.compile(
    r"\b(?:redige|rediger|ecris|ecrire|reecris|reecrire|reformule|reformuler|"
    r"traduis|traduire|compose|composer|write|rewrite|draft|rephrase|translate)\b.{0,120}?"
    r"\b(?:introduction|conclusion|discussion|abstract|resume|paragraphe|paragraph|"
    r"section|texte|text|passage|article|paper|plan|outline|ceci|cela|ca|this|it)\b"
)


def chat_request_text(message: str) -> str:
    """Separate the leading request from pasted context for routing only.

    Keep the original message intact for generation. In particular, headings and
    verbs inside an article outline must never become chat tool instructions.
    """
    raw = str(message or "").strip()
    raw = re.sub(r"^(?:bonjour|salut|hello|hi|merci|thanks|ok|d['’]accord)[.!]\s+", "", raw, flags=re.I)
    return re.split(
        r"\n\s*\n|\n(?=\s*(?:titre|title|plan|contexte|context|texte|text|"
        r"donnees|données|notes|#{1,6}\s|\d+[.)]\s))|"
        r"(?<=[.!?])\s+|\b(?:contexte|context|texte|text|plan|notes)\s*:",
        raw,
        maxsplit=1,
        flags=re.I,
    )[0].strip()


def _is_drafting_request(normalized: str) -> bool:
    if re.match(r"^(?:comment|how|qui|who)\b", normalized):
        return False
    action = _DRAFTING_RE.search(normalized) or re.search(
        r"\b(?:fais|faire|prepare|preparer|genere|generer|generate)\b.{0,60}?"
        r"\b(?:introduction|conclusion|abstract|paragraphe|paragraph)\b", normalized
    )
    return bool(action and not re.search(r"\b(?:ne|pas|not|never)\b", normalized[:action.start()]))


_AUTHOR_NAME_HINT_RE = r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'`-]{1,}"
_AUTHORSHIP_PHRASE_RE = re.compile(
    r"\b(?:ecrit|ecrite|ecrits|ecrites|publie|publiee|publies|publiees|"
    r"written|authored|published)\s+(?:par|by)\b"
)
_AUTHOR_FILTER_HINT_RE = re.compile(
    r"\b(?:author|auteur)\s*:|" + _AUTHORSHIP_PHRASE_RE.pattern
)
_RAW_AUTHOR_FILTER_HINT_RE = re.compile(
    rf"\b(?:par|by|de|d['’])\s+{_AUTHOR_NAME_HINT_RE}\b|"
    rf"\b{_AUTHOR_NAME_HINT_RE}\s+(?:a|à|ont|has|have)\s+"
    rf"(?:travaille|travaillé|travailler|worked)\s+(?:sur|on|about)\b"
)
_MARKDOWN_RE = re.compile(r"\b(?:md|markdown)\b")
_LATEX_RE = re.compile(r"\b(?:tex|latex|la\s*tex)\b")
_COMPARE_RE = re.compile(
    r"\b(?:compare|comparer|comparaison|contraste|contraster|difference|differences|"
    r"compare\s+these|compare\s+ces)\b"
)
_COMPARE_TARGET_RE = re.compile(
    r"\b(?:approche|approches|approach|approaches|methode|methodes|method|methods|"
    r"formulation|formulations|modele|modeles|model|models|resultat|resultats|"
    r"result|results)\b"
)
_EVIDENCE_MATRIX_RE = re.compile(
    r"\b(?:matrice\s+de\s+preuves?|evidence\s+matrix|tableau\s+de\s+preuves?|"
    r"preuves?\s+en\s+tableau|matrix\s+of\s+evidence)\b"
)
_BIBLIOGRAPHY_RE = re.compile(
    r"\b(?:bibliographie|bibliography|references?|ref(?:e|é)rences?|bibtex|apa)\b"
)
_CITED_SOURCE_RE = re.compile(
    r"\b(?:sources?\s+citees?|sources?\s+cit(?:e|é)es?|cited\s+sources?|"
    r"sources?\s+de\s+(?:cette|la)\s+(?:conversation|reponse|réponse|discussion))\b"
)
_FACT_CHECK_RE = re.compile(
    r"\b(?:fact\s*check|fact-check|verifie|verifier|v(?:e|é)rifie|v(?:e|é)rifier|"
    r"controle|contr(?:o|ô)le|valide|valider)\b"
)
# "Est-ce le papier original ?", "qui a introduit le modèle de Bean ?",
# "who first proposed X?": the source that introduced an idea.
_ORIGIN_RE = re.compile(
    r"\b(?:"
    r"(?:papiers?|articles?|sources?|publications?|references?)\s+(?:d\s+)?origin(?:e|al|ale|aux|el|elle)s?|"
    r"origin(?:e|al)\s+(?:papers?|articles?|sources?|publications?)|"
    r"(?:origine|source)\s+de\s+(?:l\s+|la\s+|ce\s+|cette\s+)?(?:idee|concept|notion|modele|methode|resultat)|"
    r"qui\s+a\s+(?:le\s+premier\s+|la\s+premiere\s+|en\s+premier\s+)?(?:introduit|propose|invente|formule|decouvert)|"
    r"(?:introduite?s?|proposee?s?|decrite?s?|formulee?s?)\s+pour\s+la\s+premiere\s+fois|"
    r"who\s+(?:first\s+)?(?:introduced|proposed|coined|invented|discovered|formulated)|"
    r"first\s+(?:introduced|proposed|described|coined|formulated)"
    r")\b"
)
_NOTEBOOK_CREATE_RE = re.compile(
    r"\b(?:cree|creer|create|genere|generer|mets|mettre|ajoute|ajouter|"
    r"range|ranger|build|make)\b.*\b(?:carnet|notebook)\b|"
    r"\b(?:carnet|notebook)\b.*\b(?:depuis|from|avec|with|sources?)\b"
)
_CHAT_SCOPE_RE = re.compile(
    r"\b(?:chat|conversation|discussion)\b|"
    r"\b(?:tout|tous|toute|complete|complet|entier|entiere|all|whole)\s+"
    r"(?:le\s+)?(?:chat|conversation|discussion)\b"
)
_PREVIOUS_SCOPE_RE = re.compile(
    r"\b(?:precedent|precedente|dernier|derniere|last|previous|ca|cela|ceci|this)\b"
)


_DEEP_RETRIEVAL_RE = re.compile(
    r"\b(?:revue|review|synthese|synthesis|etat de l art|state of the art|"
    r"exhaustif|exhaustive|exhaustivement|approfondi|approfondie|approfondir|"
    r"detaille|detaillee|en detail|en profondeur|compare|comparer|comparaison|"
    r"complet|complete|tous les|toutes les|litterature|literature|"
    r"main themes?|major themes?|key themes?|primary themes?|"
    r"themes? principaux?|themes? principales?|principaux themes?|principales themes?)\b"
)

_LIBRARY_OVERVIEW_RE = re.compile(
    r"\b(?:"
    r"(?:main|major|key|primary|dominant|principal|principaux|principale|principales)\s+"
    r"(?:themes?|topics?|subjects?|research areas?|domaines?|axes?)|"
    r"(?:themes?|topics?|domaines?|axes?)\s+"
    r"(?:main|major|key|primary|dominant|dominants|dominantes|principal|principaux|principales)|"
    r"(?:overview|vue d ensemble|portrait)\s+(?:of|de|du|des)\s+"
    r"(?:my\s+)?(?:library|collection|corpus|bibliotheque)"
    r")\b"
)


def is_library_overview_request(message: str) -> bool:
    """Return whether a prompt asks for a thematic overview of the collection."""

    return bool(_LIBRARY_OVERVIEW_RE.search(_normalize(message)))


_PLURAL_PAPER_NOUN_RE = re.compile(
    r"\b(?:articles|papiers|papers|publications|etudes|studies|documents|pdfs?)\b"
)


_SELECTION_SUMMARY_RE = re.compile(
    r"\b(?:resume[rsz]?|resumons|synthese|synthetise[rsz]?|summari[sz]e|summary|summaries"
    r"|overview|tl\s?dr|de quoi parlent|what are (?:these|they) about)\b"
)
# Words a bare "summarize these papers" request is made of. Anything else is
# a topic ("résume les résultats sur le courant critique"), which the
# similarity search must keep.
_SELECTION_SUMMARY_FILLER = frozenset(
    """
    resume resumer resumes resumez resumons synthese synthetise synthetiser synthetisez
    summarize summarise summary summaries overview tl dr de quoi parlent parle what are about
    moi me m nous peux peut pouvez tu vous stp svp merci please can could you give a an the
    fais faire fait donne donner une un des les le la l ces ce cet cette mes my these those them
    they this of for me pdf pdfs article articles papier papiers paper papers document documents
    publication publications etude etudes study studies source sources texte textes carnet notebook
    selection selectionnes choisis chacun chacune chaque each one by un par en in quelques few
    lignes lines phrases sentences mots words bref breve brief brievement briefly court courte short
    rapide rapidement quickly detaille detaillee detailed complet complete global globale general
    francais anglais french english et and puis then
    paragraphe paragraphes paragraph paragraphs du de d per pour with avec sa son ses leur leurs
    its their separement separately cite citer cites citez citation citations reference references
    liste list puces bullet bullets points deux trois two three
    """.split()
)


def is_selection_summary_request(message: str) -> bool:
    """"Résume-moi ces PDF": a summary of the chosen articles, not a topical question.

    Only meaningful when several articles are selected: the words of such a
    request say nothing about the papers, so a similarity search on them
    picks passages at random.
    """
    normalized = _normalize(message)
    if not _SELECTION_SUMMARY_RE.search(normalized):
        return False
    words = re.findall(r"[a-z]+", normalized)
    return not [word for word in words if word not in _SELECTION_SUMMARY_FILLER]


def is_multi_paper_comparison_request(message: str) -> bool:
    """"Compare ces articles": a comparison between several papers, not inside one."""
    normalized = _normalize(message)
    return bool(_COMPARE_RE.search(normalized) and _PLURAL_PAPER_NOUN_RE.search(normalized))


def detect_retrieval_depth(message: str) -> str:
    """Auto-classify a query as ``"deep"`` (synthesis/exhaustive) or ``"quick"``."""
    normalized = _normalize(message)
    return (
        "deep"
        if (_DEEP_RETRIEVAL_RE.search(normalized) or _LIBRARY_OVERVIEW_RE.search(normalized))
        else "quick"
    )


def resolve_retrieval_depth(message: str, requested: str | None) -> str:
    """Resolve the retrieval tier, honoring a manual override over auto-detect.

    ``requested`` of ``"quick"``/``"deep"`` forces that tier; anything else
    (``"auto"``, empty, None) falls back to :func:`detect_retrieval_depth`.
    """
    choice = str(requested or "auto").strip().lower()
    if choice in {"quick", "deep"}:
        return choice
    return detect_retrieval_depth(message)


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower()
    text = re.sub(r"['’`]", " ", text)
    text = re.sub(r"[^a-z0-9/_\s-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_LIBRARY_CITATION_RE = re.compile(
    r"\b(?:citations?|citer|cite|references?|sources?|bibliographie|bibliography)\b"
)
_NO_CITATION_RE = re.compile(
    r"\b(?:sans|without|no|pas\s+de)\s+(?:les?\s+|des\s+|any\s+)?"
    r"(?:citations?|references?|sources?)\b"
)
_CITATION_ADD_RE = re.compile(
    r"\b(?:ajoute|ajoutes|ajouter|ajoutez|rajoute|rajoutes|rajouter|rajoutez|mets|mettre|"
    r"insere|inserer|integre|integrer|inclus|inclure|appuie|appuyer|sourcer|cite|citer|"
    r"add|insert|include|support)\b"
)


def requests_library_citations(message: str) -> bool:
    """Return whether the leading request asks for citations from the library.

    Only the request is inspected: a pasted outline ending with "Références"
    must not trigger a library search.
    """
    normalized = _normalize(chat_request_text(message))
    return bool(
        _LIBRARY_CITATION_RE.search(normalized) and not _NO_CITATION_RE.search(normalized)
    )


def is_draft_citation_followup(message: str, previous_messages: list[dict] | None) -> bool:
    """Detect "add citations" right after a drafted answer.

    Without history this reads as a plain RAG question, whose retrieval query
    ignores the draft and answers that the sources do not cover the topic.
    """
    normalized = _normalize(chat_request_text(message))
    if not (requests_library_citations(message) and _CITATION_ADD_RE.search(normalized)):
        return False
    for row in reversed(list(previous_messages or [])):
        if str(row.get("role") or "") != "assistant":
            continue
        meta = row.get("meta")
        return isinstance(meta, dict) and meta.get("mode") == "drafting"
    return False


@dataclass(frozen=True)
class PaperListFollowup:
    """A short reply to a paper list that asks for the same list again or another topic."""

    query: str
    excluded_sources: tuple[dict, ...] = ()


_PAPER_LIST_TOOL = "search.local_articles"
# "Est-ce qu'il y en a d'autres ?", "tu peux tout citer ?", "any others?": every
# word must come from this vocabulary, so "d'autres articles sur le quench ?"
# (a new topic) is not mistaken for a continuation of the previous list.
_LIST_CONTINUATION_MARKER_RE = re.compile(
    r"\b(?:autres?|encore|suite|reste|plus|tout|tous|toutes|more|other|others|"
    r"else|rest|all|continue|continuer|continues|"
    # "seulement 3 ?", "only three?", "il n'y en a que 3 ?" doubt the count.
    r"seulement|only|juste|just|uniquement|y en a que?)\b"
)
_LIST_CONTINUATION_WORDS = frozenset(
    "a ai as au autre autres aux avez avoir c ca ce cela ceux celles cite citer cites citez d de des donne "
    "donner donnes du en encore est et existe il ils j je l la le les liste lister listes "
    "listez me moi montre montrer n ne non ok oui on peut peux pourrais pouvez plus qu que "
    "quels quelles reste s suite t tout tous toutes tu un une vous y "
    "articles article papiers papier publications publication etudes references sources "
    "all and any anything are can cite continue could else give have is it keep list me "
    "more of ok other others please rest show some the them there they what yes you "
    "continuer continues "
    "papers paper studies "
    "seulement juste uniquement que only just that those "
    "deux trois quatre cinq six sept huit neuf dix two three four five seven eight nine ten"
    .split()
)
_LIST_CORRECTION_RE = re.compile(
    r"^\s*(?:(?:non|no|nope|pardon|sorry|oups|oops)\b[\s,.!:;-]*)?"
    r"(?:je\s+voulais\s+dire|je\s+veux\s+dire|je\s+parlais|je\s+pensais|plut[oô]t|"
    r"i\s+meant|i\s+mean|i\s+was\s+talking\s+about|rather)\b[\s,:]*"
    r"(?:(?:des|du|de|aux|au|à|a|sur|about|of|on)\s+|d['’]\s*)?"
    r"(?:(?:les|la|le|the)\s+|l['’]\s*)?(?P<topic>.+?)\s*[.!?]*\s*$",
    re.I | re.S,
)


def _last_paper_list_query(row: dict) -> str:
    meta = row.get("meta")
    if not isinstance(meta, dict):
        return ""
    calls = (meta.get("tool_plan") or {}).get("calls") or []
    call = calls[0] if calls and isinstance(calls[0], dict) else {}
    if call.get("name") != _PAPER_LIST_TOOL:
        return ""
    return str((call.get("arguments") or {}).get("query") or "").strip()


def _is_list_continuation(message: str) -> bool:
    words = _normalize(chat_request_text(message)).replace("-", " ").split()
    return (
        0 < len(words) <= 10
        and bool(_LIST_CONTINUATION_MARKER_RE.search(" ".join(words)))
        and all(word in _LIST_CONTINUATION_WORDS or word.isdigit() for word in words)
    )


def paper_list_followup(
    message: str, previous_messages: list[dict] | None
) -> PaperListFollowup | None:
    """Resolve "d'autres ?" or "non, je voulais dire X" right after a paper list.

    Without this, the reply reaches plain RAG: its retrieval query has no topic,
    so the answer cites unrelated passages and loses the papers already listed.
    A continuation repeats the previous query without the papers already shown;
    a correction lists papers about the corrected topic.
    """
    rows = [row for row in previous_messages or [] if isinstance(row, dict)]
    assistant_rows = [row for row in rows if str(row.get("role") or "") == "assistant"]
    if not assistant_rows:
        return None
    previous_query = _last_paper_list_query(assistant_rows[-1])
    if not previous_query:
        return None
    correction = _LIST_CORRECTION_RE.match(chat_request_text(message))
    if correction:
        topic = correction.group("topic").strip(" \"'`“”‘’«»")
        if (
            topic
            and len(topic.split()) <= 12
            and re.search(r"\w{2,}", topic)
            and not _is_list_continuation(topic)
        ):
            return PaperListFollowup(query=topic)
    if not _is_list_continuation(message):
        return None
    excluded: list[dict] = []
    for row in reversed(assistant_rows):
        if _last_paper_list_query(row) != previous_query:
            break
        sources = (row.get("meta") or {}).get("sources") or []
        excluded.extend(source for source in sources if isinstance(source, dict))
    return PaperListFollowup(query=previous_query, excluded_sources=tuple(excluded))


# "Classe ces articles par thème", "résume cette liste", "lesquels parmi eux...":
# the question is about the previous answer's sources, not a new search.
_PREVIOUS_ANSWER_REF_RE = re.compile(
    r"\b(?:ces|those|these)\s+(?:\d+\s+)?(?:articles?|papiers?|papers?|publications?|"
    r"documents?|sources?|etudes?|studies|references?|travaux|resultats?|results)\b|"
    # "la liste des articles de X" asks for a new list.
    r"\b(?:cette|la|this|the)\s+(?:liste|list)\b(?!\s+(?:des?|du|d|of)\b)|"
    r"\b(?:ceux|celles)[\s-]+(?:ci|la)\b|\bparmi\s+(?:eux|elles|ceux|celles|them|these)\b|"
    # Without accents "cite"/"liste" is also the imperative ("cite-moi les
    # articles de X"), so the participle needs the noun it qualifies.
    r"\b(?:articles?|papiers?|papers?|publications?|documents?|sources?|etudes?|"
    r"references?|travaux|ceux|celles)\s+(?:deja\s+)?(?:listes?|cites?|mentionnes?)\b|"
    r"\b(?:listees?|citees?|ci[\s-]dessus|plus\s+haut|above|listed|"
    r"you\s+(?:listed|cited|found)|"
    r"(?:tu|vous)\s+(?:m\s+|nous\s+)?(?:as|avez)\s+(?:listes?|cites?|trouves?|mentionnes?))\b|"
    r"\b[a-z]+-(?:les|leur|leurs)\b|"
    r"\b(?:group|sort|classify|summari[sz]e|rank|compare|order|list)\s+them\b"
)
_ANSWER_ANALYSIS_RE = re.compile(
    r"\b(?:resume|resumer|resumes|resumez|synthese|synthetise|synthetiser|classe|classer|"
    r"classez|classement|regroupe|regrouper|groupe|grouper|categorise|categoriser|categories|"
    r"trie|trier|themes?|sujets?|thematiques?|tendances?|compare|comparer|"
    r"summari[sz]e|summary|overview|classify|group|categori[sz]e|sort|topics?|trends?)\b"
)
# Words of an analysis request that name no new subject.
_ANSWER_ANALYSIS_FILLER = frozenset(
    "fais fait faire donne donner peux pourrais moi nous stp svp please make give "
    "global globale general generale rapide court courte bref breve detaille detaillee "
    "quick short brief overall full complete complet entier une les des par principaux "
    "principales main extrais extraire extract identifie identify quels quelles sont "
    "grands grandes what which "
    # "un résumé des sujets abordés"
    "abordes abordees traites traitees evoques evoquees couverts couvertes "
    "discutes discutees presents presentes covered discussed addressed".split()
)


def refers_to_previous_answer(message: str, previous_messages: list[dict] | None) -> bool:
    """Whether the question is about the sources of the previous answer.

    "Extrais les sujets de ces articles et classe-les" after a list must work
    on the listed papers; a new retrieval answered from six passages, and the
    history passed only the first 8 sources to the model.
    """
    previous = next(
        (
            row
            for row in reversed(list(previous_messages or []))
            if isinstance(row, dict) and str(row.get("role") or "") == "assistant"
        ),
        None,
    )
    meta = previous.get("meta") if previous else None
    if not isinstance(meta, dict) or not meta.get("sources"):
        return False
    normalized = _normalize(chat_request_text(message))
    if _PREVIOUS_ANSWER_REF_RE.search(normalized):
        return True
    if meta.get("mode") != "paper_list" or not _ANSWER_ANALYSIS_RE.search(normalized):
        return False
    # Right after a list, "fais un résumé global" needs no "ces articles".
    new_subject = [
        word
        for word in re.findall(r"[a-z0-9]{4,}", normalized)
        if not _ANSWER_ANALYSIS_RE.fullmatch(word) and word not in _ANSWER_ANALYSIS_FILLER
    ]
    return not new_subject


def needs_llm_tool_planner(message: str) -> bool:
    """Reserve model routing for plausible actions missed by deterministic rules.

    Ordinary questions and explanations should reach retrieval without an extra
    model round trip. This is only a fallback gate, not an intent classifier:
    explicit commands and recognized actions are resolved before calling it.
    """
    text = _normalize(chat_request_text(message))
    if re.match(r"^(?:comment|pourquoi|how|why)\b", text):
        return False
    action = re.search(
        r"\b(?:prepare|preparer|fais|faire|faites|rassemble|rassembler|reunis|"
        r"compile|compiler|collecte|collecter|recupere|recuperer|selectionne|"
        r"selectionner|propose|proposer|organise|organiser|cherche|chercher|recherche|rechercher|"
        r"trouve|trouver|montre|montrer|donne|donner|dresse|recense|identifie|verifie|controle|"
        r"check|verify|validate|make|build|gather|collect|"
        r"retrieve|select|suggest|recommend|organize|search|find|show|list)\b",
        text,
    )
    target = re.search(
        r"\b(?:articles?|papiers?|papers?|publications?|etudes?|studies|"
        r"sources?|references?|bibliographie|bibliography|panorama|overview|"
        r"revue|review|comparatif|comparison|comparaison|matrice|matrix|"
        r"preuves?|evidence|affirmations?|claims?|passage|carnet|notebook|"
        r"bibliotheque|library|corpus|collection|ceux|celles|these|those)\b",
        text,
    )
    return bool(action and target)


def _strip_command_query(rest: str, fallback: str) -> str:
    query = str(rest or "").strip()
    return query or str(fallback or "").strip()


def _export_scope_from_text(normalized: str) -> ExportScope:
    if _CHAT_SCOPE_RE.search(normalized):
        return "chat"
    if _PREVIOUS_SCOPE_RE.search(normalized):
        return "previous"
    return "previous"


# Words between a list verb and the thing it lists: "liste-moi tous les 10
# derniers articles", "show me all the most recent papers".
_LIST_OBJECT_SKIP = frozenset(
    """
    moi me m nous vous les la le l des de d du un une tous toutes tout tout all the a an of
    every each mes my ces these those ses leurs their
    liste list complete complet complets exhaustive exhaustif
    derniers dernieres premiers premieres recents recentes principaux principales meilleurs
    plus most more recent latest last first main key top best new important importants
    relevant pertinents
    """.split()
)
_LIST_OBJECT_PAPER_RE = re.compile(
    r"(?:articles?|papiers?|papers?|publications?|etudes?|study|studies|references?|"
    r"pdfs?|documents?|titres?|titles?|travaux|works?)"
)


def _lists_papers(normalized: str, verb_re: re.Pattern[str]) -> bool:
    """Whether the verb lists papers, not something found in them.

    "Liste les articles sur X" lists papers; "Liste les méthodes utilisées
    dans les articles de ma bibliothèque" lists methods, and the papers are
    only where to look.
    """
    tokens = re.findall(r"[a-z0-9]+", normalized)
    saw_verb = False
    for index, token in enumerate(tokens):
        if not verb_re.fullmatch(token):
            continue
        saw_verb = True
        rest = [word for word in tokens[index + 1 :] if word not in _LIST_OBJECT_SKIP]
        head = next((word for word in rest if not word.isdigit()), "")
        if not head or _LIST_OBJECT_PAPER_RE.fullmatch(head):
            return True
    return not saw_verb


def _has_author_filter_hint(raw: str, normalized: str) -> bool:
    return bool(
        _AUTHOR_FILTER_HINT_RE.search(normalized)
        or _RAW_AUTHOR_FILTER_HINT_RE.search(str(raw or ""))
    )


# Help answers from the bundled wiki, never from the library: a scientific question
# routed here silently loses its sources. "settings", "library", "notebooks", "index"
# or "chat" are also research words, so a keyword is never enough. Implicit help is a
# whole, short question about the software itself; anything that goes on to name a
# subject keeps its tool, and /aide covers everything else.
_APP_NAME_RE = re.compile(r"\b(?:bibliobrain|bilbiobrain|biblio\s+brain)\b")
_HELP_QUESTION_RE = re.compile(
    r"\b(?:comment|how|ou|where|aide|help|utiliser|utilisation|use|using|"
    r"fonctionne|fonctionnement|work|works|peux|puis|can|quoi|what|expliquer|explique|explain)\b"
)
# "Que dit la littérature sur Bibliobrain ?" asks the library, not the help.
_LIBRARY_CONTENT_RE = re.compile(
    r"\b(?:litterature|literature|etudes?|study|studies|selon|according|disent|dit|"
    r"says?|parlent|discuss\w*|mention\w*)\b"
)
_DET = r"(?:(?:le|la|les|l|un|une|des|du|de|d|aux|au|mon|ma|mes|the|a|an|my)\s+)?"
_FEATURE = (
    r"(?:carnets?|notebooks?|bibliotheque|library|parametres|reglages|settings|chat|"
    r"cartographies?|zotero|ollama|lm\s+studio)"
)
# "Découverte" or "Rédaction" alone are ordinary words: only as a tab.
_FEATURE_OBJECT = (
    rf"{_DET}(?:(?:nouveau|nouvelle|new)\s+)?"
    rf"(?:{_FEATURE}|(?:onglet|tab)\s+\w+|\w+\s+(?:tab|onglet))(?:\s+(?:tab|page))?"
)
_MY_DATA = (
    r"(?:mes|my)\s+(?:pdfs?|articles?|documents?|papers?|fichiers|files|sources|"
    r"donnees|data|conversations?|notes|references)"
)
_NEW_DOCS = (
    r"(?:(?:des|d|les|de|du|un|une|a|an|some|new|nouveaux|nouvelles|autres|other)\s+){0,2}"
    r"(?:pdfs?|articles?|documents?|papers?|fichiers|files)"
)
# The object may only be followed by where it goes in the application.
_APP_PLACE = (
    rf"(?:\s+(?:de|dans|d|in|of|on|avec|with|a|to|into|depuis|from)\s+{_DET}"
    rf"(?:bibliobrain|bilbiobrain|application|app|logiciel|software|{_FEATURE}))?"
)
_HOWTO = (
    r"(?:comment(?:\s+(?:est\s+ce\s+qu\s+on|est\s+ce\s+que\s+je|est\s+ce\s+que\s+j|on|je|j|"
    r"puis\s+je|peut\s+on|peux\s+je|faire\s+pour|dois\s+je|faut\s+il))?"
    r"|how\s+(?:do|can|should|could)\s+(?:i|we|you)|how\s+to"
    r"|ou\s+(?:puis\s+je|peut\s+on|je\s+peux|on\s+peut|est\s+ce\s+que\s+je\s+peux)"
    r"|where\s+(?:do|can|should)\s+(?:i|we|you))"
)
_ANY_VERB = (
    r"(?:utiliser|utilise|use|ouvrir|ouvre|open|creer|cree|create|make|configurer|configure|"
    r"set\s+up|parametrer|parametre|connecter|connecte|connect|lier|lie|link|synchroniser|"
    r"synchronise|sync|installer|installe|install|activer|active|enable|desactiver|"
    r"desactive|disable|acceder\s+a|acceder|accede\s+a|access|trouver|trouve|find|changer|"
    r"change|modifier|modifie|modify|supprimer|supprime|delete|remove|renommer|renomme|"
    r"rename|exporter|exporte|export|partager|partage|share|organiser|organise|organize)"
)
_IMPORT_VERB = (
    r"(?:ajouter|ajoute|add|importer|importe|import|charger|charge|upload|deposer|depose)"
)
_BROKEN = (
    r"(?:zotero|ollama|lm\s+studio|le\s+chat|the\s+chat|l\s+indexation|(?:the\s+)?indexing|"
    r"l\s+import|(?:the\s+)?import|l\s+ocr|(?:the\s+)?ocr|bibliobrain)"
)
_APP_HELP_QUESTION_RE = re.compile(
    r"(?:(?:bonjour|salut|hello|hi|svp|stp|please|dis\s+moi|tell\s+me|explique\s+moi|"
    r"(?:can|could)\s+you\s+tell\s+me|je\s+(?:voudrais|veux|souhaite)\s+savoir|"
    r"i\s+(?:want|would\s+like)\s+to\s+know|(?:ca|cela)\s+ne\s+(?:marche|fonctionne)\s+pas|"
    r"it\s+(?:doesn\s+t|does\s+not)\s+work)\s+)?"
    r"(?:"
    rf"{_HOWTO}\s+(?:{_ANY_VERB}\s+{_FEATURE_OBJECT}"
    rf"|(?:{_ANY_VERB}|{_IMPORT_VERB}|indexer|indexe|index)\s+{_MY_DATA}"
    rf"|{_IMPORT_VERB}\s+{_NEW_DOCS}){_APP_PLACE}"
    rf"|(?:comment\s+(?:fonctionnent|fonctionne|marchent|marche|utilise\s+t\s+on|on\s+utilise)"
    rf"|a\s+quoi\s+(?:sert|servent))\s+{_FEATURE_OBJECT}{_APP_PLACE}"
    rf"|how\s+(?:does|do)\s+{_FEATURE_OBJECT}{_APP_PLACE}\s+work"
    rf"|what\s+(?:is|are)\s+{_FEATURE_OBJECT}\s+for"
    rf"|(?:ou|where)\s+(?:sont|est|se\s+trouvent|se\s+trouve|is|are|trouver|to\s+find)\s+"
    r"(?:(?:stockes|stockees|stocke|stockee|enregistres|enregistrees|enregistre|"
    r"enregistree|sauvegardes|sauvegardees|sauvegarde|sauvegardee|ranges|rangees)\s+)?"
    rf"(?:{_MY_DATA}|{_FEATURE_OBJECT})(?:\s+(?:stored|saved|kept|located))?{_APP_PLACE}"
    rf"|(?:pourquoi\s+|why\s+)?{_BROKEN}\s+(?:n\s+est\s+pas|is\s+not|isn\s+t)\s+"
    r"(?:detecte|reconnu|connecte|trouve|disponible|detected|recognized|recognised|"
    r"connected|found|available|working)"
    rf"|{_BROKEN}\s+(?:ne\s+(?:marche|fonctionne|repond|demarre)\s+(?:pas|plus)"
    r"|(?:doesn\s+t|does\s+not|won\s+t)\s+(?:work|respond|start|load))"
    r"|(?:pourquoi\s+)?l\s+indexation\s+(?:est|reste)\s+(?:si\s+|tres\s+|trop\s+)?lente"
    r"|why\s+is\s+(?:the\s+)?indexing\s+(?:so\s+|very\s+)?slow"
    r")(?:\s+(?:svp|stp|s\s+il\s+(?:te|vous)\s+plait|please))?"
)


def _is_app_help_request(normalized: str) -> bool:
    text = normalized.replace("-", " ")
    if _APP_NAME_RE.search(text):
        return bool(_HELP_QUESTION_RE.search(text)) and not _LIBRARY_CONTENT_RE.search(text)
    return bool(_APP_HELP_QUESTION_RE.fullmatch(text))


def is_app_help_followup(message: str, previous_messages: list[dict]) -> bool:
    last_answer = next(
        (row for row in reversed(previous_messages) if row.get("role") == "assistant"), {}
    )
    if (last_answer.get("meta") or {}).get("mode") != "app_help":
        return False
    return bool(re.fullmatch(
        r"(?:et ensuite|et apres|ou (?:est ce|ca|cela)|je ne trouve pas|"
        r"(?:peux tu )?(?:detailler|preciser)|and then|what next|where is (?:it|that)|"
        r"i can t find it|(?:can you )?(?:elaborate|explain more))[ ?!.]*",
        _normalize(message).replace("-", " "),
    ))


def detect_chat_intent(message: str) -> ChatIntent:
    """Return the intended chat mode and the query to pass downstream.

    The detector deliberately stays conservative for natural language paper lists:
    it requires a list verb, an exhaustive marker, and a paper/article noun. Explicit
    slash commands can force a mode with fewer words.
    """
    raw = str(message or "").strip()
    if not raw:
        return ChatIntent(mode="rag", query="")

    command = _COMMAND_RE.match(raw)
    if command:
        cmd = command.group("cmd").lower()
        query = _strip_command_query(command.group("rest"), raw)
        if cmd in {"help", "aide"}:
            return ChatIntent(mode="app_help", query=query, explicit=True)
        if cmd == "rag":
            return ChatIntent(mode="rag", query=query, explicit=True)
        if cmd in {"draft", "redige"}:
            return ChatIntent(mode="drafting", query=query, explicit=True)
        if cmd in {"revue", "review"}:
            return ChatIntent(mode="literature_review", query=query, explicit=True)
        if cmd in {"discover", "discovery", "article", "articles"}:
            return ChatIntent(mode="article_discovery", query=query, explicit=True)
        if cmd in {"compare", "comparaison"}:
            return ChatIntent(mode="paper_compare", query=query, explicit=True)
        if cmd in {"evidence", "preuve", "preuves", "matrix", "matrice"}:
            return ChatIntent(mode="evidence_matrix", query=query, explicit=True)
        if cmd in {"bib", "biblio", "bibliographie", "bibliography"}:
            return ChatIntent(
                mode="bibliography",
                query=query,
                explicit=True,
                export_scope=_export_scope_from_text(_normalize(query)),
            )
        if cmd in {"factcheck", "fact-check", "verify", "verifie"}:
            return ChatIntent(mode="fact_check", query=query, explicit=True)
        if cmd in {"origine", "origin"}:
            return ChatIntent(mode="origin_trace", query=query, explicit=True)
        if cmd in {"notebook", "carnet"}:
            return ChatIntent(mode="notebook_create", query=query, explicit=True)
        if cmd in {"md", "markdown", "tex", "latex"}:
            command_export_format: ExportFormat = (
                "latex" if cmd in {"tex", "latex"} else "markdown"
            )
            return ChatIntent(
                mode="document_export",
                query=query,
                explicit=True,
                export_format=command_export_format,
                export_scope=_export_scope_from_text(_normalize(query)),
            )
        return ChatIntent(mode="paper_list", query=query, explicit=True)

    request = chat_request_text(raw)
    normalized = _normalize(request)
    if _is_app_help_request(normalized):
        return ChatIntent(mode="app_help", query=raw)
    # Before the "how/why" rule: "How many papers are in my library?".
    if _LIBRARY_CATALOGUE_RE.search(normalized) and not _DISCOVERY_NEW_RE.search(normalized):
        return ChatIntent(mode="paper_list", query=raw)
    if re.match(r"^(?:comment|pourquoi|how|why)\b", normalized):
        return ChatIntent(mode="rag", query=raw)
    if _is_drafting_request(normalized) and not any(
        pattern in normalized for pattern in _LITERATURE_REVIEW_PATTERNS
    ):
        return ChatIntent(mode="drafting", query=raw)
    export_format: ExportFormat | None = None
    if _LATEX_RE.search(normalized):
        export_format = "latex"
    elif _MARKDOWN_RE.search(normalized):
        export_format = "markdown"
    if export_format and _EXPORT_VERB_RE.search(normalized):
        return ChatIntent(
            mode="document_export",
            query=raw,
            export_format=export_format,
            export_scope=_export_scope_from_text(normalized),
        )

    if _NOTEBOOK_CREATE_RE.search(normalized):
        return ChatIntent(mode="notebook_create", query=raw)

    if _EVIDENCE_MATRIX_RE.search(normalized):
        return ChatIntent(mode="evidence_matrix", query=raw)

    if _BIBLIOGRAPHY_RE.search(normalized) and (
        _CITED_SOURCE_RE.search(normalized)
        or _CHAT_SCOPE_RE.search(normalized)
        or "conversation" in normalized
    ):
        return ChatIntent(
            mode="bibliography",
            query=raw,
            export_scope=_export_scope_from_text(normalized),
        )

    if _ORIGIN_RE.search(normalized):
        return ChatIntent(mode="origin_trace", query=raw)

    if _FACT_CHECK_RE.search(normalized):
        return ChatIntent(mode="fact_check", query=raw)

    if _COMPARE_RE.search(normalized) and (
        _PAPER_NOUN_RE.search(normalized) or _COMPARE_TARGET_RE.search(normalized)
    ):
        return ChatIntent(mode="paper_compare", query=raw)

    if any(pattern in normalized for pattern in _LITERATURE_REVIEW_PATTERNS):
        return ChatIntent(mode="literature_review", query=raw)

    if (
        _LIST_VERB_RE.search(normalized)
        and _PAPER_NOUN_RE.search(normalized)
        and (_EXHAUSTIVE_RE.search(normalized) or re.search(r"\b(?:liste|list)\b", normalized))
        and _lists_papers(normalized, _LIST_VERB_RE)
    ):
        return ChatIntent(mode="paper_list", query=raw)

    # "Quels sont les articles publies par X ?" asks for a list without a list
    # verb; plain RAG would cite one chunk per passage and repeat each paper.
    if (
        _PAPER_NOUN_RE.search(normalized)
        and _AUTHORSHIP_PHRASE_RE.search(normalized)
        and not _DISCOVERY_NEW_RE.search(normalized)
    ):
        return ChatIntent(mode="paper_list", query=raw)

    # "Quels articles parlent de LTS ?" asks for the papers of the library on a
    # topic; plain RAG answers from six passages and misses most of them.
    if _WHICH_PAPERS_RE.search(normalized) and not _DISCOVERY_NEW_RE.search(normalized):
        return ChatIntent(mode="paper_list", query=raw)

    if (
        (_DISCOVERY_VERB_RE.search(normalized) or _LIST_VERB_RE.search(normalized))
        and _PAPER_NOUN_RE.search(normalized)
        and _lists_papers(normalized, _LIST_VERB_RE)
        and _lists_papers(normalized, _DISCOVERY_VERB_RE)
        and _has_author_filter_hint(request, normalized)
        and not _DISCOVERY_NEW_RE.search(normalized)
    ):
        return ChatIntent(mode="paper_list", query=raw)

    if (
        _DISCOVERY_VERB_RE.search(normalized)
        and _PAPER_NOUN_RE.search(normalized)
        and (
            _DISCOVERY_RELATION_RE.search(normalized)
            or _DISCOVERY_REFERENTIAL_RE.search(normalized)
            or _DISCOVERY_NEW_RE.search(normalized)
        )
    ):
        return ChatIntent(mode="article_discovery", query=raw)

    return ChatIntent(mode="rag", query=raw)


# Words that point at something only an image shows. Reading figures is slow
# (it unloads the chat model and runs a vision model on up to three pages),
# so the chat asks the LLM whether to do it only when the question itself
# names a visual element. Small LLMs otherwise say yes to most questions.
_VISUAL_QUESTION_RE = re.compile(
    r"\b(?:figures?|figs?|graphiques?|graphes?|graphs?|graphics?|courbes?|curves?|"
    r"diagrammes?|diagrams?|schemas?|schematics?|tableaux|tableau|tables?|images?|"
    r"illustrations?|photos?|photographies?|photographs?|charts?|plots?|plotted|"
    r"histogrammes?|histograms?|axis|abscisses?|ordonnees?|legendes?|legends?|"
    r"panneaux|panneau|panels?|series|nuages? de points|scatter|"
    r"visuels?|visuelles?|visuals?|visuellement|visually)\b"
)
# "Fais un tableau comparatif" asks to write a table, not to read one.
_VISUAL_OUTPUT_REQUEST_RE = re.compile(
    r"\b(?:(?:sous forme|sous la forme) d(?:e|u|une?)?|en|as|in|into)\s+(?:un |une |a |an )?"
    r"(?:petit |grand |court |simple |seul )?"
    r"(?:tableaux|tableau|tables?|schemas?|diagrammes?|graphiques?|charts?|diagrams?)\b"
    r"|\b(?:fai[st]?|faire|faites|dresse[rz]?|cree[rz]?|genere[rz]?|propose[rz]?|"
    r"donne[rz]?|construi[st]|construire|redige[rz]?|make|create|build|draw|generate|"
    r"produce|give)\s+(?:moi |me |nous |us )?(?:un |une |des |a |an )?"
    r"(?:petit |grand |court |simple |seul )?"
    r"(?:tableaux|tableau|tables?|schemas?|diagrammes?|graphiques?|charts?|diagrams?)\b"
)


def question_may_need_visuals(message: str) -> bool:
    """True when the question names a figure, graph, table image or similar.

    It decides only whether asking the LLM is worth it: a question that names
    no visual element is answered from the text, and the "Read figures" button
    still forces a reading.
    """
    normalized = _normalize(message)
    if not normalized:
        return False
    normalized = _VISUAL_OUTPUT_REQUEST_RE.sub(" ", normalized)
    return bool(_VISUAL_QUESTION_RE.search(normalized))


__all__ = [
    "ChatIntent",
    "ChatMode",
    "ExportFormat",
    "ExportScope",
    "PaperListFollowup",
    "detect_chat_intent",
    "detect_retrieval_depth",
    "is_draft_citation_followup",
    "is_app_help_followup",
    "is_library_overview_request",
    "needs_llm_tool_planner",
    "paper_list_followup",
    "question_may_need_visuals",
    "refers_to_previous_answer",
    "requests_library_citations",
    "resolve_retrieval_depth",
]


# Answer length picked from the question when the setting is "auto".
_DETAILED_ANSWER_RE = re.compile(
    r"\b(?:pourquoi|why|explique\w*|expliquer|explain\w*|comment|"
    r"how\s+(?:does|do|did|is|are|was|were|can|could|would|should|to)|"
    r"compare\w*|comparaison\w*|contraste\w*|differences?\s+(?:entre|between)|"
    r"resume|resumer|summari[sz]e\w*|summary|synthese\w*|synthesi[sz]e\w*|"
    r"analyse\w*|analy[sz]e\w*|analysis|methode\w*|method\w*|"
    r"detail\w*|en\s+profondeur|in\s+depth|decri\w*|describe\w*|"
    r"discute\w*|discuss\w*|mecanisme\w*|mechanism\w*)\b"
)
_CONCISE_ANSWER_RE = re.compile(
    # Definitions.
    r"^(?:qu\s+est[\s-]ce\s+qu|c\s+est\s+quoi|que\s+(?:veut\s+dire|signifie)|"
    r"definis|define|what\s+(?:is|are|does)\b)|"
    r"\b(?:definition|signification)\b|"
    # Formulas and values.
    r"\b(?:formules?|formulas?|valeurs?|values?|combien|how\s+many|how\s+much)\b|"
    # Who, when, which (singular "quel", not "quels sont").
    r"^(?:qui|quand|ou|who|when|where|which|quel|quelle|en\s+quelle\s+annee|in\s+which\s+year)\b|"
    # Yes or no.
    r"^(?:est[\s-]ce\s+que|y\s+a[\s-]t[\s-]il|existe[\s-]t[\s-]il|peut[\s-]on|a[\s-]t[\s-]on|"
    r"is|are|does|do|did|can|could|has|have|was|were)\b|"
    r"\b\w+-t-(?:il|elle|on)\b|"
    r"\bje\s+(?:cherche|voudrais\s+savoir|me\s+demande|veux\s+savoir)\s+si\b|"
    r"\b(?:i\s+wonder|i\s+want\s+to\s+know|tell\s+me)\s+(?:if|whether)\b"
)
_ENUMERATION_RE = re.compile(
    r"\b(?:liste\w*|list|lists|listing|enumere\w*|enumerate|recense\w*|"
    r"bullet\w*|puces|tableau|table|etapes|steps|"
    r"classe|classer|classify|classification|regroupe\w*|group|groups|grouping|"
    r"categor\w*|themes?|thematiques?|sujets|topics|trie|trier|sort)\b"
)
# A long question is rarely a short factual one, whatever its first word.
_CONCISE_MAX_WORDS = 30


def is_enumeration_request(message: str) -> bool:
    """A question asking for a list (themes, methods, steps, groups...)."""
    normalized = _normalize(chat_request_text(message) or message)
    return bool(_ENUMERATION_RE.search(normalized)) or is_library_overview_request(message)


def classify_answer_length(message: str) -> str:
    """"concise", "standard" or "detailed" for a question, without a model.

    Explaining, comparing, summarizing or asking for the method gets a
    detailed answer; a definition, a formula, a value, who/when/which or a
    yes-no question gets a concise one; anything else a standard one.
    """
    normalized = _normalize(chat_request_text(message) or message)
    if not normalized:
        return "standard"
    if _DETAILED_ANSWER_RE.search(normalized):
        return "detailed"
    if len(normalized.split()) <= _CONCISE_MAX_WORDS and _CONCISE_ANSWER_RE.search(normalized):
        return "concise"
    return "standard"
