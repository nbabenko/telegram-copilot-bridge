#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "state.json"


def load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key and key not in os.environ:
            os.environ[key] = value


load_env(BASE_DIR / ".env")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = {
    int(item.strip())
    for item in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if item.strip()
}
REPO_PATH = os.environ.get("REPO_PATH", str(BASE_DIR))
COPILOT_BIN = os.environ.get("COPILOT_BIN", "/usr/bin/copilot")
COPILOT_TIMEOUT = int(os.environ.get("COPILOT_TIMEOUT", "1200"))
TELEGRAM_TIMEOUT = int(os.environ.get("TELEGRAM_TIMEOUT", "30"))
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def telegram_request(method: str, payload: dict | None = None) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(f"{API_BASE}/{method}", data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT + 10) as response:
        return json.loads(response.read().decode("utf-8"))


def send_message(chat_id: int, text: str, reply_to_message_id: int | None = None) -> None:
    payload = {"chat_id": str(chat_id), "text": text}
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = str(reply_to_message_id)
    telegram_request("sendMessage", payload)


def send_typing(chat_id: int) -> None:
    telegram_request("sendChatAction", {"chat_id": str(chat_id), "action": "typing"})


def split_message(text: str, max_len: int = 3500) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = remaining.rfind(" ", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks or [""]


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"offset": 0, "sessions": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"offset": 0, "sessions": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def get_session_state(state: dict, user_id: int) -> dict:
    sessions = state.setdefault("sessions", {})
    session_key = str(user_id)
    if session_key not in sessions:
        sessions[session_key] = {"has_session": False}
    return sessions[session_key]


def access_denied_text(user_id: int) -> str:
    return (
        "You don't have access to this bot.\n\n"
        f"Your Telegram User ID is: {user_id}\n\n"
        "Please contact the bot administrator to get access."
    )


def help_text() -> str:
    return (
        "Telegram Copilot Bridge\n\n"
        f"Repo: {REPO_PATH}\n\n"
        "Commands:\n"
        "/start - show this help\n"
        "/help - show this help\n"
        "/new - start a fresh Copilot thread for your account\n"
        "/status - show bridge status\n"
        "/copilot <prompt> - send a prompt immediately\n\n"
        "Any plain text message is sent to Copilot in the configured repository.\n"
        "Telegram receives the full raw Copilot CLI output, not only the final summary."
    )


def status_text(state: dict, user_id: int) -> str:
    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=REPO_PATH,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except Exception:
        branch = "unknown"
    session_state = get_session_state(state, user_id)
    return (
        "Bridge is running\n\n"
        f"Repo: {REPO_PATH}\n"
        f"Branch: {branch}\n"
        f"Whitelisted users: {len(ALLOWED_USER_IDS)}\n"
        f"Session state: {'continuing' if session_state.get('has_session') else 'new'}"
    )


def run_copilot(prompt: str, continue_session: bool) -> tuple[bool, str]:
    command = [COPILOT_BIN]
    if continue_session:
        command.append("--continue")
    command.extend(["-p", prompt, "--allow-all-tools", "--no-color"])
    process = subprocess.run(
        command,
        cwd=REPO_PATH,
        capture_output=True,
        text=True,
        timeout=COPILOT_TIMEOUT,
        env=os.environ.copy(),
    )
    stdout = process.stdout.strip()
    stderr = process.stderr.strip()
    combined = "\n\n".join(part for part in [stdout, stderr] if part).strip()
    if process.returncode == 0:
        return True, combined or "Copilot returned no text."
    return False, combined or f"Exit code {process.returncode}"


def extract_prompt(text: str) -> tuple[str | None, bool]:
    stripped = text.strip()
    if not stripped:
        return None, False
    if stripped.startswith("/copilot"):
        prompt = stripped[len("/copilot"):].strip()
        return prompt or None, True
    if stripped.startswith("/"):
        return None, False
    return stripped, True


def handle_message(message: dict, state: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    message_id = message["message_id"]
    text = message.get("text", "")

    if user_id not in ALLOWED_USER_IDS:
        send_message(chat_id, access_denied_text(user_id), message_id)
        return

    session_state = get_session_state(state, user_id)
    command = text.strip().split()[0] if text.strip().startswith("/") else ""

    if command in {"/start", "/help"}:
        send_message(chat_id, help_text(), message_id)
        return
    if command == "/new":
        session_state["has_session"] = False
        save_state(state)
        send_message(chat_id, "Started a fresh Copilot thread for your account.", message_id)
        return
    if command == "/status":
        send_message(chat_id, status_text(state, user_id), message_id)
        return

    prompt, should_run = extract_prompt(text)
    if not should_run:
        send_message(chat_id, help_text(), message_id)
        return
    if not prompt:
        send_message(chat_id, "Send text after /copilot, or just send a plain message.", message_id)
        return

    send_typing(chat_id)
    send_message(chat_id, "Working...", message_id)
    success, result = run_copilot(prompt, bool(session_state.get("has_session")))
    if success:
        session_state["has_session"] = True
        save_state(state)
        for chunk in split_message(result):
            send_message(chat_id, chunk, message_id)
        return

    send_message(chat_id, f"Copilot failed:\n\n{result}", message_id)


def main() -> int:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    while True:
        try:
            updates = telegram_request(
                "getUpdates",
                {"offset": str(state.get("offset", 0)), "timeout": str(TELEGRAM_TIMEOUT)},
            )
            for update in updates.get("result", []):
                state["offset"] = update["update_id"] + 1
                save_state(state)
                message = update.get("message")
                if message and "text" in message:
                    handle_message(message, state)
        except KeyboardInterrupt:
            return 0
        except Exception as error:
            print(f"bridge error: {error}", file=sys.stderr, flush=True)
            time.sleep(3)


if __name__ == "__main__":
    raise SystemExit(main())