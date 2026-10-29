"""Named steps for the document preparation progress bar.

``init_rag`` reports one fraction for the whole load: checks up to 4 %, reading
and embedding the documents up to ~85 %, then opening the chat model. The bar
used to show only that number, and any step that restarted lower looked like
the whole preparation starting over. Every event now says which step it
belongs to ("Step 2 of 3"), and the fraction never goes back within a load.
"""

from __future__ import annotations

import threading
from typing import Any

STAGES = ("check", "documents", "finish")
_DOCUMENTS_FROM = 0.04
_FINISH_FROM = 0.9


def _stage_for(frac: float) -> int:
    if frac < _DOCUMENTS_FROM:
        return 1
    if frac < _FINISH_FROM:
        return 2
    return 3


class StageTracker:
    """Turns raw ``(frac, desc)`` reports into monotonic, staged events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stage = 1
        self._frac = 0.0

    def event(self, frac: float, desc: str = "") -> dict[str, Any]:
        try:
            value = float(frac)
        except (TypeError, ValueError):
            value = self._frac
        value = max(0.0, min(1.0, value))
        with self._lock:
            # A step never goes back, and neither does the bar within a load.
            self._stage = max(self._stage, _stage_for(value))
            self._frac = max(self._frac, value)
            stage = self._stage
            shown = self._frac
        return {
            "type": "progress",
            "frac": round(shown, 3),
            "desc": desc,
            "stage": stage,
            "stage_count": len(STAGES),
            "stage_key": STAGES[stage - 1],
        }
