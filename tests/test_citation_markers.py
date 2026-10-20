from src.citation_markers import (
    CitationMarker,
    ReferenceEntry,
    ReferenceList,
    extract_markers,
    normalize_text,
    parse_reference_list,
    resolve_marker,
    split_references,
    uses_glued_superscripts,
)

_FILLER = "Body text about trapped fields in bulk samples. " * 40


def _numeric_article() -> str:
    return (
        _FILLER
        + "Pulsed magnetization was first proposed by Varnel [2] and later refined [3-5].\n"
        + "Losses follow the critical state model [1, 3].\n"
        + "\nReferences\n"
        + "[1] A. Quillon, Critical state of hard superconductors, Rev. Fict. Phys. 36, 31 (1964).\n"
        + "[2] B. Varnel and C. Ostrik, Pulsed field magnetization of bulks, Fict. Lett. 12, 4 (1990).\n"
        + "[3] D. Moraine, Flux jumps in pulsed fields, J. Fict. 7, 412 (1994).\n"
        + "[4] E. Taliot, Multi-pulse sequences, J. Fict. 8, 101\n(1995).\n"
        + "[5] F. Brune, Temperature rise during pulses, J. Fict. 9, 77 (1997).\n"
    )


def test_split_references_finds_heading_in_second_half():
    text = _numeric_article()
    start, found_by = split_references(text)
    assert found_by == "heading"
    assert text[start:].lstrip().startswith("[1] A. Quillon")


def test_split_references_ignores_early_table_of_contents():
    text = "Contents\nReferences\n" + _FILLER * 2
    assert split_references(text) == (-1, "none")


def test_numbered_entries_and_markers_resolve():
    text = _numeric_article()
    refs = parse_reference_list(text)
    assert refs.style == "bracket"
    assert [entry.number for entry in refs.entries] == [1, 2, 3, 4, 5]
    assert "(1995)" in refs.entry(4).text  # entry continued on the next line
    markers = extract_markers(text[: refs.start])
    assert [marker.numbers for marker in markers] == [(2,), (3, 4, 5), (1, 3)]
    varnel = resolve_marker(markers[0], refs)
    assert len(varnel) == 1 and "Varnel" in varnel[0].text


def test_range_across_brackets_is_one_marker():
    markers = extract_markers("Coils were tested [1]–[4], then [6], [7] and [9]-[8].")
    assert [(marker.text, marker.numbers) for marker in markers] == [
        ("[1]–[4]", (1, 2, 3, 4)),
        ("[6]", (6,)),
        ("[7]", (7,)),
        ("[9]", (9,)),
        ("[8]", (8,)),
    ]


def test_padded_brackets_from_pdf_extraction():
    markers = extract_markers("up to 3 T in samples of 65 mm diameter [ 5]a t7 7 K and [12 ]–[ 14].")
    assert [marker.numbers for marker in markers] == [(5,), (12, 13, 14)]


def test_unknown_number_in_group_is_not_resolved():
    refs = ReferenceList(entries=[ReferenceEntry("A. One (1990)", 1), ReferenceEntry("B. Two (1991)", 2)])
    marker = CitationMarker("[2, 250]", 0, 8, "numeric", numbers=(2, 250))
    assert resolve_marker(marker, refs) == []


def test_entries_stop_at_a_figure_caption():
    text = (
        _FILLER
        + "\nReferences\n[1] A. Quillon, Rev. Fict. Phys. 36 (1964) 31.\n"
        + "[2] B. Moraine, J. Fict. 7 (1994) 412. Figure 18. Comparison of (a) experimental results.\n"
        + "[3] D. Varnel, J. Fict. 8 (1995) 1.\n"
    )
    refs = parse_reference_list(text)
    assert refs.entry(2).text == "[2] B. Moraine, J. Fict. 7 (1994) 412."


def test_numbered_list_ignores_volume_and_page_numbers():
    text = _FILLER + "\nReferences\n1. A. Quillon, Rev. Fict.\n36 31 (1964).\n2. B. Varnel, Fict. Lett.\n3. D. Moraine, J. Fict.\n"
    refs = parse_reference_list(text)
    assert [entry.number for entry in refs.entries] == [1, 2, 3]


def test_author_year_markers_and_entries():
    text = (
        _FILLER
        + "The flux pump idea goes back to early work (van Dorsel, 1967; Hamrick et al., 1972b). "
        + "Okonta and Pell (1981) measured it, see also ITER (2010) and Fig (2004).\n"
        + "\nReferences\n"
        + "Hamrick, J., Lisle, P., 1972b. Rectifier flux pumps. Fict. Cryog. 12, 33-41.\n"
        + "Okonta, R., Pell, S., 1981. Flux pump efficiency\nmeasured at 4 K. J. Fict. 3, 1-9.\n"
        + "van Dorsel, K., 1967. A superconducting dynamo. Fict. Phys. Lett. 2, 15.\n"
    )
    refs = parse_reference_list(text)
    assert refs.style == "author_year"
    assert len(refs.entries) == 3
    markers = extract_markers(text[: refs.start])
    assert [(marker.surname, marker.year) for marker in markers] == [
        ("Dorsel", "1967"),
        ("Hamrick", "1972"),
        ("Okonta", "1981"),
    ]
    okonta = resolve_marker(markers[2], refs)
    assert len(okonta) == 1 and "measured at 4 K" in okonta[0].text


def test_author_year_prefers_first_author():
    refs = ReferenceList(
        entries=[
            ReferenceEntry("Larsen, T., Okonta, R., 1990. Second paper."),
            ReferenceEntry("Okonta, R., 1990. First author paper."),
        ],
        style="author_year",
    )
    marker = CitationMarker("Okonta, 1990", 0, 12, "author_year", surname="Okonta", year="1990")
    assert [entry.text for entry in resolve_marker(marker, refs)] == ["Okonta, R., 1990. First author paper."]


def test_author_year_falls_back_to_the_raw_list():
    refs = ReferenceList(
        entries=[ReferenceEntry("Hamrick J 1972 Rectifier pumps")],
        style="author_year",
        text="Hamrick J 1972 Rectifier pumps\nOKONTA R. and PELL S. Flux pump efficiency, J. Fict. 3 (1981) 1-9\n",
    )
    marker = CitationMarker("Okonta, 1981", 0, 12, "author_year", surname="Okonta", year="1981")
    found = resolve_marker(marker, refs)
    assert len(found) == 1 and found[0].text.startswith("OKONTA R. and PELL S.")
    wrong_year = CitationMarker("Okonta, 1990", 0, 12, "author_year", surname="Okonta", year="1990")
    assert resolve_marker(wrong_year, refs) == []


def _glued_article() -> str:
    body = (
        "Field mapping relies on Hall probe imaging,1 and on the critical state of the windings,2 "
        "as discussed earlier.3,4 Flux pumps6-8 were developed.5 The model,9 holds at 77 K. "
        "YBa2Cu3O7 and Nb3Sn samples were cut to 12 mm2 pieces.\n"
    )
    names = ["Arvo", "Bessin", "Corlay", "Dument", "Esquel", "Farrow", "Gallant", "Hoskin", "Ivers"]
    entries = "".join(
        f"{n}A. {name}, Fict. Phys. Lett. {n}, 1 (19{70 + n}).\n" for n, name in enumerate(names, 1)
    )
    return _FILLER + body + "\n" + entries


def test_glued_superscripts_need_a_matching_numbered_list():
    text = _glued_article()
    refs = parse_reference_list(text)
    assert refs.style == "glued"
    body = text[: refs.start]
    assert uses_glued_superscripts(body, refs)
    markers = extract_markers(body, glued_superscripts=True)
    numbers = [marker.numbers for marker in markers]
    assert numbers == [(1,), (2,), (3, 4), (6, 7, 8), (5,), (9,)]
    assert all(resolve_marker(marker, refs) for marker in markers)
    # Without the confirmation, digits glued to words are never markers.
    assert extract_markers(body) == []


def test_bracket_documents_never_switch_to_glued_mode():
    text = _numeric_article()
    refs = parse_reference_list(text)
    assert not uses_glued_superscripts(text[: refs.start], refs)


def test_unicode_superscripts_are_read():
    markers = extract_markers("as shown before.¹² and in pumps,³⁻⁵")
    assert [marker.numbers for marker in markers] == [(12,), (3, 4, 5)]


def test_normalize_text_joins_hyphenated_line_breaks():
    assert normalize_text("Super-\nconducting  dyna\u00admo") == "superconducting dynamo"
