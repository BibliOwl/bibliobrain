"""Launch the desktop-managed Bibliobrain server."""

from __future__ import annotations

import argparse
import os

import uvicorn

from src.config import LOG_DIR
from src.logging_setup import configure_logging
from src.network_security import require_safe_bind_host


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the managed Bibliobrain server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()

    require_safe_bind_host(args.host)
    os.environ["BIBLIOBRAIN_MANAGED_SERVER"] = "1"
    configure_logging(LOG_DIR)
    # log_config=None keeps Uvicorn on the root handlers installed above, so the
    # access and error logs land in the rotating file as well as on the console.
    uvicorn.run("app_api:app", host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
