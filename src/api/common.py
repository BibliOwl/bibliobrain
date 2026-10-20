"""Response helpers and request dependencies shared by the feature routers.

Nothing here holds server state: the lifecycle state lives in
``src.api.application`` and the stream cancellation registry in
``src.api.streams``.
"""

import asyncio
import ipaddress
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from src.app_settings import get_app_settings
from src.http_security import client_ip_for
from src.unicode_utils import json_dumps_utf8

EXTERNAL_API_PERMISSION_CODE = "external_api_permission_required"
EXTERNAL_API_PERMISSION_MESSAGE = (
    "Fonction désactivée par confidentialité. Activez l'option API externes "
    "dans les settings avant tout appel à Semantic Scholar, Crossref, "
    "OpenAlex ou Unpaywall."
)


def api_error(
    code: str,
    *,
    status_code: int,
    message: str = "",
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"ok": False, "error": code}
    if message:
        payload["msg"] = message
    if extra:
        payload.update(extra)
    return JSONResponse(status_code=status_code, content=payload)


def sse(data: dict) -> str:
    """Encode one Server-Sent Events message."""
    return f"data: {json_dumps_utf8(data, ensure_ascii=False)}\n\n"


async def iter_sync_gen(gen):
    """Itère un générateur synchrone via to_thread sans bloquer l'event loop."""
    done = object()
    while True:
        chunk = await asyncio.to_thread(next, gen, done)
        if chunk is done:
            break
        yield chunk


def is_loopback_request(request: Request) -> bool:
    host = str(client_ip_for(request) or "").strip().lower()
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def external_api_requests_allowed() -> bool:
    """Privacy setting gating every call to Semantic Scholar, Crossref, OpenAlex..."""
    return bool(get_app_settings().get("allow_external_api_requests"))


# Route parameter: tests switch it with ``app.dependency_overrides``.
ExternalApiAllowed = Annotated[bool, Depends(external_api_requests_allowed)]


def research_scope_enabled() -> bool:
    """Server-side kill switch for the experimental people/institution mapper."""
    return bool(get_app_settings().get("research_scope_enabled", True))


def external_api_permission_response(
    message: str = EXTERNAL_API_PERMISSION_MESSAGE,
) -> JSONResponse:
    return api_error(EXTERNAL_API_PERMISSION_CODE, status_code=403, message=message)
