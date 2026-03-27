#!/usr/bin/env python3
import html
import json
import os
import re
import subprocess
import sys
import threading
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
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(Path(REPO_PATH) / ".telegram-copilot-uploads")))
COPILOT_BIN = os.environ.get("COPILOT_BIN", "/usr/bin/copilot")
COPILOT_TIMEOUT = int(os.environ.get("COPILOT_TIMEOUT", "1200"))
TELEGRAM_TIMEOUT = int(os.environ.get("TELEGRAM_TIMEOUT", "30"))
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").strip().lstrip("@").lower()
TELEGRAM_MESSAGE_MAX_LEN = 3900
STREAM_FLUSH_MAX_LEN = 3600
STREAM_FLUSH_INTERVAL = 4.0
STREAM_FLUSH_MIN_PARAGRAPH_LEN = 1200
UPLOAD_TOOL = BASE_DIR / "scripts" / "upload-media.mjs"
DEBUG_TRACE_MAX_CHARS = 400000
BOT_COMMANDS = [
    {"command": "start", "description": "Show help and quick start"},
    {"command": "help", "description": "Show help and commands"},
    {"command": "new", "description": "Start a fresh Copilot thread"},
    {"command": "status", "description": "Show bridge and session status"},
    {"command": "upload", "description": "Upload Telegram media to object storage"},
    {"command": "debug", "description": "Show or enable full technical trace"},
    {"command": "cancel", "description": "Cancel a pending upload"},
    {"command": "copilot", "description": "Send an explicit prompt to Copilot"},
]
STATE_LOCK = threading.RLock()
REQUESTS_LOCK = threading.RLock()
ACTIVE_REQUESTS: dict[int, dict] = {}

TECHNICAL_LINE_PATTERNS = [
    re.compile(r"^\s*[●◦○◆].*"),
    re.compile(r"^\s*[│└].*"),
    re.compile(r"^\s*Total usage est:.*", re.IGNORECASE),
    re.compile(r"^\s*API time spent:.*", re.IGNORECASE),
    re.compile(r"^\s*Total session time:.*", re.IGNORECASE),
    re.compile(r"^\s*Total code changes:.*", re.IGNORECASE),
    re.compile(r"^\s*Breakdown by AI model:.*", re.IGNORECASE),
    re.compile(r"^\s*claude-[\w.-]+.*", re.IGNORECASE),
    re.compile(r"^\s*gpt-[\w.-]+.*", re.IGNORECASE),
    re.compile(r"^\s*Agent started in background.*", re.IGNORECASE),
    re.compile(r"^\s*Completed\s*$", re.IGNORECASE),
]

SUMMARY_START_MARKERS = [
    "готово",
    "ось що",
    "done.",
    "done!",
    "here's what",
    "here is what",
    "fixed:",
]


def telegram_request(method: str, payload: dict | None = None) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(f"{API_BASE}/{method}", data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT + 10) as response:
        return json.loads(response.read().decode("utf-8"))


def send_message(
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
    parse_mode: str | None = None,
) -> None:
    payload = {"chat_id": str(chat_id), "text": text}
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = str(reply_to_message_id)
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    telegram_request("sendMessage", payload)


def send_typing(chat_id: int) -> None:
    telegram_request("sendChatAction", {"chat_id": str(chat_id), "action": "typing"})


def get_message_text(message: dict) -> str:
    return message.get("text") or message.get("caption") or ""


def get_message_entities(message: dict) -> list[dict]:
    return message.get("entities") or message.get("caption_entities") or []


def resolve_bot_username() -> str:
    result = telegram_request("getMe")
    if not result.get("ok"):
        raise RuntimeError("Unable to resolve Telegram bot username")
    return str(result["result"].get("username", "")).strip().lstrip("@").lower()


def sync_bot_commands() -> None:
    result = telegram_request(
        "setMyCommands",
        {"commands": json.dumps(BOT_COMMANDS, separators=(",", ":"))},
    )
    if not result.get("ok"):
        raise RuntimeError("Unable to update Telegram bot commands")


def sync_bot_commands_safe() -> None:
    try:
        sync_bot_commands()
    except Exception as error:
        print(f"bridge warning: failed to sync Telegram bot commands: {error}", file=sys.stderr, flush=True)


if not BOT_USERNAME:
    BOT_USERNAME = resolve_bot_username()


def split_message(text: str, max_len: int = TELEGRAM_MESSAGE_MAX_LEN) -> list[str]:
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


def send_text_blocks(chat_id: int, text: str) -> None:
    for chunk in split_message(text):
        send_message(chat_id, chunk)


def send_group_done_ack(chat_id: int, message: dict) -> None:
    if (message.get("chat") or {}).get("type", "private") == "private":
        return
    from_user = message.get("from") or {}
    user_id = from_user.get("id")
    first_name = html.escape(str(from_user.get("first_name", "there")))
    if not user_id:
        return
    text = f'<a href="tg://user?id={user_id}">{first_name}</a> Done'
    send_message(chat_id, text, message.get("message_id"), parse_mode="HTML")


def get_active_request(user_id: int) -> dict | None:
    with REQUESTS_LOCK:
        return ACTIVE_REQUESTS.get(user_id)


def start_active_request(user_id: int, chat_id: int, message: dict) -> dict:
    request = {
        "chat_id": chat_id,
        "message": message,
        "raw_blocks": [],
        "debug_enabled": False,
    }
    with REQUESTS_LOCK:
        ACTIVE_REQUESTS[user_id] = request
    return request


def append_active_request_block(user_id: int, block: str) -> tuple[bool, int | None]:
    with REQUESTS_LOCK:
        request = ACTIVE_REQUESTS.get(user_id)
        if request is None:
            return False, None
        request["raw_blocks"].append(block)
        return bool(request.get("debug_enabled")), request.get("chat_id")


def enable_debug_for_active_request(user_id: int) -> dict | None:
    with REQUESTS_LOCK:
        request = ACTIVE_REQUESTS.get(user_id)
        if request is None:
            return None
        request["debug_enabled"] = True
        return {
            "chat_id": request.get("chat_id"),
            "raw_blocks": list(request.get("raw_blocks") or []),
        }


def finish_active_request(user_id: int) -> dict | None:
    with REQUESTS_LOCK:
        return ACTIVE_REQUESTS.pop(user_id, None)


def strip_ansi_sequences(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)


def is_technical_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return any(pattern.match(stripped) for pattern in TECHNICAL_LINE_PATTERNS)


def build_user_facing_text(raw_text: str) -> str:
    cleaned_lines: list[str] = []
    for line in strip_ansi_sequences(raw_text).splitlines():
        if is_technical_line(line):
            continue
        cleaned_lines.append(line.rstrip())

    while cleaned_lines and not cleaned_lines[0].strip():
        cleaned_lines.pop(0)
    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()

    if not cleaned_lines:
        return "Task finished. Use /debug to view the technical trace."

    start_index = 0
    for index, line in enumerate(cleaned_lines):
        lowered = line.lower()
        if any(marker in lowered for marker in SUMMARY_START_MARKERS):
            start_index = index

    summary_lines = cleaned_lines[start_index:]
    collapsed: list[str] = []
    previous_blank = False
    for line in summary_lines:
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        collapsed.append(line)
        previous_blank = is_blank

    text = "\n".join(collapsed).strip()
    return text or "Task finished. Use /debug to view the technical trace."


def trim_debug_text(text: str) -> str:
    if len(text) <= DEBUG_TRACE_MAX_CHARS:
        return text
    omitted = len(text) - DEBUG_TRACE_MAX_CHARS
    return (
        f"[debug trace truncated, omitted {omitted} earlier characters]\n\n"
        f"{text[-DEBUG_TRACE_MAX_CHARS:]}"
    )


def format_user_label(user: dict) -> str:
    username = str(user.get("username", "")).strip()
    first_name = str(user.get("first_name", "")).strip()
    last_name = str(user.get("last_name", "")).strip()
    full_name = " ".join(part for part in [first_name, last_name] if part).strip()
    if username and full_name:
        return f"{full_name} (@{username})"
    if username:
        return f"@{username}"
    if full_name:
        return full_name
    return f"user {user.get('id', 'unknown')}"


def build_referenced_message_context(message: dict, attachment_path: Path | None = None) -> str:
    parts = [
        "Referenced Telegram message:",
        f"From: {format_user_label(message.get('from') or {})}",
    ]
    text = get_message_text(message).strip()
    if text:
        parts.extend(["Content:", text])
    else:
        parts.append("Content: <no text>")
    if attachment_path is not None:
        parts.append(f"Attachment saved at: {attachment_path}")
    elif extract_attachment(message):
        parts.append("Attachment: present in Telegram, but no local file was downloaded.")
    parts.append(
        "If the user's request says things like 'check this message' or 'reply to this', use the referenced message as the primary context."
    )
    return "\n".join(parts)


def append_referenced_download_warning(context: str, warning: str | None) -> str:
    if not warning:
        return context
    return f"{context}\nDownload warning: {warning}"


def normalize_command_token(token: str) -> str:
    if not token.startswith("/"):
        return token
    if "@" not in token:
        return token
    command, _, target = token.partition("@")
    if not BOT_USERNAME or target.lower() != BOT_USERNAME:
        return ""
    return command


def is_reply_to_bot(message: dict) -> bool:
    reply = message.get("reply_to_message") or {}
    from_user = reply.get("from") or {}
    username = str(from_user.get("username", "")).strip().lstrip("@").lower()
    return bool(from_user.get("is_bot") and BOT_USERNAME and username == BOT_USERNAME)


def message_mentions_bot(message: dict, text: str) -> bool:
    if not BOT_USERNAME:
        return False
    for entity in get_message_entities(message):
        if entity.get("type") != "mention":
            continue
        offset = entity.get("offset", 0)
        length = entity.get("length", 0)
        mention = text[offset:offset + length].strip().lstrip("@").lower()
        if mention == BOT_USERNAME:
            return True
    return False


def should_handle_message(message: dict, user_id: int, text: str) -> bool:
    chat_type = (message.get("chat") or {}).get("type", "private")
    if chat_type == "private":
        return True
    if user_id not in ALLOWED_USER_IDS:
        return False
    command = text.strip().split()[0] if text.strip().startswith("/") else ""
    if command and normalize_command_token(command):
        return True
    if is_reply_to_bot(message):
        return True
    return message_mentions_bot(message, text)


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or "telegram_file"


def download_telegram_file(file_id: str, preferred_name: str | None = None) -> Path:
    file_info = telegram_request("getFile", {"file_id": file_id})
    if not file_info.get("ok"):
        raise RuntimeError("Failed to resolve Telegram file path")
    file_path = file_info["result"].get("file_path")
    if not file_path:
        raise RuntimeError("Telegram did not return a downloadable file path")

    source_name = preferred_name or Path(file_path).name
    safe_name = sanitize_filename(source_name)
    target = UPLOAD_DIR / f"{int(time.time())}_{safe_name}"
    encoded_path = urllib.parse.quote(file_path, safe="/")
    download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{encoded_path}"
    with urllib.request.urlopen(download_url, timeout=TELEGRAM_TIMEOUT + 30) as response:
        target.write_bytes(response.read())
    return target


def extract_attachment(message: dict) -> tuple[str, str | None] | None:
    animation = message.get("animation")
    if animation:
        return animation.get("file_id"), animation.get("file_name") or "telegram_animation.mp4"

    document = message.get("document")
    if document:
        return document.get("file_id"), document.get("file_name")

    photo = message.get("photo") or []
    if photo:
        return photo[-1].get("file_id"), "telegram_photo.jpg"

    audio = message.get("audio")
    if audio:
        return audio.get("file_id"), audio.get("file_name") or "telegram_audio"

    video = message.get("video")
    if video:
        return video.get("file_id"), video.get("file_name") or "telegram_video.mp4"

    video_note = message.get("video_note")
    if video_note:
        return video_note.get("file_id"), "telegram_video_note.mp4"

    voice = message.get("voice")
    if voice:
        return voice.get("file_id"), "telegram_voice.ogg"

    paid_media = message.get("paid_media") or {}
    for item in paid_media.get("paid_media") or []:
        if item.get("type") == "video" and item.get("video"):
            video = item["video"]
            return video.get("file_id"), video.get("file_name") or "telegram_paid_video.mp4"
        if item.get("type") == "photo" and item.get("photo"):
            photo = item.get("photo") or []
            if photo:
                return photo[-1].get("file_id"), "telegram_paid_photo.jpg"

    return None


def load_state() -> dict:
    with STATE_LOCK:
        if not STATE_PATH.exists():
            return {"offset": 0, "sessions": {}}
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {"offset": 0, "sessions": {}}


def save_state(state: dict) -> None:
    with STATE_LOCK:
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def get_session_state(state: dict, user_id: int) -> dict:
    sessions = state.setdefault("sessions", {})
    session_key = str(user_id)
    if session_key not in sessions:
        sessions[session_key] = {"has_session": False}
    return sessions[session_key]


def get_latest_debug_trace(state: dict, user_id: int) -> str | None:
    latest = state.setdefault("latest_debug_traces", {})
    return latest.get(str(user_id))


def set_latest_debug_trace(state: dict, user_id: int, raw_text: str) -> None:
    latest = state.setdefault("latest_debug_traces", {})
    latest[str(user_id)] = trim_debug_text(raw_text)
    save_state(state)


def get_upload_session(state: dict, user_id: int) -> dict | None:
    uploads = state.setdefault("uploads", {})
    return uploads.get(str(user_id))


def set_upload_session(state: dict, user_id: int, session: dict) -> None:
    uploads = state.setdefault("uploads", {})
    uploads[str(user_id)] = session
    save_state(state)


def clear_upload_session(state: dict, user_id: int, cleanup_local_file: bool = False) -> None:
    uploads = state.setdefault("uploads", {})
    session = uploads.pop(str(user_id), None)
    if cleanup_local_file and session:
        local_path = session.get("local_path")
        if local_path:
            try:
                Path(local_path).unlink(missing_ok=True)
            except Exception:
                pass
    save_state(state)


def access_denied_text(user_id: int) -> str:
    return (
        "You don't have access to this bot.\n\n"
        f"Your Telegram User ID is: {user_id}\n\n"
        "Please contact the bot administrator to get access."
    )


def help_text() -> str:
    command_lines = "\n".join(
        f"/{item['command']} - {item['description']}" for item in BOT_COMMANDS
    )
    return (
        "Telegram Copilot Bridge\n\n"
        f"Repo: {REPO_PATH}\n\n"
        f"Commands:\n{command_lines}\n\n"
        "Any plain text message is sent to Copilot in the configured repository.\n"
        "Telegram shows a human-readable summary by default. Use /debug if you want the full technical trace.\n\n"
        "In group chats, the bot responds only to allowed users who mention @"
        f"{BOT_USERNAME}, use a command like /status@{BOT_USERNAME}, or reply directly to the bot."
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
        f"Session state: {'continuing' if session_state.get('has_session') else 'new'}\n"
        f"Active request: {'yes' if get_active_request(user_id) else 'no'}\n"
        f"Upload tool: {'configured' if UPLOAD_TOOL.exists() else 'missing'}"
    )


def upload_help_text() -> str:
    return (
        "Upload workflow:\n"
        "/upload with attached media, or reply /upload to a message that already has media.\n"
        "If you send /upload by itself, the bot will wait for your next media message.\n"
        "After the file is downloaded, send the storage name as plain text.\n"
        "Use /cancel to abort a pending upload."
    )


def upload_result_text(result: dict) -> str:
    lines = [
        "Upload complete.",
        f"Key: {result.get('key', 'unknown')}",
        f"URL: {result.get('url', 'unknown')}",
        f"Content-Type: {result.get('contentType', 'unknown')}",
    ]
    transcoded_from = result.get("transcodedFrom")
    if transcoded_from:
        lines.append(f"Converted from: {transcoded_from}")
    return "\n".join(lines)


def run_upload_tool(local_path: Path, upload_name: str) -> tuple[bool, str]:
    if not UPLOAD_TOOL.exists():
        return False, f"Upload helper not found at {UPLOAD_TOOL}"

    try:
        result = subprocess.run(
            ["node", str(UPLOAD_TOOL), "--file", str(local_path), "--name", upload_name],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=COPILOT_TIMEOUT,
            check=False,
            env=os.environ.copy(),
        )
    except Exception as error:
        return False, str(error)

    if result.returncode != 0:
        output = result.stderr.strip() or result.stdout.strip() or f"Exit code {result.returncode}"
        return False, output

    try:
        parsed = json.loads(result.stdout)
    except Exception as error:
        return False, f"Upload helper returned invalid JSON: {error}"

    return True, upload_result_text(parsed)


def begin_upload_from_message(state: dict, user_id: int, chat_id: int, source_message: dict) -> None:
    attachment = extract_attachment(source_message)
    if not attachment:
        send_message(chat_id, "That message does not include downloadable media.")
        return

    clear_upload_session(state, user_id, cleanup_local_file=True)

    try:
        attachment_path = download_telegram_file(*attachment)
    except Exception as error:
        send_message(chat_id, f"Failed to download Telegram attachment:\n\n{error}")
        return

    source_name = attachment[1] or attachment_path.name
    set_upload_session(
        state,
        user_id,
        {
            "chat_id": chat_id,
            "stage": "awaiting_name",
            "local_path": str(attachment_path),
            "source_name": source_name,
        },
    )
    send_message(
        chat_id,
        (
            f"Downloaded: {source_name}\n"
            "Send the storage name as plain text.\n"
            "The upload will fail if that name already exists.\n"
            "Use /cancel to discard this pending upload."
        ),
    )


def handle_pending_upload(message: dict, state: dict, upload_session: dict) -> bool:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = get_message_text(message).strip()
    command = normalize_command_token(text.split()[0]) if text.startswith("/") else ""

    if upload_session.get("chat_id") != chat_id:
        return False

    if command == "/cancel":
        clear_upload_session(state, user_id, cleanup_local_file=True)
        send_message(chat_id, "Cancelled the pending upload.")
        send_group_done_ack(chat_id, message)
        return True

    stage = upload_session.get("stage")
    if stage == "awaiting_media":
        if extract_attachment(message):
            begin_upload_from_message(state, user_id, chat_id, message)
            send_group_done_ack(chat_id, message)
            return True
        send_message(chat_id, "Send the media file to upload, or use /cancel.")
        return True

    if stage == "awaiting_name":
        if not text or command:
            send_message(chat_id, "Send the storage name as plain text, or use /cancel.")
            return True

        local_path = Path(upload_session.get("local_path", ""))
        if not local_path.exists():
            clear_upload_session(state, user_id)
            send_message(chat_id, "The pending upload file is no longer available. Start again with /upload.")
            return True

        send_typing(chat_id)
        success, result = run_upload_tool(local_path, text)
        if success:
            clear_upload_session(state, user_id, cleanup_local_file=True)
            send_message(chat_id, result)
            send_group_done_ack(chat_id, message)
            return True

        send_message(chat_id, f"Upload failed:\n\n{result}\n\nSend a different name, or use /cancel.")
        return True

    clear_upload_session(state, user_id, cleanup_local_file=True)
    send_message(chat_id, "Upload state was invalid and has been cleared. Start again with /upload.")
    return True


def stream_copilot(prompt: str, continue_session: bool, on_block) -> tuple[bool, bool, str]:
    command = [COPILOT_BIN]
    if continue_session:
        command.append("--continue")
    command.extend([
        "-p",
        prompt,
        "--allow-all-tools",
        "--no-color",
        "--add-dir",
        str(UPLOAD_DIR),
    ])
    process = subprocess.Popen(
        command,
        cwd=REPO_PATH,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=os.environ.copy(),
    )
    deadline = time.monotonic() + COPILOT_TIMEOUT
    buffer: list[str] = []
    sent_any = False
    last_flush = time.monotonic()
    assert process.stdout is not None

    def flush_buffer() -> None:
        nonlocal sent_any, last_flush
        text = "".join(buffer).strip()
        if not text:
            buffer.clear()
            return
        on_block(text)
        buffer.clear()
        sent_any = True
        last_flush = time.monotonic()

    try:
        while True:
            if time.monotonic() > deadline:
                process.kill()
                flush_buffer()
                return False, sent_any, f"Timed out after {COPILOT_TIMEOUT} seconds."

            line = process.stdout.readline()
            if line == "" and process.poll() is not None:
                break
            if line == "":
                time.sleep(0.1)
                continue

            buffer.append(line)
            current = "".join(buffer)
            if line.strip() == "" and len(current.strip()) >= STREAM_FLUSH_MIN_PARAGRAPH_LEN:
                flush_buffer()
                continue
            if len(current) >= STREAM_FLUSH_MAX_LEN:
                flush_buffer()
                continue
            if time.monotonic() - last_flush >= STREAM_FLUSH_INTERVAL and current.strip():
                flush_buffer()

        process.wait(timeout=5)
    except Exception as error:
        process.kill()
        flush_buffer()
        return False, sent_any, str(error)

    flush_buffer()
    if process.returncode == 0:
        return True, sent_any, ""
    return False, sent_any, f"Exit code {process.returncode}"


def extract_prompt(text: str) -> tuple[str | None, bool]:
    stripped = text.strip()
    if not stripped:
        return None, False
    if stripped.startswith("/copilot"):
        prompt = stripped[len("/copilot"):].strip()
        return prompt or None, True
    if stripped.startswith("/") and "@" in stripped.split()[0]:
        command, _, remainder = stripped.partition(" ")
        normalized = normalize_command_token(command)
        if normalized == "/copilot":
            prompt = remainder.strip()
            return prompt or None, True
        return None, False
    if stripped.startswith("/"):
        return None, False
    if BOT_USERNAME:
        stripped = re.sub(rf"(?i)@{re.escape(BOT_USERNAME)}\b[:,\-]?\s*", "", stripped).strip()
    if not stripped:
        return None, False
    return stripped, True


def default_reply_prompt() -> str:
    return "Review the referenced Telegram message and respond to it."


def handle_debug_command(message: dict, state: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    active_request = enable_debug_for_active_request(user_id)
    if active_request and active_request.get("chat_id") == chat_id:
        raw_blocks = active_request.get("raw_blocks") or []
        if raw_blocks:
            send_message(chat_id, "Debug mode enabled for the current request. Sending the technical trace collected so far.")
            for block in raw_blocks:
                send_text_blocks(chat_id, block)
        else:
            send_message(chat_id, "Debug mode enabled for the current request. Technical details will appear as they are produced.")
        return

    latest_trace = get_latest_debug_trace(state, user_id)
    if latest_trace:
        send_message(chat_id, "Latest technical trace:")
        send_text_blocks(chat_id, latest_trace)
        return

    send_message(chat_id, "No active or recent request is available for debug.")


def process_copilot_request(state: dict, message: dict, prompt: str, continue_session: bool, user_id: int, chat_id: int) -> None:
    request = start_active_request(user_id, chat_id, message)
    send_typing(chat_id)
    send_message(chat_id, "Working...")

    def on_block(block: str) -> None:
        debug_enabled, debug_chat_id = append_active_request_block(user_id, block)
        if debug_enabled and debug_chat_id is not None:
            send_text_blocks(debug_chat_id, block)

    success, sent_any, result = stream_copilot(prompt, continue_session, on_block)
    completed_request = finish_active_request(user_id) or request
    raw_text = "\n\n".join(completed_request.get("raw_blocks") or [])
    if raw_text:
        set_latest_debug_trace(state, user_id, raw_text)

    if success:
        session_state = get_session_state(state, user_id)
        session_state["has_session"] = True
        save_state(state)
        summary_text = build_user_facing_text(raw_text)
        if summary_text:
            send_text_blocks(chat_id, summary_text)
        elif not sent_any:
            send_message(chat_id, "Task finished.")
        send_group_done_ack(chat_id, message)
        return

    if result:
        send_message(chat_id, "The task failed. Use /debug to see the technical details.")
        if completed_request.get("debug_enabled"):
            send_text_blocks(chat_id, result)
    send_group_done_ack(chat_id, message)


def handle_message(message: dict, state: dict) -> None:
    chat_id = message["chat"]["id"]
    chat_type = message["chat"].get("type", "private")
    user_id = message["from"]["id"]
    message_id = message["message_id"]
    text = get_message_text(message)

    if user_id not in ALLOWED_USER_IDS:
        if chat_type == "private":
            send_message(chat_id, access_denied_text(user_id))
        return

    upload_session = get_upload_session(state, user_id)
    upload_session_active = bool(upload_session and upload_session.get("chat_id") == chat_id)

    if not should_handle_message(message, user_id, text) and not upload_session_active:
        return

    if upload_session_active and handle_pending_upload(message, state, upload_session):
        return

    session_state = get_session_state(state, user_id)
    command = normalize_command_token(text.strip().split()[0]) if text.strip().startswith("/") else ""

    if command in {"/start", "/help"}:
        send_message(chat_id, help_text())
        send_group_done_ack(chat_id, message)
        return
    if command == "/debug":
        handle_debug_command(message, state)
        return
    if command == "/cancel":
        send_message(chat_id, "There is no pending upload to cancel.")
        send_group_done_ack(chat_id, message)
        return
    if command == "/new":
        if get_active_request(user_id):
            send_message(chat_id, "A request is still running. Wait for it to finish, or use /debug to watch the technical trace.")
            return
        session_state["has_session"] = False
        save_state(state)
        send_message(chat_id, "Started a fresh Copilot thread for your account.")
        send_group_done_ack(chat_id, message)
        return
    if command == "/status":
        send_message(chat_id, status_text(state, user_id))
        send_group_done_ack(chat_id, message)
        return
    if command == "/upload":
        current_attachment = extract_attachment(message)
        reply_message = message.get("reply_to_message")
        reply_attachment = extract_attachment(reply_message) if reply_message else None

        if current_attachment:
            begin_upload_from_message(state, user_id, chat_id, message)
        elif reply_attachment and reply_message:
            begin_upload_from_message(state, user_id, chat_id, reply_message)
        else:
            set_upload_session(state, user_id, {"chat_id": chat_id, "stage": "awaiting_media"})
            send_message(chat_id, upload_help_text())
        send_group_done_ack(chat_id, message)
        return

    if get_active_request(user_id):
        send_message(chat_id, "A request is already running. Wait for it to finish, or use /debug to watch the full technical trace.")
        return

    attachment_path = None
    attachment = extract_attachment(message)
    if attachment:
        try:
            attachment_path = download_telegram_file(*attachment)
        except Exception as error:
            send_message(chat_id, f"Failed to download Telegram attachment:\n\n{error}")
            send_group_done_ack(chat_id, message)
            return

    referenced_message = message.get("reply_to_message")
    referenced_attachment_path = None
    referenced_attachment_warning = None
    if referenced_message:
        referenced_attachment = extract_attachment(referenced_message)
        if referenced_attachment:
            try:
                referenced_attachment_path = download_telegram_file(*referenced_attachment)
            except Exception as error:
                referenced_attachment_warning = str(error)
                print(
                    f"bridge warning: failed to download referenced attachment for message {message_id}: {error}",
                    file=sys.stderr,
                    flush=True,
                )

    prompt, should_run = extract_prompt(text)
    if referenced_message and not prompt:
        prompt = default_reply_prompt()
        should_run = True
    if not should_run:
        send_message(chat_id, help_text())
        send_group_done_ack(chat_id, message)
        return
    if not prompt:
        send_message(chat_id, "Send text after /copilot, or just send a plain message.")
        send_group_done_ack(chat_id, message)
        return

    if attachment_path is not None:
        prompt = (
            f"{prompt}\n\n"
            f"Use the uploaded Telegram file at: {attachment_path}\n"
            f"This file is stored inside the repository-accessible upload directory.\n"
            f"Read and process that file directly from disk."
        )

    if referenced_message:
        referenced_context = build_referenced_message_context(referenced_message, referenced_attachment_path)
        prompt = f"{prompt}\n\n{append_referenced_download_warning(referenced_context, referenced_attachment_warning)}"

    worker = threading.Thread(
        target=process_copilot_request,
        args=(
            state,
            message,
            prompt,
            bool(session_state.get("has_session")),
            user_id,
            chat_id,
        ),
        daemon=True,
    )
    worker.start()


def main() -> int:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    sync_bot_commands_safe()
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
                if message and ("text" in message or "caption" in message or extract_attachment(message)):
                    handle_message(message, state)
        except KeyboardInterrupt:
            return 0
        except Exception as error:
            print(f"bridge error: {error}", file=sys.stderr, flush=True)
            time.sleep(3)


if __name__ == "__main__":
    raise SystemExit(main())