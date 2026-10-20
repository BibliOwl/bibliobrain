from __future__ import annotations

from pathlib import Path

from scripts.collect_third_party_licenses import (
    is_license_entry,
    locked_distribution_names,
    safe_entry_name,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")


def test_third_party_notices_cover_embedded_and_notable_runtime_components() -> None:
    notices = _read("THIRD_PARTY_NOTICES.md")

    for component in (
        "Marked",
        "KaTeX",
        "DOMPurify",
        "FlatBuffers",
        "Inter Variable Font",
        "JetBrains Mono Variable Font",
        "LangSmith SDK",
        "certifi",
        "langchain-core",
        "NumPy",
        "pypdfium2",
        "PyTorch",
    ):
        assert component in notices

    assert "THIRD_PARTY_LICENSES/Inter-OFL-1.1.txt" in notices
    assert "THIRD_PARTY_LICENSES/JetBrainsMono-OFL-1.1.txt" in notices


def test_font_license_texts_are_bundled() -> None:
    for relative, copyright_notice in (
        (
            "THIRD_PARTY_LICENSES/Inter-OFL-1.1.txt",
            "Copyright (c) 2016 The Inter Project Authors",
        ),
        (
            "THIRD_PARTY_LICENSES/JetBrainsMono-OFL-1.1.txt",
            "Copyright 2020 The JetBrains Mono Project Authors",
        ),
    ):
        license_text = _read(relative)
        assert copyright_notice in license_text
        assert "SIL OPEN FONT LICENSE Version 1.1" in license_text
        assert "PERMISSION" in license_text
        assert "DISCLAIMER" in license_text


def test_contribution_policy_requires_the_versioned_nonexclusive_cla() -> None:
    contributing = _read("CONTRIBUTING.md")
    cla = _read("CLA.md")

    assert "I have read and accept CLA.md version 1.1" in contributing
    assert "Version 1.1" in cla
    assert "non-exclusive" in cla
    assert "sublicensable" in cla
    assert "commercial or proprietary terms" in cla
    assert "French law" in cla


def test_privacy_notice_names_the_controller_retention_and_rights() -> None:
    privacy = _read("PRIVACY.md")
    html = _read("static/index.html")
    i18n = _read("static/js/i18n.js")

    for language_marker in ("## Français", "## English"):
        assert language_marker in privacy
    assert privacy.count("bibliobrain@proton.me") >= 4
    assert "un an au plus" in privacy
    assert "one year at most" in privacy
    assert "cnil.fr" in privacy
    # The bug report form states who is responsible before anything is sent.
    assert 'data-i18n="bugReport.dataResponsibility"' in html
    assert "'bugReport.dataResponsibility'" in i18n
    # GitHub sees the IP address of the update check: it is not called anonymous.
    assert "requête anonyme" not in html
    assert "anonymous request" not in i18n


def test_application_links_to_its_source_code() -> None:
    html = _read("static/index.html")

    assert 'id="about-source-link" href="https://github.com/bibliOwl/bibliobrain"' in html
    assert 'data-i18n="about.license"' in html


def test_license_collector_recognizes_lock_and_safe_notice_paths(tmp_path: Path) -> None:
    lockfile = tmp_path / "requirements.lock"
    lockfile.write_text(
        "demo-package==1.2.3 \\\n+    --hash=sha256:demo\n# torch is installed separately\n",
        encoding="utf-8",
    )

    assert locked_distribution_names(lockfile) == ["demo-package", "torch"]
    assert is_license_entry("demo-1.0.dist-info/licenses/LICENSE.txt") is True
    assert is_license_entry("demo-1.0.dist-info/licenses/Apache-2.0.txt") is True
    assert is_license_entry("demo/module.py") is False
    assert safe_entry_name("../../../demo.dist-info/licenses/LICENSE.txt") == (
        "demo.dist-info__licenses__LICENSE.txt"
    )
