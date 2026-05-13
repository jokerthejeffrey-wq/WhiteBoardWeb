# app.py
# Render Start Command:
# gunicorn app:app
#
# requirements.txt:
# Flask
# Werkzeug
# requests
# gunicorn
#
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
    render_template_string, jsonify, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename


APP_NAME = os.environ.get("APP_NAME", "Public Whiteboard")
DISCORD_API = "https://discord.com/api/v10"

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
DISCORD_DB_CHANNEL_ID = os.environ.get("DISCORD_DB_CHANNEL_ID", "").strip()

CREATOR_EMAIL = os.environ.get("CREATOR_EMAIL", "tuna.iren@outlook.com").strip().lower()
MOD_EMAILS = [x.strip().lower() for x in os.environ.get("MOD_EMAILS", "").split(",") if x.strip()]

BOARD_W = int(os.environ.get("BOARD_W", "1600"))
BOARD_H = int(os.environ.get("BOARD_H", "1000"))

MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", str(10 * 1024 * 1024)))
MAX_DB_SIZE = int(os.environ.get("MAX_DB_SIZE", str(7 * 1024 * 1024)))
MAX_TOTAL_ITEMS = int(os.environ.get("MAX_TOTAL_ITEMS", "220"))
MAX_ITEMS_PER_USER = int(os.environ.get("MAX_ITEMS_PER_USER", "70"))
MAX_DRAW_POINTS = int(os.environ.get("MAX_DRAW_POINTS", "260"))

CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "45"))
FAST_BOOT_MESSAGE_PAGES = int(os.environ.get("FAST_BOOT_MESSAGE_PAGES", "4"))
DB_SNAPSHOT_KEEP = max(1, int(os.environ.get("DB_SNAPSHOT_KEEP", "2")))
DB_SNAPSHOT_DELETE_LIMIT = max(1, int(os.environ.get("DB_SNAPSHOT_DELETE_LIMIT", "20")))
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
            return jsonify({"ok": False, "error": "Login first."}), 401 if request.path.startswith("/api/") else redirect(url_for("home"))
        return fn(*args, **kwargs)
    return wrapper


def allowed_image(filename):
    return Path((filename or "").lower()).suffix in ALLOWED_IMAGE_EXTENSIONS


def allowed_audio(filename):
    return Path((filename or "").lower()).suffix in ALLOWED_AUDIO_EXTENSIONS


def blank_db():
    return {
        "version": 3,
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

    clean["version"] = 3
    clean.setdefault("created_at", now_ms())
    clean.setdefault("updated_at", now_ms())
    return clean


def item_limit_ok(db, user):
    total = len(db.get("items", {}))
    if total >= MAX_TOTAL_ITEMS:
        return False, "Board is full. Delete old items first."

    mine = len([x for x in db.get("items", {}).values() if x.get("user_id") == user.get("id")])
    if mine >= MAX_ITEMS_PER_USER:
        return False, "You reached your item limit. Delete old items first."

    return True, ""


def rate_limit_user(db, user, key, seconds):
    users = db.get("users", {})
    live = users.get(user.get("id"), user)
    cooldowns = live.setdefault("cooldowns", {})
    last = int(cooldowns.get(key, 0) or 0)
    remaining = seconds - (int(time.time()) - last)

    if remaining > 0 and not is_staff_email(live.get("email", "")):
        return False, f"Slow down. Try again in {remaining} seconds."

    cooldowns[key] = int(time.time())
    users[live["id"]] = live
    db["users"] = users
    return True, ""


# -----------------------------
# Storage
# -----------------------------

def require_storage_config():
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN in Render environment.")
    if not DISCORD_DB_CHANNEL_ID:
        raise RuntimeError("Missing DISCORD_DB_CHANNEL_ID in Render environment.")


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
            raise RuntimeError(f"Storage API error {r.status_code}: {r.text[:500]}")

        return r

    raise RuntimeError("Storage API rate-limit retry failed.")


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
    if keep is None:
        keep = DB_SNAPSHOT_KEEP
    if delete_limit is None:
        delete_limit = DB_SNAPSHOT_DELETE_LIMIT

    try:
        messages = fetch_messages(max_pages=20, stop_after_snapshot=False)
    except Exception:
        return {"ok": False, "deleted": 0, "kept": 0}

    snapshots = [m for m in messages if (m.get("content", "") or "").startswith("WBDBSNAP|")]
    snapshots.sort(key=lambda m: int(m.get("id", "0")), reverse=True)

    to_keep = snapshots[:keep]
    to_delete = snapshots[keep:keep + delete_limit]

    deleted = 0
    for msg in to_delete:
        if delete_message(msg.get("id", "")):
            deleted += 1
            time.sleep(0.25)

    if deleted:
        clear_cache()

    return {"ok": True, "deleted": deleted, "kept": len(to_keep), "total": len(snapshots)}


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
        raise ValueError("Board data is too large. Delete old items first.")

    post_attachment(
        content=f"WBDBSNAP|v3|gz|{int(time.time())}",
        filename="whiteboard-db.json.gz",
        file_bytes=raw,
        content_type="application/gzip",
    )

    if AUTO_DELETE_OLD_SNAPSHOTS:
        cleanup_old_snapshots()

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
        "x": int(item.get("x", 120)),
        "y": int(item.get("y", 120)),
        "w": int(item.get("w", 220)),
        "h": int(item.get("h", 140)),
        "z": int(item.get("z", 1)),
        "text": item.get("text", ""),
        "color": item.get("color", "#111111"),
        "bg": item.get("bg", "#fff8cc"),
        "font": int(item.get("font", 18)),
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


def all_public_items(db, user=None):
    items = [public_item(x, user) for x in db["items"].values()]
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
:root{
    --glass:rgba(255,255,255,.26);
    --glass2:rgba(255,255,255,.16);
    --line:rgba(255,255,255,.38);
    --dark:rgba(20,24,32,.82);
    --muted:rgba(20,24,32,.55);
    --blue:#1976ff;
    --red:#ff4d5d;
    --shadow:0 22px 80px rgba(31,42,68,.16);
}
html,body{margin:0;height:100%;overflow:hidden}
body{
    font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",Arial,sans-serif;
    color:var(--dark);
    background:
        radial-gradient(circle at 16% 12%, rgba(173,210,255,.72), transparent 33%),
        radial-gradient(circle at 82% 10%, rgba(255,220,245,.64), transparent 31%),
        radial-gradient(circle at 56% 88%, rgba(199,255,230,.58), transparent 35%),
        linear-gradient(135deg,#f7f9ff,#e9eef8 44%,#f8f1ff);
}
body:before{
    content:"";position:fixed;inset:0;pointer-events:none;
    background:linear-gradient(rgba(255,255,255,.42) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.42) 1px,transparent 1px);
    background-size:34px 34px;
    mask-image:linear-gradient(to bottom,rgba(0,0,0,.55),rgba(0,0,0,.18));
}
button,input,textarea,select{font:inherit}
button,.btn{
    border:1px solid var(--line);
    background:rgba(255,255,255,.24);
    color:var(--dark);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.55),0 10px 24px rgba(31,42,68,.10);
    backdrop-filter:blur(22px) saturate(150%);
    border-radius:3px;
    padding:10px 13px;
    font-weight:800;
    cursor:pointer;
    text-decoration:none;
    display:inline-flex;
    justify-content:center;
    align-items:center;
}
button:hover,.btn:hover{background:rgba(255,255,255,.36)}
.primary{background:rgba(25,118,255,.88)!important;color:white!important;border-color:rgba(25,118,255,.35)!important}
.danger{background:rgba(255,77,93,.14)!important;color:#b02034!important;border-color:rgba(255,77,93,.24)!important}
.topbar{
    position:fixed;z-index:100;left:18px;right:18px;top:14px;height:54px;
    display:flex;align-items:center;justify-content:space-between;gap:12px;
    padding:10px 14px;border:1px solid var(--line);border-radius:4px;
    background:var(--glass);backdrop-filter:blur(34px) saturate(210%);
    box-shadow:var(--shadow);
}
.logo{display:flex;align-items:center;gap:10px;font-weight:900;letter-spacing:-.75px;font-size:20px}
.logo-mark{
    width:32px;height:32px;border-radius:3px;
    background:linear-gradient(135deg,rgba(255,255,255,.95),rgba(180,216,255,.92) 42%,rgba(25,118,255,.9));
    box-shadow:inset 0 1px 0 rgba(255,255,255,.75),0 8px 18px rgba(25,118,255,.22);
}
.nav{display:flex;align-items:center;gap:8px}
.user-pill{
    font-size:13px;padding:8px 11px;border-radius:3px;background:rgba(255,255,255,.42);
    border:1px solid var(--line);font-weight:800;backdrop-filter:blur(18px);
}
.alert,.success{
    position:fixed;z-index:101;top:78px;left:50%;transform:translateX(-50%);
    padding:10px 14px;border-radius:3px;font-weight:800;font-size:14px;border:1px solid;
    backdrop-filter:blur(24px);box-shadow:var(--shadow);
}
.alert{background:rgba(255,239,239,.78);border-color:rgba(255,77,93,.28);color:#982033}
.success{background:rgba(235,255,241,.78);border-color:rgba(38,170,84,.24);color:#13622e}
.workspace{position:fixed;inset:0;padding-top:0}
.board-wrap{position:absolute;inset:0;overflow:auto;padding:92px 28px 32px 116px}
.board{
    position:relative;width:{{ board_w }}px;height:{{ board_h }}px;
    background:
        linear-gradient(rgba(40,54,80,.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(40,54,80,.035) 1px, transparent 1px),
        rgba(255,255,255,.74);
    background-size:30px 30px,30px 30px,100% 100%;
    border:1px solid rgba(255,255,255,.34);
    border-radius:4px;
    box-shadow:0 30px 90px rgba(30,42,68,.16),inset 0 1px 0 rgba(255,255,255,.55),inset 0 0 0 1px rgba(255,255,255,.18);
    overflow:hidden;
    backdrop-filter:blur(18px);
}
.tool-rail{
    position:fixed;z-index:90;left:18px;top:92px;width:72px;
    padding:10px;border-radius:4px;border:1px solid var(--line);
    background:rgba(255,255,255,.18);backdrop-filter:blur(36px) saturate(220%);
    box-shadow:var(--shadow);
    display:flex;flex-direction:column;gap:9px;
}
.tool-btn{
    width:50px;height:50px;padding:0;border-radius:3px;font-size:18px;font-weight:900;
}
.tool-btn.active{background:rgba(25,118,255,.9)!important;color:white!important}
.tool-panel{
    position:fixed;z-index:89;left:102px;top:92px;width:286px;max-height:calc(100vh - 116px);overflow:auto;
    border-radius:4px;border:1px solid var(--line);
    background:rgba(255,255,255,.18);backdrop-filter:blur(38px) saturate(220%);
    box-shadow:var(--shadow);
    padding:14px;
    display:none;
}
.tool-panel.show{display:block}
.tool-title{font-size:14px;font-weight:900;margin:2px 0 12px;color:var(--dark)}
.tool-line{height:1px;background:rgba(255,255,255,.26);margin:12px -2px}
label{display:block;font-size:12px;font-weight:900;color:var(--muted);margin:10px 0 6px}
input,textarea,select{
    width:100%;border:1px solid rgba(255,255,255,.58);border-radius:3px;background:rgba(255,255,255,.26);
    outline:none;padding:10px 11px;color:var(--dark);backdrop-filter:blur(18px);
}
textarea{min-height:92px;resize:vertical}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.small{font-size:12px;color:var(--muted);line-height:1.42}
.board-item{
    position:absolute;border-radius:4px;box-shadow:0 14px 34px rgba(31,42,68,.20);
    user-select:none;touch-action:none;
}
.board-item.can-edit{cursor:move}
.text-item{
    border:1px solid rgba(255,255,255,.7);
    padding:14px 15px;
    white-space:pre-wrap;
    line-height:1.25;
    overflow:hidden;
    backdrop-filter:blur(14px);
}
.image-item,.audio-item,.drawing-item{
    border:1px solid rgba(255,255,255,.72);
    background:rgba(255,255,255,.22);
    padding:7px;
    overflow:hidden;
    backdrop-filter:blur(14px);
}
.image-item img{width:100%;height:100%;object-fit:contain;display:block;border-radius:3px;pointer-events:none}
.audio-card{
    width:100%;height:100%;display:flex;flex-direction:column;justify-content:center;gap:8px;
    padding:11px;background:rgba(255,255,255,.18);border-radius:3px;
}
.audio-card audio{width:100%}
.drawing-item svg{width:100%;height:100%;display:block;overflow:visible}
.toolbar{
    position:absolute;left:0;top:-40px;display:none;gap:5px;white-space:nowrap;z-index:9999;
}
.board-item.can-edit:hover .toolbar{display:flex}
.toolbar button{
    font-size:12px;padding:7px 9px;border-radius:3px;background:rgba(255,255,255,.30)!important;color:var(--dark)!important;
}
.user-tag{
    position:absolute;right:7px;bottom:6px;font-size:11px;padding:4px 7px;border-radius:3px;
    background:rgba(255,255,255,.26);color:rgba(20,24,32,.55);font-weight:800;
}
#drawCanvas{position:absolute;left:0;top:0;width:{{ board_w }}px;height:{{ board_h }}px;z-index:9998;display:none;cursor:crosshair}
body.draw-mode #drawCanvas{display:block}
.blur-lock{
    position:fixed;inset:0;z-index:99999;background:rgba(242,246,255,.28);backdrop-filter:blur(30px) saturate(210%);
    display:flex;align-items:center;justify-content:center;padding:18px;
}
.login-box{
    width:min(440px,94vw);border-radius:4px;border:1px solid var(--line);
    background:rgba(255,255,255,.20);backdrop-filter:blur(42px) saturate(230%);
    box-shadow:0 30px 100px rgba(31,42,68,.28);overflow:hidden;
}
.login-head{padding:20px 20px 10px;font-weight:950;font-size:24px;letter-spacing:-1px}
.tabs{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:14px 18px 4px}
.login-content{padding:0 18px 18px}
.hidden{display:none!important}
@media(max-width:850px){
    html,body{overflow:auto}
    .topbar{left:10px;right:10px;top:10px}
    .board-wrap{position:relative;height:72vh;padding:92px 16px 18px 96px}
    .tool-rail{left:12px;top:82px}
    .tool-panel{left:92px;right:12px;width:auto;top:82px}
}
</style>
</head>
<body>
<div class="topbar">
    <div class="logo"><span class="logo-mark"></span>{{ app_name }}</div>
    <div class="nav">
        {% if user %}
            <span class="user-pill">@{{ user.username }}</span>
            <button onclick="openSettings()">Settings</button>
            <a class="btn" href="{{ url_for('logout') }}">Logout</a>
        {% else %}
            <span class="user-pill">guest</span>
        {% endif %}
    </div>
</div>

{% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        {% for category,message in messages %}
            <div class="{{ 'success' if category == 'success' else 'alert' }}">{{ message }}</div>
        {% endfor %}
    {% endif %}
{% endwith %}

<div class="workspace">
    {% if user %}
    <div class="tool-rail">
        <button class="tool-btn active" data-tool="text" onclick="showTool('text')">T</button>
        <button class="tool-btn" data-tool="image" onclick="showTool('image')">◎</button>
        <button class="tool-btn" data-tool="audio" onclick="showTool('audio')">♫</button>
        <button class="tool-btn" data-tool="draw" onclick="showTool('draw')">✎</button>
        {% if staff %}
        <a class="tool-btn btn" href="{{ url_for('cleanup_route') }}" title="clean">⌫</a>
        {% endif %}
    </div>

    <section id="tool-text" class="tool-panel show">
        <div class="tool-title">Text</div>
        <form action="{{ url_for('add_text') }}" method="POST">
            <label>Text</label>
            <textarea name="text" placeholder="write something" required></textarea>
            <div class="form-row">
                <div><label>Text</label><input name="color" type="color" value="#111111"></div>
                <div><label>Box</label><input name="bg" type="color" value="#fff8cc"></div>
            </div>
            <label>Size</label>
            <input name="font" type="number" value="18" min="10" max="60">
            <div class="tool-line"></div>
            <button class="primary" type="submit">Add</button>
        </form>
    </section>

    <section id="tool-image" class="tool-panel">
        <div class="tool-title">Image</div>
        <form action="{{ url_for('add_image') }}" method="POST" enctype="multipart/form-data">
            <input name="image" type="file" accept=".png,.jpg,.jpeg,.gif,.webp" required>
            <div class="tool-line"></div>
            <button class="primary" type="submit">Upload</button>
        </form>
    </section>

    <section id="tool-audio" class="tool-panel">
        <div class="tool-title">Audio</div>
        <form action="{{ url_for('add_audio') }}" method="POST" enctype="multipart/form-data">
            <input name="audio" type="file" accept=".mp3,.wav,.ogg,.m4a" required>
            <div class="tool-line"></div>
            <button class="primary" type="submit">Upload</button>
        </form>
    </section>

    <section id="tool-draw" class="tool-panel">
        <div class="tool-title">Draw</div>
        <div class="form-row">
            <div><label>Color</label><input id="drawColor" type="color" value="#111111"></div>
            <div><label>Size</label><input id="drawSize" type="number" value="4" min="1" max="30"></div>
        </div>
        <div class="tool-line"></div>
        <button id="drawToggle" class="primary" onclick="toggleDraw()">Start</button>
        <p class="small">One stroke saves when you release the mouse.</p>
    </section>
    {% endif %}

    <main class="board-wrap">
        <div id="board" class="board">
            {{ board_items|safe }}
            <canvas id="drawCanvas" width="{{ board_w }}" height="{{ board_h }}"></canvas>
        </div>
    </main>
</div>

{% if not user %}
<div class="blur-lock">
    <div class="login-box">
        <div class="login-head">your name..</div>
        <div class="tabs">
            <button class="primary" onclick="showAuth('register')">Create</button>
            <button onclick="showAuth('login')">Login</button>
        </div>
        <div class="login-content">
            <form id="registerBox" action="{{ url_for('register') }}" method="POST">
                <label>Name</label>
                <input name="username" placeholder="your name" required>
                <label>Email</label>
                <input name="email" type="email" placeholder="email" required>
                <label>Password</label>
                <input name="password" type="password" placeholder="password" required>
                <div class="tool-line"></div>
                <button class="primary" type="submit">Create Account</button>
            </form>

            <form id="loginBox" class="hidden" action="{{ url_for('login') }}" method="POST">
                <label>Email or name</label>
                <input name="email_or_user" placeholder="email or name" required>
                <label>Password</label>
                <input name="password" type="password" placeholder="password" required>
                <div class="tool-line"></div>
                <button class="primary" type="submit">Login</button>
            </form>
        </div>
    </div>
</div>
{% endif %}

<div id="settingsModal" class="blur-lock hidden">
    <div class="login-box">
        <div class="login-head">Settings</div>
        <div class="login-content" style="padding-top:10px">
            <form action="{{ url_for('settings') }}" method="POST">
                <label>Name</label>
                <input name="username" value="{{ user.username if user else '' }}" required>
                <p class="small">You can change your name once every 10 days.</p>
                <div class="tool-line"></div>
                <button class="primary" type="submit">Save</button>
                <button type="button" onclick="closeSettings()">Cancel</button>
            </form>
        </div>
    </div>
</div>

<script>
const CURRENT_USER = {{ current_user_id|tojson }};
const BOARD_W = {{ board_w|tojson }};
const BOARD_H = {{ board_h|tojson }};
let ITEMS = {{ items|tojson }};

function q(id){ return document.getElementById(id); }

function showTool(name){
    document.querySelectorAll(".tool-panel").forEach(x=>x.classList.remove("show"));
    document.querySelectorAll(".tool-btn").forEach(x=>x.classList.remove("active"));
    const panel = q("tool-" + name);
    const btn = document.querySelector(`[data-tool="${name}"]`);
    if(panel) panel.classList.add("show");
    if(btn) btn.classList.add("active");
}

function showAuth(which){
    q("registerBox").classList.toggle("hidden", which !== "register");
    q("loginBox").classList.toggle("hidden", which !== "login");
}
function openSettings(){ q("settingsModal").classList.remove("hidden"); }
function closeSettings(){ q("settingsModal").classList.add("hidden"); }

async function postJson(url, data){
    const res = await fetch(url, {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify(data || {})
    });
    let out = {};
    try{ out = await res.json(); }catch(e){}
    if(!res.ok || out.ok === false){
        alert(out.error || "Action failed");
        return null;
    }
    return out;
}

function getItem(id){ return ITEMS.find(x => x.id === id); }

function editText(id){
    const item = getItem(id);
    if(!item) return;
    const next = prompt("Edit text:", item.text || "");
    if(next === null) return;
    postJson("/api/edit-text/" + encodeURIComponent(id), {text: next}).then(out=>{
        if(!out) return;
        item.text = next;
        const el = q("item-" + id);
        const body = el.querySelector("[data-text-body]");
        if(body) body.textContent = next;
    });
}

function deleteItem(id){
    if(!confirm("Delete this board item?")) return;
    postJson("/api/delete-item/" + encodeURIComponent(id), {}).then(out=>{
        if(!out) return;
        const el = q("item-" + id);
        if(el) el.remove();
        ITEMS = ITEMS.filter(x => x.id !== id);
    });
}

function resizeItem(id, delta){
    const item = getItem(id);
    if(!item) return;
    item.w = Math.max(70, Math.min(800, item.w + delta));
    item.h = Math.max(55, Math.min(600, item.h + delta));
    const el = q("item-" + id);
    if(el){
        el.style.width = item.w + "px";
        el.style.height = item.h + "px";
    }
    saveItemPosition(item);
}

function layerItem(id, delta){
    const item = getItem(id);
    if(!item) return;
    item.z = Math.max(1, Math.min(9999, item.z + delta));
    const el = q("item-" + id);
    if(el) el.style.zIndex = item.z;
    saveItemPosition(item);
}

let lastMoveSave = 0;
function saveItemPosition(item){
    const now = Date.now();
    if(now - lastMoveSave < 850) return;
    lastMoveSave = now;
    postJson("/api/move-item/" + encodeURIComponent(item.id), {
        x:item.x, y:item.y, w:item.w, h:item.h, z:item.z
    });
}

function bindDragging(){
    document.querySelectorAll(".board-item.can-edit").forEach(el=>{
        if(el.dataset.bound === "1") return;
        el.dataset.bound = "1";
        let dragging = false;
        let startX = 0, startY = 0, startLeft = 0, startTop = 0;

        el.addEventListener("mousedown", e=>{
            if(e.target.closest("button") || document.body.classList.contains("draw-mode")) return;
            dragging = true;
            startX = e.clientX;
            startY = e.clientY;
            startLeft = parseInt(el.style.left || "0");
            startTop = parseInt(el.style.top || "0");
            el.style.opacity = ".82";
            document.body.style.userSelect = "none";
            e.preventDefault();
        });

        document.addEventListener("mousemove", e=>{
            if(!dragging) return;
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            const id = el.dataset.id;
            const item = getItem(id);
            if(!item) return;
            const nx = Math.max(0, Math.min(BOARD_W - item.w, startLeft + dx));
            const ny = Math.max(0, Math.min(BOARD_H - item.h, startTop + dy));
            item.x = Math.round(nx);
            item.y = Math.round(ny);
            el.style.left = item.x + "px";
            el.style.top = item.y + "px";
        });

        document.addEventListener("mouseup", ()=>{
            if(!dragging) return;
            dragging = false;
            el.style.opacity = "1";
            document.body.style.userSelect = "";
            const item = getItem(el.dataset.id);
            if(item) saveItemPosition(item);
        });
    });
}

// drawing
let drawMode = false;
let drawing = false;
let points = [];
const canvas = q("drawCanvas");
const ctx = canvas ? canvas.getContext("2d") : null;

function toggleDraw(){
    drawMode = !drawMode;
    document.body.classList.toggle("draw-mode", drawMode);
    const btn = q("drawToggle");
    if(btn) btn.textContent = drawMode ? "Stop" : "Start";
}

function boardPoint(e){
    const rect = q("board").getBoundingClientRect();
    return {
        x: Math.round(e.clientX - rect.left),
        y: Math.round(e.clientY - rect.top)
    };
}

if(canvas && ctx){
    canvas.addEventListener("mousedown", e=>{
        if(!drawMode) return;
        drawing = true;
        points = [];
        const p = boardPoint(e);
        points.push(p);
        ctx.strokeStyle = q("drawColor").value || "#111111";
        ctx.lineWidth = Math.max(1, Math.min(30, parseInt(q("drawSize").value || "4")));
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        e.preventDefault();
    });

    canvas.addEventListener("mousemove", e=>{
        if(!drawMode || !drawing) return;
        const p = boardPoint(e);
        const last = points[points.length - 1];
        if(last && Math.abs(last.x - p.x) + Math.abs(last.y - p.y) < 4) return;
        points.push(p);
        ctx.lineTo(p.x, p.y);
        ctx.stroke();
    });

    document.addEventListener("mouseup", ()=>{
        if(!drawMode || !drawing) return;
        drawing = false;
        if(points.length < 2) return;
        const color = q("drawColor").value || "#111111";
        const size = parseInt(q("drawSize").value || "4");
        postJson("/api/add-drawing", {points, stroke: color, stroke_width: size}).then(out=>{
            ctx.clearRect(0,0,canvas.width,canvas.height);
            if(out && out.reload) location.reload();
        });
    });
}

document.addEventListener("DOMContentLoaded", bindDragging);
bindDragging();
</script>
</body>
</html>
"""


def render_board_item(item):
    can_edit = bool(item.get("can_edit"))
    classes = "board-item "
    if item["type"] == "text":
        classes += "text-item "
    elif item["type"] == "image":
        classes += "image-item "
    elif item["type"] == "audio":
        classes += "audio-item "
    else:
        classes += "drawing-item "
    if can_edit:
        classes += "can-edit"

    style = f"left:{item['x']}px;top:{item['y']}px;width:{item['w']}px;height:{item['h']}px;z-index:{item['z']};"

    if item["type"] == "text":
        style += f"background:{esc(item['bg'])};color:{esc(item['color'])};font-size:{item['font']}px;"
        inner = f"<div data-text-body>{esc(item['text'])}</div>"
    elif item["type"] == "image":
        inner = f"<img src='{esc(item['file_url'])}' alt='{esc(item['filename'])}'>"
    elif item["type"] == "audio":
        inner = f"""
        <div class="audio-card">
            <b>{esc(item['filename'])}</b>
            <audio controls preload="none" src="{esc(item['file_url'])}"></audio>
            <span class="small">{file_size_text(item['size'])}</span>
        </div>
        """
    else:
        pts = item.get("points", [])
        point_str = " ".join(f"{int(p.get('x', 0))},{int(p.get('y', 0))}" for p in pts if isinstance(p, dict))
        inner = f"""
        <svg viewBox="0 0 {item['w']} {item['h']}" preserveAspectRatio="none">
            <polyline points="{esc(point_str)}" fill="none" stroke="{esc(item.get('stroke', '#111111'))}" stroke-width="{int(item.get('stroke_width', 4))}" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        """

    toolbar = ""
    if can_edit:
        edit_btn = ""
        if item["type"] == "text":
            edit_btn = f"<button onclick=\"editText('{esc(item['id'])}')\">edit</button>"
        toolbar = f"""
        <div class="toolbar">
            {edit_btn}
            <button onclick="resizeItem('{esc(item['id'])}', 25)">+</button>
            <button onclick="resizeItem('{esc(item['id'])}', -25)">-</button>
            <button onclick="layerItem('{esc(item['id'])}', 1)">front</button>
            <button onclick="layerItem('{esc(item['id'])}', -1)">back</button>
            <button class="danger" onclick="deleteItem('{esc(item['id'])}')">delete</button>
        </div>
        """

    return f"""
    <div id="item-{esc(item['id'])}" class="{classes}" data-id="{esc(item['id'])}" style="{style}">
        {toolbar}
        {inner}
        <div class="user-tag">@{esc(item['username'])}</div>
    </div>
    """


def html_page(items=None):
    user = current_user()
    user_obj = None
    if user:
        user_obj = {"id": user.get("id", ""), "username": user.get("username", ""), "email": user.get("email", "")}

    board_items = "".join(render_board_item(x) for x in (items or []))

    return render_template_string(
        HTML,
        app_name=APP_NAME,
        user=user_obj,
        staff=current_is_staff(),
        current_user_id=user.get("id", "") if user else "",
        board_w=BOARD_W,
        board_h=BOARD_H,
        items=items or [],
        board_items=board_items,
    )


# -----------------------------
# Routes
# -----------------------------

@app.route("/")
def home():
    try:
        db = load_store()["db"]
        items = all_public_items(db, current_user())
    except Exception:
        items = []
    return html_page(items)


@app.route("/register", methods=["POST"])
def register():
    if current_user():
        return redirect(url_for("home"))

    username = clean_username(request.form.get("username"))
    email = normalize_email(request.form.get("email"))
    password = request.form.get("password", "")

    if not valid_username(username):
        flash("Name must be 3-24 characters and use letters, numbers, dot, dash, or underscore.", "error")
        return redirect(url_for("home"))

    if not EMAIL_REGEX.fullmatch(email):
        flash("Use a real email format.", "error")
        return redirect(url_for("home"))

    if len(password) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("home"))

    try:
        db = load_store(force=True)["db"]
    except Exception as e:
        flash(f"Storage error: {e}", "error")
        return redirect(url_for("home"))

    uid = user_id_from_email(email)

    for existing_id, existing in db["users"].items():
        if normalize_email(existing.get("email")) == email and existing_id != uid:
            flash("Email already exists. Login instead.", "error")
            return redirect(url_for("home"))
        if existing.get("username", "").lower() == username.lower() and existing_id != uid:
            flash("Name already exists.", "error")
            return redirect(url_for("home"))

    if uid in db["users"]:
        flash("Account already exists. Login instead.", "error")
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
        flash(f"Could not save account: {e}", "error")
        return redirect(url_for("home"))

    session["email"] = email
    session["user_id"] = uid
    flash("Account created.", "success")
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
        flash(f"Storage error: {e}", "error")
        return redirect(url_for("home"))

    found = None
    for user in db["users"].values():
        if normalize_email(user.get("email")) == normalize_email(email_or_user) or user.get("username", "").lower() == email_or_user.lower():
            found = user
            break

    if not found or not check_password_hash(found.get("password_hash", ""), password):
        flash("Wrong login.", "error")
        return redirect(url_for("home"))

    session["email"] = found.get("email", "")
    session["user_id"] = found.get("id", "")
    flash("Logged in.", "success")
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("home"))


@app.route("/settings", methods=["POST"])
@login_required
def settings():
    user = current_user()
    new_name = clean_username(request.form.get("username"))

    if not valid_username(new_name):
        flash("Name must be 3-24 characters and use letters, numbers, dot, dash, or underscore.", "error")
        return redirect(url_for("home"))

    try:
        db = load_store(force=True)["db"]
    except Exception as e:
        flash(f"Storage error: {e}", "error")
        return redirect(url_for("home"))

    live_user = db["users"].get(user["id"])
    if not live_user:
        session.clear()
        flash("Account not found. Login again.", "error")
        return redirect(url_for("home"))

    if new_name.lower() == live_user.get("username", "").lower():
        flash("That is already your name.", "success")
        return redirect(url_for("home"))

    last_changed = int(live_user.get("name_changed_at", live_user.get("created", 0)) or 0)
    remaining = NAME_CHANGE_COOLDOWN_SECONDS - (int(time.time()) - last_changed)

    if remaining > 0 and not is_staff_email(live_user.get("email", "")):
        flash(f"You can change your name again in {seconds_text(remaining)}.", "error")
        return redirect(url_for("home"))

    for existing_id, existing in db["users"].items():
        if existing_id != live_user["id"] and existing.get("username", "").lower() == new_name.lower():
            flash("That name is already taken.", "error")
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
        flash(f"Could not save name: {e}", "error")
        return redirect(url_for("home"))

    flash("Name changed. You cannot change it again for 10 days.", "success")
    return redirect(url_for("home"))


@app.route("/add-text", methods=["POST"])
@login_required
def add_text():
    user = current_user()
    text = clean_text(request.form.get("text"), 800)
    color = safe_hex_color(request.form.get("color"), "#111111")
    bg = safe_hex_color(request.form.get("bg"), "#fff8cc")
    font = clamp_int(request.form.get("font"), 10, 60, 18)

    if not text:
        flash("Text cannot be empty.", "error")
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
        flash(f"Storage error: {e}", "error")
        return redirect(url_for("home"))

    item_id = secrets.token_hex(10)
    x = secrets.randbelow(max(1, BOARD_W - 300))
    y = secrets.randbelow(max(1, BOARD_H - 180))

    db["items"][item_id] = {
        "id": item_id, "type": "text", "user_id": user["id"], "username": user["username"],
        "x": x, "y": y, "w": 260, "h": 150, "z": int(time.time()) % 9000 + 1,
        "text": text, "color": color, "bg": bg, "font": font,
        "created": int(time.time()), "updated": int(time.time()),
    }

    try:
        save_db(db)
    except Exception as e:
        flash(f"Could not save text: {e}", "error")
        return redirect(url_for("home"))

    flash("Text added.", "success")
    return redirect(url_for("home"))


def add_uploaded_file(kind):
    user = current_user()
    field = "image" if kind == "image" else "audio"

    if field not in request.files:
        flash("No file selected.", "error")
        return redirect(url_for("home"))

    uploaded = request.files[field]
    if not uploaded.filename:
        flash("No file selected.", "error")
        return redirect(url_for("home"))

    original_name = secure_filename(uploaded.filename)
    if kind == "image" and not allowed_image(original_name):
        flash("Only PNG, JPG, JPEG, GIF, and WEBP images are allowed.", "error")
        return redirect(url_for("home"))
    if kind == "audio" and not allowed_audio(original_name):
        flash("Only MP3, WAV, OGG, and M4A audio files are allowed.", "error")
        return redirect(url_for("home"))

    file_bytes = uploaded.read()
    if not file_bytes:
        flash("Empty file.", "error")
        return redirect(url_for("home"))

    if len(file_bytes) > MAX_FILE_SIZE:
        flash(f"File too large. Max size is {file_size_text(MAX_FILE_SIZE)}.", "error")
        return redirect(url_for("home"))

    try:
        db = load_store(force=True)["db"]
        ok, msg = item_limit_ok(db, user)
        if not ok:
            flash(msg, "error")
            return redirect(url_for("home"))

        ok, msg = rate_limit_user(db, user, "upload", 25)
        if not ok:
            flash(msg, "error")
            return redirect(url_for("home"))
    except Exception as e:
        flash(f"Storage error: {e}", "error")
        return redirect(url_for("home"))

    item_id = secrets.token_hex(10)

    try:
        msg = post_attachment(
            content=f"WBFILE|{kind}|{item_id}",
            filename=original_name,
            file_bytes=file_bytes,
            content_type=uploaded.content_type or "application/octet-stream",
        )
    except Exception as e:
        flash(f"Could not upload file: {e}", "error")
        return redirect(url_for("home"))

    attachments = msg.get("attachments", []) or []
    attachment = attachments[0] if attachments else {}

    x = secrets.randbelow(max(1, BOARD_W - 330))
    y = secrets.randbelow(max(1, BOARD_H - 260))
    w, h = (300, 220) if kind == "image" else (320, 140)

    db["items"][item_id] = {
        "id": item_id, "type": kind, "user_id": user["id"], "username": user["username"],
        "x": x, "y": y, "w": w, "h": h, "z": int(time.time()) % 9000 + 1,
        "filename": original_name, "size": len(file_bytes),
        "file_message_id": msg.get("id", ""),
        "file_url": attachment.get("url", ""),
        "file_proxy_url": attachment.get("proxy_url", ""),
        "created": int(time.time()), "updated": int(time.time()),
    }

    try:
        save_db(db)
    except Exception as e:
        flash(f"File uploaded, but board save failed: {e}", "error")
        return redirect(url_for("home"))

    flash("File added.", "success")
    return redirect(url_for("home"))


@app.route("/add-image", methods=["POST"])
@login_required
def add_image():
    return add_uploaded_file("image")


@app.route("/add-audio", methods=["POST"])
@login_required
def add_audio():
    return add_uploaded_file("audio")


@app.route("/board-file/<item_id>")
def board_file(item_id):
    try:
        db = load_store()["db"]
        item = db["items"].get(item_id)
    except Exception:
        abort(404)

    if not item or item.get("type") not in {"image", "audio"}:
        abort(404)

    info = None
    if item.get("file_message_id"):
        info = attachment_info_from_message(item.get("file_message_id"))

    url = ""
    if info:
        url = info.get("url") or info.get("proxy_url")
    if not url:
        url = item.get("file_url") or item.get("file_proxy_url")

    if not url:
        abort(404)

    return redirect(url)


# -----------------------------
# API editing
# -----------------------------

def get_item_for_edit(item_id):
    store = load_store(force=True)
    db = store["db"]
    item = db["items"].get(item_id)

    if not item:
        return db, None, "Item not found."

    user = current_user()
    if not item_can_edit(item, user):
        return db, None, "You can only edit your own items."

    return db, item, ""


@app.route("/api/move-item/<item_id>", methods=["POST"])
@login_required
def api_move_item(item_id):
    db, item, err = get_item_for_edit(item_id)
    if err:
        return jsonify({"ok": False, "error": err}), 403

    data = request.get_json(silent=True) or {}
    user = current_user()

    # small save limit for dragging so rapid drags cannot spam snapshots
    ok, msg = rate_limit_user(db, user, "move", 1)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 429

    item["x"] = clamp_int(data.get("x"), 0, BOARD_W - 40, int(item.get("x", 0)))
    item["y"] = clamp_int(data.get("y"), 0, BOARD_H - 40, int(item.get("y", 0)))
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
        return jsonify({"ok": False, "error": "Only text items can be edited."}), 400

    data = request.get_json(silent=True) or {}
    text = clean_text(data.get("text"), 800)

    if not text:
        return jsonify({"ok": False, "error": "Text cannot be empty."}), 400

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
        return jsonify({"ok": False, "error": "Draw something first."}), 400

    points = points[:MAX_DRAW_POINTS]
    clean_points = []
    for p in points:
        if not isinstance(p, dict):
            continue
        x = clamp_int(p.get("x"), 0, BOARD_W, 0)
        y = clamp_int(p.get("y"), 0, BOARD_H, 0)
        clean_points.append({"x": x, "y": y})

    if len(clean_points) < 2:
        return jsonify({"ok": False, "error": "Not enough drawing points."}), 400

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

    min_x = max(0, min(p["x"] for p in clean_points) - 20)
    min_y = max(0, min(p["y"] for p in clean_points) - 20)
    max_x = min(BOARD_W, max(p["x"] for p in clean_points) + 20)
    max_y = min(BOARD_H, max(p["y"] for p in clean_points) + 20)
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

    return jsonify({"ok": True, "reload": True})


@app.route("/cleanup")
@login_required
def cleanup_route():
    if not current_is_staff():
        return "Only staff can clean old saves.", 403

    result = cleanup_old_snapshots(keep=DB_SNAPSHOT_KEEP, delete_limit=100)
    return (
        "Cleanup done.<br>"
        f"Kept newest saves: {result.get('kept', 0)}<br>"
        f"Deleted old saves: {result.get('deleted', 0)}<br>"
        f"Total old saves found: {result.get('total', 0)}"
    )


@app.errorhandler(413)
def too_large(error):
    flash(f"File too large. Maximum is {file_size_text(MAX_FILE_SIZE)}.", "error")
    return redirect(request.referrer or url_for("home"))


@app.errorhandler(404)
def not_found(error):
    return redirect(url_for("home"))


if __name__ == "__main__":
    print(APP_NAME)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
