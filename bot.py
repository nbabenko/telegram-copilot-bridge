#!/usr/bin/env python3
import html
import json
import os
import signal
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
GITHUB_API_BASE = "https://api.github.com"
GITHUB_ACTIONS_TOKEN = os.environ.get("GITHUB_ACTIONS_TOKEN", "").strip()
GITHUB_API_TOKEN = GITHUB_ACTIONS_TOKEN or os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_ACTIONS_REPO = os.environ.get("GITHUB_ACTIONS_REPO", "").strip()
GITHUB_POLL_INTERVAL = max(15, int(os.environ.get("GITHUB_POLL_INTERVAL", "45")))
ACTION_SELECTION_TTL = 900
ACTION_RUNS_PER_WORKFLOW = 10
TELEGRAM_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024
BOT_COMMANDS = [
    {"command": "start", "description": "Show help and quick start"},
    {"command": "help", "description": "Show help and commands"},
    {"command": "new", "description": "Start a fresh Copilot thread"},
    {"command": "status", "description": "Show bridge and session status"},
    {"command": "actions", "description": "List GitHub Actions workflows"},
    {"command": "subscriptions", "description": "Show this chat's workflow subscriptions"},
    {"command": "watch", "description": "Subscribe this chat to a workflow"},
    {"command": "unwatch", "description": "Unsubscribe this chat from a workflow"},
    {"command": "upload", "description": "Upload Telegram media to object storage"},
    {"command": "debug", "description": "Show or enable full technical trace"},
    {"command": "cancel", "description": "Cancel a pending upload or active request"},
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


def run_git_command(args: list[str], cwd: str | Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd or REPO_PATH),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return completed.stdout.strip()


def parse_github_repo_slug(remote_url: str) -> str | None:
    text = remote_url.strip()
    if not text:
        return None

    ssh_match = re.match(r"^(?:ssh://)?git@github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", text, re.IGNORECASE)
    if ssh_match:
        return ssh_match.group(1)

    https_match = re.match(r"^https://github\.com/([^/]+/[^/]+?)(?:\.git)?$", text, re.IGNORECASE)
    if https_match:
        return https_match.group(1)

    return None


def resolve_github_actions_repo() -> str | None:
    if GITHUB_ACTIONS_REPO:
        return GITHUB_ACTIONS_REPO
    try:
        remote_url = run_git_command(["remote", "get-url", "origin"], cwd=REPO_PATH)
    except Exception:
        return None
    return parse_github_repo_slug(remote_url)


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


def github_api_request(path: str, params: dict | None = None) -> dict:
    url = f"{GITHUB_API_BASE}{path}"
    if params:
        query = urllib.parse.urlencode({key: str(value) for key, value in params.items()})
        url = f"{url}?{query}"

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "telegram-copilot-bridge",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_API_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_API_TOKEN}"

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT + 10) as response:
        return json.loads(response.read().decode("utf-8"))


def list_github_workflows(repo_slug: str) -> list[dict]:
    payload = github_api_request(f"/repos/{repo_slug}/actions/workflows", {"per_page": 100})
    return payload.get("workflows") or []


def list_github_workflow_runs(repo_slug: str, workflow_id: int | str) -> list[dict]:
    payload = github_api_request(
        f"/repos/{repo_slug}/actions/workflows/{workflow_id}/runs",
        {"per_page": ACTION_RUNS_PER_WORKFLOW, "exclude_pull_requests": "true"},
    )
    return payload.get("workflow_runs") or []


def get_actions_state(state: dict) -> dict:
    return state.setdefault(
        "github_actions",
        {
            "subscriptions": {},
            "selection_cache": {},
            "known_runs": {},
            "initialized": False,
        },
    )


def get_chat_action_subscriptions(state: dict, chat_id: int) -> list[int]:
    actions = get_actions_state(state)
    raw = actions.setdefault("subscriptions", {}).get(str(chat_id), [])
    normalized: list[int] = []
    for item in raw:
        try:
            normalized.append(int(item))
        except Exception:
            continue
    return sorted(set(normalized))


def set_chat_action_subscriptions(state: dict, chat_id: int, workflow_ids: list[int]) -> None:
    with STATE_LOCK:
        actions = get_actions_state(state)
        subscriptions = actions.setdefault("subscriptions", {})
        key = str(chat_id)
        normalized = sorted(set(int(item) for item in workflow_ids))
        if normalized:
            subscriptions[key] = normalized
        else:
            subscriptions.pop(key, None)
        save_state(state)


def get_all_action_subscriptions(state: dict) -> dict[int, list[int]]:
    with STATE_LOCK:
        actions = get_actions_state(state)
        subscriptions = actions.setdefault("subscriptions", {})
        result: dict[int, list[int]] = {}
        for chat_key, raw_ids in subscriptions.items():
            try:
                chat_id = int(chat_key)
            except Exception:
                continue
            normalized: list[int] = []
            for item in raw_ids or []:
                try:
                    normalized.append(int(item))
                except Exception:
                    continue
            if normalized:
                result[chat_id] = sorted(set(normalized))
        return result


def set_action_selection(state: dict, user_id: int, chat_id: int, repo_slug: str, workflows: list[dict]) -> None:
    items = [
        {
            "id": int(workflow.get("id", 0)),
            "name": str(workflow.get("name", "")).strip(),
            "path": str(workflow.get("path", "")).strip(),
        }
        for workflow in workflows
        if workflow.get("id")
    ]
    with STATE_LOCK:
        actions = get_actions_state(state)
        selection_cache = actions.setdefault("selection_cache", {})
        selection_cache[f"{user_id}:{chat_id}"] = {
            "repo": repo_slug,
            "created_at": int(time.time()),
            "items": items,
        }
        save_state(state)


def get_action_selection(state: dict, user_id: int, chat_id: int) -> dict | None:
    with STATE_LOCK:
        actions = get_actions_state(state)
        selection_cache = actions.setdefault("selection_cache", {})
        selection = selection_cache.get(f"{user_id}:{chat_id}")
        if not selection:
            return None
        created_at = int(selection.get("created_at", 0))
        if time.time() - created_at > ACTION_SELECTION_TTL:
            selection_cache.pop(f"{user_id}:{chat_id}", None)
            save_state(state)
            return None
        return selection


def serialize_workflow_run(run: dict) -> dict:
    run_title = str(run.get("display_title") or "").strip()
    if not run_title:
        run_number = int(run.get("run_number", 0) or 0)
        fallback_name = str(run.get("name", "workflow")).strip() or "workflow"
        run_title = f"{fallback_name} #{run_number}" if run_number else fallback_name
    return {
        "id": int(run.get("id", 0)),
        "workflow_id": int(run.get("workflow_id", 0)),
        "name": str(run.get("name", "workflow")).strip() or "workflow",
        "run_title": run_title,
        "run_number": int(run.get("run_number", 0) or 0),
        "status": str(run.get("status", "unknown")).strip() or "unknown",
        "conclusion": str(run.get("conclusion") or "").strip(),
        "event": str(run.get("event", "unknown")).strip() or "unknown",
        "branch": str(run.get("head_branch", "unknown")).strip() or "unknown",
        "actor": str((run.get("actor") or {}).get("login", "unknown")).strip() or "unknown",
        "url": str(run.get("html_url", "")).strip(),
        "updated_at": str(run.get("updated_at", "")).strip(),
        "created_at": str(run.get("created_at", "")).strip(),
    }


def is_active_workflow_run(run: dict) -> bool:
    return str(run.get("status", "")).strip().lower() != "completed"


def workflow_started_text(run: dict) -> str:
    run_name = html.escape(run["run_title"])
    workflow_name = html.escape(run["name"])
    run_url = html.escape(run["url"], quote=True)
    return (
        f"GitHub Actions: {workflow_name} started\n"
        f"Run: <a href=\"{run_url}\">{run_name}</a>"
    )


def workflow_finished_text(run: dict) -> str:
    conclusion = run.get("conclusion") or run.get("status") or "unknown"
    run_name = html.escape(run["run_title"])
    workflow_name = html.escape(run["name"])
    conclusion_text = html.escape(conclusion)
    run_url = html.escape(run["url"], quote=True)
    return (
        f"GitHub Actions: {workflow_name} finished\n"
        f"Run: <a href=\"{run_url}\">{run_name}</a>\n\n"
        f"Result: {conclusion_text}"
    )


def action_subscriptions_text(repo_slug: str | None, workflows: list[dict], subscribed_ids: list[int]) -> str:
    if not subscribed_ids:
        repo_line = f" for {repo_slug}" if repo_slug else ""
        return f"No GitHub Actions subscriptions in this chat{repo_line}."

    workflow_by_id = {int(workflow.get("id", 0)): workflow for workflow in workflows if workflow.get("id")}
    lines = [f"GitHub Actions subscriptions for this chat ({repo_slug or 'repo unknown'}):", ""]
    for workflow_id in subscribed_ids:
        workflow = workflow_by_id.get(workflow_id)
        if workflow:
            name = str(workflow.get("name", "workflow")).strip() or "workflow"
            path = Path(str(workflow.get("path", ""))).name or "unknown"
            lines.append(f"- {name} ({path})")
        else:
            lines.append(f"- Workflow #{workflow_id}")
    return "\n".join(lines)


def actions_list_text(repo_slug: str, workflows: list[dict], subscribed_ids: list[int]) -> str:
    if not workflows:
        return f"No GitHub Actions workflows were found for {repo_slug}."

    lines = [f"Available GitHub Actions for {repo_slug}:", ""]
    for index, workflow in enumerate(workflows, start=1):
        workflow_id = int(workflow.get("id", 0))
        name = str(workflow.get("name", "workflow")).strip() or "workflow"
        path = Path(str(workflow.get("path", ""))).name or "unknown"
        watching = " [watching here]" if workflow_id in subscribed_ids else ""
        lines.append(f"{index}. {name}{watching}")
        lines.append(f"   {path}")
    lines.extend([
        "",
        "Use /watch <number> to subscribe this chat.",
        "Use /subscriptions to inspect current subscriptions.",
        "Use /unwatch <number> or /unwatch all to stop notifications.",
    ])
    return "\n".join(lines)


def parse_command_argument(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def resolve_workflow_selection(argument: str, selection: dict | None, workflows: list[dict]) -> tuple[dict | None, str | None]:
    query = argument.strip()
    if not query:
        return None, "Send /watch <number> or /unwatch <number>. Use /actions to see the current list."

    if query.lower() == "all":
        return {"id": -1, "name": "all", "path": "all"}, None

    workflow_by_id = {int(workflow.get("id", 0)): workflow for workflow in workflows if workflow.get("id")}
    if query.isdigit():
        if selection:
            items = selection.get("items") or []
            index = int(query) - 1
            if 0 <= index < len(items):
                item_id = int(items[index].get("id", 0))
                workflow = workflow_by_id.get(item_id)
                if workflow:
                    return workflow, None
                return None, "That workflow is no longer available. Run /actions again."
        workflow = workflow_by_id.get(int(query))
        if workflow:
            return workflow, None

    lowered = query.casefold()
    exact_matches: list[dict] = []
    fuzzy_matches: list[dict] = []
    for workflow in workflows:
        name = str(workflow.get("name", "")).strip()
        filename = Path(str(workflow.get("path", ""))).name
        workflow_id = str(workflow.get("id", "")).strip()
        candidates = [name.casefold(), filename.casefold(), workflow_id.casefold()]
        if lowered in candidates:
            exact_matches.append(workflow)
            continue
        if lowered in name.casefold() or lowered in filename.casefold():
            fuzzy_matches.append(workflow)

    if len(exact_matches) == 1:
        return exact_matches[0], None
    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0], None
    if len(exact_matches) > 1 or len(fuzzy_matches) > 1:
        return None, "That matches multiple workflows. Use /actions and pick the number instead."

    return None, "Workflow not found. Use /actions to list available workflows."


def get_active_request(user_id: int) -> dict | None:
    with REQUESTS_LOCK:
        return ACTIVE_REQUESTS.get(user_id)


def start_active_request(user_id: int, chat_id: int, message: dict) -> dict:
    request = {
        "chat_id": chat_id,
        "message": message,
        "raw_blocks": [],
        "debug_enabled": False,
        "process": None,
        "cancel_requested": False,
        "started_at": int(time.time()),
    }
    with REQUESTS_LOCK:
        ACTIVE_REQUESTS[user_id] = request
    return request


def bind_active_request_process(user_id: int, process: subprocess.Popen) -> bool:
    with REQUESTS_LOCK:
        request = ACTIVE_REQUESTS.get(user_id)
        if request is None:
            return False
        request["process"] = process
        if request.get("cancel_requested"):
            try:
                process.terminate()
            except Exception:
                pass
        return True


def cancel_active_request(user_id: int) -> dict | None:
    with REQUESTS_LOCK:
        request = ACTIVE_REQUESTS.get(user_id)
        if request is None:
            return None
        request["cancel_requested"] = True
        process = request.get("process")
    if process is not None and process.poll() is None:
        try:
            process.terminate()
        except Exception:
            pass
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


def describe_attachment(message: dict) -> dict | None:
    animation = message.get("animation")
    if animation:
        return {
            "file_id": animation.get("file_id"),
            "preferred_name": animation.get("file_name") or "telegram_animation.mp4",
            "file_size": int(animation.get("file_size") or 0),
            "kind": "animation",
        }

    document = message.get("document")
    if document:
        return {
            "file_id": document.get("file_id"),
            "preferred_name": document.get("file_name"),
            "file_size": int(document.get("file_size") or 0),
            "kind": "document",
        }

    photo = message.get("photo") or []
    if photo:
        largest = photo[-1]
        return {
            "file_id": largest.get("file_id"),
            "preferred_name": "telegram_photo.jpg",
            "file_size": int(largest.get("file_size") or 0),
            "kind": "photo",
        }

    audio = message.get("audio")
    if audio:
        return {
            "file_id": audio.get("file_id"),
            "preferred_name": audio.get("file_name") or "telegram_audio",
            "file_size": int(audio.get("file_size") or 0),
            "kind": "audio",
        }

    video = message.get("video")
    if video:
        return {
            "file_id": video.get("file_id"),
            "preferred_name": video.get("file_name") or "telegram_video.mp4",
            "file_size": int(video.get("file_size") or 0),
            "kind": "video",
        }

    video_note = message.get("video_note")
    if video_note:
        return {
            "file_id": video_note.get("file_id"),
            "preferred_name": "telegram_video_note.mp4",
            "file_size": int(video_note.get("file_size") or 0),
            "kind": "video_note",
        }

    voice = message.get("voice")
    if voice:
        return {
            "file_id": voice.get("file_id"),
            "preferred_name": "telegram_voice.ogg",
            "file_size": int(voice.get("file_size") or 0),
            "kind": "voice",
        }

    paid_media = message.get("paid_media") or {}
    for item in paid_media.get("paid_media") or []:
        if item.get("type") == "video" and item.get("video"):
            video = item["video"]
            return {
                "file_id": video.get("file_id"),
                "preferred_name": video.get("file_name") or "telegram_paid_video.mp4",
                "file_size": int(video.get("file_size") or 0),
                "kind": "paid_video",
            }
        if item.get("type") == "photo" and item.get("photo"):
            photo = item.get("photo") or []
            if photo:
                largest = photo[-1]
                return {
                    "file_id": largest.get("file_id"),
                    "preferred_name": "telegram_paid_photo.jpg",
                    "file_size": int(largest.get("file_size") or 0),
                    "kind": "paid_photo",
                }

    return None


def extract_attachment(message: dict) -> tuple[str, str | None] | None:
    attachment = describe_attachment(message)
    if not attachment:
        return None
    return attachment.get("file_id"), attachment.get("preferred_name")


def oversize_download_error(file_size: int) -> RuntimeError:
    return RuntimeError(
        "Telegram Bot API refused to download this file because it is larger than 20 MB. "
        f"Reported size: {file_size / (1024 * 1024):.1f} MB. "
        "Please send a smaller file, compress the video, or upload it outside Telegram."
    )


def build_download_urls(file_path: str) -> list[str]:
    encoded_path = urllib.parse.quote(file_path, safe="/")
    urls = [
        f"https://api.telegram.org/file/bot{BOT_TOKEN}/{encoded_path}",
    ]
    raw_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    if raw_url not in urls:
        urls.append(raw_url)
    return urls


def download_telegram_file(file_id: str, preferred_name: str | None = None, file_size: int = 0) -> Path:
    if file_size and file_size > TELEGRAM_DOWNLOAD_MAX_BYTES:
        raise oversize_download_error(file_size)

    file_info = telegram_request("getFile", {"file_id": file_id})
    if not file_info.get("ok"):
        raise RuntimeError("Failed to resolve Telegram file path")
    result = file_info["result"] or {}
    file_path = result.get("file_path")
    if not file_path:
        raise RuntimeError("Telegram did not return a downloadable file path")

    resolved_size = int(result.get("file_size") or file_size or 0)
    if resolved_size and resolved_size > TELEGRAM_DOWNLOAD_MAX_BYTES:
        raise oversize_download_error(resolved_size)

    source_name = preferred_name or Path(file_path).name
    safe_name = sanitize_filename(source_name)
    target = UPLOAD_DIR / f"{int(time.time())}_{safe_name}"
    last_error: Exception | None = None
    for download_url in build_download_urls(file_path):
        try:
            with urllib.request.urlopen(download_url, timeout=TELEGRAM_TIMEOUT + 30) as response:
                target.write_bytes(response.read())
            return target
        except Exception as error:
            last_error = error
            continue

    print(
        f"bridge warning: failed Telegram download for file_id={file_id} file_path={file_path!r} size={resolved_size or 'unknown'}",
        file=sys.stderr,
        flush=True,
    )
    if last_error is not None:
        raise RuntimeError(f"Telegram file download failed: {last_error}")
    raise RuntimeError("Telegram file download failed for an unknown reason")


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


def busy_lock_text() -> str:
    return (
        "I am still working on your previous request.\n\n"
        "Use /cancel to stop it, /debug to inspect the live technical trace, "
        "or wait for it to finish before starting a new task."
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
        "GitHub Actions subscriptions are managed per chat: use /actions, then /watch <number>.\n\n"
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
    repo_slug = resolve_github_actions_repo() or "not configured"
    subscriptions = get_all_action_subscriptions(state)
    chat_count = len(subscriptions)
    return (
        "Bridge is running\n\n"
        f"Repo: {REPO_PATH}\n"
        f"Branch: {branch}\n"
        f"GitHub Actions repo: {repo_slug}\n"
        f"Action subscription chats: {chat_count}\n"
        f"Whitelisted users: {len(ALLOWED_USER_IDS)}\n"
        f"Session state: {'continuing' if session_state.get('has_session') else 'new'}\n"
        f"Active request: {'yes' if get_active_request(user_id) else 'no'}\n"
        f"Upload tool: {'configured' if UPLOAD_TOOL.exists() else 'missing'}"
    )


def fetch_actions_repo_and_workflows() -> tuple[str | None, list[dict], str | None]:
    repo_slug = resolve_github_actions_repo()
    if not repo_slug:
        return None, [], (
            "GitHub Actions repo is not configured. Set GITHUB_ACTIONS_REPO=owner/repo, "
            "or point REPO_PATH at a GitHub clone with an origin remote."
        )
    try:
        workflows = list_github_workflows(repo_slug)
    except Exception as error:
        return repo_slug, [], f"Failed to load GitHub Actions workflows: {error}"
    return repo_slug, workflows, None


def handle_actions_command(message: dict, state: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    repo_slug, workflows, error = fetch_actions_repo_and_workflows()
    if error:
        send_message(chat_id, error)
        return
    set_action_selection(state, user_id, chat_id, repo_slug or "", workflows)
    subscribed_ids = get_chat_action_subscriptions(state, chat_id)
    send_text_blocks(chat_id, actions_list_text(repo_slug or "unknown", workflows, subscribed_ids))


def handle_subscriptions_command(message: dict, state: dict) -> None:
    chat_id = message["chat"]["id"]
    repo_slug, workflows, error = fetch_actions_repo_and_workflows()
    if error and not get_chat_action_subscriptions(state, chat_id):
        send_message(chat_id, error)
        return
    send_text_blocks(
        chat_id,
        action_subscriptions_text(repo_slug, workflows, get_chat_action_subscriptions(state, chat_id)),
    )


def handle_watch_command(message: dict, state: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    argument = parse_command_argument(get_message_text(message))
    repo_slug, workflows, error = fetch_actions_repo_and_workflows()
    if error:
        send_message(chat_id, error)
        return

    selection = get_action_selection(state, user_id, chat_id)
    workflow, resolution_error = resolve_workflow_selection(argument, selection, workflows)
    if resolution_error:
        send_message(chat_id, resolution_error)
        return
    if not workflow or workflow.get("id") == -1:
        send_message(chat_id, "Use /watch <number> to subscribe one workflow at a time.")
        return

    workflow_id = int(workflow.get("id", 0))
    subscribed_ids = get_chat_action_subscriptions(state, chat_id)
    if workflow_id in subscribed_ids:
        send_message(chat_id, "This chat is already subscribed to that workflow.")
        return

    subscribed_ids.append(workflow_id)
    set_chat_action_subscriptions(state, chat_id, subscribed_ids)

    status_note = "Notifications will be posted here when it starts and when it finishes."
    try:
        runs = list_github_workflow_runs(repo_slug or "", workflow_id)
    except Exception:
        runs = []
    if runs:
        latest = serialize_workflow_run(runs[0])
        if is_active_workflow_run(latest):
            status_note = (
                "Notifications will be posted here. "
                f"Current latest run is already active: #{latest['run_number']} ({latest['status']})."
            )
        else:
            result = latest.get("conclusion") or latest.get("status") or "unknown"
            status_note = (
                "Notifications will be posted here. "
                f"Latest completed run: #{latest['run_number']} ({result})."
            )

    send_message(
        chat_id,
        (
            f"Subscribed this chat to {workflow.get('name', 'workflow')}.\n\n"
            f"Repo: {repo_slug}\n"
            f"Workflow file: {Path(str(workflow.get('path', ''))).name or 'unknown'}\n"
            f"{status_note}"
        ),
    )


def handle_unwatch_command(message: dict, state: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    argument = parse_command_argument(get_message_text(message))
    subscribed_ids = get_chat_action_subscriptions(state, chat_id)
    if not subscribed_ids:
        send_message(chat_id, "This chat has no GitHub Actions subscriptions.")
        return

    repo_slug, workflows, error = fetch_actions_repo_and_workflows()
    if error:
        workflows = []
    selection = get_action_selection(state, user_id, chat_id)
    workflow, resolution_error = resolve_workflow_selection(argument, selection, workflows)
    if resolution_error:
        send_message(chat_id, resolution_error)
        return
    if not workflow:
        send_message(chat_id, "Workflow not found. Use /subscriptions or /actions first.")
        return

    if int(workflow.get("id", 0)) == -1:
        set_chat_action_subscriptions(state, chat_id, [])
        send_message(chat_id, "Removed all GitHub Actions subscriptions from this chat.")
        return

    workflow_id = int(workflow.get("id", 0))
    if workflow_id not in subscribed_ids:
        send_message(chat_id, "This chat is not subscribed to that workflow.")
        return

    set_chat_action_subscriptions(state, chat_id, [item for item in subscribed_ids if item != workflow_id])
    send_message(chat_id, f"Unsubscribed this chat from {workflow.get('name', 'workflow')}.")


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


def build_copilot_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("GITHUB_ACTIONS_TOKEN", None)
    if GITHUB_API_TOKEN and env.get("GITHUB_TOKEN", "").strip() == GITHUB_API_TOKEN:
        env.pop("GITHUB_TOKEN", None)
    return env


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except Exception:
        return
    try:
        process.wait(timeout=5)
        return
    except Exception:
        pass
    try:
        process.kill()
        process.wait(timeout=5)
    except Exception:
        pass


def begin_upload_from_message(state: dict, user_id: int, chat_id: int, source_message: dict) -> None:
    attachment = describe_attachment(source_message)
    if not attachment or not attachment.get("file_id"):
        send_message(chat_id, "That message does not include downloadable media.")
        return

    clear_upload_session(state, user_id, cleanup_local_file=True)

    try:
        attachment_path = download_telegram_file(
            str(attachment.get("file_id")),
            attachment.get("preferred_name"),
            int(attachment.get("file_size") or 0),
        )
    except Exception as error:
        send_message(chat_id, f"Failed to download Telegram attachment:\n\n{error}")
        return

    source_name = attachment.get("preferred_name") or attachment_path.name
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
        env=build_copilot_env(),
    )
    worker = threading.current_thread()
    user_id = getattr(worker, "user_id", None)
    if user_id is None:
        terminate_process(process)
        return False, False, "Worker metadata is missing for this request."
    if not bind_active_request_process(user_id, process):
        terminate_process(process)
        return False, False, "Request state was lost before Copilot started."
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
            active_request = get_active_request(user_id)
            if active_request is None or active_request.get("cancel_requested"):
                terminate_process(process)
                flush_buffer()
                return False, sent_any, "Request was cancelled."
            if time.monotonic() > deadline:
                terminate_process(process)
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
        terminate_process(process)
        flush_buffer()
        return False, sent_any, str(error)

    flush_buffer()
    active_request = get_active_request(user_id)
    if active_request is None or active_request.get("cancel_requested"):
        return False, sent_any, "Request was cancelled."
    if process.returncode == 0:
        return True, sent_any, ""
    if process.returncode in {-signal.SIGTERM, -signal.SIGKILL}:
        return False, sent_any, "Request was cancelled."
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
    worker = threading.current_thread()
    worker.user_id = user_id

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

    session_state = get_session_state(state, user_id)
    session_state["has_session"] = False
    save_state(state)

    if completed_request.get("cancel_requested") or result == "Request was cancelled.":
        send_message(chat_id, "Cancelled the active request and reset your Copilot session. You can start a new one now.")
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
    if command == "/actions":
        handle_actions_command(message, state)
        send_group_done_ack(chat_id, message)
        return
    if command == "/subscriptions":
        handle_subscriptions_command(message, state)
        send_group_done_ack(chat_id, message)
        return
    if command == "/watch":
        handle_watch_command(message, state)
        send_group_done_ack(chat_id, message)
        return
    if command == "/unwatch":
        handle_unwatch_command(message, state)
        send_group_done_ack(chat_id, message)
        return
    if command == "/debug":
        handle_debug_command(message, state)
        return
    if command == "/cancel":
        active_request = get_active_request(user_id)
        if active_request:
            cancel_active_request(user_id)
            send_message(chat_id, "Stopping the active request. I will reset your session as soon as the worker exits.")
            return
        send_message(chat_id, "There is no pending upload or active request to cancel.")
        send_group_done_ack(chat_id, message)
        return
    if command == "/new":
        if get_active_request(user_id):
            send_message(chat_id, busy_lock_text())
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
        send_message(chat_id, busy_lock_text())
        return

    attachment_path = None
    attachment = describe_attachment(message)
    if attachment and attachment.get("file_id"):
        try:
            attachment_path = download_telegram_file(
                str(attachment.get("file_id")),
                attachment.get("preferred_name"),
                int(attachment.get("file_size") or 0),
            )
        except Exception as error:
            send_message(chat_id, f"Failed to download Telegram attachment:\n\n{error}")
            send_group_done_ack(chat_id, message)
            return

    referenced_message = message.get("reply_to_message")
    referenced_attachment_path = None
    referenced_attachment_warning = None
    if referenced_message:
        referenced_attachment = describe_attachment(referenced_message)
        if referenced_attachment and referenced_attachment.get("file_id"):
            try:
                referenced_attachment_path = download_telegram_file(
                    str(referenced_attachment.get("file_id")),
                    referenced_attachment.get("preferred_name"),
                    int(referenced_attachment.get("file_size") or 0),
                )
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


def poll_github_action_updates(state: dict) -> None:
    while True:
        try:
            repo_slug = resolve_github_actions_repo()
            subscriptions_by_chat = get_all_action_subscriptions(state)
            subscribed_workflow_ids = sorted({
                workflow_id
                for workflow_ids in subscriptions_by_chat.values()
                for workflow_id in workflow_ids
            })

            if not repo_slug or not subscribed_workflow_ids:
                time.sleep(GITHUB_POLL_INTERVAL)
                continue

            notifications: list[tuple[int, str]] = []
            changed = False

            with STATE_LOCK:
                actions = get_actions_state(state)
                known_runs = actions.setdefault("known_runs", {})
                initialized = bool(actions.get("initialized"))

                for workflow_id in subscribed_workflow_ids:
                    runs = list_github_workflow_runs(repo_slug, workflow_id)
                    for raw_run in runs:
                        run = serialize_workflow_run(raw_run)
                        if not run["id"] or run["workflow_id"] != workflow_id:
                            continue

                        run_key = str(run["id"])
                        previous = known_runs.get(run_key)
                        current_status = run.get("status", "unknown")
                        current_conclusion = run.get("conclusion", "")

                        if previous is None:
                            known_runs[run_key] = run
                            changed = True
                            if initialized and is_active_workflow_run(run):
                                for chat_id, workflow_ids in subscriptions_by_chat.items():
                                    if workflow_id in workflow_ids:
                                        notifications.append((chat_id, workflow_started_text(run)))
                            continue

                        previous_status = str(previous.get("status", "unknown"))
                        previous_conclusion = str(previous.get("conclusion", ""))
                        if previous_status != current_status or previous_conclusion != current_conclusion:
                            known_runs[run_key] = run
                            changed = True
                            if previous_status != "completed" and current_status == "completed":
                                for chat_id, workflow_ids in subscriptions_by_chat.items():
                                    if workflow_id in workflow_ids:
                                        notifications.append((chat_id, workflow_finished_text(run)))

                known_ids = {str(run_id) for run_id in known_runs.keys()}
                active_known: dict[str, dict] = {}
                for run_key in sorted(known_ids, key=int, reverse=True):
                    entry = known_runs.get(run_key)
                    if not entry:
                        continue
                    workflow_id = int(entry.get("workflow_id", 0) or 0)
                    if workflow_id not in subscribed_workflow_ids:
                        changed = True
                        continue
                    per_workflow_count = sum(
                        1 for existing in active_known.values()
                        if int(existing.get("workflow_id", 0) or 0) == workflow_id
                    )
                    if per_workflow_count >= ACTION_RUNS_PER_WORKFLOW:
                        changed = True
                        continue
                    active_known[run_key] = entry

                if len(active_known) != len(known_runs):
                    known_runs.clear()
                    known_runs.update(active_known)
                    changed = True

                if not initialized:
                    actions["initialized"] = True
                    changed = True

                if changed:
                    save_state(state)

            for chat_id, text in notifications:
                try:
                    send_message(chat_id, text, parse_mode="HTML")
                except Exception as error:
                    print(
                        f"bridge warning: failed to send GitHub Actions notification to chat {chat_id}: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
        except Exception as error:
            print(f"bridge warning: GitHub Actions polling failed: {error}", file=sys.stderr, flush=True)

        time.sleep(GITHUB_POLL_INTERVAL)


def main() -> int:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    sync_bot_commands_safe()
    state = load_state()
    threading.Thread(target=poll_github_action_updates, args=(state,), daemon=True).start()
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