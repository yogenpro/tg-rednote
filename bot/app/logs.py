"""Logging that a human can tail and a machine can query.

Two formats off one set of call sites:

* `text` (default) — what you want when watching a terminal;
* `json` — one object per line for Alloy → Loki, with a stable `event` name and
  typed fields, so counting failures or plotting latency doesn't mean parsing
  English.

Call sites add structure with `fields(...)`:

    log.info("delivered %d item(s)", n, extra=fields(event="delivery", items=n))

The fields ride in a single nested dict, which keeps them clear of the reserved
`LogRecord` attributes (`name`, `message`, `args`, …) that would otherwise
collide and raise at runtime.

Every line emitted while handling one submission carries the same `rid`, so a
single note's whole journey — fetch, comments, delivery, publish — can be
pulled out of a busy log with one filter.
"""

from __future__ import annotations

import contextvars
import json
import logging
import secrets
import sys
from datetime import datetime, timezone

# Set per submission; asyncio tasks each get their own copy.
current_rid: contextvars.ContextVar[str] = contextvars.ContextVar("rid", default="")

# Attributes logging puts on every record. Anything else a call site attached is
# ours, but we only read the one key to keep the contract obvious.
FIELDS_KEY = "fields"


def new_rid() -> str:
    """A short correlation id — long enough not to collide within a log file."""
    return secrets.token_hex(4)


def fields(**values) -> dict:
    """Structured fields for one log call. See the module docstring."""
    return {FIELDS_KEY: values}


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.rid = current_rid.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = getattr(record, "rid", "")
        if rid:
            payload["rid"] = rid
        payload.update(getattr(record, FIELDS_KEY, None) or {})
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ContextFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # httpx narrates every request at INFO; that is Alloy's bandwidth, not ours.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
