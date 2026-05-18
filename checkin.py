"""checkin — core logic for the proactive check-in plugin.

Callable from the cron script as a library; does not import Hermes agent internals.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config defaults — must match plugin.yaml defaults
# ---------------------------------------------------------------------------

DEFAULTS = {
    "enabled": False,
    "frequency_minutes": 60,
    "quiet_hours": "",
    "timezone": "UTC",
    "max_per_day": 2,
    "idle_minutes": 120,
    "telegram_chat_id": "",
    "style": "gentle",
    "checkin_types": ["casual_checkin", "resume_prompt", "unfinished_thread_nudge"],
    "model": "auto",
    "provider": "",
    "state_dir": "",
}

# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

try:
    from hermes_constants import get_hermes_home
except Exception:
    import os

    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        val = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


def resolve_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge user config with defaults."""
    if not raw:
        raw = {}
    cfg = {**DEFAULTS, **raw}
    # Normalise state_dir relative to HERMES_HOME
    if not cfg["state_dir"]:
        cfg["state_dir"] = str(get_hermes_home() / "plugins" / "proactive-checkins" / "state")
    return cfg


# ---------------------------------------------------------------------------
# State paths
# ---------------------------------------------------------------------------

def _state_path(state_dir: str, name: str) -> Path:
    p = Path(state_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p / name


def _load_json(state_dir: str, name: str) -> Dict[str, Any]:
    path = _state_path(state_dir, name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(state_dir: str, name: str, data: Dict[str, Any]) -> None:
    _state_path(state_dir, name).write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------

def in_quiet_hours(quiet_hours: str, timezone_str: str = "UTC") -> bool:
    """Return True if current local time is within the quiet-hours window.

    quiet_hours format: "HH:MM-HH:MM" e.g. "22:00-08:00"
    Handles overnight windows (start > end).
    """
    if not quiet_hours:
        return False

    try:
        import zoneinfo

        tz = zoneinfo.ZoneInfo(timezone_str)
    except Exception:
        tz = timezone.utc

    now = datetime.now(tz)
    current_mins = now.hour * 60 + now.minute

    try:
        start_str, end_str = quiet_hours.split("-")
        start_h, start_m = map(int, start_str.strip().split(":"))
        end_h, end_m = map(int, end_str.strip().split(":"))
        start_mins = start_h * 60 + start_m
        end_mins = end_h * 60 + end_m
    except Exception:
        return False

    if start_mins <= end_mins:
        # Normal range, e.g. 09:00-17:00
        return start_mins <= current_mins <= end_mins
    else:
        # Overnight range, e.g. 22:00-08:00
        return current_mins >= start_mins or current_mins <= end_mins


# ---------------------------------------------------------------------------
# Idle detection
# ---------------------------------------------------------------------------

def get_last_activity_ts(state_dir: str) -> Optional[datetime]:
    """Return the last recorded activity timestamp, or None if no record."""
    data = _load_json(state_dir, "activity.json")
    ts_str = data.get("last_activity_ts")
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def minutes_since_last_activity(state_dir: str) -> Optional[float]:
    """Return minutes since last activity, or None if no record."""
    ts = get_last_activity_ts(state_dir)
    if ts is None:
        return None
    delta = datetime.now(timezone.utc) - ts
    return delta.total_seconds() / 60.0


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_daily_sent_count(state_dir: str) -> int:
    data = _load_json(state_dir, "daily_sent.json")
    if data.get("date") != _today_key():
        return 0
    return data.get("count", 0)


def increment_daily_sent(state_dir: str) -> None:
    data = _load_json(state_dir, "daily_sent.json")
    if data.get("date") != _today_key():
        data = {"date": _today_key(), "count": 0}
    data["count"] = data["count"] + 1
    _save_json(state_dir, "daily_sent.json", data)


def get_consecutive_no_reply(state_dir: str) -> int:
    return _load_json(state_dir, "counter.json").get("consecutive_no_reply", 0)


def increment_no_reply(state_dir: str) -> int:
    state = _load_json(state_dir, "counter.json")
    n = state.get("consecutive_no_reply", 0) + 1
    state["consecutive_no_reply"] = n
    _save_json(state_dir, "counter.json", state)
    return n


def record_sent(state_dir: str, checkin_type: str, message_preview: str) -> None:
    state = {
        "date": _today_key(),
        "type": checkin_type,
        "preview": message_preview[:80],
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_json(state_dir, "last_sent.json", state)


def get_last_sent_info(state_dir: str) -> Dict[str, Any]:
    return _load_json(state_dir, "last_sent.json")


# ---------------------------------------------------------------------------
# Session message reading
# ---------------------------------------------------------------------------

def get_recent_sessions(hermes_home: Path, limit: int = 5) -> List[Dict[str, Any]]:
    """Return the most recent session dicts, newest first."""
    sessions: List[Dict[str, Any]] = []
    sessions_dir = hermes_home / "sessions"
    if not sessions_dir.is_dir():
        return sessions

    for f in sorted(sessions_dir.glob("session_*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            sessions.append(data)
            if len(sessions) >= limit:
                break
        except Exception:
            continue
    return sessions


def get_last_message_pair(session: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Return (last_user_msg, last_assistant_msg) from a session dict.

    Looks at the 'messages' list, skipping system/tool messages.
    Returns (None, None) if no user/assistant pair found.
    """
    messages = session.get("messages", [])
    user_msgs = []
    assistant_msgs = []

    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, list):
            text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            content = " ".join(text_parts)
        elif not isinstance(content, str):
            content = str(content) if content else ""

        if role == "user" and content.strip():
            user_msgs.append(content.strip())
        elif role == "assistant" and content.strip():
            assistant_msgs.append(content.strip())

    last_user = user_msgs[-1] if user_msgs else None
    last_asst = assistant_msgs[-1] if assistant_msgs else None
    return last_user, last_asst


# ---------------------------------------------------------------------------
# Check-in type selection and warrant checking
# ---------------------------------------------------------------------------

@dataclass
class CheckinDecision:
    should_send: bool
    reason: str
    checkin_type: Optional[str] = None
    system_prompt: Optional[str] = None


def _type_warrants_followup(checkin_type: str, last_user: Optional[str], last_asst: Optional[str]) -> bool:
    """Return True if the check-in type is warranted given the conversation context."""
    if not last_asst:
        return False

    if checkin_type == "unfinished_thread_nudge":
        # Hermes asked a question or suggested something — user never answered
        question_indicators = ["?", "could you", "would you", "should i",
                               "want me to", "let me know", "does that work",
                               "does this make sense", "any questions"]
        has_question = any(ind in last_asst.lower() for ind in question_indicators)
        user_replied = last_user is not None
        return has_question and not user_replied

    elif checkin_type == "resume_prompt":
        # User was working on something, went quiet mid-task
        work_indicators = ["working on", "i'll", "let me", "starting",
                           "running", "checking", "looking into"]
        has_work = any(ind in (last_user or "").lower() or ind in (last_asst or "").lower()
                       for ind in work_indicators)
        user_replied = last_user is not None
        return has_work and not user_replied

    elif checkin_type == "casual_checkin":
        # Always warranted as a fallback if other types don't apply
        return True

    return False


def _build_checkin_prompt(checkin_type: str, last_asst: Optional[str], style: str) -> str:
    """Build the system prompt for generating the check-in message."""

    style_guidance = {
        "gentle": "Be warm, soft, and low-pressure. Don't make it sound like a reminder.",
        "friendly": "Be casual and upbeat. A little emoji is fine. Keep it light.",
        "minimal": "Be brief and to the point. No fluff.",
    }
    guidance = style_guidance.get(style, style_guidance["gentle"])

    type_instruction = {
        "unfinished_thread_nudge": (
            "The last message you sent asked the user a question or sought confirmation. "
            "Acknowledge they may have been busy, and gently offer to revisit if they want."
        ),
        "resume_prompt": (
            "The user seemed to be working on something or waiting on a result. "
            "Gently check if they want to pick it back up."
        ),
        "casual_checkin": (
            "Just a light, human check-in. The user has been quiet for a while. "
            "Say something short and warm that doesn't require a response."
        ),
    }

    context = ""
    if last_asst:
        context = f"\n\nYour last message in the conversation was:\n\"\"\"\n{last_asst[:500]}\n\"\"\""

    return f"""You are generating a short, human-feeling proactive check-in message.

{guidance}

{type_instruction.get(checkin_type, type_instruction["casual_checkin"])}
{context}

Generate a single check-in message (1-2 sentences max). It should:
- Feel natural, not scripted
- Never be demanding or needy
- Leave the door open for the user to pick up where they left off, or not
- Never say "I noticed you haven't responded"

Output ONLY the message itself — no quotes, no prefixes, no explanations."""


def make_decision(cfg: Dict[str, Any]) -> CheckinDecision:
    """Main decision function. Returns CheckinDecision with should_send and reason."""

    if not cfg.get("enabled"):
        return CheckinDecision(False, "disabled")

    state_dir = cfg["state_dir"]
    hermes_home = get_hermes_home()

    # 1. Quiet hours check
    if in_quiet_hours(cfg.get("quiet_hours", ""), cfg.get("timezone", "UTC")):
        return CheckinDecision(False, "within quiet hours")

    # 2. Rate limit — daily cap
    daily_sent = get_daily_sent_count(state_dir)
    if daily_sent >= cfg.get("max_per_day", 2):
        return CheckinDecision(False, f"daily cap reached ({daily_sent}/{cfg['max_per_day']})")

    # 3. Idle time check
    idle_mins = minutes_since_last_activity(state_dir)
    if idle_mins is None:
        # No activity recorded yet — first time setup, assume not idle
        idle_mins = 0
    min_idle = cfg.get("idle_minutes", 120)
    if idle_mins < min_idle:
        return CheckinDecision(False, f"not idle enough ({idle_mins:.0f} min < {min_idle} min)")

    # 4. Consecutive no-reply check — stop after 3 consecutive check-ins with no user reply
    no_reply_count = get_consecutive_no_reply(state_dir)
    if no_reply_count >= 3:
        return CheckinDecision(False, f"stopping after {no_reply_count} consecutive non-replies")

    # 5. Read session history to determine check-in type
    sessions = get_recent_sessions(hermes_home, limit=3)
    last_user: Optional[str] = None
    last_asst: Optional[str] = None
    for session in sessions:
        u, a = get_last_message_pair(session)
        if u or a:
            last_user, last_asst = u, a
            break

    # 6. Select check-in type
    checkin_types = cfg.get("checkin_types", ["casual_checkin", "resume_prompt", "unfinished_thread_nudge"])
    selected_type: Optional[str] = None
    for ct in checkin_types:
        if _type_warrants_followup(ct, last_user, last_asst):
            selected_type = ct
            break

    if selected_type is None:
        # Fallback to casual_checkin if no type warranted
        selected_type = "casual_checkin"

    # 7. Build the prompt for message generation
    system_prompt = _build_checkin_prompt(selected_type, last_asst, cfg.get("style", "gentle"))

    return CheckinDecision(
        should_send=True,
        reason=f"idle {idle_mins:.0f} min, type={selected_type}",
        checkin_type=selected_type,
        system_prompt=system_prompt,
    )
