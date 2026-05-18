"""proactive-checkins plugin — optional, configurable proactive follow-up messages.

Wires two behaviours:

1. ``on_session_end`` hook — records last activity timestamp so the cron
   script can determine how long the user has been idle.

2. ``on_session_start`` hook — resets the consecutive-no-reply counter when
   the user sends a new message, so Hermes stops sending check-ins after
   repeated non-responses.

The actual scheduling is handled by a cron job that calls
``scripts/checkin_cron.py`` — plugins have no built-in background scheduling,
so a dumb cron poll approximates the behaviour.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

try:
    from hermes_constants import get_hermes_home
except Exception:
    import os

    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        val = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


def _state_dir() -> Path:
    return get_hermes_home() / "plugins" / "proactive-checkins" / "state"


def _state_file(name: str) -> Path:
    d = _state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / name


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Last activity tracking (called from on_session_end)
# ---------------------------------------------------------------------------

def record_activity(session_id: str) -> None:
    """Touch the last-activity timestamp so the cron script knows idle time."""
    state = _load_json(_state_file("activity.json"))
    state["last_session_id"] = session_id
    state["last_activity_ts"] = _now_ts()
    _save_json(_state_file("activity.json"), state)


# ---------------------------------------------------------------------------
# Consecutive non-response counter (called from on_session_start)
# ---------------------------------------------------------------------------

def record_reply() -> None:
    """Reset the consecutive-non-response counter when user sends a message."""
    state = _load_json(_state_file("counter.json"))
    state["consecutive_no_reply"] = 0
    _save_json(_state_file("counter.json"), state)


def increment_no_reply() -> int:
    """Increment and return the consecutive-no-reply counter."""
    state = _load_json(_state_file("counter.json"))
    n = state.get("consecutive_no_reply", 0) + 1
    state["consecutive_no_reply"] = n
    state["last_increment_ts"] = _now_ts()
    _save_json(_state_file("counter.json"), state)
    return n


# ---------------------------------------------------------------------------
# Daily sent counter
# ---------------------------------------------------------------------------

def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_daily_sent() -> int:
    state = _load_json(_state_file("daily_sent.json"))
    if state.get("date") != _today_key():
        return 0
    return state.get("count", 0)


def increment_daily_sent() -> None:
    state = _load_json(_state_file("daily_sent.json"))
    if state.get("date") != _today_key():
        state = {"date": _today_key(), "count": 0}
    state["count"] = state["count"] + 1
    _save_json(_state_file("daily_sent.json"), state)


# ---------------------------------------------------------------------------
# Last sent check-in tracking (to avoid re-sending same type too soon)
# ---------------------------------------------------------------------------

def get_last_sent_info() -> Dict[str, Any]:
    return _load_json(_state_file("last_sent.json"))


def record_sent(checkin_type: str, message_preview: str) -> None:
    state = {
        "date": _today_key(),
        "type": checkin_type,
        "preview": message_preview[:80],
        "sent_at": _now_ts(),
    }
    _save_json(_state_file("last_sent.json"), state)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    **_: Any,
) -> None:
    if session_id:
        record_activity(session_id)


def _on_session_start(session_id: str = "", **_: Any) -> None:
    record_reply()


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_session_start", _on_session_start)
