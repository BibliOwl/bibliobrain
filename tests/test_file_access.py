from __future__ import annotations

from pathlib import Path

from src.file_access import resolve_pdf_path


def test_resolve_pdf_path_accepts_pdf_under_data_root(workspace_tmp_dir: Path) -> None:
    """Le helper doit autoriser un PDF valide situé sous DATA_PATH."""
    data_dir = workspace_tmp_dir / "data"
    data_dir.mkdir()
    pdf_path = data_dir / "papers" / "paper.pdf"
    pdf_path.parent.mkdir()
    pdf_path.write_bytes(b"%PDF-1.7\n")

    resolved = resolve_pdf_path(str(data_dir), "papers/paper.pdf")

    assert resolved == pdf_path.resolve()


def test_resolve_pdf_path_rejects_parent_traversal(workspace_tmp_dir: Path) -> None:
    """Un chemin qui tente de remonter hors du dossier de données doit être refusé."""
    data_dir = workspace_tmp_dir / "data"
    data_dir.mkdir()
    outside_pdf = workspace_tmp_dir / "secret.pdf"
    outside_pdf.write_bytes(b"%PDF-1.7\n")

    resolved = resolve_pdf_path(str(data_dir), "../secret.pdf")

    assert resolved is None


def test_resolve_pdf_path_allows_legacy_storage_prefix(workspace_tmp_dir: Path) -> None:
    """Le préfixe historique `storage/` reste accepté pour ne pas casser l'UI existante."""
    data_dir = workspace_tmp_dir / "storage"
    data_dir.mkdir()
    pdf_path = data_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n")

    resolved = resolve_pdf_path(str(data_dir), "storage/paper.pdf")

    assert resolved == pdf_path.resolve()
