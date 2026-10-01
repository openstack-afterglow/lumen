"""Process logging to stderr and an optional rotating file."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

_file_handler: RotatingFileHandler | None = None


def configure_logging(process: Literal["api", "worker", "controller"]) -> None:
    """Configure process logs; LUMEN_LOG_DIRECTORY enables the additional file sink."""
    global _file_handler

    root = logging.getLogger()
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    if not any(
        isinstance(handler, logging.StreamHandler) and handler.stream in (sys.stdout, sys.stderr)
        for handler in root.handlers
    ):
        root.addHandler(logging.StreamHandler(sys.stderr))

    directory = os.environ.get("LUMEN_LOG_DIRECTORY")
    filename = str(Path(directory).expanduser().resolve() / f"{process}.log") if directory else None
    if _file_handler is not None and (_file_handler.baseFilename != filename or _file_handler not in root.handlers):
        root.removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None
    if filename is not None and _file_handler is None:
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(filename, maxBytes=10 * 1024 * 1024, backupCount=5)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
        _file_handler = handler

    # Uvicorn's default loggers have their own handlers and do not propagate to root.
    # Use the same stderr/file sinks, without its raw request-target access log.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        for handler in uvicorn_logger.handlers[:]:
            uvicorn_logger.removeHandler(handler)
        uvicorn_logger.setLevel(logging.NOTSET)
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
