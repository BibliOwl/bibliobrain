"""Content-hash cache busting for the assets referenced by index.html.

Static files are cached by browsers for an hour (``CacheControlStaticFiles``).
Instead of bumping ``?v=`` strings by hand, the served index.html carries a
short hash of each referenced file, so a changed file always gets a new URL.
The source index.html writes ``?v=auto`` everywhere; a test keeps it that way.
"""

import functools
import hashlib
import re
from pathlib import Path

_VERSIONED_ASSET_RE = re.compile(r"(?P<url>/static/(?P<path>[\w./-]+))\?v=[\w.-]*")


@functools.lru_cache(maxsize=512)
def _content_hash(path: str, mtime_ns: int, size: int) -> str:
    # mtime and size are part of the cache key: an edited file is hashed again.
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


def asset_version(static_dir: Path, relative_path: str) -> str | None:
    """Short content hash of a file under ``static_dir``, or None when absent."""
    root = static_dir.resolve()
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    stat = target.stat()
    return _content_hash(str(target), stat.st_mtime_ns, stat.st_size)


def versioned_index_html(static_dir: Path) -> str:
    """index.html where every ``/static/...?v=`` names the asset's current content."""
    html = (static_dir / "index.html").read_text(encoding="utf-8")

    def replace(match: re.Match[str]) -> str:
        version = asset_version(static_dir, match.group("path"))
        return f"{match.group('url')}?v={version}" if version else match.group(0)

    return _VERSIONED_ASSET_RE.sub(replace, html)
