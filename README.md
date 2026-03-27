# Telegram Copilot Bridge

Minimal Telegram bot that forwards plain text messages to GitHub Copilot CLI for a specific local repository and sends a cleaned human-readable summary back to Telegram by default.

## What It Does

- Polls a Telegram bot with `getUpdates`
- Whitelists specific Telegram user IDs
- Runs `copilot -p` inside a configured repository
- Continues Copilot context per Telegram user
- Returns a human-readable summary by default and exposes the full technical trace through `/debug`
- Includes replied-to Telegram message text and attachments as Copilot context
- Accepts `/upload`, asks for a storage name, and uploads Telegram media without overwriting existing names

## Requirements

- Linux VM or server
- Python 3
- Node.js and npm
- GitHub Copilot CLI installed and authenticated
- A Telegram bot token from BotFather
- At least one Telegram numeric user ID to whitelist
- Object storage credentials for uploads
- `ffmpeg` if `.mov` inputs should be converted to MP4 automatically

## Files

- `bot.py` - the bridge process
- `.env.example` - environment template
- `scripts/upload-media.mjs` - upload helper used by `/upload`
- `deploy/telegram-copilot-bridge.service.example` - systemd service template

## Setup

1. Clone this repository.
2. Copy `.env.example` to `.env`.
3. Edit `.env`:
   - set `TELEGRAM_BOT_TOKEN`
  - set `BOT_USERNAME` or let the bridge resolve it via `getMe`
   - set `ALLOWED_USER_IDS`
   - set `REPO_PATH`
  - set `UPLOAD_DIR` to a folder inside the target repository if you want uploaded files to be readable by Copilot without extra path permissions
  - set the `OBJECT_STORAGE_*` variables if `/upload` should work
4. Install the upload-helper dependency:

```bash
npm install
```
5. Authenticate Copilot CLI on the machine:

```bash
copilot login
```

6. Start locally for a quick test:

```bash
python3 bot.py
```

## Commands

- `/start` - show help
- `/help` - show help
- `/new` - start a fresh Copilot thread for the current Telegram account
- `/status` - show repo and session status
- `/debug` - show the latest full technical trace or attach to the current request trace
- `/upload` - upload Telegram media to object storage after you provide a name
- `/cancel` - cancel a pending upload
- `/copilot <prompt>` - send an explicit prompt
- plain text message - send that text to Copilot

## Output Modes

By default, Telegram receives a cleaned user-facing summary instead of the raw tool-by-tool Copilot trace.

Use `/debug` in one of two ways:

- if a request is currently running, `/debug` replays the full technical trace from the start and keeps streaming technical details until that request finishes
- if no request is running, `/debug` returns the full technical trace for your latest completed reply

This keeps ordinary chats readable while preserving a way to inspect the raw execution details when needed.
## File Uploads

The bridge can process Telegram attachments such as documents and photos.

- uploads are downloaded locally before Copilot is called
- the downloaded path is appended to the Copilot prompt
- by default, uploads are stored in `.telegram-copilot-uploads/` inside the configured repository
- the bridge also passes `--add-dir` for that upload directory to Copilot CLI

This keeps file paths accessible without requiring manual approval for unrelated locations.

## Storage Uploads

`/upload` is separate from the Copilot flow.

- send `/upload` with attached media, or reply `/upload` to a Telegram message that already has media
- if you send `/upload` without media first, the bridge waits for your next media message
- after the file is downloaded locally, the bridge asks for a storage name
- the upload helper refuses to overwrite an existing object key
- `.mov` files are converted to `.mp4` with `ffmpeg` before upload
- other file types are uploaded as-is and keep their extension

The current key pattern is `uploads/<name><extension>`. Override the prefix with `OBJECT_STORAGE_PREFIX` if needed.

## Group Chats

The bot can be added to group chats.

In groups, it responds only when the sender is whitelisted and one of these is true:

- the message directly mentions the bot, for example `@your_bot_name explain this file`
- the message uses a bot-addressed command such as `/status@your_bot_name`
- the message is a reply to one of the bot's own messages

When you reply to another Telegram message and mention the bot, the bridge now appends the referenced message text and any referenced attachment path to the Copilot prompt.

No Telegram setting change is required for reply-context support if you already mention the bot when replying.

If you want the bot to trigger on ordinary group messages without being mentioned, that is a separate behavior change: you would need to disable privacy mode in BotFather with `/setprivacy` and also relax the group trigger rules in [bot.py](bot.py#L199).

Messages from non-whitelisted users are ignored in groups.

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
- It can include the text and downloadable file from a replied-to Telegram message, but it still cannot interpret image pixels from a screenshot.
- It uses long polling, not webhooks.
- Session continuation is tracked per Telegram user ID in `state.json`.