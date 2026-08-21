"""Durable state: one small JSON file on one volume (PLAN §5).

Everything else in the process is disposable. The file holds secrets — the XHS
cookie and the 1point3acres one — so it is written 0600 and replaced atomically.
"""

from __future__ import annotations

import json
import os
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PAIRING_ALPHABET = string.ascii_uppercase + string.digits


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def generate_pairing_code() -> str:
    """Six characters of CSPRNG output, grouped for readability."""
    raw = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(6))
    return f"{raw[:3]}-{raw[3:]}"


class State:
    DEFAULTS: dict[str, Any] = {
        "owner_id": None,
        "allowlist": [],
        # Group chats whose RedNote links become submissions. A group is added
        # when an allowlisted user adds the bot to it, or by /allowgroup.
        "groups": {},  # str(chat_id) -> {"title": str, "added_by": int, "at": iso}
        "pairing_code_used": False,
        "xhs_cookie": None,
        "cookie_set_at": None,
        "cookie_status": "unset",  # unset | ok | stale
        # 1point3acres sits behind a Cloudflare managed challenge, so there is
        # no anonymous mode: the owner's browser cookie is the only way in.
        # cf_clearance is bound to the User-Agent that solved the challenge,
        # so the UA is stored with it rather than assumed.
        "acres_cookie": None,
        "acres_ua": None,
        "acres_cookie_set_at": None,
        "acres_cookie_status": "unset",  # unset | ok | stale
        "last_successful_fetch": None,
        # note_id -> {"chat": str, "message_id": int, "at": iso}. Keeps a
        # resubmitted link from posting to the channel twice.
        "published": {},
    }

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = dict(self.DEFAULTS)
        self.load()

    # ---- persistence -------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"state file {self.path} is unreadable: {exc}") from None
        if not isinstance(loaded, dict):
            raise SystemExit(f"state file {self.path} is not a JSON object")
        self.data = {**self.DEFAULTS, **loaded}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    # ---- accessors ---------------------------------------------------

    @property
    def owner_id(self) -> int | None:
        return self.data.get("owner_id")

    @property
    def allowlist(self) -> list[int]:
        return list(self.data.get("allowlist") or [])

    @property
    def cookie(self) -> str | None:
        return self.data.get("xhs_cookie") or None

    @property
    def cookie_status(self) -> str:
        return self.data.get("cookie_status") or "unset"

    @property
    def acres_cookie(self) -> str | None:
        return self.data.get("acres_cookie") or None

    @property
    def acres_ua(self) -> str | None:
        return self.data.get("acres_ua") or None

    @property
    def acres_cookie_status(self) -> str:
        return self.data.get("acres_cookie_status") or "unset"

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self.allowlist

    def claim_owner(self, user_id: int) -> None:
        self.data["owner_id"] = user_id
        self.data["pairing_code_used"] = True
        if user_id not in self.allowlist:
            self.data["allowlist"] = [*self.allowlist, user_id]
        self.save()

    def allow(self, user_id: int) -> bool:
        if user_id in self.allowlist:
            return False
        self.data["allowlist"] = [*self.allowlist, user_id]
        self.save()
        return True

    def deny(self, user_id: int) -> bool:
        if user_id not in self.allowlist or user_id == self.owner_id:
            return False
        self.data["allowlist"] = [u for u in self.allowlist if u != user_id]
        self.save()
        return True

    @property
    def groups(self) -> dict:
        return dict(self.data.get("groups") or {})

    def listens_in(self, chat_id: int) -> bool:
        return str(chat_id) in (self.data.get("groups") or {})

    def allow_group(self, chat_id: int, title: str, added_by: int | None = None) -> bool:
        groups = self.groups
        if str(chat_id) in groups:
            return False
        groups[str(chat_id)] = {"title": title, "added_by": added_by, "at": utcnow()}
        self.data["groups"] = groups
        self.save()
        return True

    def deny_group(self, chat_id: int) -> bool:
        groups = self.groups
        if str(chat_id) not in groups:
            return False
        groups.pop(str(chat_id))
        self.data["groups"] = groups
        self.save()
        return True

    def set_cookie(self, cookie: str) -> None:
        self.data["xhs_cookie"] = cookie
        self.data["cookie_set_at"] = utcnow()
        self.data["cookie_status"] = "ok"
        self.save()

    def clear_cookie(self) -> None:
        self.data["xhs_cookie"] = None
        self.data["cookie_set_at"] = None
        self.data["cookie_status"] = "unset"
        self.save()

    def mark_cookie_stale(self) -> bool:
        """Returns True if this call is what flipped the status (so we notify once)."""
        if self.cookie_status == "stale":
            return False
        self.data["cookie_status"] = "stale"
        self.save()
        return True

    def set_acres_cookie(self, cookie: str, user_agent: str = "") -> None:
        self.data["acres_cookie"] = cookie
        self.data["acres_ua"] = user_agent or None
        self.data["acres_cookie_set_at"] = utcnow()
        self.data["acres_cookie_status"] = "ok"
        self.save()

    def clear_acres_cookie(self) -> None:
        self.data["acres_cookie"] = None
        self.data["acres_ua"] = None
        self.data["acres_cookie_set_at"] = None
        self.data["acres_cookie_status"] = "unset"
        self.save()

    def mark_acres_cookie_stale(self) -> bool:
        """True only on the transition, so the owner is told once."""
        if self.acres_cookie_status == "stale":
            return False
        self.data["acres_cookie_status"] = "stale"
        self.save()
        return True

    def mark_acres_success(self) -> None:
        if self.acres_cookie and self.acres_cookie_status != "ok":
            self.data["acres_cookie_status"] = "ok"
            self.save()

    PUBLISHED_LIMIT = 1000

    def published(self, note_id: str) -> dict | None:
        if not note_id:
            return None
        entry = (self.data.get("published") or {}).get(note_id)
        return entry if isinstance(entry, dict) else None

    def record_published(self, note_id: str, chat: str, message_id: int) -> None:
        if not note_id:
            return
        index = dict(self.data.get("published") or {})
        index.pop(note_id, None)  # re-insert so the newest sits last
        index[note_id] = {"chat": str(chat), "message_id": message_id, "at": utcnow()}
        while len(index) > self.PUBLISHED_LIMIT:
            index.pop(next(iter(index)))
        self.data["published"] = index
        self.save()

    def forget_published(self, note_id: str) -> bool:
        index = dict(self.data.get("published") or {})
        if note_id not in index:
            return False
        index.pop(note_id)
        self.data["published"] = index
        self.save()
        return True

    def mark_fetch_success(self) -> None:
        self.data["last_successful_fetch"] = utcnow()
        if self.cookie and self.cookie_status != "ok":
            self.data["cookie_status"] = "ok"
        self.save()
