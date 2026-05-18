#!/usr/bin/env python3
"""
checkin_cron.py — no-agent cron worker for proactive check-ins.

Called by a hermes cron job every N minutes. Reads the check-in config,
makes the decision via checkin.py, and sends the message via the Telegram API
(or falls back to the configured delivery channel).

Usage:
    python3 checkin_cron.py

Environment variables (optional, override config file):
    HERMES_HOME          — Hermes home directory
    CHECKIN_TELEGRAM_CHAT_ID  — Telegram chat ID to send to
    CHECKIN_PROVIDER     — model provider (default: auto)
    CHECKIN_MODEL        — model name (default: auto)

No arguments. Exit codes:
    0  — ran successfully (check sent or skipped intentionally)
    1  — error (will show in cron run log)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Bootstrap — add plugin dir to path for the library import
# ---------------------------------------------------------------------------

PLUGIN_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PLUGIN_DIR))

from checkin import make_decision, resolve_config
from checkin import in_quiet_hours, minutes_since_last_activity
from checkin import get_daily_sent_count, get_consecutive_no_reply
from checkin import increment_daily_sent, record_sent, increment_no_reply

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("checkin_cron")


# ---------------------------------------------------------------------------
# Hermes home
# ---------------------------------------------------------------------------

def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val).resolve() if val else (Path.home() / ".hermes")


# ---------------------------------------------------------------------------
# Config file reading (from Hermes config — loaded by the cron skill prompt)
# ---------------------------------------------------------------------------

def _load_plugin_config() -> Dict[str, Any]:
    """Read plugin config from HERMES_HOME/config.yaml if present."""
    config_file = _hermes_home() / "config.yaml"
    if not config_file.exists():
        return {}

    try:
        import yaml
        with open(config_file, encoding="utf-8") as f:
            full = yaml.safe_load(f) or {}
        plugins = full.get("plugins", {})
        if isinstance(plugins, dict):
            return plugins.get("proactive-checkins", {})
    except Exception as exc:
        log.warning("Could not read plugin config: %s", exc)

    return {}


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

def _get_telegram_config() -> tuple[Optional[str], Optional[str]]:
    """Return (bot_token, chat_id) from config or env."""
    chat_id = os.environ.get("CHECKIN_TELEGRAM_CHAT_ID", "").strip()
    bot_token = os.environ.get("HERMES_TELEGRAM_BOT_TOKEN", "").strip()

    # Try reading from Hermes config
    if not chat_id or not bot_token:
        try:
            import yaml
            config_file = _hermes_home() / "config.yaml"
            if config_file.exists():
                with open(config_file, encoding="utf-8") as f:
                    full = yaml.safe_load(f) or {}
                tg = full.get("platforms", {}).get("telegram", {})
                if isinstance(tg, dict):
                    if not bot_token:
                        bot_token = tg.get("bot_token", "") or tg.get("token", "")
                    if not chat_id:
                        chat_id = tg.get("allowed_chat_ids", [""])[0] if isinstance(tg.get("allowed_chat_ids"), list) else ""
        except Exception:
            pass

    return bot_token if bot_token else None, chat_id if chat_id else None


def _send_telegram_message(bot_token: str, chat_id: str, text: str) -> bool:
    """Send a message via the Telegram Bot API. Returns True on success."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            if body.get("ok"):
                return True
            log.error("Telegram API error: %s", body.get("description", "unknown"))
    except urllib.error.URLError as exc:
        log.error("Telegram request failed: %s", exc)
    except Exception as exc:
        log.error("Telegram send error: %s", exc)
    return False


# ---------------------------------------------------------------------------
# Model call (lightweight — generate the check-in message)
# ---------------------------------------------------------------------------

def _call_model(prompt: str, cfg: Dict[str, Any]) -> Optional[str]:
    """Call the configured model to generate a check-in message.

    Uses the OpenAI-compatible API endpoint if available (same as Hermes provider).
    Falls back to a simple local model call via HTTP.
    """
    import urllib.request
    import urllib.error

    # Build the message
    model = cfg.get("model", "auto")
    provider = cfg.get("provider", "")

    # Try to read from hermes config / env
    api_base = os.environ.get("OPENAI_API_BASE", "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "dummy").strip()

    if not api_base:
        # Try reading from Hermes config
        try:
            import yaml
            config_file = _hermes_home() / "config.yaml"
            if config_file.exists():
                with open(config_file, encoding="utf-8") as f:
                    full = yaml.safe_load(f) or {}
                api_base = full.get("model_providers", {}).get("openai", {}).get("openai", {}).get("base_url", "") or \
                           full.get("openai_api_base", "")
        except Exception:
            pass

    if not api_base:
        log.warning("No API base configured — cannot generate check-in message")
        return None

    messages = [{"role": "user", "content": prompt}]
    payload = json.dumps({
        "model": model if model != "auto" else "gpt-4o-mini",
        "messages": messages,
        "max_tokens": 150,
        "temperature": 0.8,
    }).encode("utf-8")

    req = urllib.request.Request(
        api_base.rstrip("/") + "/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
            choices = body.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "").strip()
    except urllib.error.HTTPError as exc:
        log.error("Model API HTTP error %s: %s", exc.code, exc.read())
    except Exception as exc:
        log.error("Model API call failed: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Fallback: static templates (when no model available)
# ---------------------------------------------------------------------------

_FALLBACK_MESSAGES = {
    "unfinished_thread_nudge": [
        "Hey — just circling back. Want me to pick up where we left off?",
        "No rush, but I'm around if you want to continue with that thread earlier.",
        "Quick check-in: anything from earlier you'd like to revisit?",
    ],
    "resume_prompt": [
        "Hey — you went quiet after that last thread. Want me to keep an eye on it?",
        "Just a heads up: I'm still around if you want to continue with anything.",
        "You seemed to be working on something — want me to help with anything?",
    ],
    "casual_checkin": [
        "Hey — anything you want me to keep an eye on this afternoon?",
        "Quick check-in: anything I can help with?",
        "Hey — I'm around if you need me.",
    ],
}


def _fallback_message(checkin_type: str) -> str:
    import random
    opts = _FALLBACK_MESSAGES.get(checkin_type, _FALLBACK_MESSAGES["casual_checkin"])
    return random.choice(opts)


# ---------------------------------------------------------------------------
# State update helpers
# ---------------------------------------------------------------------------

def _record_checkin_sent(state_dir: str, checkin_type: str, message: str) -> None:
    increment_daily_sent(state_dir)
    record_sent(state_dir, checkin_type, message)
    increment_no_reply(state_dir)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # Load config
    raw_cfg = _load_plugin_config()
    cfg = resolve_config(raw_cfg)

    log.info("Running proactive check-in decision...")

    decision = make_decision(cfg)

    if not decision.should_send:
        log.info("Skipping check-in: %s", decision.reason)
        return 0

    log.info("Check-in warranted: %s", decision.reason)

    state_dir = cfg["state_dir"]

    # Generate message
    if decision.system_prompt:
        message = _call_model(decision.system_prompt, cfg)
        if not message:
            message = _fallback_message(decision.checkin_type or "casual_checkin")
    else:
        message = _fallback_message(decision.checkin_type or "casual_checkin")

    message = message.strip()

    # Deliver
    bot_token, chat_id = _get_telegram_config()

    if bot_token and chat_id:
        success = _send_telegram_message(bot_token, chat_id, message)
        if success:
            _record_checkin_sent(state_dir, decision.checkin_type or "casual_checkin", message)
            log.info("Check-in sent: %s", message[:80])
            print(message)
            return 0
        else:
            log.error("Failed to send Telegram message")
            return 1
    else:
        # No Telegram config — output to stdout for cron capture
        log.warning("No Telegram bot token/chat_id configured — printing to stdout")
        _record_checkin_sent(state_dir, decision.checkin_type or "casual_checkin", message)
        print(message)
        return 0


if __name__ == "__main__":
    sys.exit(main())
