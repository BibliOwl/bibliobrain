"""Child-process entry point: parse one document and print its units as JSON.

Run as ``python -I src/document_worker.py <path> <max_units> <max_chars>`` by
``src.document_isolation``. Isolated mode drops the script directory from
``sys.path``, so the repository root is added back explicitly. Only the JSON
result goes to stdout; anything a parser library prints is sent to stderr.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_EXIT_REJECTED = 2
_EXIT_CRASHED = 3


def _emit(stream, payload: dict) -> None:
    stream.write(json.dumps(payload, ensure_ascii=True).encode("ascii"))
    stream.flush()


def main(argv: list[str]) -> int:
    result_stream = sys.stdout.buffer
    sys.stdout = sys.stderr
    try:
        path, max_units, max_chars = argv[1], int(argv[2]), int(argv[3])
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from src.document_formats import (
            DocumentFormatError,
            PypdfLoader,
            extract_units_in_process,
        )

        try:
            units = extract_units_in_process(
                path, pdf_loader_class=PypdfLoader, max_units=max_units or None
            )
            total = 0
            rows = []
            for unit in units:
                total += len(unit.text)
                if total > max_chars:
                    raise DocumentFormatError("Texte extrait trop volumineux")
                rows.append([unit.text, unit.page, unit.content_type, unit.location_label])
        except DocumentFormatError as exc:
            _emit(result_stream, {"error": str(exc)})
            return _EXIT_REJECTED
        _emit(result_stream, {"units": rows})
        return 0
    except MemoryError:
        _emit(result_stream, {"error": "Mémoire insuffisante pour analyser ce document"})
        return _EXIT_CRASHED
    except Exception as exc:
        _emit(result_stream, {"error": f"Analyse impossible ({type(exc).__name__})"})
        return _EXIT_CRASHED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
