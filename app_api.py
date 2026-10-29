"""FastAPI entry point: uvicorn app_api:app --port 7860."""

import os

from src.api import (
    bug_reports,
    chat,
    discovery,
    drafting,
    files,
    graphs,
    index_selection,
    index_transfer,
    library,
    lifecycle,
    models,
    notebooks,
    ocr,
    settings,
    updates,
)
from src.api.application import app

app.include_router(chat.router)
app.include_router(graphs.router)
app.include_router(bug_reports.router)
app.include_router(notebooks.router)
app.include_router(drafting.router)
app.include_router(discovery.router)
app.include_router(library.router)
app.include_router(models.router)
app.include_router(settings.router)
app.include_router(lifecycle.router)
app.include_router(files.router)
app.include_router(ocr.router)
app.include_router(index_transfer.router)
app.include_router(index_selection.router)
app.include_router(updates.router)


if __name__ == "__main__":
    import uvicorn

    from src.api.application import _persisted_app_settings
    from src.library import init_zotero
    from src.network_security import require_safe_bind_host

    # Init Zotero au démarrage
    if _persisted_app_settings.get("metadata_source") != "none":
        init_zotero()
    bind_host = os.getenv("BIBLIOBRAIN_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    require_safe_bind_host(bind_host)
    # Reuse this app: importing app_api again would register its routers twice.
    # log_config=None keeps Uvicorn on the handlers installed by configure_logging.
    uvicorn.run(app, host=bind_host, port=7860, reload=False, log_config=None)
