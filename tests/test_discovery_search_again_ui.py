from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP = ROOT / "static" / "js" / "app"
DISCOVERY_JS = (APP / "discovery.js").read_text(encoding="utf-8")
CHAT_JS = (APP / "chat.js").read_text(encoding="utf-8")
WORKSPACES_JS = (APP / "workspaces.js").read_text(encoding="utf-8")


def test_search_again_updates_the_opened_search_and_compares_with_it() -> None:
    run = DISCOVERY_JS[DISCOVERY_JS.index("async function runArticleDiscovery") :]
    assert "const againId = _discoverySearchAgainId();" in run
    assert "history_id: againId || null," in run
    assert "state.discoveryCompareTo = againId ? _discoveryResultKeys(state.discoveryResults) : null;" in run
    # New papers first, the seen ones after (dimmed by the card class).
    assert "[...results.filter((item) => !item._seen), ...results.filter((item) => item._seen)]" in DISCOVERY_JS
    assert "item._seen ? 'is-seen' : ''" in DISCOVERY_JS
    assert "'discovery.searchAgain' : 'discovery.run'" in DISCOVERY_JS


def test_opened_search_shows_when_and_can_be_renamed_or_deleted() -> None:
    assert 'id="discovery-search-meta"' in HTML
    assert 'data-discovery-search-action="rename"' in HTML
    assert 'data-discovery-search-action="delete"' in HTML
    assert "method: 'PATCH'" in DISCOVERY_JS
    assert "setDiscoveryOpenedSearch(search);" in CHAT_JS
    assert "setDiscoveryOpenedSearch(null);" in CHAT_JS


def test_results_can_be_selected_in_bulk_and_added_to_a_notebook() -> None:
    assert 'class="discovery-select"><input type="checkbox" data-compare-index=' in DISCOVERY_JS
    assert "if (input.checked) discoveryRefinement.selected.add(id);" in WORKSPACES_JS
    assert "compare.disabled = selected.size < 2 || selected.size > 4;" in WORKSPACES_JS
    assert "addDiscoveryResultsToNotebook([...discoveryRefinement.selected])" in WORKSPACES_JS
    # Text sources only: the full text is never downloaded.
    assert "/sources`" in DISCOVERY_JS and "'discovery.sourceAbstractOnly'" in DISCOVERY_JS


def test_each_result_quotes_its_matching_passage_and_offers_similar() -> None:
    assert "function _discoveryMatchingPassage(item, question)" in DISCOVERY_JS
    assert 'class="discovery-quote"' in DISCOVERY_JS
    assert "findSimilarDiscoveryArticles(idx)" in DISCOVERY_JS
    assert "hideDiscoveryResult(idx)" in DISCOVERY_JS


def test_history_is_grouped_by_date_and_filterable() -> None:
    assert 'id="discovery-history-filter"' in HTML
    assert "discoveryHistoryGroup(item.updated || item.created || '')" in CHAT_JS
    assert "function onDiscoveryHistoryFilterInput(event)" in CHAT_JS
