from src.chat_term_grounding import ungrounded_terms

# The passage the 3B model cited when it wrote "basés sur l'iode".
_FE_SC = (
    "High temperature superconductivity Uchida, Shin-ichi 2015\n"
    "Chapter 4 Iron-Based Superconductors. Abstract The iron-based superconductors "
    "(Fe-SCs) comprise a big family having the highest Tc = 55 K. In this family, an "
    "indispensable element is the typical magnetic element Fe, and the common building "
    "block is composed of Fe and As (Se) layers, as in LaFeAsO1-xFx and SmFeAsO1-xFx."
)


def test_an_element_no_passage_names_is_listed() -> None:
    answer = (
        "Uchida (2015) explique que les composés de fer basés sur l'iode atteignent Tc = 55 K [S2]."
    )
    assert ungrounded_terms(answer, [_FE_SC]) == ["iode"]


def test_elements_written_as_symbols_or_in_formulas_are_grounded() -> None:
    answer = (
        "Les supraconducteurs à base de fer (Fe-SCs) reposent sur des couches de fer et "
        "d'arsenic, ou de sélénium, comme LaFeAsO1-xFx [S1]."
    )
    assert ungrounded_terms(answer, [_FE_SC]) == []


def test_english_element_names_are_checked_too() -> None:
    assert ungrounded_terms("They are iodine-based compounds [S1].", [_FE_SC]) == ["iodine"]


def test_formulas_and_acronyms_absent_from_the_passages_are_listed() -> None:
    answer = "Le composé YBa2Cu3O7 et les FeSe sont des IOD [S1]."
    terms = ungrounded_terms(answer, [_FE_SC])
    assert "YBa2Cu3O7" in terms
    assert "IOD" in terms
    # FeSe: Fe and Se are both in the passage, only the spelling differs.
    assert "FeSe" not in terms


def test_citation_labels_roman_numerals_and_the_question_are_not_flagged() -> None:
    answer = "Le chapitre II décrit les SHT étudiés [S2] [S10]."
    assert ungrounded_terms(answer, [_FE_SC], question="Que disent les SHT ?") == []


def test_without_passages_nothing_is_checked() -> None:
    assert ungrounded_terms("Le composé est basé sur l'iode.", []) == []


def test_common_words_that_name_metals_are_not_flagged() -> None:
    # "or" (gold) and "lead" are ordinary words.
    assert ungrounded_terms("Or, this may lead to a higher Tc [S1].", [_FE_SC]) == []


def test_equations_of_the_answer_are_not_read_as_names() -> None:
    answer = r"Les pertes valent \(Q = \frac{1}{4}\int_{L}\int_{A_{sc}} E J\,ds\) [S1]."
    assert ungrounded_terms(answer, [_FE_SC]) == []


def test_capitalised_compounds_and_app_words_are_not_acronyms() -> None:
    answer = "La stabilité Long-Term est discutée dans l'index RAG [S1]."
    assert ungrounded_terms(answer, [_FE_SC]) == []
