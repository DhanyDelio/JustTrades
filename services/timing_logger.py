from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
DASHBOARD_TIMING_LOG_PATH = LOGS_DIR / "dashboard_timing.log"


class _MilliFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created)
        return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(record.msecs):03d}"


_TIMING_LOGGER = logging.getLogger("dashboard_timing")
if not _TIMING_LOGGER.handlers:
    _TIMING_LOGGER.setLevel(logging.INFO)
    _TIMING_LOGGER.propagate = False
    _file_handler = logging.FileHandler(DASHBOARD_TIMING_LOG_PATH, mode="a", encoding="utf-8")
    _file_handler.setFormatter(_MilliFormatter("%(asctime)s %(message)s"))
    _TIMING_LOGGER.addHandler(_file_handler)


def log_timing(message: str, *, echo: bool = True) -> None:
    """Write timing diagnostics to the file, optionally mirroring to stdout."""
    if echo:
        print(message)
    _TIMING_LOGGER.info(message)
