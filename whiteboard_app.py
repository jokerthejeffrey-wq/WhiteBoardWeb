# app.py
# Render Start Command:
# gunicorn app:app
#
# requirements.txt:
# Flask
# Werkzeug
# requests
# gunicorn

import os
import re
import json
import gzip
import time
import html
import secrets
import hashlib
from io import BytesIO
from pathlib import Path
from functools import wraps

import requests
from flask import (
    Flask, request, redirect, url_for, session, flash,
    render_template_string, jsonify, abort, Response
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename


APP_NAME = os.environ.get("APP_NAME", "Boardlume")
DISCORD_API = "https://discord.com/api/v10"

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
DISCORD_DB_CHANNEL_ID = os.environ.get("DISCORD_DB_CHANNEL_ID", "").strip()

CREATOR_EMAIL = os.environ.get("CREATOR_EMAIL", "tuna.iren@outlook.com").strip().lower()
MOD_EMAILS = [x.strip().lower() for x in os.environ.get("MOD_EMAILS", "").split(",") if x.strip()]

MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", str(10 * 1024 * 1024)))
MAX_DB_SIZE = int(os.environ.get("MAX_DB_SIZE", str(7 * 1024 * 1024)))

MAX_TOTAL_ITEMS = int(os.environ.get("MAX_TOTAL_ITEMS", "400"))
MAX_ITEMS_PER_USER = int(os.environ.get("MAX_ITEMS_PER_USER", "100"))
MAX_DRAW_POINTS = int(os.environ.get("MAX_DRAW_POINTS", "300"))
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "300"))
MAX_TEXT_W = int(os.environ.get("MAX_TEXT_W", "700"))
MAX_TEXT_H = int(os.environ.get("MAX_TEXT_H", "300"))

CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "45"))
FAST_BOOT_MESSAGE_PAGES = int(os.environ.get("FAST_BOOT_MESSAGE_PAGES", "4"))
DB_SNAPSHOT_KEEP = max(1, int(os.environ.get("DB_SNAPSHOT_KEEP", "1")))
DB_SNAPSHOT_DELETE_LIMIT = max(1, int(os.environ.get("DB_SNAPSHOT_DELETE_LIMIT", "200")))
AUTO_DELETE_OLD_SNAPSHOTS = os.environ.get("AUTO_DELETE_OLD_SNAPSHOTS", "1").strip().lower() not in {"0", "false", "no", "off"}

NAME_CHANGE_COOLDOWN_SECONDS = int(os.environ.get("NAME_CHANGE_COOLDOWN_SECONDS", str(10 * 24 * 60 * 60)))

ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a"}
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(64))
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE

CACHE = {"time": 0, "store": None}
ATTACHMENT_CACHE = {"items": {}, "seconds": 20 * 60}


# -----------------------------
# Helpers
# -----------------------------

def esc(value):
    return html.escape(str(value or ""), quote=True)


def now_ms():
    return int(time.time() * 1000)


def normalize_email(email):
    return (email or "").strip().lower()


def user_id_from_email(email):
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


def clean_text(value, limit=1000):
    value = (value or "").strip()
    value = re.sub(r"\r\n", "\n", value)
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    return value[:limit]


def clean_username(username):
    return (username or "").strip()[:24]


def valid_username(username):
    username = clean_username(username)
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{3,24}", username))


def clamp_int(value, low, high, default):
    try:
        value = int(value)
    except Exception:
        return default
    return max(low, min(high, value))


def safe_hex_color(value, default):
    value = (value or "").strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        return value
    return default


def seconds_text(seconds):
    seconds = max(0, int(seconds))
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f"{days} days {hours} hours"
    if hours > 0:
        return f"{hours} hours {minutes} minutes"
    return f"{minutes} minutes"


def file_size_text(size):
    try:
        size = int(size)
    except Exception:
        size = 0
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def is_staff_email(email):
    email = normalize_email(email)
    return email == CREATOR_EMAIL or email in MOD_EMAILS


def current_email():
    return session.get("email", "")


def current_user_id():
    if session.get("user_id"):
        return session.get("user_id")
    email = current_email()
    if not email:
        return ""
    return user_id_from_email(email)


def current_user():
    uid = current_user_id()
    if not uid:
        return None
    try:
        db = load_store()["db"]
        user = db["users"].get(uid)
        if user:
            return user
    except Exception:
        return None
    return None


def current_is_staff():
    user = current_user()
    return bool(user and is_staff_email(user.get("email", "")))


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Login first."}), 401
            return redirect(url_for("home"))
        return fn(*args, **kwargs)
    return wrapper


def allowed_image(filename):
    return Path((filename or "").lower()).suffix in ALLOWED_IMAGE_EXTENSIONS


def allowed_audio(filename):
    return Path((filename or "").lower()).suffix in ALLOWED_AUDIO_EXTENSIONS


def guess_content_type(filename, fallback="application/octet-stream"):
    ext = Path((filename or "").lower()).suffix

    mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
    }

    return mapping.get(ext, fallback)


def blank_db():
    return {
        "version": 4,
        "users": {},
        "items": {},
        "created_at": now_ms(),
        "updated_at": now_ms(),
    }


def normalize_db(db):
    if not isinstance(db, dict):
        db = blank_db()

    clean = blank_db()
    clean.update(db)

    if not isinstance(clean.get("users"), dict):
        clean["users"] = {}
    if not isinstance(clean.get("items"), dict):
        clean["items"] = {}

    clean["version"] = 4
    clean.setdefault("created_at", now_ms())
    clean.setdefault("updated_at", now_ms())
    return clean


def item_limit_ok(db, user):
    total = len(db.get("items", {}))
    if total >= MAX_TOTAL_ITEMS:
        return False, "board full"

    mine = len([x for x in db.get("items", {}).values() if x.get("user_id") == user.get("id")])
    if mine >= MAX_ITEMS_PER_USER:
        return False, "item limit reached"

    return True, ""


def rate_limit_user(db, user, key, seconds):
    users = db.get("users", {})
    live = users.get(user.get("id"), user)
    cooldowns = live.setdefault("cooldowns", {})
    last = int(cooldowns.get(key, 0) or 0)
    remaining = seconds - (int(time.time()) - last)

    if remaining > 0 and not is_staff_email(live.get("email", "")):
        return False, f"wait {remaining}s"

    cooldowns[key] = int(time.time())
    users[live["id"]] = live
    db["users"] = users
    return True, ""


def item_bounds(item):
    x = int(item.get("x", 0))
    y = int(item.get("y", 0))
    w = int(item.get("w", 100))
    h = int(item.get("h", 100))
    return x, y, w, h


def intersects(item, left, top, right, bottom, margin=900):
    x, y, w, h = item_bounds(item)
    return not (
        x + w < left - margin or
        x > right + margin or
        y + h < top - margin or
        y > bottom + margin
    )


# -----------------------------
# Discord snapshot storage
# -----------------------------

def require_storage_config():
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN.")
    if not DISCORD_DB_CHANNEL_ID:
        raise RuntimeError("Missing DISCORD_DB_CHANNEL_ID.")


def discord_request(method, endpoint, **kwargs):
    require_storage_config()

    url = endpoint if endpoint.startswith("http") else f"{DISCORD_API}{endpoint}"
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bot {DISCORD_BOT_TOKEN}"

    for _ in range(5):
        r = requests.request(method, url, headers=headers, timeout=45, **kwargs)

        if r.status_code == 429:
            try:
                retry_after = float(r.json().get("retry_after", 1))
            except Exception:
                retry_after = 1
            time.sleep(retry_after)
            continue

        if not (200 <= r.status_code < 300):
            raise RuntimeError(f"storage error {r.status_code}")

        return r

    raise RuntimeError("storage rate limit")


def clear_cache():
    CACHE["time"] = 0
    CACHE["store"] = None


def fetch_messages(max_pages=FAST_BOOT_MESSAGE_PAGES, stop_after_snapshot=True):
    messages_all = []
    before = None

    for _ in range(max_pages):
        params = {"limit": 100}
        if before:
            params["before"] = before

        r = discord_request("GET", f"/channels/{DISCORD_DB_CHANNEL_ID}/messages", params=params)
        messages = r.json()

        if not messages:
            break

        messages_all.extend(messages)
        before = messages[-1]["id"]

        if stop_after_snapshot and any((m.get("content", "") or "").startswith("WBDBSNAP|") for m in messages):
            break

        if len(messages) < 100:
            break

    return messages_all


def post_attachment(content, filename, file_bytes, content_type):
    payload = {"content": content}
    data = {"payload_json": json.dumps(payload)}
    files = {"files[0]": (filename, BytesIO(file_bytes), content_type or "application/octet-stream")}
    r = discord_request("POST", f"/channels/{DISCORD_DB_CHANNEL_ID}/messages", data=data, files=files)
    clear_cache()
    return r.json()


def delete_message(message_id):
    if not message_id:
        return False
    try:
        discord_request("DELETE", f"/channels/{DISCORD_DB_CHANNEL_ID}/messages/{message_id}")
        return True
    except Exception:
        return False


def cleanup_old_snapshots(keep=None, delete_limit=None):
    # Deletes only old WBDBSNAP database messages.
    # It does NOT delete uploaded images/audio attachments.
    # This keeps the board database storage small while preserving files.
    if keep is None:
        keep = DB_SNAPSHOT_KEEP
    if delete_limit is None:
        delete_limit = DB_SNAPSHOT_DELETE_LIMIT

    keep = max(1, int(keep))
    delete_limit = max(1, int(delete_limit))

    total_deleted = 0
    total_found = 0
    total_kept = 0

    # Run multiple passes because Discord messages are paginated.
    # This removes old database snapshots aggressively without touching file uploads.
    for _ in range(6):
        try:
            messages = fetch_messages(max_pages=20, stop_after_snapshot=False)
        except Exception:
            break

        snapshots = [
            m for m in messages
            if (m.get("content", "") or "").startswith("WBDBSNAP|")
        ]

        snapshots.sort(key=lambda m: int(m.get("id", "0")), reverse=True)

        total_found = max(total_found, len(snapshots))
        total_kept = keep

        to_delete = snapshots[keep:keep + delete_limit]

        if not to_delete:
            break

        deleted_this_pass = 0

        for msg in to_delete:
            if delete_message(msg.get("id", "")):
                deleted_this_pass += 1
                total_deleted += 1
                time.sleep(0.22)

        if deleted_this_pass == 0:
            break

        clear_cache()

        if len(to_delete) < delete_limit:
            break

    return {
        "ok": True,
        "deleted": total_deleted,
        "kept": total_kept,
        "total": total_found,
    }


def load_store(force=False):
    now = time.time()
    if not force and CACHE["store"] is not None and now - CACHE["time"] < CACHE_SECONDS:
        return CACHE["store"]

    messages = fetch_messages(max_pages=FAST_BOOT_MESSAGE_PAGES, stop_after_snapshot=True)

    db = blank_db()
    snapshot_loaded = False

    for msg in messages:
        content = msg.get("content", "") or ""
        attachments = msg.get("attachments", []) or []

        if content.startswith("WBDBSNAP|") and attachments:
            try:
                url = attachments[0].get("url", "")
                r = requests.get(url, timeout=60)
                r.raise_for_status()
                raw = r.content
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                db = normalize_db(json.loads(raw.decode("utf-8")))
                snapshot_loaded = True
                break
            except Exception:
                continue

    store = {"db": normalize_db(db), "snapshot_loaded": snapshot_loaded, "message_count": len(messages)}
    CACHE["time"] = now
    CACHE["store"] = store
    return store


def save_db(db):
    db = normalize_db(db)
    db["updated_at"] = now_ms()

    raw_json = json.dumps(db, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    raw = gzip.compress(raw_json, compresslevel=6)

    if len(raw) > MAX_DB_SIZE:
        raise ValueError("board data too large")

    post_attachment(
        content=f"WBDBSNAP|v4|gz|{int(time.time())}",
        filename="whiteboard-db.json.gz",
        file_bytes=raw,
        content_type="application/gzip",
    )

    if AUTO_DELETE_OLD_SNAPSHOTS:
        # After every board update, keep only the newest DB snapshot.
        # This deletes old database saves, not uploaded files.
        cleanup_old_snapshots(keep=DB_SNAPSHOT_KEEP, delete_limit=DB_SNAPSHOT_DELETE_LIMIT)

    clear_cache()


def cache_attachment(kind, key, info):
    if not key or not info:
        return
    ATTACHMENT_CACHE["items"][(kind, str(key))] = {"time": time.time(), "info": info}


def get_cached_attachment(kind, key):
    item = ATTACHMENT_CACHE["items"].get((kind, str(key)))
    if not item:
        return None
    if time.time() - item.get("time", 0) > ATTACHMENT_CACHE["seconds"]:
        ATTACHMENT_CACHE["items"].pop((kind, str(key)), None)
        return None
    return item.get("info")


def attachment_info_from_message(message_id):
    cached = get_cached_attachment("message", message_id)
    if cached:
        return cached

    try:
        r = discord_request("GET", f"/channels/{DISCORD_DB_CHANNEL_ID}/messages/{message_id}")
        msg = r.json()
        attachments = msg.get("attachments", []) or []
        if not attachments:
            return None

        a = attachments[0]
        info = {
            "url": a.get("url", ""),
            "proxy_url": a.get("proxy_url", ""),
            "filename": a.get("filename", ""),
            "size": a.get("size", 0),
            "content_type": a.get("content_type", ""),
        }
        cache_attachment("message", message_id, info)
        return info
    except Exception:
        return None


# -----------------------------
# Item serialization
# -----------------------------

def item_can_edit(item, user=None):
    if not user:
        user = current_user()
    if not user:
        return False
    return item.get("user_id") == user.get("id") or is_staff_email(user.get("email", ""))


def public_item(item, user=None):
    kind = item.get("type", "text")
    out = {
        "id": item.get("id", ""),
        "type": kind,
        "user_id": item.get("user_id", ""),
        "username": item.get("username", "unknown"),
        "x": int(item.get("x", 0)),
        "y": int(item.get("y", 0)),
        "w": int(item.get("w", 220)),
        "h": int(item.get("h", 140)),
        "z": int(item.get("z", 1)),
        "text": item.get("text", ""),
        "color": item.get("color", "#111111"),
        "bg": item.get("bg", "#ffffff"),
        "font": clamp_int(item.get("font"), 10, 72, 18),
        "stroke": item.get("stroke", "#111111"),
        "stroke_width": int(item.get("stroke_width", 4)),
        "points": item.get("points", []),
        "filename": item.get("filename", ""),
        "size": int(item.get("size", 0)),
        "can_edit": item_can_edit(item, user),
        "created": int(item.get("created", 0)),
    }

    if kind in {"image", "audio"}:
        out["file_url"] = url_for("board_file", item_id=item.get("id", ""))
    else:
        out["file_url"] = ""

    return out


def visible_items(db, user, left, top, right, bottom):
    items = []
    for raw in db.get("items", {}).values():
        if intersects(raw, left, top, right, bottom, margin=1200):
            items.append(public_item(raw, user))
    items.sort(key=lambda x: (x["z"], x["created"]))
    return items


# -----------------------------
# UI
# -----------------------------

HTML = """
<!DOCTYPE html>
<html>
<head>
<title>{{ app_name }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{box-sizing:border-box}
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#fff;color:#111;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",Arial,sans-serif;touch-action:none}
button,input,textarea{font:inherit}
#viewport{position:fixed;inset:0;overflow:hidden;background-color:#fff;background-image:linear-gradient(rgba(0,0,0,.055) 1px, transparent 1px),linear-gradient(90deg, rgba(0,0,0,.055) 1px, transparent 1px);background-size:42px 42px;cursor:default}
#world{position:absolute;left:0;top:0;transform-origin:0 0}
.item{position:absolute;user-select:none;touch-action:none}
.item.editable{cursor:move}
.text-item{background:transparent;white-space:pre-wrap;overflow:hidden;padding:34px 8px 4px 8px;line-height:1.05;border:0;box-shadow:none;font-weight:400}
.image-item{overflow:visible!important}.image-item img{width:100%;height:100%;object-fit:contain;display:block;pointer-events:none}
.audio-item{background:#fff;padding:34px 8px 8px 8px;overflow:visible!important}
.audio-item audio{width:100%}
.draw-item{overflow:visible!important}.draw-item svg{width:100%;height:100%;display:block;overflow:visible}
.tag{position:absolute;right:3px;bottom:2px;font-size:10px;color:#aaa;background:rgba(255,255,255,.65);padding:1px 4px}
.toolbar{position:absolute;left:6px;top:6px;display:none;gap:4px;z-index:99999;background:rgba(255,255,255,.86);padding:4px;box-shadow:0 8px 24px rgba(0,0,0,.10)}
.item.editable:hover .toolbar,.toolbar:hover{display:flex}
.toolbar button{border:0;background:#111;color:#fff;font-size:11px;padding:6px 7px;cursor:pointer}
#drawCanvas{position:fixed;left:0;top:0;width:100vw;height:100vh;display:none;z-index:9998;cursor:crosshair}
body.draw-mode #drawCanvas{display:block}
#tools{position:fixed;z-index:10050;left:14px;top:50%;transform:translateY(-50%);display:flex;flex-direction:column;gap:8px}
.tool{width:44px;height:44px;border:0;background:rgba(255,255,255,.78);backdrop-filter:blur(18px);box-shadow:0 10px 30px rgba(0,0,0,.08);font-weight:900;font-size:16px}
.tool.active{background:#111;color:#fff}
.panel{position:fixed;z-index:10040;left:66px;top:50%;transform:translateY(-50%);width:auto;background:rgba(255,255,255,.78);backdrop-filter:blur(22px);box-shadow:0 20px 60px rgba(0,0,0,.10);padding:8px;display:none}
.panel.show{display:flex;gap:8px;align-items:center}
label{display:block;font-size:11px;font-weight:800;color:#777;margin:8px 0 5px}
input,textarea{width:100%;border:0;background:rgba(240,240,240,.75);outline:0;padding:9px}
textarea{min-height:90px;resize:vertical}
.row{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.draft-wrap{position:absolute;width:360px;height:130px;background:transparent;border:2px dashed #111;z-index:9997;box-shadow:none;overflow:visible}
.draft-input{position:absolute;inset:0;width:100%;height:100%;border:0!important;background:transparent!important;outline:0;resize:none;overflow:hidden;padding:10px 14px;line-height:1.05;font-size:58px;font-weight:400;color:#111111;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",Arial,sans-serif}
.draft-input::placeholder{color:rgba(0,0,0,.25)}
.draft-handle{position:absolute;width:11px;height:11px;background:#fff;border:2px solid #111;z-index:9999}
.draft-handle.tl{left:-7px;top:-7px;cursor:nwse-resize}.draft-handle.tm{left:50%;top:-7px;transform:translateX(-50%);cursor:ns-resize}.draft-handle.tr{right:-7px;top:-7px;cursor:nesw-resize}
.draft-handle.ml{left:-7px;top:50%;transform:translateY(-50%);cursor:ew-resize}.draft-handle.mr{right:-7px;top:50%;transform:translateY(-50%);cursor:ew-resize}
.draft-handle.bl{left:-7px;bottom:-7px;cursor:nesw-resize}.draft-handle.bm{left:50%;bottom:-7px;transform:translateX(-50%);cursor:ns-resize}.draft-handle.br{right:-7px;bottom:-7px;cursor:nwse-resize}
.color-dot{width:24px;height:24px;border-radius:50%;border:2px solid rgba(0,0,0,.14);box-shadow:0 4px 14px rgba(0,0,0,.08);cursor:pointer}
.color-dot.active{outline:3px solid #111;outline-offset:2px}
.btn{border:0;background:#111;color:white;padding:10px 12px;font-weight:800;margin-top:8px}
.btn2{border:0;background:#eee;color:#111;padding:10px 12px;font-weight:800;margin-top:8px}
#loginOverlay{position:fixed;inset:0;z-index:99999;background:rgba(255,255,255,.35);backdrop-filter:blur(16px);display:flex;align-items:center;justify-content:center}
#loginBox{width:min(390px,92vw);background:rgba(255,255,255,.82);backdrop-filter:blur(26px);box-shadow:0 30px 100px rgba(0,0,0,.14);padding:18px}
#settingsOverlay{position:fixed;inset:0;z-index:10060;background:rgba(255,255,255,.35);backdrop-filter:blur(16px);display:none;align-items:center;justify-content:center}
.hidden{display:none!important}
.msg{position:fixed;z-index:1000;top:12px;left:50%;transform:translateX(-50%);background:#111;color:white;padding:9px 12px;font-size:13px}
</style>
</head>
<body>

{% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        {% for category,message in messages %}
            <div class="msg">{{ message }}</div>
        {% endfor %}
    {% endif %}
{% endwith %}

<div id="viewport">
    <div id="world"></div>
    <canvas id="drawCanvas"></canvas>
</div>

{% if user %}
<div id="tools">
    <button class="tool active" data-tool="text" onclick="createTextBox()">T</button>
    <button class="tool" data-tool="image" onclick="pickImage()">◎</button>
    <button class="tool" data-tool="audio" onclick="pickAudio()">♫</button>
    <button class="tool" data-tool="draw" onclick="toggleDrawPalette()">✎</button>
    <button class="tool" onclick="openSettings()">⚙</button>
</div>

<form id="imageForm" action="{{ url_for('add_image') }}" method="POST" enctype="multipart/form-data" class="hidden">
    <input name="x" id="imageX" type="hidden">
    <input name="y" id="imageY" type="hidden">
    <input id="imagePicker" name="image" type="file" accept=".png,.jpg,.jpeg,.gif,.webp">
</form>

<form id="audioForm" action="{{ url_for('add_audio') }}" method="POST" enctype="multipart/form-data" class="hidden">
    <input name="x" id="audioX" type="hidden">
    <input name="y" id="audioY" type="hidden">
    <input id="audioPicker" name="audio" type="file" accept=".mp3,.wav,.ogg,.m4a">
</form>

<div id="panel-draw" class="panel">
    <button class="color-dot active" style="background:#111111" onclick="selectDrawColor('#111111', this)"></button>
    <button class="color-dot" style="background:#ff3b30" onclick="selectDrawColor('#ff3b30', this)"></button>
    <button class="color-dot" style="background:#ff9500" onclick="selectDrawColor('#ff9500', this)"></button>
    <button class="color-dot" style="background:#ffcc00" onclick="selectDrawColor('#ffcc00', this)"></button>
    <button class="color-dot" style="background:#34c759" onclick="selectDrawColor('#34c759', this)"></button>
    <button class="color-dot" style="background:#007aff" onclick="selectDrawColor('#007aff', this)"></button>
    <button class="color-dot" style="background:#af52de" onclick="selectDrawColor('#af52de', this)"></button>
</div>

<div id="settingsOverlay">
    <div id="loginBox">
        <form action="{{ url_for('settings') }}" method="POST">
            <label>name</label>
            <input name="username" value="{{ user.username }}" required>
            <button class="btn" type="submit">save</button>
            <button class="btn2" type="button" onclick="closeSettings()">cancel</button>
        </form>
    </div>
</div>
{% endif %}

{% if not user %}
<div id="loginOverlay">
    <div id="loginBox">
        <form id="registerBox" action="{{ url_for('register') }}" method="POST">
            <input name="username" placeholder="your name.." required>
            <br><br>
            <input name="email" type="email" placeholder="email" required>
            <br><br>
            <input name="password" type="password" placeholder="password" required>
            <button class="btn" type="submit">create</button>
            <button class="btn2" type="button" onclick="showAuth('login')">login</button>
        </form>

        <form id="loginBoxForm" class="hidden" action="{{ url_for('login') }}" method="POST">
            <input name="email_or_user" placeholder="email or name" required>
            <br><br>
            <input name="password" type="password" placeholder="password" required>
            <button class="btn" type="submit">login</button>
            <button class="btn2" type="button" onclick="showAuth('register')">create</button>
        </form>
    </div>
</div>
{% endif %}

<script>
let camera = {x: Number(localStorage.camX || 0), y: Number(localStorage.camY || 0)};
let loaded = new Map();
let loading = false;
let lastLoadKey = "";
const viewport = document.getElementById("viewport");
const world = document.getElementById("world");

function screenToWorld(sx, sy){
    return {x: Math.round(sx - camera.x), y: Math.round(sy - camera.y)};
}
function centerWorld(){
    return screenToWorld(window.innerWidth/2, window.innerHeight/2);
}
function fillCenter(xid,yid){
    const p = centerWorld();
    const x = document.getElementById(xid);
    const y = document.getElementById(yid);
    if(x) x.value = p.x;
    if(y) y.value = p.y;
}

function pickImage(){
    setActiveTool("image");
    hidePanels();
    fillCenter("imageX","imageY");
    document.getElementById("imagePicker").click();
}
function pickAudio(){
    setActiveTool("audio");
    hidePanels();
    fillCenter("audioX","audioY");
    document.getElementById("audioPicker").click();
}
document.addEventListener("change", e=>{
    if(e.target && e.target.id === "imagePicker" && e.target.files.length){
        uploadPickedFile("image");
    }
    if(e.target && e.target.id === "audioPicker" && e.target.files.length){
        uploadPickedFile("audio");
    }
});

async function uploadPickedFile(kind){
    const isImage = kind === "image";
    const picker = document.getElementById(isImage ? "imagePicker" : "audioPicker");
    const xInput = document.getElementById(isImage ? "imageX" : "audioX");
    const yInput = document.getElementById(isImage ? "imageY" : "audioY");

    if(!picker || !picker.files || !picker.files.length) return;

    const p = centerWorld();
    if(xInput) xInput.value = p.x;
    if(yInput) yInput.value = p.y;

    const form = new FormData();
    form.append(isImage ? "image" : "audio", picker.files[0]);
    form.append("x", p.x);
    form.append("y", p.y);

    picker.value = "";

    try{
        const res = await fetch(isImage ? "/api/add-image" : "/api/add-audio", {
            method: "POST",
            body: form
        });

        let out = {};
        try{ out = await res.json(); }catch(e){}

        if(!res.ok || out.ok === false){
            alert(out.error || "upload failed");
            return;
        }

        if(out.item && !loaded.has(out.item.id)){
            loaded.set(out.item.id, out.item);
            world.insertAdjacentHTML("beforeend", itemHtml(out.item));
            const el = document.getElementById("item-" + out.item.id);
            if(el && out.item.type === "text"){
                el.querySelector("[data-text-body]").textContent = out.item.text || "";
            }
            bindDragging();
        }

        lastLoadKey = "";
        setTimeout(loadViewport, 250);
    }catch(err){
        alert("upload failed");
    }
}

let draftBox = null;
let draftInput = null;
let draftSaveLock = false;

function createTextBox(){
    setActiveTool("text");
    hidePanels();
    if(draftBox) return;

    const p = centerWorld();

    const box = document.createElement("div");
    box.className = "draft-wrap";
    box.style.left = p.x + "px";
    box.style.top = p.y + "px";
    box.style.width = "360px";
    box.style.height = "130px";

    const input = document.createElement("textarea");
    input.className = "draft-input";
    input.placeholder = "text box";
    input.maxLength = 300;
    input.spellcheck = false;
    input.style.color = selectedDrawColor || "#111111";

    box.appendChild(input);

    ["tl","tm","tr","ml","mr","bl","bm","br"].forEach(pos=>{
        const h = document.createElement("span");
        h.className = "draft-handle " + pos;
        h.dataset.handle = pos;
        h.addEventListener("mousedown", startDraftResize);
        box.appendChild(h);
    });

    world.appendChild(box);
    draftBox = box;
    draftInput = input;
    input.focus();

    input.addEventListener("keydown", e=>{
        if(e.key === "Enter" && (e.ctrlKey || e.metaKey)){
            e.preventDefault();
            saveDraftText();
        }
        if(e.key === "Escape"){
            e.preventDefault();
            cancelDraftText();
        }
    });
}

function cancelDraftText(){
    if(draftBox){
        draftBox.remove();
        draftBox = null;
        draftInput = null;
    }
}

function draftCleanText(){
    if(!draftInput) return "";
    return draftInput.value.trim().slice(0,300);
}

async function saveDraftText(){
    if(!draftBox || !draftInput || draftSaveLock) return;

    const txt = draftCleanText();
    if(!txt){
        cancelDraftText();
        return;
    }

    draftSaveLock = true;

    const data = {
        text: txt,
        color: selectedDrawColor || "#111111",
        x: parseInt(draftBox.style.left || "0"),
        y: parseInt(draftBox.style.top || "0"),
        w: Math.max(120, Math.min(700, Math.round(draftBox.offsetWidth))),
        h: Math.max(70, Math.min(300, Math.round(draftBox.offsetHeight)))
    };

    const out = await postJson("/api/add-text-box", data);

    if(out && out.ok){
        cancelDraftText();

        if(out.item && !loaded.has(out.item.id)){
            loaded.set(out.item.id, out.item);
            world.insertAdjacentHTML("beforeend", itemHtml(out.item));
            const el = document.getElementById("item-" + out.item.id);
            if(el && out.item.type === "text"){
                el.querySelector("[data-text-body]").textContent = out.item.text || "";
            }
            bindDragging();
        }

        lastLoadKey = "";
        setTimeout(loadViewport, 250);
    }

    draftSaveLock = false;
}

let draftResize = null;

function startDraftResize(e){
    if(!draftBox) return;

    e.preventDefault();
    e.stopPropagation();

    draftResize = {
        handle: e.target.dataset.handle || "br",
        x: e.clientX,
        y: e.clientY,
        left: parseInt(draftBox.style.left || "0"),
        top: parseInt(draftBox.style.top || "0"),
        w: draftBox.offsetWidth,
        h: draftBox.offsetHeight
    };
}

document.addEventListener("mousemove", e=>{
    if(!draftResize || !draftBox) return;

    let dx = e.clientX - draftResize.x;
    let dy = e.clientY - draftResize.y;

    let left = draftResize.left;
    let top = draftResize.top;
    let w = draftResize.w;
    let h = draftResize.h;

    const handle = draftResize.handle;

    if(handle.includes("r")) w = draftResize.w + dx;
    if(handle.includes("l")){
        w = draftResize.w - dx;
        left = draftResize.left + dx;
    }

    if(handle.includes("b")) h = draftResize.h + dy;
    if(handle.includes("t")){
        h = draftResize.h - dy;
        top = draftResize.top + dy;
    }

    w = Math.max(120, Math.min(700, w));
    h = Math.max(70, Math.min(300, h));

    draftBox.style.left = left + "px";
    draftBox.style.top = top + "px";
    draftBox.style.width = w + "px";
    draftBox.style.height = h + "px";
});

document.addEventListener("mouseup", ()=>{
    draftResize = null;
});

document.addEventListener("mousedown", e=>{
    if(draftBox && !draftBox.contains(e.target) && !e.target.closest("#tools")){
        saveDraftText();
    }
}, true);

function applyCamera(){
    world.style.transform = `translate(${camera.x}px, ${camera.y}px)`;
    viewport.style.backgroundPosition = `${camera.x}px ${camera.y}px`;
    localStorage.camX = camera.x;
    localStorage.camY = camera.y;
    scheduleLoad();
}
applyCamera();

function setActiveTool(name){
    if(name !== "draw"){
        drawMode = false;
        document.body.classList.remove("draw-mode");
    }
    document.querySelectorAll(".tool").forEach(x=>x.classList.remove("active"));
    const b = document.querySelector(`[data-tool="${name}"]`);
    if(b) b.classList.add("active");
}
function hidePanels(){
    document.querySelectorAll(".panel").forEach(x=>x.classList.remove("show"));
}
function showAuth(which){
    document.getElementById("registerBox").classList.toggle("hidden", which !== "register");
    document.getElementById("loginBoxForm").classList.toggle("hidden", which !== "login");
}
function openSettings(){ document.getElementById("settingsOverlay").style.display="flex"; }
function closeSettings(){ document.getElementById("settingsOverlay").style.display="none"; }

let panning=false, panStart={x:0,y:0}, camStart={x:0,y:0};
viewport.addEventListener("contextmenu", e=>e.preventDefault());

// Block browser zoom cheating: Ctrl/Cmd + wheel and Ctrl/Cmd + + - 0.
// Normal wheel only moves the board camera, so UI/tool size stays unchanged.
window.addEventListener("wheel", e=>{
    if(e.ctrlKey || e.metaKey){
        e.preventDefault();
        return;
    }

    const typing = e.target && (
        e.target.tagName === "TEXTAREA" ||
        e.target.tagName === "INPUT" ||
        e.target.isContentEditable
    );

    if(typing) return;

    e.preventDefault();

    camera.x -= e.deltaX;
    camera.y -= e.deltaY;

    if(e.shiftKey){
        camera.x -= e.deltaY;
        camera.y += e.deltaY;
    }

    applyCamera();
}, {passive:false});

window.addEventListener("keydown", e=>{
    const key = String(e.key || "").toLowerCase();

    if((e.ctrlKey || e.metaKey) && ["+","=","-","_","0"].includes(key)){
        e.preventDefault();
        return false;
    }
}, {capture:true});

["gesturestart","gesturechange","gestureend"].forEach(name=>{
    window.addEventListener(name, e=>e.preventDefault(), {passive:false});
});

viewport.addEventListener("mousedown", e=>{
    if(e.button !== 2) return;
    panning=true;
    panStart={x:e.clientX,y:e.clientY};
    camStart={x:camera.x,y:camera.y};
    viewport.style.cursor="grabbing";
});
document.addEventListener("mousemove", e=>{
    if(!panning) return;
    camera.x = camStart.x + (e.clientX-panStart.x);
    camera.y = camStart.y + (e.clientY-panStart.y);
    applyCamera();
});
document.addEventListener("mouseup", ()=>{
    if(panning){
        panning=false;
        viewport.style.cursor="default";
        loadViewport();
    }
});

function itemHtml(item){
    const editable = item.can_edit ? " editable" : "";
    let html = "";
    let style = `left:${item.x}px;top:${item.y}px;width:${item.w}px;height:${item.h}px;z-index:${item.z};`;

    let toolbar = "";
    if(item.can_edit){
        const edit = item.type === "text" ? `<button onclick="editText('${item.id}')">edit</button>` : "";
        toolbar = `<div class="toolbar">${edit}<button onclick="resizeItem('${item.id}',25)">+</button><button onclick="resizeItem('${item.id}',-25)">-</button><button onclick="layerItem('${item.id}',1)">front</button><button onclick="layerItem('${item.id}',-1)">back</button><button onclick="deleteItem('${item.id}')">del</button></div>`;
    }

    if(item.type === "text"){
        const bg = item.bg || "transparent";
        style += `background:${bg};color:${item.color};font-size:${item.font}px;`;
        html = `<div id="item-${item.id}" class="item text-item${editable}" data-id="${item.id}" style="${style}">${toolbar}<div data-text-body></div><div class="tag">@${escapeHtml(item.username)}</div></div>`;
    }else if(item.type === "image"){
        html = `<div id="item-${item.id}" class="item image-item${editable}" data-id="${item.id}" style="${style}">${toolbar}<img src="${item.file_url}?v=${item.created || Date.now()}" onload="this.dataset.ok='1'" onerror="this.style.display='none'; this.parentElement.insertAdjacentHTML('beforeend', '<div style=&quot;font-size:12px;color:#999;padding:8px&quot;>image loading failed</div>')"><div class="tag">@${escapeHtml(item.username)}</div></div>`;
    }else if(item.type === "audio"){
        html = `<div id="item-${item.id}" class="item audio-item${editable}" data-id="${item.id}" style="${style}">${toolbar}<audio controls preload="none" src="${item.file_url}"></audio><div class="tag">@${escapeHtml(item.username)}</div></div>`;
    }else{
        const pts = (item.points || []).map(p=>`${p.x},${p.y}`).join(" ");
        html = `<div id="item-${item.id}" class="item draw-item${editable}" data-id="${item.id}" style="${style}">${toolbar}<svg viewBox="0 0 ${item.w} ${item.h}" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="${item.stroke}" stroke-width="${item.stroke_width}" stroke-linecap="round" stroke-linejoin="round"/></svg><div class="tag">@${escapeHtml(item.username)}</div></div>`;
    }
    return html;
}
function escapeHtml(s){
    return String(s||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;").replaceAll("'","&#039;");
}

function unloadFarItems(){
    const left = -camera.x - 2200;
    const top = -camera.y - 2200;
    const right = -camera.x + window.innerWidth + 2200;
    const bottom = -camera.y + window.innerHeight + 2200;
    for(const [id,item] of loaded.entries()){
        if(item.x + item.w < left || item.x > right || item.y + item.h < top || item.y > bottom){
            const el = document.getElementById("item-"+id);
            if(el) el.remove();
            loaded.delete(id);
        }
    }
}

let loadTimer = null;
function scheduleLoad(){
    clearTimeout(loadTimer);
    loadTimer = setTimeout(loadViewport, 120);
}
async function loadViewport(){
    if(loading) return;
    const left = Math.round(-camera.x);
    const top = Math.round(-camera.y);
    const w = window.innerWidth;
    const h = window.innerHeight;
    const key = `${Math.floor(left/700)}:${Math.floor(top/700)}:${Math.floor(w/500)}:${Math.floor(h/500)}`;
    if(key === lastLoadKey) return;
    lastLoadKey = key;
    loading = true;
    try{
        const res = await fetch(`/api/items?x=${left}&y=${top}&w=${w}&h=${h}`);
        const data = await res.json();
        if(data.ok){
            data.items.forEach(item=>{
                if(loaded.has(item.id)){
                    loaded.set(item.id, item);
                    const oldEl = document.getElementById("item-" + item.id);
                    if(oldEl){
                        oldEl.style.left = item.x + "px";
                        oldEl.style.top = item.y + "px";
                        oldEl.style.width = item.w + "px";
                        oldEl.style.height = item.h + "px";
                        oldEl.style.zIndex = item.z;
                    }
                    return;
                }

                loaded.set(item.id,item);
                world.insertAdjacentHTML("beforeend", itemHtml(item));
                const el = document.getElementById("item-"+item.id);
                if(item.type === "text"){
                    el.querySelector("[data-text-body]").textContent = item.text || "";
                }
            });
            bindDragging();
            unloadFarItems();
        }
    }catch(e){}
    loading=false;
}
window.addEventListener("resize", scheduleLoad);
setInterval(()=>{ lastLoadKey=""; loadViewport(); }, 15000);
loadViewport();

async function postJson(url, data){
    const res = await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data||{})});
    let out={};
    try{out=await res.json();}catch(e){}
    if(!res.ok || out.ok===false){ alert(out.error || "failed"); return null; }
    return out;
}
function getItem(id){ return loaded.get(id); }
function editText(id){
    const item=getItem(id); if(!item) return;
    const next=prompt("edit", item.text||""); if(next===null) return;
    postJson("/api/edit-text/"+id,{text:next}).then(out=>{
        if(!out)return;
        item.text=next;
        const el=document.getElementById("item-"+id);
        if(el) el.querySelector("[data-text-body]").textContent=next;
    });
}
function deleteItem(id){
    if(!confirm("delete?")) return;
    postJson("/api/delete-item/"+id,{}).then(out=>{
        if(!out)return;
        const el=document.getElementById("item-"+id); if(el) el.remove();
        loaded.delete(id);
    });
}
function resizeItem(id,delta){
    const item=getItem(id); if(!item)return;
    item.w=Math.max(50,Math.min(900,item.w+delta));
    item.h=Math.max(45,Math.min(700,item.h+delta));
    const el=document.getElementById("item-"+id);
    if(el){el.style.width=item.w+"px";el.style.height=item.h+"px";}
    savePos(item);
}
function layerItem(id,delta){
    const item=getItem(id); if(!item)return;
    item.z=Math.max(1,Math.min(9999,item.z+delta));
    const el=document.getElementById("item-"+id); if(el)el.style.zIndex=item.z;
    savePos(item);
}
let lastMoveSave=0;
function savePos(item){
    const n=Date.now();
    if(n-lastMoveSave<850)return;
    lastMoveSave=n;
    postJson("/api/move-item/"+item.id,{x:item.x,y:item.y,w:item.w,h:item.h,z:item.z});
}
function bindDragging(){
    document.querySelectorAll(".item.editable").forEach(el=>{
        if(el.dataset.bound==="1")return;
        el.dataset.bound="1";
        let drag=false, sx=0, sy=0, ox=0, oy=0;
        el.addEventListener("mousedown",e=>{
            if(e.button===2 || e.target.closest("button") || document.body.classList.contains("draw-mode"))return;
            drag=true; sx=e.clientX; sy=e.clientY;
            ox=parseInt(el.style.left||"0"); oy=parseInt(el.style.top||"0");
            e.preventDefault();
        });
        document.addEventListener("mousemove",e=>{
            if(!drag)return;
            const item=getItem(el.dataset.id); if(!item)return;
            item.x=ox+(e.clientX-sx);
            item.y=oy+(e.clientY-sy);
            el.style.left=item.x+"px"; el.style.top=item.y+"px";
        });
        document.addEventListener("mouseup",()=>{
            if(!drag)return;
            drag=false;
            const item=getItem(el.dataset.id); if(item) savePos(item);
        });
    });
}

// drawing
let drawMode=false, drawing=false, points=[];
let selectedDrawColor = "#111111";
let selectedDrawSize = 4;
const canvas=document.getElementById("drawCanvas");
const ctx=canvas ? canvas.getContext("2d") : null;

function resizeDrawCanvas(){
    if(!canvas) return;
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
}
resizeDrawCanvas();
window.addEventListener("resize", resizeDrawCanvas);

function toggleDrawPalette(){
    setActiveTool("draw");
    const p = document.getElementById("panel-draw");
    if(p) p.classList.toggle("show");
    drawMode = true;
    document.body.classList.add("draw-mode");
}
function selectDrawColor(color, el){
    selectedDrawColor = color;
    document.querySelectorAll(".color-dot").forEach(x=>x.classList.remove("active"));
    if(el) el.classList.add("active");
    if(draftInput){
        draftInput.style.color = selectedDrawColor;
    }
    drawMode = true;
    document.body.classList.add("draw-mode");
}
function drawPoint(e){
    const p=screenToWorld(e.clientX,e.clientY);
    return {x:e.clientX, y:e.clientY, realX:p.x, realY:p.y};
}
if(canvas && ctx){
    canvas.addEventListener("mousedown",e=>{
        if(!drawMode)return;
        drawing=true; points=[];
        const p=drawPoint(e);
        points.push({x:p.realX,y:p.realY});
        ctx.strokeStyle=selectedDrawColor || "#111";
        ctx.lineWidth=selectedDrawSize;
        ctx.lineCap="round"; ctx.lineJoin="round"; ctx.beginPath(); ctx.moveTo(p.x,p.y);
        e.preventDefault();
    });
    canvas.addEventListener("mousemove",e=>{
        if(!drawMode||!drawing)return;
        const p=drawPoint(e);
        const last=points[points.length-1];
        if(last && Math.abs(last.x-p.realX)+Math.abs(last.y-p.realY)<4)return;
        points.push({x:p.realX,y:p.realY});
        ctx.lineTo(p.x,p.y); ctx.stroke();
    });
    document.addEventListener("mouseup",()=>{
        if(!drawMode||!drawing)return;
        drawing=false;
        ctx.clearRect(0,0,canvas.width,canvas.height);
        if(points.length<2)return;
        postJson("/api/add-drawing",{points,stroke:selectedDrawColor,stroke_width:selectedDrawSize}).then(out=>{
            lastLoadKey=""; loadViewport();
        });
    });
}
</script>
</body>
</html>
"""


def html_page():
    user = current_user()
    user_obj = None
    if user:
        user_obj = {"id": user.get("id", ""), "username": user.get("username", ""), "email": user.get("email", "")}
    return render_template_string(HTML, user=user_obj)


# -----------------------------
# Routes
# -----------------------------

@app.route("/")
def home():
    return html_page()


@app.route("/api/items")
def api_items():
    user = current_user()
    x = clamp_int(request.args.get("x"), -10_000_000, 10_000_000, 0)
    y = clamp_int(request.args.get("y"), -10_000_000, 10_000_000, 0)
    w = clamp_int(request.args.get("w"), 100, 8000, 1600)
    h = clamp_int(request.args.get("h"), 100, 8000, 1000)
    try:
        db = load_store()["db"]
        items = visible_items(db, user, x, y, x+w, y+h)
        return jsonify({"ok": True, "items": items, "updated_at": db.get("updated_at", 0)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "items": []}), 500


@app.route("/register", methods=["POST"])
def register():
    if current_user():
        return redirect(url_for("home"))

    username = clean_username(request.form.get("username"))
    email = normalize_email(request.form.get("email"))
    password = request.form.get("password", "")

    if not valid_username(username):
        flash("bad name", "error")
        return redirect(url_for("home"))

    if not EMAIL_REGEX.fullmatch(email):
        flash("bad email", "error")
        return redirect(url_for("home"))

    if len(password) < 6:
        flash("password too short", "error")
        return redirect(url_for("home"))

    try:
        db = load_store(force=True)["db"]
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("home"))

    uid = user_id_from_email(email)

    for existing_id, existing in db["users"].items():
        if normalize_email(existing.get("email")) == email and existing_id != uid:
            flash("email exists", "error")
            return redirect(url_for("home"))
        if existing.get("username", "").lower() == username.lower() and existing_id != uid:
            flash("name exists", "error")
            return redirect(url_for("home"))

    if uid in db["users"]:
        flash("account exists", "error")
        return redirect(url_for("home"))

    db["users"][uid] = {
        "id": uid,
        "username": username,
        "email": email,
        "password_hash": generate_password_hash(password),
        "created": int(time.time()),
        "name_changed_at": int(time.time()),
        "cooldowns": {},
    }

    try:
        save_db(db)
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("home"))

    session["email"] = email
    session["user_id"] = uid
    return redirect(url_for("home"))


@app.route("/login", methods=["POST"])
def login():
    if current_user():
        return redirect(url_for("home"))

    email_or_user = clean_text(request.form.get("email_or_user"), 120)
    password = request.form.get("password", "")

    try:
        db = load_store(force=True)["db"]
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("home"))

    found = None
    for user in db["users"].values():
        if normalize_email(user.get("email")) == normalize_email(email_or_user) or user.get("username", "").lower() == email_or_user.lower():
            found = user
            break

    if not found or not check_password_hash(found.get("password_hash", ""), password):
        flash("wrong login", "error")
        return redirect(url_for("home"))

    session["email"] = found.get("email", "")
    session["user_id"] = found.get("id", "")
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/settings", methods=["POST"])
@login_required
def settings():
    user = current_user()
    new_name = clean_username(request.form.get("username"))

    if not valid_username(new_name):
        flash("bad name", "error")
        return redirect(url_for("home"))

    try:
        db = load_store(force=True)["db"]
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("home"))

    live_user = db["users"].get(user["id"])
    if not live_user:
        session.clear()
        return redirect(url_for("home"))

    if new_name.lower() == live_user.get("username", "").lower():
        return redirect(url_for("home"))

    last_changed = int(live_user.get("name_changed_at", live_user.get("created", 0)) or 0)
    remaining = NAME_CHANGE_COOLDOWN_SECONDS - (int(time.time()) - last_changed)

    if remaining > 0 and not is_staff_email(live_user.get("email", "")):
        flash(f"wait {seconds_text(remaining)}", "error")
        return redirect(url_for("home"))

    for existing_id, existing in db["users"].items():
        if existing_id != live_user["id"] and existing.get("username", "").lower() == new_name.lower():
            flash("taken", "error")
            return redirect(url_for("home"))

    live_user["username"] = new_name
    live_user["name_changed_at"] = int(time.time())
    db["users"][live_user["id"]] = live_user

    for item in db["items"].values():
        if item.get("user_id") == live_user["id"]:
            item["username"] = new_name
            item["updated"] = int(time.time())

    try:
        save_db(db)
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("home"))

    return redirect(url_for("home"))



@app.route("/api/add-text-box", methods=["POST"])
@login_required
def api_add_text_box():
    user = current_user()
    data = request.get_json(silent=True) or {}

    text = clean_text(data.get("text"), MAX_TEXT_CHARS)
    color = safe_hex_color(data.get("color"), "#111111")
    x = clamp_int(data.get("x"), -10_000_000, 10_000_000, 0)
    y = clamp_int(data.get("y"), -10_000_000, 10_000_000, 0)
    w = clamp_int(data.get("w"), 80, MAX_TEXT_W, 260)
    h = clamp_int(data.get("h"), 50, MAX_TEXT_H, 120)

    if not text:
        return jsonify({"ok": False, "error": "empty"}), 400

    try:
        db = load_store(force=True)["db"]
        ok, msg = item_limit_ok(db, user)
        if not ok:
            return jsonify({"ok": False, "error": msg}), 400
        ok, msg = rate_limit_user(db, user, "text", 8)
        if not ok:
            return jsonify({"ok": False, "error": msg}), 429
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    item_id = secrets.token_hex(10)
    db["items"][item_id] = {
        "id": item_id,
        "type": "text",
        "user_id": user["id"],
        "username": user["username"],
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "z": int(time.time()) % 9000 + 1,
        "text": text,
        "color": color,
        "bg": "transparent",
        "font": 58,
        "created": int(time.time()),
        "updated": int(time.time()),
    }

    try:
        save_db(db)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "id": item_id, "item": public_item(db["items"][item_id], user)})


@app.route("/add-text", methods=["POST"])
@login_required
def add_text():
    user = current_user()
    text = clean_text(request.form.get("text"), MAX_TEXT_CHARS)
    color = safe_hex_color(request.form.get("color"), "#111111")
    bg = safe_hex_color(request.form.get("bg"), "#ffffff")
    font = clamp_int(request.form.get("font"), 10, 36, 18)
    x = clamp_int(request.form.get("x"), -10_000_000, 10_000_000, 0)
    y = clamp_int(request.form.get("y"), -10_000_000, 10_000_000, 0)

    if not text:
        return redirect(url_for("home"))

    try:
        db = load_store(force=True)["db"]
        ok, msg = item_limit_ok(db, user)
        if not ok:
            flash(msg, "error")
            return redirect(url_for("home"))
        ok, msg = rate_limit_user(db, user, "text", 8)
        if not ok:
            flash(msg, "error")
            return redirect(url_for("home"))
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("home"))

    item_id = secrets.token_hex(10)

    db["items"][item_id] = {
        "id": item_id, "type": "text", "user_id": user["id"], "username": user["username"],
        "x": x, "y": y, "w": 260, "h": 150, "z": int(time.time()) % 9000 + 1,
        "text": text, "color": color, "bg": bg, "font": font,
        "created": int(time.time()), "updated": int(time.time()),
    }

    try:
        save_db(db)
    except Exception as e:
        flash(str(e), "error")

    return redirect(url_for("home"))


def add_uploaded_file(kind, api=False):
    user = current_user()
    field = "image" if kind == "image" else "audio"

    def fail(message, status=400):
        if api:
            return jsonify({"ok": False, "error": message}), status
        flash(message, "error")
        return redirect(url_for("home"))

    if field not in request.files:
        return fail("no file selected")

    uploaded = request.files[field]
    if not uploaded.filename:
        return fail("no file selected")

    original_name = secure_filename(uploaded.filename)

    if kind == "image" and not allowed_image(original_name):
        return fail("bad image type")

    if kind == "audio" and not allowed_audio(original_name):
        return fail("bad audio type")

    file_bytes = uploaded.read()

    if not file_bytes:
        return fail("empty file")

    if len(file_bytes) > MAX_FILE_SIZE:
        return fail("file too large")

    x = clamp_int(request.form.get("x"), -10_000_000, 10_000_000, 0)
    y = clamp_int(request.form.get("y"), -10_000_000, 10_000_000, 0)

    try:
        db = load_store(force=True)["db"]
        ok, msg = item_limit_ok(db, user)
        if not ok:
            return fail(msg)

        ok, msg = rate_limit_user(db, user, "upload", 8 if api else 25)
        if not ok:
            return fail(msg, 429)
    except Exception as e:
        return fail(str(e), 500)

    item_id = secrets.token_hex(10)

    try:
        msg = post_attachment(
            content=f"WBFILE|{kind}|{item_id}",
            filename=original_name,
            file_bytes=file_bytes,
            content_type=uploaded.content_type or guess_content_type(original_name),
        )
    except Exception as e:
        return fail(str(e), 500)

    attachments = msg.get("attachments", []) or []
    attachment = attachments[0] if attachments else {}

    w, h = (300, 220) if kind == "image" else (320, 80)

    db["items"][item_id] = {
        "id": item_id,
        "type": kind,
        "user_id": user["id"],
        "username": user["username"],
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "z": int(time.time()) % 9000 + 1,
        "filename": original_name,
        "size": len(file_bytes),
        "file_message_id": msg.get("id", ""),
        "file_url": attachment.get("url", ""),
        "file_proxy_url": attachment.get("proxy_url", ""),
        "created": int(time.time()),
        "updated": int(time.time()),
    }

    try:
        save_db(db)
    except Exception as e:
        return fail(str(e), 500)

    if api:
        return jsonify({
            "ok": True,
            "id": item_id,
            "item": public_item(db["items"][item_id], user),
        })

    return redirect(url_for("home"))


@app.route("/add-image", methods=["POST"])
@login_required
def add_image():
    return add_uploaded_file("image", api=False)


@app.route("/add-audio", methods=["POST"])
@login_required
def add_audio():
    return add_uploaded_file("audio", api=False)


@app.route("/api/add-image", methods=["POST"])
@login_required
def api_add_image():
    return add_uploaded_file("image", api=True)


@app.route("/api/add-audio", methods=["POST"])
@login_required
def api_add_audio():
    return add_uploaded_file("audio", api=True)


@app.route("/board-file/<item_id>")
def board_file(item_id):
    try:
        db = load_store()["db"]
        item = db["items"].get(item_id)
    except Exception:
        abort(404)

    if not item or item.get("type") not in {"image", "audio"}:
        abort(404)

    # Support both the new field names and older DB snapshots from previous versions.
    message_id = (
        item.get("file_message_id")
        or item.get("image_message_id")
        or item.get("audio_message_id")
        or ""
    )

    stored_url = (
        item.get("file_url")
        or item.get("image_url")
        or item.get("audio_url")
        or ""
    )

    stored_proxy_url = (
        item.get("file_proxy_url")
        or item.get("image_proxy_url")
        or item.get("audio_proxy_url")
        or ""
    )

    info = None
    if message_id:
        info = attachment_info_from_message(message_id)

    url = ""
    content_type = ""

    if info:
        url = info.get("url") or info.get("proxy_url") or ""
        content_type = info.get("content_type") or ""

    if not url:
        url = stored_url or stored_proxy_url

    if not content_type:
        content_type = guess_content_type(item.get("filename", ""))

    if not url:
        abort(404)

    # Proxy the file through this app instead of redirecting.
    # This fixes cases where Discord's attachment URL/proxy URL does not render directly
    # inside the whiteboard image/audio tag.
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()

        final_type = r.headers.get("Content-Type") or content_type or guess_content_type(item.get("filename", ""))

        return Response(
            r.content,
            mimetype=final_type,
            headers={
                "Cache-Control": "public, max-age=600",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except Exception:
        # Last fallback: try Discord proxy URL if the normal URL failed.
        fallback = ""
        if info:
            fallback = info.get("proxy_url") or ""

        if fallback and fallback != url:
            try:
                r = requests.get(fallback, timeout=60)
                r.raise_for_status()

                final_type = r.headers.get("Content-Type") or content_type or guess_content_type(item.get("filename", ""))

                return Response(
                    r.content,
                    mimetype=final_type,
                    headers={
                        "Cache-Control": "public, max-age=600",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            except Exception:
                pass

        abort(404)


def get_item_for_edit(item_id):
    store = load_store(force=True)
    db = store["db"]
    item = db["items"].get(item_id)

    if not item:
        return db, None, "not found"

    user = current_user()
    if not item_can_edit(item, user):
        return db, None, "not yours"

    return db, item, ""


@app.route("/api/move-item/<item_id>", methods=["POST"])
@login_required
def api_move_item(item_id):
    db, item, err = get_item_for_edit(item_id)
    if err:
        return jsonify({"ok": False, "error": err}), 403

    data = request.get_json(silent=True) or {}
    user = current_user()
    ok, msg = rate_limit_user(db, user, "move", 1)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 429

    item["x"] = clamp_int(data.get("x"), -10_000_000, 10_000_000, int(item.get("x", 0)))
    item["y"] = clamp_int(data.get("y"), -10_000_000, 10_000_000, int(item.get("y", 0)))
    item["w"] = clamp_int(data.get("w"), 50, 900, int(item.get("w", 220)))
    item["h"] = clamp_int(data.get("h"), 45, 700, int(item.get("h", 140)))
    item["z"] = clamp_int(data.get("z"), 1, 9999, int(item.get("z", 1)))
    item["updated"] = int(time.time())
    db["items"][item_id] = item

    try:
        save_db(db)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True})


@app.route("/api/edit-text/<item_id>", methods=["POST"])
@login_required
def api_edit_text(item_id):
    db, item, err = get_item_for_edit(item_id)
    if err:
        return jsonify({"ok": False, "error": err}), 403

    if item.get("type") != "text":
        return jsonify({"ok": False, "error": "not text"}), 400

    data = request.get_json(silent=True) or {}
    text = clean_text(data.get("text"), 800)

    if not text:
        return jsonify({"ok": False, "error": "empty"}), 400

    user = current_user()
    ok, msg = rate_limit_user(db, user, "edit", 5)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 429

    item["text"] = text
    item["updated"] = int(time.time())
    db["items"][item_id] = item

    try:
        save_db(db)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True})


@app.route("/api/delete-item/<item_id>", methods=["POST"])
@login_required
def api_delete_item(item_id):
    db, item, err = get_item_for_edit(item_id)
    if err:
        return jsonify({"ok": False, "error": err}), 403

    user = current_user()
    ok, msg = rate_limit_user(db, user, "delete", 3)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 429

    db["items"].pop(item_id, None)

    try:
        save_db(db)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True})


@app.route("/api/add-drawing", methods=["POST"])
@login_required
def api_add_drawing():
    user = current_user()
    data = request.get_json(silent=True) or {}
    points = data.get("points", [])

    if not isinstance(points, list) or len(points) < 2:
        return jsonify({"ok": False, "error": "empty"}), 400

    points = points[:MAX_DRAW_POINTS]
    clean_points = []
    for p in points:
        if not isinstance(p, dict):
            continue
        x = clamp_int(p.get("x"), -10_000_000, 10_000_000, 0)
        y = clamp_int(p.get("y"), -10_000_000, 10_000_000, 0)
        clean_points.append({"x": x, "y": y})

    if len(clean_points) < 2:
        return jsonify({"ok": False, "error": "empty"}), 400

    try:
        db = load_store(force=True)["db"]
        ok, msg = item_limit_ok(db, user)
        if not ok:
            return jsonify({"ok": False, "error": msg}), 400
        ok, msg = rate_limit_user(db, user, "draw", 12)
        if not ok:
            return jsonify({"ok": False, "error": msg}), 429
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    min_x = min(p["x"] for p in clean_points) - 20
    min_y = min(p["y"] for p in clean_points) - 20
    max_x = max(p["x"] for p in clean_points) + 20
    max_y = max(p["y"] for p in clean_points) + 20
    w = max(60, max_x - min_x)
    h = max(60, max_y - min_y)

    rel_points = [{"x": p["x"] - min_x, "y": p["y"] - min_y} for p in clean_points]
    item_id = secrets.token_hex(10)

    db["items"][item_id] = {
        "id": item_id, "type": "drawing", "user_id": user["id"], "username": user["username"],
        "x": min_x, "y": min_y, "w": w, "h": h, "z": int(time.time()) % 9000 + 1,
        "points": rel_points,
        "stroke": safe_hex_color(data.get("stroke"), "#111111"),
        "stroke_width": clamp_int(data.get("stroke_width"), 1, 30, 4),
        "created": int(time.time()), "updated": int(time.time()),
    }

    try:
        save_db(db)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True})



@app.route("/api/debug-item/<item_id>")
@login_required
def api_debug_item(item_id):
    if not current_is_staff():
        return jsonify({"ok": False, "error": "no"}), 403

    try:
        db = load_store(force=True)["db"]
        item = db.get("items", {}).get(item_id)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    if not item:
        return jsonify({"ok": False, "error": "not found"}), 404

    return jsonify({
        "ok": True,
        "item": {
            "id": item.get("id"),
            "type": item.get("type"),
            "filename": item.get("filename"),
            "file_message_id": item.get("file_message_id"),
            "image_message_id": item.get("image_message_id"),
            "file_url_exists": bool(item.get("file_url")),
            "image_url_exists": bool(item.get("image_url")),
            "x": item.get("x"),
            "y": item.get("y"),
            "w": item.get("w"),
            "h": item.get("h"),
        }
    })


@app.route("/cleanup")
@login_required
def cleanup_route():
    if not current_is_staff():
        return "no", 403
    result = cleanup_old_snapshots(keep=DB_SNAPSHOT_KEEP, delete_limit=DB_SNAPSHOT_DELETE_LIMIT)
    return f"deleted {result.get('deleted', 0)}"


@app.errorhandler(413)
def too_large(error):
    flash("too large", "error")
    return redirect(request.referrer or url_for("home"))


@app.errorhandler(404)
def not_found(error):
    return redirect(url_for("home"))


if __name__ == "__main__":
    print(APP_NAME)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
