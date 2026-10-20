import hashlib
import re
from pathlib import Path

from src.static_assets import asset_version, versioned_index_html

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
LOCAL_ASSET_RE = re.compile(r'(?:src|href)="(/static/[^"]+\.(?:js|css))(?:\?v=([^"]*))?"')


def _sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def test_every_local_script_and_stylesheet_is_cache_busted() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assets = LOCAL_ASSET_RE.findall(html)

    assert assets
    unversioned = [url for url, version in assets if not version]
    assert unversioned == [], "add ?v= so the served page versions these assets"


def test_served_index_versions_each_asset_by_its_content() -> None:
    served = versioned_index_html(STATIC_DIR)

    assets = LOCAL_ASSET_RE.findall(served)
    assert assets
    for url, version in assets:
        assert version == _sha12(STATIC_DIR / url.removeprefix("/static/")), url


def test_asset_version_changes_with_content(tmp_path: Path) -> None:
    (tmp_path / "js").mkdir()
    script = tmp_path / "js" / "app.js"
    script.write_text("console.log(1);\n", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<script src="/static/js/app.js?v=manual-label-1"></script>\n'
        '<script src="/static/js/missing.js?v=kept-1"></script>\n'
        '<link href="/static/../secret.css?v=kept-2">\n',
        encoding="utf-8",
    )

    first = versioned_index_html(tmp_path)
    script.write_text("console.log(2); // edited\n", encoding="utf-8")
    second = versioned_index_html(tmp_path)

    assert f"/static/js/app.js?v={_sha12(script)}" in second
    assert first != second
    assert "manual-label-1" not in first
    assert "/static/js/missing.js?v=kept-1" in second
    assert "/static/../secret.css?v=kept-2" in second


def test_asset_version_rejects_paths_outside_the_static_dir(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

    assert asset_version(static_dir, "../secret.txt") is None
