"""Run complete-document OCR acceptance against an isolated local vector index.

Uses local library PDFs, installed Ollama models and the reusable OCR cache.
Private outputs belong in the ignored benchmark_reports directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="benchmark_reports/ocr-acceptance-2026-09-08")
    parser.add_argument("--scan-only", action="store_true")
    parser.add_argument("--answer-model", default="ornith-1.5:9b")
    parser.add_argument("--render-samples", action="store_true")
    args = parser.parse_args()
    from src import config
    from src import document_ocr as ocr
    from src.app_settings import get_app_settings
    from src.rag import state
    from src.rag.ocr_ingestion import enrich
    from src.zotero_db import load_zotero_library

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    library = Path(get_app_settings()["data_path"])
    config.DATA_PATH = str(library)
    zotero_path = get_app_settings().get("zotero_path")
    if zotero_path and Path(zotero_path).is_file():
        state._zotero_library = load_zotero_library(zotero_path)
    choices = [("scanned_article", "RS6RCWU8"), ("mixed_pdf", "SNKS3N6E"), ("equation_book", "FGBVHZPR")]
    pdfs = []
    inventories = []
    for kind, key in choices:
        path = next((library / key).glob("*.pdf"))
        rel = str(path.relative_to(library))
        pdfs.append((rel, str(path)))
        inv = ocr.scan_pdf(str(path), threading.Event())
        info = {"kind": kind, "file": rel, "pages": len(inv["pages"]),
                "candidates": [p for p in inv["pages"] if p["reason"] != "native"]}
        inventories.append(info)
        print(json.dumps({**info, "candidates": len(info["candidates"])}, ensure_ascii=False), flush=True)
    ocr.write_json(out / "documents.json", inventories)
    if args.render_samples:
        for index, page in [(0, 0), (1, 1), (2, 145), (2, 10), (2, 356)]:
            png, blank = ocr.render_page(pdfs[index][1], page)
            (out / f"source-{index}-page-{page + 1}.png").write_bytes(png)
            print(json.dumps({"render_document": index, "page": page + 1, "blank": blank}), flush=True)
    if args.scan_only:
        return

    from chromadb.config import Settings
    from langchain_chroma import Chroma
    from langchain_community.document_loaders import PyPDFLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    from src.llm_provider import (
        create_embeddings,
        get_embedding_provider_mode,
        required_embedding_model_name,
    )
    from src.rag.document_loader import _article_id_from_item
    from src.rag.lexical_index import rebuild_from_vectorstore, search
    from src.rag.zotero_integration import _build_item_metadata

    if get_embedding_provider_mode() != "ollama":
        raise RuntimeError("Acceptance run requires local Ollama embeddings")
    embeddings = create_embeddings(required_embedding_model_name())
    index_path = str(out / "index")
    vector = Chroma(collection_name="ocr_acceptance", persist_directory=index_path,
                    embedding_function=embeddings, client_settings=Settings(anonymized_telemetry=False))
    # On resume, don't put old native text back on pages already enriched by OCR.
    if not (out / "native_complete.json").exists():
        for rel, path in pdfs:
            pages = PyPDFLoader(path).load()
            for page in pages:
                page.metadata.update(_build_item_metadata(rel, path, None))
                page.metadata["article_id"] = _article_id_from_item(rel, None)
            chunks = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200).split_documents(pages)
            ids = ["native-" + hashlib.sha256(f"{rel}:{i}:{d.page_content}".encode()).hexdigest()
                   for i, d in enumerate(chunks)]
            existing = set(vector.get(include=[])["ids"])
            pending = [(doc, uid) for doc, uid in zip(chunks, ids) if uid not in existing]
            for offset in range(0, len(pending), 50):
                batch = pending[offset:offset + 50]
                vector.add_documents([d for d, uid in batch], ids=[uid for d, uid in batch])
            print(json.dumps({"native_indexed": rel, "chunks": len(chunks)}), flush=True)
        rebuild_from_vectorstore(index_path, vector)
        ocr.write_json(out / "native_complete.json", {"embedding_model": required_embedding_model_name()})

    def progress(report):
        ocr.write_json(out / "progress.json", report)
        if report["status"] != "scanning":
            print(json.dumps({k: report.get(k) for k in ("status", "completed", "candidates", "current_page", "review")}), flush=True)

    report = enrich(vector, index_path, pdfs, chunk_size=1000, model=ocr.DEFAULT_MODEL,
                    stop=threading.Event(), progress=progress)
    ocr.write_json(out / "ingestion.json", report)
    if report["status"] not in {"completed", "completed_with_review"}:
        raise RuntimeError("Acceptance ingestion did not finish")

    from dataclasses import asdict

    from src.chat_output_guard import validate_chat_output
    from src.llm_provider import invoke_ollama_chat, unload_ollama_model
    from src.rag.query_engine import (
        _build_source_citation_guide,
        _format_context_for_direct_chat,
        format_sources_api,
    )

    unload_ollama_model(ocr.DEFAULT_MODEL)
    queries = ["Trapped field distribution Sm Ba Cu O bulk superconductors",
               "anisotropic thermal diffusivity conductivity YBCO c axis",
               "equal-area theorem critical generation short normal zones"]
    retrieval = []
    for (rel, _), query in zip(pdfs, queries):
        global_hits = vector.similarity_search(query, k=8)
        hits = vector.similarity_search(query, k=3, filter={"$and": [
            {"rel_path": rel}, {"extraction_method": "ocr"}]})
        lexical_hits = search(index_path, query, limit=12)
        prompt = ("Réponds en français en deux phrases maximum à partir des sources ci-dessous. "
                  "Décris le résultat ou principe concernant : " + query + ". "
                  "Cite chaque phrase avec un label autorisé. N’invente aucune valeur.\n\n"
                  + _build_source_citation_guide(hits) + "\n\n"
                  + _format_context_for_direct_chat(hits))
        _, answer, answer_metrics = invoke_ollama_chat(
            args.answer_model, [{"role": "user", "content": prompt}], think=False,
            temperature=0, options_override={"num_predict": 500, "num_ctx": 8192},
            request_timeout_seconds=180,
        )
        retrieval.append({"query": query, "expected_file": rel,
            "found_globally": any(d.metadata.get("rel_path") == rel for d in global_hits),
            "sources": format_sources_api(hits),
            "text": [d.page_content for d in hits],
            "ocr_hits": sum(d.metadata.get("extraction_method") == "ocr" for d in hits),
            "lexical_hits": sum(d.metadata.get("rel_path") == rel for d in lexical_hits),
            "answer": answer, "answer_context": "recovered_ocr_passages", "answer_done": bool(answer_metrics.get("done")),
            "guard": asdict(validate_chat_output(answer, sources=hits)),
            "answer_model": args.answer_model})
        ocr.write_json(out / "retrieval.json", retrieval)
    ocr.write_json(out / "retrieval.json", retrieval)
    print(json.dumps({"finished": True, "report": str(out),
                      "retrieval": [{k: v for k, v in r.items() if k not in {"sources", "text"}} for r in retrieval]}), flush=True)


if __name__ == "__main__":
    main()
