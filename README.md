# MoelachBot

A Slack bot that posts randomly selected Death Rattles from Brandon Sanderson's *Stormlight Archive* series. Supports both reactive posting (triggered by channel activity) and scheduled posting at fixed times.

## Setup

### Slack App

1. Create a Slack app at https://api.slack.com/apps
2. Enable **Socket Mode** and generate an app-level token (`xapp-...`)
3. Add the following **Bot Token Scopes** under OAuth & Permissions:
   - `channels:history` — receive message events
   - `channels:read` — resolve channel names to IDs
   - `chat:write` — post messages
4. Subscribe to the `message.channels` event under Event Subscriptions
5. Install the app to your workspace and copy the bot token (`xoxb-...`)

### Installation

Requires Python 3.10+.

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your tokens and channel configuration
```

### Configuration

All configuration is via environment variables (or `.env` file):

| Variable | Required | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Bot user OAuth token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | App-level token for Socket Mode (`xapp-...`) |
| `MONITORED_CHANNELS` | No | Comma-separated channel IDs or names for reactive posting |
| `COOLDOWN_HOURS` | No | Hours between reactive posts per channel (default: 48) |
| `RANDOM_DELAY_MAX_MINUTES` | No | Max random delay before reactive post (default: 180) |
| `SCHEDULE_CHANNEL` | No | Channel for scheduled posts (omit to disable scheduling) |
| `SCHEDULE_DAY` | No | Day of week, e.g. `wed` (default: `wed`) |
| `SCHEDULE_HOUR` | No | Hour in 24h format (default: 10) |
| `SCHEDULE_MINUTE` | No | Minute (default: 0) |
| `SCHEDULE_TIMEZONE` | No | Timezone, e.g. `America/Los_Angeles` |
| `SCHEDULE_RATTLE` | No | Pin scheduled post to a specific rattle number (1–34) |

## Usage

### Start the bot

```bash
python bot.py
```

### Send a custom message

```bash
python bot.py --send "Your message here"
python bot.py --send "Your message here" --channel general
```

### Reset cooldowns

```bash
python bot.py --reset-cooldown              # Clear all channel cooldowns
python bot.py --reset-cooldown C01ABCDEF    # Clear a specific channel
```

## Death Rattles

The 34 death rattle quotes live in `death_rattles.md`. Edit that file to add, remove, or reorder quotes — no code changes needed. The bot cycles through all rattles before repeating any.

## How It Works

**Reactive posting:** When a message is posted in a monitored channel, the bot waits a random delay (0 to `RANDOM_DELAY_MAX_MINUTES`), then posts a death rattle — as long as the channel isn't on cooldown.

**Scheduled posting:** Uses APScheduler to post a death rattle to `SCHEDULE_CHANNEL` at the configured day and time each week.

**State** is persisted to `state.json` (cooldown timestamps and used rattle indices). Logs go to both the console and `moelachbot.log`.
