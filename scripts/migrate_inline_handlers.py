"""Inventory inline browser event handlers left in the static frontend.

The migration to ``static/js/static-event-bindings.js`` is complete: a new
static handler goes in that table under a descriptive marker (for example
``switch-tab-chat``) and the element gets ``data-bb-static-on<event>="<marker>"``.
This script only reports inline ``on<event>=`` attributes that slipped back in.
"""

from __future__ import annotations

import collections
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [ROOT / "static" / "index.html", *(ROOT / "static" / "js").glob("*.js")]
HANDLER_RE = re.compile(
    r"(?<![\w-])on(?P<event>click|change|input|keydown|keyup|submit|dragstart|dragover|drop|"
    r"contextmenu|dblclick|pointerdown|mousedown|mouseover|mouseout|focus|blur)"
    r"=(?P<quote>[\"'])(?P<code>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)


def main() -> None:
    found: list[tuple[Path, str, str]] = []
    for path in TARGETS:
        text = path.read_text(encoding="utf-8")
        found.extend(
            (path, match.group("event").lower(), match.group("code"))
            for match in HANDLER_RE.finditer(text)
        )
    counts = collections.Counter(code for _path, _event, code in found)
    print(f"handlers={len(found)} unique={len(counts)}")
    for code, count in counts.most_common():
        print(f"{count:3} {code}")


if __name__ == "__main__":
    main()
