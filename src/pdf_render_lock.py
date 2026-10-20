"""PDFium calls must be serialized across background OCR and interactive figures."""

from functools import wraps
from threading import RLock

PDFIUM_LOCK = RLock()


def pdfium_locked(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with PDFIUM_LOCK:
            return function(*args, **kwargs)
    return locked
