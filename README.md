# proactive-checkins

Optional, configurable proactive check-ins for Hermes Agent. Hermes occasionally
sends a short, warm follow-up message when the conversation has been idle,
the user seems to have left off mid-thread, or there's something worth circling
back on.

**Off by default** — explicitly opt-in per the config below.

## How it works

1. A **cron job** fires every N minutes (default: 60)
2. The cron script (`scripts/checkin_cron.py`) checks:
   - Quiet hours window
   - Idle time since last user activity
   - Daily send cap
   - Whether the last Hermes message warrants a follow-up
3. If all guards pass → generates a short message and sends via Telegram
4. If the user doesn't reply after 3 consecutive check-ins → stops

The **plugin** (`__init__.py`) handles state tracking:
- `on_session_end` → records last activity timestamp
- `on_session_start` → resets the consecutive-no-reply counter

## Setup

### 1. Install the plugin

```bash
hermes plugins install proactive-checkins
# or point at a Git URL
hermes plugins install https://github.com/you/proactive-checkins
```

### 2. Enable in `config.yaml`

```yaml
plugins:
  proactive-checkins:
    enabled: true
    frequency_minutes: 60      # cron interval
    idle_minutes: 120          # min idle before first check-in
    quiet_hours: "22:00-08:00" # no check-ins during this window (empty = off)
    timezone: "UTC"             # for quiet hours
    max_per_day: 2             # hard daily cap
    style: gentle              # gentle | friendly | minimal
    checkin_types:
      - casual_checkin          # always available
      - resume_prompt           # triggers if user was mid-task
      - unfinished_thread_nudge # triggers if last Hermes msg was a question
    telegram_chat_id: "5446506042"  # your Telegram chat ID
```

### 3. Set up a cron job

```bash
hermes cron create \
  --name "Proactive check-ins" \
  --script ~/.hermes/plugins/proactive-checkins/scripts/checkin_cron.py \
  --schedule "*/30 * * * *" \
  --no-agent \
  --deliver origin
```

This runs every 30 minutes. Set `--schedule "*/15 * * * *"` for 15-minute intervals.

Or create it inline in chat:

```
/cron create Proactive check-ins --script ~/.hermes/plugins/proactive-checkins/scripts/checkin_cron.py --schedule "*/30 * * * *" --no-agent
```

## Config reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Master switch — must be `true` to activate |
| `frequency_minutes` | int | `60` | How often the cron fires (informational — set via cron schedule) |
| `idle_minutes` | int | `120` | Minimum idle time before sending a check-in |
| `quiet_hours` | string | `""` | Window with no check-ins, e.g. `"22:00-08:00"`. Empty = off |
| `timezone` | string | `"UTC"` | Timezone for quiet hours |
| `max_per_day` | int | `2` | Hard daily cap on sent check-ins |
| `style` | string | `"gentle"` | `gentle` (warm, soft) / `friendly` (casual, emoji OK) / `minimal` (terse) |
| `checkin_types` | list | all three | Which check-in types to use and in what priority order |
| `model` | string | `"auto"` | Model to generate messages (or `"auto"` for cheapest) |
| `provider` | string | `""` | Provider for the model (empty = use default) |
| `telegram_chat_id` | string | `""` | Chat ID for Telegram delivery. Empty = use session origin |

## Check-in types

- **`casual_checkin`** — Always valid as a fallback. "Hey — anything you want me to keep an eye on this afternoon?"
- **`resume_prompt`** — Triggers if the last exchange showed the user was working on something. "You seemed to be mid-task — want to pick that back up?"
- **`unfinished_thread_nudge`** — Triggers if Hermes's last message asked a question. "Hey — want me to revisit that question from earlier?"

## State files

State lives in `~/.hermes/plugins/proactive-checkins/state/`:

- `activity.json` — last user activity timestamp (updated by plugin hook)
- `counter.json` — consecutive no-reply counter (reset on user reply)
- `daily_sent.json` — daily sent count with date key (reset each calendar day)
- `last_sent.json` — info about the last sent check-in (type + preview)

## Telegram setup

The script reads Telegram credentials from (in order of precedence):

1. Environment variables: `HERMES_TELEGRAM_BOT_TOKEN`, `CHECKIN_TELEGRAM_CHAT_ID`
2. `~/.hermes/config.yaml` → `platforms.telegram.bot_token` / `allowed_chat_ids`

Make sure your Telegram bot has been started by the user (send `/start` to it first).

## Fallback messages

If the model API call fails, a static template is used instead:

- **casual_checkin**: "Hey — anything you want me to keep an eye on this afternoon?"
- **resume_prompt**: "Just a heads up: I'm still around if you want to continue."
- **unfinished_thread_nudge**: "Hey — just circling back. Want me to pick up where we left off?"

## Stopping behavior

If the user doesn't reply after 3 consecutive check-ins, the plugin stops sending.
The counter resets as soon as the user sends any message.

## Uninstall

```bash
hermes plugins remove proactive-checkins
# Also remove the cron job:
hermes cron list   # find the check-in job ID
hermes cron remove <job_id>
```
