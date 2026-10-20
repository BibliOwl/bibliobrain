"""Update check HTTP routes: status, manual check and one-click update."""

from fastapi import APIRouter, HTTPException, Request

from src import update_check
from src.api import application
from src.api.common import is_loopback_request

router = APIRouter()


def _require_local(request: Request) -> None:
    # Replacing the program is reserved to this computer, never to a LAN client.
    if not is_loopback_request(request):
        raise HTTPException(
            status_code=403,
            detail="La mise à jour est réservée à cet appareil.",
        )


def _index_or_operation_busy() -> bool:
    return application._rag_init_lock.locked() or application._managed_active_operations > 0


@router.get("/api/update/status")
def api_update_status():
    return update_check.get_status()


@router.post("/api/update/check")
def api_update_check(request: Request, force: bool = False):
    """``force`` is a user click on "Check now"; otherwise only when enabled and due."""
    _require_local(request)
    return update_check.check_now(force=force)


@router.post("/api/update/start")
def api_update_start(request: Request):
    _require_local(request)
    result = update_check.start_update(is_busy=_index_or_operation_busy)
    return {**result, "status": update_check.get_status()}
