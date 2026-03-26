# Telegram Copilot Bridge

Minimal Telegram bot that forwards plain text messages to GitHub Copilot CLI for a specific local repository and sends the full raw CLI output back to Telegram.

## What It Does

- Polls a Telegram bot with `getUpdates`
- Whitelists specific Telegram user IDs
- Runs `copilot -p` inside a configured repository
- Continues Copilot context per Telegram user
- Returns full Copilot CLI output as plain Telegram messages

## Requirements

- Linux VM or server
- Python 3
- GitHub Copilot CLI installed and authenticated
- A Telegram bot token from BotFather
- At least one Telegram numeric user ID to whitelist

## Files

- `bot.py` - the bridge process
- `.env.example` - environment template
- `deploy/telegram-copilot-bridge.service.example` - systemd service template

## Setup

1. Clone this repository.
2. Copy `.env.example` to `.env`.
3. Edit `.env`:
   - set `TELEGRAM_BOT_TOKEN`
   - set `ALLOWED_USER_IDS`
   - set `REPO_PATH`
4. Authenticate Copilot CLI on the machine:

```bash
copilot login
```

5. Start locally for a quick test:

```bash
python3 bot.py
```

## Commands

- `/start` - show help
- `/help` - show help
- `/new` - start a fresh Copilot thread for the current Telegram account
- `/status` - show repo and session status
- `/copilot <prompt>` - send an explicit prompt
- plain text message - send that text to Copilot

## Systemd

Use the example unit in `deploy/telegram-copilot-bridge.service.example` and replace `__REPO_PATH__` with your clone path.

Then install and start it:

```bash
sudo cp deploy/telegram-copilot-bridge.service.example /etc/systemd/system/telegram-copilot-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-copilot-bridge.service
```

## Security Notes

- Do not commit `.env`
- Whitelist only trusted Telegram user IDs
- Copilot CLI runs with the permissions of the service user
- Revoke Copilot CLI authorization in GitHub at:
  - `Settings -> Applications -> Authorized OAuth Apps -> GitHub Copilot CLI`

## Notes

- This bridge is text-only. It does not render screenshots.
- It uses long polling, not webhooks.
- Session continuation is tracked per Telegram user ID in `state.json`.