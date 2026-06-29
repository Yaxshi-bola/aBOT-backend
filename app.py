# flask_app.py
# Complete backend for "Hammaga Yetadi" Telegram Mini App Ecosystem (v4.0)
# iOS 26 Liquid Glass Edition + Pro Button + Auto Webhook/Polling
#
# Yangiliklar (v4.0):
#   - Webhook URL berilsa webhook, aks holda avtomatik POLLING
#   - /api/my-redemptions — foydalanuvchining olgan sovg'alari
#   - /api/pro-link — "Pro olish" tugmasi uchun maxfiy havola
#   - redemption_id tipidagi xatolar tuzatildi
#   - Xatolarni yaxshiroq log qilish
#
# Author: Antigravity AI
# Date: 2026-06-28

import os
import sys
import logging
import hmac
import hashlib
import json
import time
import secrets
import urllib.parse
import threading
import requests as http_requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ─── LOGGING ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ─── ENV VALIDATION ──────────────────────────────────────────────────
required_envs = [
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID",
    "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
    "BOT_USERNAME", "APP_NAME", "MINI_APP_URL"
]
missing_envs = [env for env in required_envs if not os.getenv(env)]
if missing_envs:
    logger.critical("Missing env vars: %s", ', '.join(missing_envs))
    sys.exit(1)

# ─── CONFIG ──────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API = "https://api.telegram.org/bot" + BOT_TOKEN
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
BOT_USERNAME = os.getenv("BOT_USERNAME")
APP_NAME = os.getenv("APP_NAME")
MINI_APP_URL = os.getenv("MINI_APP_URL").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
PRO_LINK = os.getenv("PRO_LINK", "").strip()

try:
    ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
except ValueError:
    ADMIN_IDS = []

# ─── TELEGRAM HELPERS ────────────────────────────────────────────────
def tg_api(method, data=None, retries=3):
    url = TELEGRAM_API + "/" + method
    for attempt in range(retries):
        try:
            r = http_requests.post(url, json=data or {}, timeout=15)
            return r.json()
        except Exception as e:
            logger.error("Telegram API xatosi (%s), urinish %d/%d: %s", method, attempt + 1, retries, e)
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))  # 1.5s, 3s kutish
            else:
                return {"ok": False}

def tg_send_message(chat_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return tg_api("sendMessage", data)

import re

def parse_telegram_message_link(link):
    if not link:
        return None
    link = link.strip()
    # Matches:
    # https://t.me/c/1234567890/123
    # https://t.me/username/123
    # t.me/username/123
    pattern = r"(?:https?://)?(?:t\.me/)(c/)?([^/]+)/(\d+)"
    match = re.search(pattern, link)
    if match:
        is_private = bool(match.group(1))
        chat_identifier = match.group(2)
        message_id = int(match.group(3))
        
        if is_private:
            try:
                val = int(chat_identifier)
                if val > 0:
                    chat_id = int("-100" + str(val))
                else:
                    chat_id = val
            except ValueError:
                chat_id = chat_identifier
        else:
            if not chat_identifier.startswith("@") and not chat_identifier.isdigit():
                chat_id = "@" + chat_identifier
            else:
                chat_id = chat_identifier
                
        return {"chat_id": chat_id, "message_id": message_id}
    return None

def tg_copy_message(chat_id, from_chat_id, message_id):
    data = {
        "chat_id": chat_id,
        "from_chat_id": from_chat_id,
        "message_id": message_id
    }
    return tg_api("copyMessage", data)

admin_gift_setting_state = {}

membership_cache = {}
membership_cache_lock = threading.Lock()

def is_user_active_in_db(user_id):
    try:
        users = db_select("users", {"telegram_id": "eq." + str(user_id), "select": "is_active"})
        if users:
            return bool(users[0].get("is_active"))
    except Exception:
        pass
    return False

def tg_check_member(user_id, bypass_cache=False):
    now = time.time()
    user_str = str(user_id)
    
    # 1. Check in-memory cache (valid for 10 minutes)
    if not bypass_cache:
        with membership_cache_lock:
            if user_str in membership_cache:
                cached_val, expires_at = membership_cache[user_str]
                if now < expires_at:
                    return cached_val

    # 2. Check Telegram API
    try:
        result = tg_api("getChatMember", {"chat_id": CHANNEL_ID, "user_id": user_id})
        if result.get("ok"):
            status = result["result"]["status"]
            is_member = status in ["creator", "administrator", "member"]
            
            # Only cache if they are a member (True)
            if is_member:
                with membership_cache_lock:
                    membership_cache[user_str] = (True, now + 600)
            return is_member
        
        # If Telegram API fails (e.g. rate limit), fallback to DB
        logger.warning("getChatMember ok=False: %s. Using DB fallback.", result)
        return is_user_active_in_db(user_id)
    except Exception as e:
        logger.error("Channel membership check failed for %s: %s. Using DB fallback.", user_id, e)
        return is_user_active_in_db(user_id)

# ─── SUPABASE REST ───────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_REST = SUPABASE_URL + "/rest/v1"
SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": "Bearer " + SUPABASE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

def db_select(table, params=None):
    url = SUPABASE_REST + "/" + table
    r = http_requests.get(url, headers=SUPABASE_HEADERS, params=params or {}, timeout=15)
    r.raise_for_status()
    return r.json()

def db_insert(table, data):
    url = SUPABASE_REST + "/" + table
    h = dict(SUPABASE_HEADERS)
    h["Prefer"] = "return=representation"
    r = http_requests.post(url, headers=h, json=data, timeout=15)
    r.raise_for_status()
    return r.json()

def db_update(table, data, match_column, match_value):
    url = SUPABASE_REST + "/" + table
    params = {match_column: "eq." + str(match_value)}
    h = dict(SUPABASE_HEADERS)
    h["Prefer"] = "return=representation"
    r = http_requests.patch(url, headers=h, json=data, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def db_delete(table, match_column, match_value):
    url = SUPABASE_REST + "/" + table
    params = {match_column: "eq." + str(match_value)}
    r = http_requests.delete(url, headers=SUPABASE_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return []

def is_user_admin(telegram_id):
    if not telegram_id:
        return False
    try:
        telegram_id = int(telegram_id)
    except (ValueError, TypeError):
        return False
    if telegram_id in ADMIN_IDS:
        return True
    try:
        res = db_select("admins", {"telegram_id": "eq." + str(telegram_id)})
        return bool(res)
    except Exception:
        logger.warning("Error checking admin status in DB for %s", telegram_id)
        return False

def get_all_admin_ids():
    ids = list(ADMIN_IDS)
    try:
        res = db_select("admins", {"select": "telegram_id"})
        for r in res:
            tid = r.get("telegram_id")
            if tid:
                try:
                    tid = int(tid)
                    if tid not in ids:
                        ids.append(tid)
                except ValueError:
                    pass
    except Exception:
        logger.warning("Error fetching all admins from DB")
    return ids

def perform_database_backup(admin_id_to_notify=None):
    try:
        os.makedirs("backups", exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"backup_{timestamp}.json"
        filepath = os.path.join("backups", filename)
        
        tables = ["users", "gifts", "transactions", "redemptions", "canva_pro_codes", "admins", "referral_events", "gift_views", "broadcasts"]
        backup_data = {
            "backup_timestamp": time.time(),
        }
        
        for t in tables:
            try:
                backup_data[t] = db_select(t)
            except Exception as ex:
                logger.warning("Backup failed to fetch table %s: %s", t, ex)
                backup_data[t] = {"error": str(ex)}
                
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, ensure_ascii=False, indent=2)
            
        logger.info("Zaxira nusxa yaratildi: %s", filepath)
        
        if admin_id_to_notify:
            url = TELEGRAM_API + "/sendDocument"
            with open(filepath, "rb") as doc:
                r = http_requests.post(
                    url,
                    data={"chat_id": admin_id_to_notify, "caption": f"📂 <b>Zaxira nusxasi (Backup)</b>\n\nMuddati: {time.strftime('%Y-%m-%d %H:%M:%S')}\nFayl: {filename}", "parse_mode": "HTML"},
                    files={"document": doc},
                    timeout=30
                )
                if not r.json().get("ok"):
                    logger.error("Backup yuborishda xatolik: %s", r.text)
        return True, filename
    except Exception as e:
        logger.exception("Backup olishda xatolik yuz berdi")
        return False, str(e)

def run_daily_backup():
    try:
        last_backup_file = "last_backup.txt"
        now = time.time()
        should_backup = False
        if not os.path.exists(last_backup_file):
            should_backup = True
        else:
            with open(last_backup_file, "r") as f:
                last_time = float(f.read().strip() or 0)
            if now - last_time >= 86400:
                should_backup = True
        
        if should_backup:
            logger.info("Avtomatik kunlik backup boshlanmoqda...")
            if ADMIN_IDS:
                perform_database_backup(ADMIN_IDS[0])
            with open(last_backup_file, "w") as f:
                f.write(str(now))
    except Exception:
        logger.exception("Error in daily backup scheduler")

def backup_scheduler_loop():
    time.sleep(300)
    while True:
        run_daily_backup()
        time.sleep(3600)

# ─── FLASK APP ───────────────────────────────────────────────────────
app = Flask(__name__)

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Admin-Token, X-Telegram-Bot-Api-Secret-Token'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    if request.method == 'OPTIONS':
        response.status_code = 200
    return response

# ─── SECURITY ────────────────────────────────────────────────────────

def validate_webhook_secret(req):
    if not WEBHOOK_SECRET:
        return True
    token = req.headers.get('X-Telegram-Bot-Api-Secret-Token')
    if not token:
        return False
    return hmac.compare_digest(token, WEBHOOK_SECRET)

def validate_init_data(init_data_string):
    if not init_data_string:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data_string))
        if 'hash' not in parsed:
            return None
        received_hash = parsed.pop('hash')
        check_string = "\n".join("%s=%s" % (k, parsed[k]) for k in sorted(parsed.keys()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(computed, received_hash):
            user_str = parsed.get('user')
            if user_str:
                return json.loads(user_str)
        return None
    except Exception:
        logger.exception("initData validation error")
        return None

# ─── VIEW TOKENS (defence-in-depth for sensitive gift content) ───────
view_tokens = {}
view_tokens_lock = threading.Lock()

def issue_view_token(redemption_id, user_id, gift_type, max_views=5, ttl_seconds=600):
    """Bir redemption uchun vaqtinchalik view token berish."""
    token = secrets.token_urlsafe(24)
    with view_tokens_lock:
        view_tokens[token] = {
            "redemption_id": str(redemption_id),
            "user_id": user_id,
            "gift_type": gift_type,
            "max_views": max_views,
            "view_count": 0,
            "expires_at": time.time() + ttl_seconds,
            "created_at": time.time()
        }
    cleanup_expired_tokens()
    return token

def consume_view_token(token, user_id):
    """Tokenni sarflash va tegishli ma'lumotni qaytarish."""
    cleanup_expired_tokens()
    with view_tokens_lock:
        data = view_tokens.get(token)
        if not data:
            return None, "Token eskirgan yoki mavjud emas"
        if data["user_id"] != user_id:
            return None, "Token boshqa foydalanuvchiga tegishli"
        if data["view_count"] >= data["max_views"]:
            return None, "Ko'rish limiti tugagan"
        data["view_count"] += 1
        return data, None

def cleanup_expired_tokens():
    """Muddati o'tgan tokenlarni o'chirish."""
    now = time.time()
    with view_tokens_lock:
        expired = [t for t, d in view_tokens.items() if d["expires_at"] < now]
        for t in expired:
            del view_tokens[t]

# ─── SYSTEM SETTINGS ─────────────────────────────────────────────────
SETTINGS_FILE = "system_settings.json"
settings_lock = threading.Lock()

def get_system_setting(key, default=None):
    with settings_lock:
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    data = json.load(f)
                    return data.get(key, default)
            except Exception:
                pass
    return default

def set_system_setting(key, value):
    with settings_lock:
        data = {}
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    data = json.load(f)
            except Exception:
                pass
        data[key] = value
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump(data, f)
            return True
        except Exception:
            return False

# ─── RATE LIMITER ────────────────────────────────────────────────────
rate_limit_store = {}
rate_limit_lock = threading.Lock()

def is_rate_limited(ip, max_req=20, window=60):
    now = time.time()
    with rate_limit_lock:
        if ip not in rate_limit_store:
            rate_limit_store[ip] = []
        rate_limit_store[ip] = [t for t in rate_limit_store[ip] if now - t < window]
        if len(rate_limit_store[ip]) >= max_req:
            return True
        rate_limit_store[ip].append(now)
        return False

# ─── CHANNEL INVITE LINKS ────────────────────────────────────────────
CHANNEL_LINKS_FILE = "channel_invite_links.json"
channel_links_lock = threading.Lock()

def get_or_create_channel_invite_link(user_id):
    with channel_links_lock:
        links = {}
        if os.path.exists(CHANNEL_LINKS_FILE):
            try:
                with open(CHANNEL_LINKS_FILE, "r") as f:
                    links = json.load(f)
            except Exception:
                pass
        
        user_str = str(user_id)
        if user_str in links:
            return links[user_str]
        
        # Create a new invite link
        res = tg_api("createChatInviteLink", {
            "chat_id": CHANNEL_ID,
            "name": f"ref_{user_id}"
        })
        if res.get("ok"):
            link = res["result"]["invite_link"]
            links[user_str] = link
            try:
                with open(CHANNEL_LINKS_FILE, "w") as f:
                    json.dump(links, f)
            except Exception:
                pass
            return link
        else:
            logger.error("Failed to create chat invite link: %s", res)
        return None

def handle_chat_member_update(chat_member):
    try:
        new_state = chat_member.get("new_chat_member", {})
        status = new_state.get("status")
        user_obj = new_state.get("user", {})
        user_id = user_obj.get("id")
        
        if not user_id:
            return

        # 1. User joined the channel
        if status == "member":
            invite_link = chat_member.get("invite_link", {})
            link_name = invite_link.get("name", "")
            if link_name and link_name.startswith("ref_"):
                try:
                    referrer_id = int(link_name.split("_")[1])
                    # Register the referral
                    if referrer_id != user_id:
                        # Create or get invitee user, set referred_by
                        get_or_create_user(
                            telegram_id=user_id,
                            username=user_obj.get("username"),
                            first_name=user_obj.get("first_name", ""),
                            last_name=user_obj.get("last_name"),
                            referred_by=referrer_id
                        )
                        # Activate the user (rewards referrer with 50 and invitee with 10)
                        check_and_activate_user(user_id)
                        logger.info(f"Referral registered via channel invite link: {user_id} referred by {referrer_id}")
                except Exception as e:
                    logger.exception("Error processing referral from invite link")
            else:
                # Normal join (no referral link, or they just joined)
                # We can activate them if they exist in our DB
                try:
                    users = db_select("users", {"telegram_id": "eq." + str(user_id)})
                    if users and not users[0].get("is_active"):
                        check_and_activate_user(user_id)
                except Exception:
                    pass

        # 2. User left the channel (status is left, kicked)
        elif status in ["left", "kicked"]:
            try:
                # Set is_active = False in the database
                db_update("users", {"is_active": False}, "telegram_id", user_id)
                logger.info(f"User {user_id} left the channel. Deactivated in DB.")
                
                # Also clear their membership cache so the bot/app immediately knows they left
                user_str = str(user_id)
                with membership_cache_lock:
                    if user_str in membership_cache:
                        del membership_cache[user_str]
            except Exception:
                logger.exception(f"Error deactivating user {user_id} who left the channel")
    except Exception:
        logger.exception("Error in handle_chat_member_update")

# ─── DATABASE HELPERS ────────────────────────────────────────────────

def get_or_create_user(telegram_id, username=None, first_name="", last_name=None, referred_by=None):
    try:
        users = db_select("users", {"telegram_id": "eq." + str(telegram_id)})
        if users:
            user = users[0]
            update_data = {
                "last_seen": "now()", "username": username,
                "first_name": first_name, "last_name": last_name
            }
            # If the user is not active and doesn't have a referrer, set it now
            if not user.get("is_active") and not user.get("referred_by") and referred_by and referred_by != telegram_id:
                ref_check = db_select("users", {"telegram_id": "eq." + str(referred_by)})
                if ref_check:
                    update_data["referred_by"] = referred_by
                    user["referred_by"] = referred_by

            db_update("users", update_data, "telegram_id", telegram_id)
            user.update(update_data)
            return user

        new_user = {
            "telegram_id": telegram_id, "username": username,
            "first_name": first_name, "last_name": last_name,
            "referred_by": None, "points": 0, "referral_count": 0,
            "is_active": False, "is_banned": False
        }

        if referred_by and referred_by != telegram_id:
            ref_check = db_select("users", {"telegram_id": "eq." + str(referred_by)})
            if ref_check:
                new_user["referred_by"] = referred_by

        result = db_insert("users", new_user)
        return result[0] if result else new_user
    except Exception:
        logger.exception("DB error: get_or_create_user %s", telegram_id)
        raise

def check_and_activate_user(telegram_id):
    try:
        users = db_select("users", {"telegram_id": "eq." + str(telegram_id)})
        if not users:
            raise Exception("Foydalanuvchi topilmadi")

        user = users[0]
        if user["is_active"]:
            return user

        # Check if they have ever been activated before
        was_ever_activated = user.get("activated_at") is not None

        if not was_ever_activated:
            new_points = user["points"] + 10
            updated = db_update("users", {
                "is_active": True, "activated_at": "now()", "points": new_points
            }, "telegram_id", telegram_id)
            
            db_insert("transactions", {
                "user_id": telegram_id, "amount": 10,
                "type": "activation_bonus",
                "description": "Kanalga a'zo bo'lganlik uchun bonus"
            })

            referrer_id = user.get("referred_by")
            if referrer_id:
                try:
                    # Double-check that this referred user has never had a referral event recorded
                    ref_events = db_select("referral_events", {"referred_id": "eq." + str(telegram_id)})
                    if not ref_events:
                        db_insert("referral_events", {
                            "referrer_id": referrer_id, "referred_id": telegram_id,
                            "points_awarded": 50
                        })
                        referrers = db_select("users", {"telegram_id": "eq." + str(referrer_id)})
                        if referrers:
                            ref = referrers[0]
                            ref_points = ref["points"] + 50
                            ref_count = ref["referral_count"] + 1
                            db_update("users", {
                                "points": ref_points, "referral_count": ref_count
                            }, "telegram_id", referrer_id)
                            db_insert("transactions", {
                                "user_id": referrer_id, "amount": 50,
                                "type": "referral_bonus",
                                "description": "Taklif: %s" % user["first_name"]
                            })
                            tg_send_message(referrer_id,
                                "🎉 <b>Tabriklaymiz!</b>\n\n"
                                "%s sizning taklifingiz orqali qo'shildi.\n"
                                "Hisobingizga +50 ball qo'shildi! 💰\n"
                                "Joriy balansingiz: %d ball" % (user["first_name"], ref_points)
                            )
                except Exception as e:
                    logger.warning("Referral already processed or error: %s", e)
        else:
            # Just reactivate them without any points or referral rewards
            updated = db_update("users", {
                "is_active": True
            }, "telegram_id", telegram_id)

        updated_user = updated[0] if updated else user
        return updated_user
    except Exception:
        logger.exception("Error activating user %s", telegram_id)
        raise

# ═══════════════════════════════════════════════════════════════════
# TELEGRAM WEBHOOK & POLLING
# ═══════════════════════════════════════════════════════════════════

@app.route('/webhook', methods=['POST'])
def webhook():
    if not validate_webhook_secret(request):
        return 'Forbidden', 403
    try:
        update = request.get_json(force=True)
        process_telegram_update(update)
        return '', 200
    except Exception:
        logger.exception("Webhook processing error")
        return '', 200

def handle_callback_query(callback_query):
    try:
        user = callback_query.get("from", {})
        user_id = user.get("id")
        chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
        cb_id = callback_query.get("id")
        data = callback_query.get("data")
        
        if data == "check_sub":
            is_member = tg_check_member(user_id, bypass_cache=True)
            if is_member:
                tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Ajoyib! Obuna tasdiqlandi.", "show_alert": False})
                activated_user = check_and_activate_user(user_id)
                welcome = (
                    "🎉 <b>Rahmat!</b> Obuna muvaffaqiyatli tasdiqlandi.\n\n"
                    "💰 Balansingiz: %d ball\n\n"
                    "Boshlash uchun quyidagi tugmani bosing 👇"
                ) % activated_user["points"]
                keyboard = {
                    "inline_keyboard": [[{
                        "text": "🚀 Ilovani ochish",
                        "web_app": {"url": MINI_APP_URL},
                        "style": "primary"
                    }]]
                }
                tg_send_message(chat_id, welcome, keyboard)
            else:
                tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Siz hali kanalga a'zo bo'lmadingiz. Iltimos, obuna bo'ling.", "show_alert": True})
    except Exception:
        logger.exception("Error in handle_callback_query")

def process_telegram_update(update):
    chat_member = update.get("chat_member")
    if chat_member:
        handle_chat_member_update(chat_member)
        return

    callback_query = update.get("callback_query")
    if callback_query:
        handle_callback_query(callback_query)
        return

    message = update.get("message")
    if not message:
        return
    text = message.get("text", "")
    caption = message.get("caption", "")
    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    user_id = user.get("id")
    username = user.get("username")
    first_name = user.get("first_name", "")
    last_name = user.get("last_name")

    is_admin = is_user_admin(user_id)

    # Check if admin is in the middle of setting a gift message
    if is_admin and user_id in admin_gift_setting_state:
        gift_id = admin_gift_setting_state.pop(user_id)
        gift_content_json = json.dumps({
            "chat_id": chat_id,
            "message_id": message.get("message_id")
        })
        try:
            db_update("gifts", {"gift_content": gift_content_json}, "id", gift_id)
            tg_send_message(chat_id, "✅ <b>Xabar muvaffaqiyatli saqlandi!</b>\n\nEndi ushbu sovg'a sotib olinganda foydalanuvchiga xuddi shu xabar nusxalanib (copyMessage) yuboriladi.")
        except Exception:
            logger.exception("Error saving gift message")
            tg_send_message(chat_id, "❌ Xabarni saqlashda xatolik yuz berdi.")
        return

    # Enforce channel subscription for all non-admins
    if not is_admin:
        is_member = tg_check_member(user_id)
        if not is_member:
            channel_url = "https://t.me/" + CHANNEL_ID.lstrip("@")
            subscribe_keyboard = {
                "inline_keyboard": [
                    [{"text": "📢 Kanalga obuna bo'lish", "url": channel_url, "style": "primary"}],
                    [{"text": "✅ Obunani tekshirish", "callback_data": "check_sub", "style": "success"}]
                ]
            }
            welcome = (
                "Assalomu alaykum, <b>%s</b>! 👋\n\n"
                "🎁 <b>Hammaga Yetadi</b> ilovasidan foydalanish uchun avval rasmiy kanalimizga obuna bo'ling, "
                "so'ng <b>«Obunani tekshirish»</b> tugmasini bosing."
            ) % first_name
            tg_send_message(chat_id, welcome, subscribe_keyboard)
            return

    if text.startswith("/broadcast") or caption.startswith("/broadcast"):
        if is_admin:
            handle_broadcast_command(chat_id, user_id, message)
            return

    if text.startswith("/start"):
        handle_start(chat_id, user_id, username, first_name, last_name, text)
    elif text == "/help":
        handle_help(chat_id)
    elif text == "/stats":
        handle_stats(chat_id, user_id)
    elif text == "/referral":
        handle_referral(chat_id, user_id)
    elif text.startswith("/grant") and is_admin:
        handle_grant(chat_id, user_id, text)
    elif text.startswith("/addcanva") and is_admin:
        handle_add_canva(chat_id, text)
    elif text.startswith("/setprolink") and is_admin:
        handle_set_pro_link(chat_id, text)
    elif text.startswith("/setgiftmsg") and is_admin:
        parts = text.split()
        if len(parts) < 2:
            tg_send_message(chat_id, "❓ <b>Noto'g'ri format.</b>\n\nFoydalanish: <code>/setgiftmsg [sovg'a_id]</code>\nMisol: <code>/setgiftmsg 5</code>")
            return
        try:
            gift_id = int(parts[1])
            gifts = db_select("gifts", {"id": "eq." + str(gift_id)})
            if not gifts:
                tg_send_message(chat_id, "❌ Bunday sovg'a topilmadi.")
                return
            admin_gift_setting_state[user_id] = gift_id
            tg_send_message(chat_id, "📥 <b>Kutilyapti...</b>\n\nEndi foydalanuvchiga yubormoqchi bo'lgan xabaringizni (fayl, video, rasm, matn, ovozli xabar va h.k.) shu yerga yuboring. Men uni saqlab olaman.")
        except ValueError:
            tg_send_message(chat_id, "❌ Sovg'a ID raqam bo'lishi kerak.")
        return

# ─── POLLING (webhook URL bo'lmasa avtomatik ishlaydi) ───────────────

def poll_updates():
    """Long-polling orqali yangilanishlarni olish."""
    logger.info("Polling boshlandi...")
    offset = 0
    while True:
        try:
            r = http_requests.get(
                TELEGRAM_API + "/getUpdates",
                params={"timeout": 30, "offset": offset, "allowed_updates": '["message", "callback_query", "chat_member"]'},
                timeout=35
            )
            data = r.json()
            if not data.get("ok"):
                logger.error("getUpdates xatosi: %s", data)
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    process_telegram_update(upd)
                except Exception:
                    logger.exception("Update processing error: %s", upd.get("update_id"))
        except http_requests.exceptions.Timeout:
            continue
        except Exception:
            logger.exception("Polling xatosi")
            time.sleep(5)

# ─── BOT COMMAND HANDLERS ────────────────────────────────────────────

def handle_start(chat_id, user_id, username, first_name, last_name, text):
    referred_by = None
    parts = text.split()
    start_param = None
    if len(parts) > 1:
        start_param = parts[1]
        if start_param.startswith("r_"):
            try:
                referred_by = int(start_param.split("_")[1])
            except (ValueError, IndexError):
                pass

    try:
        # Run DB operations in the background to respond instantly
        def run_db_and_activate():
            try:
                user = get_or_create_user(user_id, username, first_name, last_name, referred_by)
                is_member = tg_check_member(user_id)
                if is_member and not user.get("is_active"):
                    check_and_activate_user(user_id)
            except Exception:
                logger.exception("Error in background start DB thread")

        threading.Thread(target=run_db_and_activate, daemon=True).start()

        # Send welcome message immediately
        webapp_url = MINI_APP_URL
        if start_param:
            webapp_url = MINI_APP_URL + "/?startapp=" + start_param

        welcome = (
            "Assalomu alaykum, <b>%s</b>! 👋\n\n"
            "🎉 <b>Hammaga Yetadi</b> ilovasiga xush kelibsiz!\n\n"
            "🎁 Do'stlarni taklif qilib ball to'plang va sovg'alarga ega bo'ling!\n\n"
            "Boshlash uchun quyidagi tugmani bosing 👇"
        ) % first_name

        keyboard = {
            "inline_keyboard": [[{
                "text": "🚀 Ilovani ochish",
                "web_app": {"url": webapp_url},
                "style": "primary"
            }]]
        }
        tg_send_message(chat_id, welcome, keyboard)
    except Exception:
        logger.exception("Error in /start")
        tg_send_message(chat_id, "⚠️ Tizimda xatolik yuz berdi. Iltimos, qayta urinib ko'ring: /start")

def handle_help(chat_id):
    tg_send_message(chat_id,
        "🤖 <b>Bot yo'riqnomasi</b>\n\n"
        "1️⃣ <b>Ball yig'ish:</b>\n"
        "  • Kanalga obuna bo'lish: +10 ball\n"
        "  • Do'stlarni taklif qilish: +50 ball\n\n"
        "2️⃣ <b>Buyruqlar:</b>\n"
        "  • /start — Ilovani ochish\n"
        "  • /stats — Statistika va reyting\n"
        "  • /referral — Shaxsiy taklif havolasi\n"
        "  • /help — Ushbu yo'riqnoma\n\n"
        "3️⃣ <b>Sovg'alar:</b>\n"
        "  Ballarni sovg'alar do'konida haqiqiy sovg'alarga almashtiring."
    )

def handle_stats(chat_id, user_id):
    try:
        users = db_select("users", {"telegram_id": "eq." + str(user_id)})
        if not users:
            tg_send_message(chat_id, "Avval /start buyrug'ini bosing.")
            return
        u = users[0]
        all_active = db_select("users", {"is_active": "eq.true", "order": "points.desc", "select": "telegram_id"})
        rank = "?"
        for i, a in enumerate(all_active):
            if a["telegram_id"] == user_id:
                rank = i + 1
                break
        tg_send_message(chat_id,
            "📊 <b>Sizning statistikangiz</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💰 <b>Ball:</b> %d\n"
            "👥 <b>Takliflar:</b> %d ta\n"
            "🏆 <b>Reyting:</b> #%s\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "📢 /referral — taklif havolangiz\n"
            "Ko'proq do'st taklif qiling!" % (u["points"], u["referral_count"], rank)
        )
    except Exception:
        logger.exception("Error in /stats")
        tg_send_message(chat_id, "Xatolik yuz berdi. Qaytadan urinib ko'ring.")

def handle_referral(chat_id, user_id):
    ref_link = "https://t.me/%s/%s?startapp=r_%s" % (BOT_USERNAME, APP_NAME, user_id)
    share_text = "Do'stim, bu ajoyib ilovaga qo'shil va sovg'alarga ega bo'l! 🎁"
    share_url = "https://t.t.me/share/url?url=%s&text=%s" % (
        urllib.parse.quote(ref_link), urllib.parse.quote(share_text)
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "📤 Do'stlarga ulashish", "url": share_url}],
            [{"text": "🚀 Ilovani ochish", "web_app": {"url": MINI_APP_URL + "/?startapp=r_" + str(user_id)}}]
        ]
    }
    tg_send_message(chat_id,
        "🔗 <b>Sizning taklif havolangiz:</b>\n\n"
        "<code>%s</code>\n\n"
        "Do'stlaringizga ulashing — ular kanalga a'zo bo'lsa, sizga <b>+50 ball</b> taqdim etiladi! 💰" % ref_link,
        keyboard
    )

def handle_grant(chat_id, admin_id, text):
    parts = text.split()
    if len(parts) != 3:
        tg_send_message(chat_id, "Foydalanish: /grant [user_id] [ball]")
        return
    try:
        target_id = int(parts[1])
        amount = int(parts[2])
        users = db_select("users", {"telegram_id": "eq." + str(target_id)})
        if not users:
            tg_send_message(chat_id, "Foydalanuvchi topilmadi.")
            return
        u = users[0]
        new_pts = u["points"] + amount
        db_update("users", {"points": new_pts}, "telegram_id", target_id)
        db_insert("transactions", {
            "user_id": target_id, "amount": amount,
            "type": "admin_grant",
            "description": "Admin (%d) tomonidan" % admin_id
        })
        tg_send_message(target_id,
            "🎁 Hisobingizga <b>%d ball</b> qo'shildi!\n"
            "💰 Joriy balans: <b>%d ball</b>" % (amount, new_pts)
        )
        tg_send_message(chat_id, "✅ %d foydalanuvchiga %d ball yuborildi." % (target_id, amount))
    except ValueError:
        tg_send_message(chat_id, "ID va miqdor son bo'lishi kerak.")
    except Exception:
        logger.exception("Grant error")
        tg_send_message(chat_id, "Xatolik yuz berdi.")

def handle_add_canva(chat_id, text):
    parts = text.split(maxsplit=2)
    if len(parts) != 3:
        tg_send_message(chat_id, "Foydalanish: /addcanva [gift_id] [link]")
        return
    try:
        gift_id = int(parts[1])
        link = parts[2].strip()
        if not (link.startswith("http://") or link.startswith("https://")):
            tg_send_message(chat_id, "Link http:// yoki https:// bilan boshlanishi kerak.")
            return
        gifts = db_select("gifts", {"id": "eq." + str(gift_id)})
        if not gifts:
            tg_send_message(chat_id, "Bunday gift_id topilmadi.")
            return
        db_insert("canva_pro_codes", {
            "gift_id": gift_id, "code_value": link, "is_used": False
        })
        tg_send_message(chat_id, "✅ Canva Pro link qo'shildi (gift_id=%d)." % gift_id)
    except ValueError:
        tg_send_message(chat_id, "gift_id butun son bo'lishi kerak.")
    except Exception:
        logger.exception("AddCanva error")
        tg_send_message(chat_id, "Xatolik yuz berdi.")

def handle_set_pro_link(chat_id, text):
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        tg_send_message(chat_id, "Foydalanish: /setprolink [havola]")
        return
    link = parts[1].strip()
    if not (link.startswith("http://") or link.startswith("https://")):
        tg_send_message(chat_id, "Havola http:// yoki https:// bilan boshlanishi kerak.")
        return
    
    if set_system_setting("pro_link", link):
        tg_send_message(chat_id, f"✅ <b>Pro olish havolasi yangilandi:</b>\n<code>{link}</code>")
    else:
        tg_send_message(chat_id, "❌ Havolani saqlashda xatolik yuz berdi.")

def handle_broadcast_command(chat_id, admin_id, message):
    reply_to = message.get("reply_to_message")
    target_msg = reply_to if reply_to else message
    broadcast_text = ""
    if reply_to:
        broadcast_text = target_msg.get("text", "") or target_msg.get("caption", "")
    else:
        cmd_text = message.get("text", "")
        cmd_caption = message.get("caption", "")
        if cmd_text.startswith("/broadcast"):
            broadcast_text = cmd_text[len("/broadcast"):].strip()
        elif cmd_caption.startswith("/broadcast"):
            broadcast_text = cmd_caption[len("/broadcast"):].strip()

    if not broadcast_text and not any(k in target_msg for k in ["photo","video","document","audio","voice","animation"]):
        tg_send_message(chat_id, "Foydalanish: /broadcast [matn] yoki xabarga reply qiling.")
        return

    try:
        active_users = db_select("users", {"is_active": "eq.true", "select": "telegram_id"})
    except Exception as e:
        logger.error("Broadcast error: %s", e)
        tg_send_message(chat_id, "Baza bilan ulanish xatosi.")
        return

    if not active_users:
        tg_send_message(chat_id, "Faol foydalanuvchilar topilmadi.")
        return

    status_msg = tg_send_message(chat_id, "📢 Reklama yuborish boshlandi (Jami: %d ta)..." % len(active_users))
    status_msg_id = status_msg.get("result", {}).get("message_id")

    try:
        broadcast_rec = db_insert("broadcasts", {
            "admin_id": admin_id,
            "message_text": broadcast_text or "[Media Fayl]",
            "status": "running"
        })
        bid = broadcast_rec[0]["id"] if broadcast_rec else None
    except Exception:
        bid = None

    sent, failed = 0, 0
    method = "sendMessage"
    media_key = "text"
    media_id = None

    if "photo" in target_msg:
        method = "sendPhoto"; media_key = "photo"; media_id = target_msg["photo"][-1]["file_id"]
    elif "video" in target_msg:
        method = "sendVideo"; media_key = "video"; media_id = target_msg["video"]["file_id"]
    elif "document" in target_msg:
        method = "sendDocument"; media_key = "document"; media_id = target_msg["document"]["file_id"]
    elif "audio" in target_msg:
        method = "sendAudio"; media_key = "audio"; media_id = target_msg["audio"]["file_id"]
    elif "voice" in target_msg:
        method = "sendVoice"; media_key = "voice"; media_id = target_msg["voice"]["file_id"]
    elif "animation" in target_msg:
        method = "sendAnimation"; media_key = "animation"; media_id = target_msg["animation"]["file_id"]

    for u in active_users:
        target_chat_id = u["telegram_id"]
        payload = {"chat_id": target_chat_id}
        if method == "sendMessage":
            payload["text"] = broadcast_text
            payload["parse_mode"] = "HTML"
        else:
            payload[media_key] = media_id
            if broadcast_text:
                payload["caption"] = broadcast_text
                payload["parse_mode"] = "HTML"
        result = tg_api(method, payload)
        if result.get("ok"):
            sent += 1
        else:
            failed += 1
        time.sleep(0.04)

    if bid:
        try:
            db_update("broadcasts", {"sent_count": sent, "failed_count": failed, "status": "done"}, "id", bid)
        except Exception:
            pass

    report = (
        "📢 <b>Reklama yakunlandi!</b>\n\n"
        "✅ Muvaffaqiyatli: <b>%d</b>\n"
        "❌ Muvaffaqiyatsiz: <b>%d</b>"
    ) % (sent, failed)

    if status_msg_id:
        tg_api("editMessageText", {"chat_id": chat_id, "message_id": status_msg_id, "text": report, "parse_mode": "HTML"})
    else:
        tg_send_message(chat_id, report)

# ─── CALLBACK QUERY HANDLER (Obunani tekshirish tugmasi uchun) ─────

@app.route('/callback', methods=['POST'])
def callback_handler():
    """Inline keyboard callbacklarini qayta ishlash (faqat webhook rejimida)."""
    if not validate_webhook_secret(request):
        return 'Forbidden', 403
    try:
        update = request.get_json(force=True)
        callback_query = update.get("callback_query")
        if not callback_query:
            return '', 200

        user_id = callback_query["from"]["id"]
        chat_id = callback_query["message"]["chat"]["id"]
        message_id = callback_query["message"]["message_id"]
        data = callback_query.get("data", "")

        if data == "check_sub":
            is_member = tg_check_member(user_id)
            if is_member:
                try:
                    user = check_and_activate_user(user_id)
                    welcome = (
                        "✅ <b>Tabriklaymiz!</b> Kanalga muvaffaqiyatli a'zo bo'ldingiz.\n\n"
                        "💰 Balansingiz: <b>%d ball</b>\n"
                        "👥 Takliflar: <b>%d</b>\n\n"
                        "Quyidagi tugma orqali ilovani oching 👇"
                    ) % (user["points"], user["referral_count"])
                    keyboard = {
                        "inline_keyboard": [[{
                            "text": "🚀 Ilovani ochish",
                            "web_app": {"url": MINI_APP_URL}
                        }]]
                    }
                    tg_api("editMessageText", {
                        "chat_id": chat_id, "message_id": message_id,
                        "text": welcome, "parse_mode": "HTML", "reply_markup": keyboard
                    })
                    tg_api("answerCallbackQuery", {
                        "callback_query_id": callback_query["id"],
                        "text": "✅ Muvaffaqiyatli faollashtirildi!",
                        "show_alert": False
                    })
                except Exception:
                    logger.exception("check_sub callback error")
                    tg_api("answerCallbackQuery", {
                        "callback_query_id": callback_query["id"],
                        "text": "⚠️ Xatolik yuz berdi",
                        "show_alert": True
                    })
            else:
                tg_api("answerCallbackQuery", {
                    "callback_query_id": callback_query["id"],
                    "text": "❌ Siz hali kanalga a'zo bo'lmadingiz",
                    "show_alert": True
                })
        return '', 200
    except Exception:
        logger.exception("Callback processing error")
        return '', 200

# ═══════════════════════════════════════════════════════════════════
# MINI APP API ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/auth', methods=['POST', 'OPTIONS'])
def api_auth():
    if request.method == 'OPTIONS':
        return '', 200
    if is_rate_limited(request.remote_addr):
        return jsonify({"success": False, "error": "Tezlik cheklovi"}), 429
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info:
        return jsonify({"success": False, "error": "Avtorizatsiya xatosi"}), 401
    tid = user_info.get("id")
    uname = user_info.get("username")
    fname = user_info.get("first_name", "")
    lname = user_info.get("last_name", "")

    referred_by_id = None
    rc = data.get("referral_code")
    if rc and rc.startswith("r_"):
        try:
            referred_by_id = int(rc.split("_")[1])
        except (ValueError, IndexError):
            pass

    try:
        user = get_or_create_user(tid, uname, fname, lname, referred_by_id)
        is_member = tg_check_member(tid)
        if is_member:
            if not user.get("is_active"):
                user = check_and_activate_user(tid)
        else:
            if user.get("is_active"):
                db_update("users", {"is_active": False}, "telegram_id", tid)
                user["is_active"] = False

        return jsonify({
            "success": True,
            "user": {
                "telegram_id": user["telegram_id"], "username": user["username"],
                "first_name": user["first_name"], "points": user["points"],
                "referral_count": user["referral_count"], "is_active": user["is_active"]
            },
            "is_member": is_member,
            "is_admin": is_user_admin(tid),
            "channel_url": "https://t.me/" + CHANNEL_ID.lstrip("@"),
            "referral_link": get_or_create_channel_invite_link(tid) or ("https://t.me/%s/%s?startapp=r_%s" % (BOT_USERNAME, APP_NAME, tid)),
            "has_pro_link": bool(get_system_setting("pro_link", PRO_LINK))
        })
    except Exception:
        logger.exception("Error in /api/auth")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

@app.route('/api/verify', methods=['POST', 'OPTIONS'])
def api_verify():
    if request.method == 'OPTIONS':
        return '', 200
    if is_rate_limited(request.remote_addr):
        return jsonify({"success": False, "error": "Tezlik cheklovi"}), 429
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info:
        return jsonify({"success": False, "error": "Avtorizatsiya xatosi"}), 401
    tid = user_info.get("id")
    try:
        if not tg_check_member(tid, bypass_cache=True):
            return jsonify({"success": False, "error": "Siz hali kanalga a'zo bo'lmadingiz."}), 403
        user = check_and_activate_user(tid)
        return jsonify({
            "success": True,
            "user": {
                "telegram_id": user["telegram_id"], "username": user["username"],
                "first_name": user["first_name"], "points": user["points"],
                "referral_count": user["referral_count"], "is_active": user["is_active"]
            },
            "is_admin": is_user_admin(tid)
        })
    except Exception:
        logger.exception("Error in /api/verify")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

@app.route('/api/leaderboard', methods=['GET', 'OPTIONS'])
def api_leaderboard():
    if request.method == 'OPTIONS':
        return '', 200
    try:
        users = db_select("users", {
            "is_active": "eq.true",
            "is_banned": "eq.false",
            "order": "points.desc",
            "limit": "20",
            "select": "first_name,username,points,referral_count,telegram_id"
        })
        board = []
        for i, u in enumerate(users or []):
            un = u.get("username")
            masked = None
            if un:
                masked = "@%s***%s" % (un[:2], un[-2:]) if len(un) > 4 else "@%s***" % un[0]
            board.append({
                "rank": i + 1, "first_name": u.get("first_name", "Foydalanuvchi"),
                "username": masked, "points": u.get("points", 0),
                "referral_count": u.get("referral_count", 0)
            })
        return jsonify({"success": True, "leaderboard": board})
    except Exception:
        logger.exception("Error in /api/leaderboard")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

# ─── GIFTS ───────────────────────────────────────────────────────────

@app.route('/api/gifts', methods=['GET', 'OPTIONS'])
def api_gifts():
    if request.method == 'OPTIONS':
        return '', 200
    try:
        gifts = db_select("gifts", {"is_active": "eq.true", "order": "points_cost.asc"})
        safe_gifts = []
        for g in gifts:
            gift_type = g.get("gift_type", "physical")
            available_codes = -1
            if gift_type == "canva_pro_link":
                try:
                    codes = db_select("canva_pro_codes", {
                        "gift_id": "eq." + str(g["id"]), "is_used": "eq.false", "select": "id"
                    })
                    available_codes = len(codes)
                except Exception:
                    available_codes = 0
            safe_gifts.append({
                "id": g["id"],
                "name": g["name"],
                "description": g.get("description"),
                "points_cost": g["points_cost"],
                "image_url": g.get("image_url"),
                "stock": g.get("stock", -1),
                "gift_type": gift_type,
                "has_content": g.get("gift_content") is not None or gift_type != "physical",
                "category": g.get("category", "general"),
                "accent_color": g.get("accent_color", "#007AFF"),
                "available_codes": available_codes
            })
        return jsonify({"success": True, "gifts": safe_gifts})
    except Exception:
        logger.exception("Error fetching gifts")
        return jsonify({"success": False, "error": "Sovg'alarni yuklashda xatolik"}), 500

@app.route('/api/redeem', methods=['POST', 'OPTIONS'])
def api_redeem():
    if request.method == 'OPTIONS':
        return '', 200
    if is_rate_limited(request.remote_addr, max_req=10):
        return jsonify({"success": False, "error": "Tezlik cheklovi"}), 429

    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info:
        return jsonify({"success": False, "error": "Avtorizatsiya xatosi"}), 401

    tid = user_info.get("id")
    gift_id = data.get("gift_id")

    if not gift_id:
        return jsonify({"success": False, "error": "Noto'g'ri sovg'a IDsi"}), 400

    try:
        users = db_select("users", {"telegram_id": "eq." + str(tid)})
        if not users:
            return jsonify({"success": False, "error": "Foydalanuvchi topilmadi"}), 404
        user = users[0]

        gifts = db_select("gifts", {"id": "eq." + str(gift_id), "is_active": "eq.true"})
        if not gifts:
            return jsonify({"success": False, "error": "Sovg'a mavjud emas yoki o'chirilgan"}), 404
        gift = gifts[0]
        gift_type = gift.get("gift_type", "physical")

        if gift["stock"] == 0:
            return jsonify({"success": False, "error": "Bu sovg'a hozirda tugagan"}), 400

        if user["points"] < gift["points_cost"]:
            return jsonify({"success": False, "error": "Ballaringiz etarli emas"}), 400

        assigned_code_id = None
        if gift_type == "canva_pro_link":
            codes = db_select("canva_pro_codes", {
                "gift_id": "eq." + str(gift_id), "is_used": "eq.false",
                "order": "id.asc", "limit": "1"
            })
            if not codes:
                return jsonify({"success": False, "error": "Bu sovg'a uchun hozircha kodlar tugagan. Admin bilan bog'laning."}), 400
            assigned_code_id = codes[0]["id"]
            db_update("canva_pro_codes", {
                "is_used": True, "assigned_to": tid, "used_at": "now()"
            }, "id", assigned_code_id)

        new_pts = user["points"] - gift["points_cost"]
        db_update("users", {"points": new_pts}, "telegram_id", tid)

        if gift["stock"] > 0:
            db_update("gifts", {"stock": gift["stock"] - 1}, "id", gift_id)

        db_insert("transactions", {
            "user_id": tid,
            "amount": -gift["points_cost"],
            "type": "redeem",
            "description": "Sovg'aga almashtirildi: %s" % gift["name"]
        })

        redemption_record = db_insert("redemptions", {
            "user_id": tid, "gift_id": gift_id, "status": "pending"
        })
        redemption_id = str(redemption_record[0]["id"]) if redemption_record else None

        # Check if there is a saved message in gift_content
        gift_content = gift.get("gift_content", "").strip()
        saved_msg = None
        if gift_content:
            try:
                saved_msg = json.loads(gift_content)
            except Exception:
                parsed = parse_telegram_message_link(gift_content)
                if parsed:
                    saved_msg = parsed

        if saved_msg and isinstance(saved_msg, dict) and "chat_id" in saved_msg and "message_id" in saved_msg:
            # Automatic message delivery!
            res = tg_copy_message(tid, saved_msg["chat_id"], saved_msg["message_id"])
            if res.get("ok"):
                db_update("redemptions", {"status": "approved"}, "id", redemption_id)
                
                user_msg = (
                    "🎁 <b>Sovg'angiz yetkazildi!</b>\n\n"
                    "• <b>Sovg'a:</b> %s\n"
                    "• <b>Sarflangan ball:</b> %d\n"
                    "• <b>Holati:</b> Yetkazildi ✅\n\n"
                    "Yuqoridagi xabarni qabul qilib oling."
                ) % (gift["name"], gift["points_cost"])
                tg_send_message(tid, user_msg)

                admin_msg = (
                    "🚨 <b>Avtomatik sovg'a yetkazildi!</b>\n\n"
                    "👤 Foydalanuvchi: %s (ID: %d)\n"
                    "🎁 Sovg'a: %s\n"
                    "💰 Narxi: %d ball\n"
                    "📦 Redemption ID: <code>%s</code>"
                ) % (user["first_name"], tid, gift["name"], gift["points_cost"], redemption_id or "—")
                for aid in get_all_admin_ids():
                    tg_send_message(aid, admin_msg)
            else:
                err_desc = res.get("description", "Noma'lum xatolik")
                admin_err_msg = (
                    "❌ <b>Avtomatik yetkazishda xatolik!</b>\n\n"
                    "👤 Foydalanuvchi: %s (ID: %d)\n"
                    "🎁 Sovg'a: %s\n"
                    "🔗 Xabar havolasi: <code>%s</code>\n"
                    "⚠️ <b>Telegram Xatoligi:</b> <code>%s</code>\n\n"
                    "<i>Maslahat: Botingiz ushbu xabar olingan kanalda a'zo va admin ekanligini tekshiring!</i>"
                ) % (user["first_name"], tid, gift["name"], gift_content, err_desc)
                for aid in get_all_admin_ids():
                    tg_send_message(aid, admin_err_msg)
                
                db_update("redemptions", {"status": "pending"}, "id", redemption_id)
                user_msg = (
                    "🎁 <b>Sovg'aga almashtirish so'rovingiz qabul qilindi!</b>\n\n"
                    "• <b>Sovg'a:</b> %s\n"
                    "• <b>Sarflangan ball:</b> %d\n"
                    "• <b>Holati:</b> Kutilmoqda ⏳\n\n"
                    "Avtomatik yetkazishda texnik muammo bo'ldi. Adminlarimiz tez orada sovg'ani sizga yetkazishadi."
                ) % (gift["name"], gift["points_cost"])
                tg_send_message(tid, user_msg)
        elif gift_type == "canva_pro_link":
            view_token = issue_view_token(
                redemption_id=redemption_id,
                user_id=tid,
                gift_type=gift_type,
                max_views=5,
                ttl_seconds=600
            ) if redemption_id else None
            user_msg = (
                "🎁 <b>Canva Pro sovg'angiz tayyor!</b>\n\n"
                "• <b>Sovg'a:</b> %s\n"
                "• <b>Sarflangan ball:</b> %d\n"
                "• <b>Holati:</b> Faollashtirildi ✅\n\n"
                "🔒 Canva Pro havolasini ilovaning <b>«Sovg'alarim»</b> bo'limidan ko'rishingiz mumkin.\n"
                "Havola himoyalangan: uni nusxalab bo'lmaydi, faqat brauzerda ochish mumkin.\n"
                "Havola 10 daqiqa faol, 5 marta ko'rish mumkin."
            ) % (gift["name"], gift["points_cost"])
            tg_send_message(tid, user_msg)

            admin_msg = (
                "🚨 <b>Canva Pro sovg'asi biriktirildi!</b>\n\n"
                "👤 Foydalanuvchi: %s (ID: %d)\n"
                "🎁 Sovg'a: %s\n"
                "💰 Narxi: %d ball\n"
                "🔑 Biriktirilgan kod ID: %d\n"
                "📦 Redemption ID: <code>%s</code>"
            ) % (user["first_name"], tid, gift["name"], gift["points_cost"],
                 assigned_code_id or 0, redemption_id or "—")
        else:
            user_msg = (
                "🎁 <b>Sovg'aga almashtirish so'rovingiz qabul qilindi!</b>\n\n"
                "• <b>Sovg'a:</b> %s\n"
                "• <b>Sarflangan ball:</b> %d\n"
                "• <b>Holati:</b> Kutilmoqda ⏳\n\n"
                "Adminlarimiz tez orada so'rovni ko'rib chiqib, siz bilan bog'lanishadi."
            ) % (gift["name"], gift["points_cost"])
            tg_send_message(tid, user_msg)

            admin_msg = (
                "🚨 <b>Yangi sovg'a so'rovi!</b>\n\n"
                "👤 <b>Foydalanuvchi:</b> %s (ID: %d)\n"
                "🎁 <b>Sovg'a:</b> %s\n"
                "💰 <b>Narxi:</b> %d ball\n"
                "📝 <b>Holati:</b> Tasdiqlash kutilmoqda\n\n"
                "So'rov ID: <code>%s</code>"
            ) % (user["first_name"], tid, gift["name"], gift["points_cost"], redemption_id or "Noma'lum")

        for aid in get_all_admin_ids():
            tg_send_message(aid, admin_msg)

        response_data = {
            "success": True,
            "user": {"telegram_id": tid, "points": new_pts},
            "redemption_id": redemption_id
        }
        if gift_type == "canva_pro_link" and redemption_id:
            response_data["view_token"] = view_token
            response_data["has_view"] = True
        else:
            response_data["has_view"] = False

        return jsonify(response_data)
    except Exception:
        logger.exception("Redeem error")
        return jsonify({"success": False, "error": "So'rovni qayta ishlashda xatolik"}), 500

# ─── MY REDEMPTIONS (foydalanuvchining olgan sovg'alari) ─────────────

@app.route('/api/my-redemptions', methods=['POST', 'OPTIONS'])
def api_my_redemptions():
    if request.method == 'OPTIONS':
        return '', 200
    if is_rate_limited(request.remote_addr):
        return jsonify({"success": False, "error": "Tezlik cheklovi"}), 429

    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info:
        return jsonify({"success": False, "error": "Avtorizatsiya xatosi"}), 401
    tid = user_info.get("id")

    try:
        redemptions = db_select("redemptions", {
            "user_id": "eq." + str(tid),
            "order": "id.desc"
        })
        result = []
        for r in redemptions:
            try:
                gifts = db_select("gifts", {"id": "eq." + str(r["gift_id"])})
                if not gifts:
                    continue
                g = gifts[0]
                redemption_id = str(r["id"])
                gift_type = g.get("gift_type", "physical")

                view_token = None
                has_view = False
                if gift_type == "canva_pro_link":
                    view_token = issue_view_token(
                        redemption_id=redemption_id,
                        user_id=tid,
                        gift_type=gift_type,
                        max_views=5,
                        ttl_seconds=600
                    )
                    has_view = True

                result.append({
                    "id": redemption_id,
                    "gift_id": g["id"],
                    "gift_name": g["name"],
                    "gift_type": gift_type,
                    "gift_image_url": g.get("image_url", ""),
                    "status": r.get("status", "pending"),
                    "has_view": has_view,
                    "view_token": view_token
                })
            except Exception:
                logger.exception("Single redemption processing error")
                continue
        return jsonify({"success": True, "redemptions": result})
    except Exception:
        logger.exception("Error in /api/my-redemptions")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

# ─── PRO LINK (Asosiy ekrandagi "Pro olish" tugmasi uchun) ───────────

@app.route('/api/pro-link', methods=['POST', 'OPTIONS'])
def api_pro_link():
    """Pro olish tugmasi uchun maxfiy havola. Link faqat tg.openLink orqali ochiladi."""
    if request.method == 'OPTIONS':
        return '', 200
    if is_rate_limited(request.remote_addr):
        return jsonify({"success": False, "error": "Tezlik cheklovi"}), 429

    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info:
        return jsonify({"success": False, "error": "Avtorizatsiya xatosi"}), 401

    dynamic_pro_link = get_system_setting("pro_link", PRO_LINK)
    if not dynamic_pro_link:
        return jsonify({"success": False, "error": "Pro havola hozircha mavjud emas"}), 404

    return jsonify({"success": True, "link": dynamic_pro_link})

# ─── GIFT VIEW (protected content rendering) ─────────────────────────

@app.route('/api/gift-view', methods=['POST', 'OPTIONS'])
def api_gift_view():
    if request.method == 'OPTIONS':
        return '', 200
    if is_rate_limited(request.remote_addr, max_req=20):
        return jsonify({"success": False, "error": "Tezlik cheklovi"}), 429

    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info:
        return jsonify({"success": False, "error": "Avtorizatsiya xatosi"}), 401
    tid = user_info.get("id")
    view_token = data.get("view_token")
    redemption_id = data.get("redemption_id")

    if not view_token or not redemption_id:
        return jsonify({"success": False, "error": "Noto'g'ri so'rov"}), 400

    token_data, err = consume_view_token(view_token, tid)
    if err:
        return jsonify({"success": False, "error": err}), 403
    if token_data["redemption_id"] != str(redemption_id):
        return jsonify({"success": False, "error": "Token redemption_id mos kelmadi"}), 403

    try:
        redemptions = db_select("redemptions", {
            "id": "eq." + str(redemption_id), "user_id": "eq." + str(tid)
        })
        if not redemptions:
            return jsonify({"success": False, "error": "Redemption topilmadi"}), 404

        redemption = redemptions[0]
        gifts = db_select("gifts", {"id": "eq." + str(redemption["gift_id"])})
        if not gifts:
            return jsonify({"success": False, "error": "Sovg'a topilmadi"}), 404
        gift = gifts[0]
        gift_type = gift.get("gift_type", "physical")

        content = None
        if gift_type == "canva_pro_link":
            codes = db_select("canva_pro_codes", {
                "assigned_to": "eq." + str(tid),
                "gift_id": "eq." + str(redemption["gift_id"]),
                "is_used": "eq.true",
                "order": "id.desc", "limit": "1"
            })
            if codes:
                content = codes[0]["code_value"]

        if not content:
            return jsonify({"success": False, "error": "Kontent topilmadi"}), 404

        try:
            existing = db_select("gift_views", {
                "redemption_id": "eq." + str(redemption_id),
                "user_id": "eq." + str(tid)
            })
            if existing:
                db_update("gift_views", {
                    "view_count": (existing[0].get("view_count") or 0) + 1,
                    "last_viewed_at": "now()"
                }, "id", existing[0]["id"])
            else:
                db_insert("gift_views", {
                    "redemption_id": redemption_id, "user_id": tid,
                    "view_count": 1, "last_viewed_at": "now()"
                })
        except Exception:
            logger.warning("gift_views audit update failed (non-critical)")

        remaining_views = max(0, token_data["max_views"] - token_data["view_count"])
        return jsonify({
            "success": True,
            "gift_type": gift_type,
            "content": content,
            "gift_name": gift["name"],
            "remaining_views": remaining_views,
            "expires_in": int(token_data["expires_at"] - time.time())
        })
    except Exception:
        logger.exception("gift-view error")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

# ─── STATS ───────────────────────────────────────────────────────────

@app.route('/api/stats', methods=['POST', 'GET', 'OPTIONS'])
def api_stats():
    if request.method == 'OPTIONS':
        return '', 200
    
    # Authenticate via either X-Admin-Token or init_data
    is_admin = False
    token = request.headers.get("X-Admin-Token")
    if token and token == ADMIN_SECRET:
        is_admin = True
    else:
        # Check body/params for init_data
        init_data = None
        if request.method == 'POST':
            data = request.json or {}
            init_data = data.get("init_data")
        else:
            init_data = request.args.get("init_data")
            
        if init_data:
            user_info = validate_init_data(init_data)
            if user_info and is_user_admin(user_info.get("id")):
                is_admin = True
                
    if not is_admin:
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
        
    try:
        all_u = db_select("users", {"select": "telegram_id,points,is_active,referral_count,first_name"})
        active = [u for u in all_u if u.get("is_active")]
        total_pts = sum(u.get("points", 0) for u in all_u)
        top = max(all_u, key=lambda x: x.get("referral_count", 0), default={"first_name": "Yo'q", "referral_count": 0})
        return jsonify({
            "success": True, "total_users": len(all_u), "active_users": len(active),
            "total_points_distributed": total_pts,
            "top_referrer": {"name": top.get("first_name"), "referrals": top.get("referral_count")}
        })
    except Exception:
        logger.exception("Error in /api/stats")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

@app.route('/api/admin/grant', methods=['POST', 'OPTIONS'])
def api_admin_grant():
    if request.method == 'OPTIONS':
        return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
        
    target_id = data.get("target_id")
    amount = data.get("amount")
    if not target_id or amount is None:
        return jsonify({"success": False, "error": "Noto'g'ri ma'lumotlar"}), 400
        
    try:
        target_id = int(target_id)
        amount = int(amount)
        users = db_select("users", {"telegram_id": "eq." + str(target_id)})
        if not users:
            return jsonify({"success": False, "error": "Foydalanuvchi topilmadi"}), 404
        u = users[0]
        new_pts = u["points"] + amount
        db_update("users", {"points": new_pts}, "telegram_id", target_id)
        db_insert("transactions", {
            "user_id": target_id, "amount": amount,
            "type": "admin_grant",
            "description": "Admin (%d) tomonidan Mini App orqali" % user_info.get("id")
        })
        tg_send_message(target_id,
            "🎁 Hisobingizga <b>%d ball</b> qo'shildi!\n"
            "💰 Joriy balans: <b>%d ball</b>" % (amount, new_pts)
        )
        return jsonify({"success": True, "new_points": new_pts})
    except ValueError:
        return jsonify({"success": False, "error": "ID va miqdor son bo'lishi kerak"}), 400
    except Exception:
        logger.exception("API Grant error")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

@app.route('/api/admin/addcanva', methods=['POST', 'OPTIONS'])
def api_admin_addcanva():
    if request.method == 'OPTIONS':
        return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
        
    gift_id = data.get("gift_id")
    link = data.get("link", "").strip()
    if not gift_id or not link:
        return jsonify({"success": False, "error": "Noto'g'ri ma'lumotlar"}), 400
        
    if not (link.startswith("http://") or link.startswith("https://")):
        return jsonify({"success": False, "error": "Havola http:// yoki https:// bilan boshlanishi kerak"}), 400
        
    try:
        gift_id = int(gift_id)
        gifts = db_select("gifts", {"id": "eq." + str(gift_id)})
        if not gifts:
            return jsonify({"success": False, "error": "Bunday sovg'a topilmadi"}), 404
        db_insert("canva_pro_codes", {
            "gift_id": gift_id, "code_value": link, "is_used": False
        })
        return jsonify({"success": True})
    except ValueError:
        return jsonify({"success": False, "error": "Gift ID butun son bo'lishi kerak"}), 400
    except Exception:
        logger.exception("API AddCanva error")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

@app.route('/api/admin/broadcast', methods=['POST', 'OPTIONS'])
def api_admin_broadcast():
    if request.method == 'OPTIONS':
        return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
        
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"success": False, "error": "Xabar matni bo'sh bo'lishi mumkin emas"}), 400
        
    def run_broadcast():
        try:
            active_users = db_select("users", {"is_active": "eq.true", "select": "telegram_id"})
            if not active_users:
                return
            
            bid = None
            try:
                broadcast_rec = db_insert("broadcasts", {
                    "admin_id": user_info.get("id"),
                    "message_text": text,
                    "status": "running"
                })
                bid = broadcast_rec[0]["id"] if broadcast_rec else None
            except Exception:
                pass
                
            sent, failed = 0, 0
            for u in active_users:
                target_chat_id = u["telegram_id"]
                payload = {"chat_id": target_chat_id, "text": text, "parse_mode": "HTML"}
                result = tg_api("sendMessage", payload)
                if result.get("ok"):
                    sent += 1
                else:
                    failed += 1
                time.sleep(0.04)
                
            if bid:
                try:
                    db_update("broadcasts", {"sent_count": sent, "failed_count": failed, "status": "done"}, "id", bid)
                except Exception:
                    pass
                    
            tg_send_message(user_info.get("id"),
                "📢 <b>Mini App orqali yuborilgan reklama yakunlandi!</b>\n\n"
                "✅ Muvaffaqiyatli: <b>%d</b>\n"
                "❌ Muvaffaqiyatsiz: <b>%d</b>" % (sent, failed)
            )
        except Exception:
            logger.exception("Broadcast thread error")
            
    threading.Thread(target=run_broadcast, daemon=True).start()
    return jsonify({"success": True, "message": "Reklama yuborish fon rejimida boshlandi. Yakunlanganda sizga xabar yuboriladi."})

# ─── ADMIN MANAGEMENT ────────────────────────────────────────────────
@app.route('/api/admin/admins/add', methods=['POST', 'OPTIONS'])
def api_admin_add():
    if request.method == 'OPTIONS': return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
    target_id = data.get("target_id")
    if not target_id:
        return jsonify({"success": False, "error": "Foydalanuvchi IDsi talab qilinadi"}), 400
    try:
        target_id = int(target_id)
        users = db_select("users", {"telegram_id": "eq." + str(target_id)})
        if not users:
            return jsonify({"success": False, "error": "Foydalanuvchi topilmadi"}), 404
        
        db_insert("admins", {
            "telegram_id": target_id,
            "added_by": user_info.get("id")
        })
        return jsonify({"success": True, "message": "Foydalanuvchi muvaffaqiyatli admin qilindi"})
    except Exception as e:
        if "duplicate key" in str(e).lower() or "409" in str(e):
            return jsonify({"success": False, "error": "Bu foydalanuvchi allaqachon admin"}), 400
        logger.exception("Error adding admin")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

@app.route('/api/admin/admins/remove', methods=['POST', 'OPTIONS'])
def api_admin_remove():
    if request.method == 'OPTIONS': return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
    target_id = data.get("target_id")
    if not target_id:
        return jsonify({"success": False, "error": "Foydalanuvchi IDsi talab qilinadi"}), 400
    try:
        target_id = int(target_id)
        if target_id in ADMIN_IDS:
            return jsonify({"success": False, "error": "Asosiy adminni o'chirib bo'lmaydi"}), 400
        
        db_delete("admins", "telegram_id", target_id)
        return jsonify({"success": True, "message": "Foydalanuvchi adminlikdan o'chirildi"})
    except Exception:
        logger.exception("Error removing admin")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

@app.route('/api/admin/admins/list', methods=['POST', 'OPTIONS'])
def api_admin_list():
    if request.method == 'OPTIONS': return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
    try:
        admin_ids = get_all_admin_ids()
        result = []
        for aid in admin_ids:
            users = db_select("users", {"telegram_id": "eq." + str(aid)})
            u = users[0] if users else {"telegram_id": aid, "first_name": "Asosiy Admin", "username": None}
            result.append({
                "telegram_id": u["telegram_id"],
                "first_name": u.get("first_name"),
                "username": u.get("username"),
                "is_super": aid in ADMIN_IDS
            })
        return jsonify({"success": True, "admins": result})
    except Exception:
        logger.exception("Error listing admins")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

# ─── GIFTS CRUD ──────────────────────────────────────────────────────
@app.route('/api/admin/gifts/add', methods=['POST', 'OPTIONS'])
def api_admin_gift_add():
    if request.method == 'OPTIONS': return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
    
    gift_data = data.get("gift")
    if not gift_data or not gift_data.get("name") or not gift_data.get("points_cost"):
        return jsonify({"success": False, "error": "Noto'g'ri ma'lumotlar"}), 400
        
    try:
        new_gift = {
            "name": gift_data["name"].strip(),
            "description": gift_data.get("description", "").strip(),
            "points_cost": int(gift_data["points_cost"]),
            "stock": int(gift_data.get("stock", -1)),
            "gift_type": gift_data.get("gift_type", "physical"),
            "accent_color": gift_data.get("accent_color", "#0A84FF"),
            "category": gift_data.get("category", "general"),
            "image_url": gift_data.get("image_url", "").strip(),
            "gift_content": gift_data.get("gift_content", "").strip(),
            "is_active": True
        }
        res = db_insert("gifts", new_gift)
        return jsonify({"success": True, "gift": res[0] if res else new_gift})
    except Exception:
        logger.exception("Error adding gift")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

@app.route('/api/admin/gifts/edit', methods=['POST', 'OPTIONS'])
def api_admin_gift_edit():
    if request.method == 'OPTIONS': return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
    
    gift_data = data.get("gift")
    if not gift_data or not gift_data.get("id") or not gift_data.get("name"):
        return jsonify({"success": False, "error": "Noto'g'ri ma'lumotlar"}), 400
        
    try:
        gift_id = int(gift_data["id"])
        updated_fields = {
            "name": gift_data["name"].strip(),
            "description": gift_data.get("description", "").strip(),
            "points_cost": int(gift_data["points_cost"]),
            "stock": int(gift_data.get("stock", -1)),
            "gift_type": gift_data.get("gift_type", "physical"),
            "accent_color": gift_data.get("accent_color", "#0A84FF"),
            "category": gift_data.get("category", "general"),
            "image_url": gift_data.get("image_url", "").strip(),
            "gift_content": gift_data.get("gift_content", "").strip()
        }
        res = db_update("gifts", updated_fields, "id", gift_id)
        return jsonify({"success": True, "gift": res[0] if res else updated_fields})
    except Exception:
        logger.exception("Error editing gift")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

@app.route('/api/admin/gifts/delete', methods=['POST', 'OPTIONS'])
def api_admin_gift_delete():
    if request.method == 'OPTIONS': return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
    
    gift_id = data.get("gift_id")
    if not gift_id:
        return jsonify({"success": False, "error": "Gift ID talab qilinadi"}), 400
        
    try:
        gift_id = int(gift_id)
        db_update("gifts", {"is_active": False}, "id", gift_id)
        return jsonify({"success": True, "message": "Sovg'a o'chirildi (arxivlandi)"})
    except Exception:
        logger.exception("Error deleting gift")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

# ─── USER SEARCH & MANAGEMENT ────────────────────────────────────────
@app.route('/api/admin/users/search', methods=['POST', 'OPTIONS'])
def api_admin_user_search():
    if request.method == 'OPTIONS': return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
    
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"success": False, "error": "Qidiruv matni bo'sh"}), 400
        
    try:
        is_num = False
        try:
            int(query)
            is_num = True
        except ValueError:
            pass
            
        if is_num:
            params = {
                "or": f"(telegram_id.eq.{query},username.ilike.*{query}*,first_name.ilike.*{query}*)"
            }
        else:
            params = {
                "or": f"(username.ilike.*{query}*,first_name.ilike.*{query}*)"
            }
            
        users = db_select("users", params)
        return jsonify({"success": True, "users": users or []})
    except Exception:
        logger.exception("Error searching users")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

@app.route('/api/admin/users/update', methods=['POST', 'OPTIONS'])
def api_admin_user_update():
    if request.method == 'OPTIONS': return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
    
    target_id = data.get("target_id")
    if not target_id:
        return jsonify({"success": False, "error": "Foydalanuvchi IDsi talab qilinadi"}), 400
        
    try:
        target_id = int(target_id)
        users = db_select("users", {"telegram_id": "eq." + str(target_id)})
        if not users:
            return jsonify({"success": False, "error": "Foydalanuvchi topilmadi"}), 404
        u = users[0]
        
        updated_fields = {}
        if "points" in data:
            updated_fields["points"] = int(data["points"])
            diff = updated_fields["points"] - u["points"]
            if diff != 0:
                db_insert("transactions", {
                    "user_id": target_id, "amount": diff,
                    "type": "admin_adjustment",
                    "description": f"Admin ({user_info.get('id')}) tomonidan o'zgartirildi"
                })
                tg_send_message(target_id, f"📝 Balansingiz admin tomonidan o'zgartirildi. Yangi balans: <b>{updated_fields['points']} ball</b>")
                
        if "is_banned" in data:
            updated_fields["is_banned"] = bool(data["is_banned"])
            
        if updated_fields:
            res = db_update("users", updated_fields, "telegram_id", target_id)
            u = res[0] if res else u
            
        return jsonify({"success": True, "user": u})
    except Exception:
        logger.exception("Error updating user")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

@app.route('/api/admin/users/delete', methods=['POST', 'OPTIONS'])
def api_admin_delete_user():
    if request.method == 'OPTIONS':
        return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Ruxsat etilmadi"}), 403
    
    target_id = data.get("target_id")
    if not target_id:
        return jsonify({"success": False, "error": "Target ID kiritilmadi"}), 400
    
    try:
        target_id = int(target_id)
        
        # 1. Delete from child tables first to avoid 409 Foreign Key Conflict
        for table, col in [
            ("referral_events", "referrer_id"),
            ("referral_events", "referred_id"),
            ("transactions", "user_id"),
            ("redemptions", "user_id"),
            ("gift_views", "user_id")
        ]:
            try:
                db_delete(table, col, target_id)
            except Exception as ex:
                logger.warning("Failed to delete from %s (%s=%d): %s", table, col, target_id, ex)
        
        # 2. Finally delete from the users table
        db_delete("users", "telegram_id", target_id)
        
        logger.info(f"User {target_id} deleted by admin {user_info.get('id')}")
        return jsonify({"success": True, "message": "Foydalanuvchi muvaffaqiyatli o'chirildi va reytingdan olib tashlandi."})
    except Exception as e:
        logger.exception("Error deleting user")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/settings', methods=['POST', 'OPTIONS'])
def api_admin_get_settings():
    if request.method == 'OPTIONS':
        return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
    
    return jsonify({
        "success": True,
        "settings": {
            "pro_link": get_system_setting("pro_link", PRO_LINK)
        }
    })

@app.route('/api/admin/settings/update', methods=['POST', 'OPTIONS'])
def api_admin_update_settings():
    if request.method == 'OPTIONS':
        return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
    
    new_settings = data.get("settings", {})
    if "pro_link" in new_settings:
        set_system_setting("pro_link", new_settings["pro_link"].strip())
        
    return jsonify({
        "success": True,
        "message": "Tizim sozlamalari muvaffaqiyatli saqlandi!"
    })

@app.route('/api/admin/users/transactions', methods=['POST', 'OPTIONS'])
def api_admin_user_transactions():
    if request.method == 'OPTIONS': return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
        
    target_id = data.get("target_id")
    if not target_id:
        return jsonify({"success": False, "error": "Foydalanuvchi IDsi talab qilinadi"}), 400
        
    try:
        target_id = int(target_id)
        
        # 1. Fetch transactions
        txs = db_select("transactions", {
            "user_id": "eq." + str(target_id),
            "order": "created_at.desc",
            "limit": "50"
        })
        
        # 2. Fetch who referred this user
        referred_by_user = None
        users_self = db_select("users", {"telegram_id": "eq." + str(target_id)})
        if users_self:
            ref_id = users_self[0].get("referred_by")
            if ref_id:
                ref_users = db_select("users", {"telegram_id": "eq." + str(ref_id)})
                if ref_users:
                    referred_by_user = {
                        "telegram_id": ref_users[0]["telegram_id"],
                        "first_name": ref_users[0].get("first_name"),
                        "username": ref_users[0].get("username"),
                        "points": ref_users[0].get("points"),
                        "is_banned": ref_users[0].get("is_banned")
                    }
                    
        # 3. Fetch referrals (users referred by this user)
        referrals = db_select("users", {
            "referred_by": "eq." + str(target_id),
            "order": "created_at.desc"
        })
        referrals_list = []
        for r in (referrals or []):
            referrals_list.append({
                "telegram_id": r["telegram_id"],
                "first_name": r.get("first_name"),
                "username": r.get("username"),
                "points": r.get("points"),
                "is_active": r.get("is_active"),
                "is_banned": r.get("is_banned"),
                "created_at": r.get("created_at")
            })
            
        return jsonify({
            "success": True, 
            "transactions": txs or [],
            "referred_by_user": referred_by_user,
            "referrals": referrals_list
        })
    except Exception:
        logger.exception("Error fetching user transactions/details")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

# ─── MANUAL BACKUP ───────────────────────────────────────────────────
@app.route('/api/admin/backup', methods=['POST', 'OPTIONS'])
def api_admin_backup():
    if request.method == 'OPTIONS': return '', 200
    data = request.json or {}
    user_info = validate_init_data(data.get("init_data"))
    if not user_info or not is_user_admin(user_info.get("id")):
        return jsonify({"success": False, "error": "Taqiqlangan"}), 403
        
    try:
        success, res = perform_database_backup(user_info.get("id"))
        if success:
            return jsonify({"success": True, "message": f"Zaxira nusxasi muvaffaqiyatli olindi va sizga Telegram orqali yuborildi. Fayl: {res}"})
        else:
            return jsonify({"success": False, "error": f"Backup xatoligi: {res}"}), 500
    except Exception:
        logger.exception("Error in backup endpoint")
        return jsonify({"success": False, "error": "Server xatosi"}), 500

# ─── HEALTH ──────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "ok": True,
        "ts": time.time(),
userbot_init_error = None
userbot_start_error = None

@app.route('/api/debug/userbot', methods=['GET'])
def api_debug_userbot():
    global userbot_client, userbot_init_error, userbot_start_error
    status = {
        "env_present": {
            "auth_key": bool(os.getenv("USERBOT_AUTH_KEY_HEX")),
            "dc_id": bool(os.getenv("USERBOT_DC_ID")),
            "port": bool(os.getenv("USERBOT_PORT")),
            "server_address": bool(os.getenv("USERBOT_SERVER_ADDRESS"))
        },
        "client_initialized": userbot_client is not None,
        "init_error": userbot_init_error,
        "start_error": userbot_start_error
    }
    if userbot_client:
        try:
            status["is_connected"] = userbot_client.is_connected()
            if userbot_client.is_connected():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                me = loop.run_until_complete(userbot_client.get_me())
                status["me"] = f"{me.first_name} (@{me.username})" if me else "None"
        except Exception as e:
            status["error"] = str(e)
    return jsonify(status), 200

# ─── USERBOT (TELETHON) INTEGRATION ───────────────────────────────────
import sqlite3
import asyncio

USERBOT_API_ID = 37593868
USERBOT_API_HASH = "f66d341a320b19d10e3b53c1408c1f29"
userbot_client = None

def init_telethon_session():
    global userbot_init_error
    auth_key_hex = os.getenv("USERBOT_AUTH_KEY_HEX")
    dc_id = os.getenv("USERBOT_DC_ID")
    port = os.getenv("USERBOT_PORT")
    server_address = os.getenv("USERBOT_SERVER_ADDRESS")

    if not auth_key_hex or not dc_id or not port or not server_address:
        logger.info("Userbot muhit o'zgaruvchilari sozlanmagan. Userbot o'tkazib yuborildi.")
        return False

    server_address = server_address.strip('"\'')
    session_file = "userbot.session"
    
    try:
        conn = sqlite3.connect(session_file)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS version (version INTEGER)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                dc_id INTEGER PRIMARY KEY,
                server_address TEXT,
                port INTEGER,
                auth_key BLOB,
                takeout_id INTEGER
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY,
                hash INTEGER,
                username TEXT,
                phone TEXT,
                name TEXT
            )
        """)
        c.execute("DELETE FROM version")
        c.execute("INSERT INTO version VALUES (7)")
        auth_key_bytes = bytes.fromhex(auth_key_hex)
        c.execute("DELETE FROM sessions")
        c.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, NULL)", 
                  (int(dc_id), server_address, int(port), auth_key_bytes))
        conn.commit()
        conn.close()
        logger.info("Telethon session fayli 'userbot.session' muvaffaqiyatli yaratildi!")
        return True
    except Exception as e:
        userbot_init_error = f"Session init error: {str(e)}"
        logger.error("Telethon session yaratishda xatolik: %s", e)
        return False

def start_userbot_thread():
    global userbot_client, userbot_init_error, userbot_start_error
    if not init_telethon_session():
        return
        
    try:
        from telethon import TelegramClient, events
        
        async def run_client():
            global userbot_client, userbot_start_error
            logger.info("Telethon Userbot Render-da ishga tushmoqda...")
            try:
                # Create the client inside the thread's event loop
                userbot_client = TelegramClient("userbot", USERBOT_API_ID, USERBOT_API_HASH)
                
                @userbot_client.on(events.NewMessage(pattern='/userbot_ping'))
                async def handler(event):
                    await event.respond("Userbot Render-da faol! 🚀")
                    
                await userbot_client.connect()
                if not await userbot_client.is_user_authorized():
                    userbot_start_error = "Session is NOT authorized! Telegram rejected the session."
                    logger.error("Userbot session is not authorized!")
                else:
                    logger.info("✅ Telethon Userbot muvaffaqiyatli ishga tushdi va Render-da ulandi!")
                    await userbot_client.run_until_disconnected()
            except Exception as e:
                userbot_start_error = f"Run client error: {str(e)}"
                logger.error("Error in run_client: %s", e)
            
        def loop_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run_client())
            
        t = threading.Thread(target=loop_thread, daemon=True)
        t.start()
    except Exception as e:
        userbot_init_error = f"Thread start error: {str(e)}"
        logger.error("Telethon Userbot-ni ishga tushirishda xatolik: %s", e)

# ─── STARTUP ─────────────────────────────────────────────────────────

def setup_webhook():
    """Webhook URL berilgan bo'lsa, webhook o'rnatamiz."""
    if not WEBHOOK_URL:
        return False
    url = WEBHOOK_URL + "/webhook"
    result = tg_api("setWebhook", {
        "url": url,
        "secret_token": WEBHOOK_SECRET if WEBHOOK_SECRET else None,
        "allowed_updates": ["message", "callback_query", "chat_member"],
        "drop_pending_updates": True
    })
    if result.get("ok"):
        logger.info("Webhook o'rnatildi: %s", url)
        return True
    logger.error("Webhook o'rnatilmadi: %s", result)
    return False

def start_polling_thread():
    """Alohida thread'da polling boshlash."""
    # Avval webhook'ni o'chirib qo'yamiz (agar bo'lsa)
    try:
        tg_api("deleteWebhook", {"drop_pending_updates": False})
        logger.info("Eski webhook o'chirildi (polling rejimiga o'tildi)")
    except Exception:
        pass

    t = threading.Thread(target=poll_updates, daemon=True)
    t.start()
    return t

def initialize_app():
    # Webhook yoki polling rejimini tanlash
    if WEBHOOK_URL:
        if setup_webhook():
            logger.info("WEBHOOK rejimida ishlamoqda: %s", WEBHOOK_URL)
        else:
            logger.warning("Webhook o'rnatilmadi, POLLING rejimiga o'tildi")
            start_polling_thread()
    else:
        logger.info("POLLING rejimida ishlamoqda (WEBHOOK_URL berilmagan)")
        start_polling_thread()

    # Eski webhook'larni ham tozalaymiz (agar WEBHOOK_URL berilmagan bo'lsa)
    if not WEBHOOK_URL:
        try:
            tg_api("deleteWebhook", {})
        except Exception:
            pass

    # Start backup scheduler thread
    threading.Thread(target=backup_scheduler_loop, daemon=True).start()

    # Start Telethon Userbot thread
    start_userbot_thread()

# Run initialization when imported (e.g. by gunicorn)
initialize_app()

if __name__ == '__main__':
    port = int(os.getenv("PORT", "5000"))
    app.run(host='0.0.0.0', port=port, debug=False)
