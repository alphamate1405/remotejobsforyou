import os
import sqlite3
import hmac
import hashlib
import json
import logging
import socket
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "@your_channel")
DODO_API_KEY        = os.getenv("DODO_API_KEY", "YOUR_DODO_API_KEY")
DODO_PRODUCT_ID     = os.getenv("DODO_PRODUCT_ID", "YOUR_DODO_PRODUCT_ID")
DODO_WEBHOOK_SECRET = os.getenv("DODO_WEBHOOK_SECRET", "YOUR_WEBHOOK_SECRET")
WEBHOOK_PORT        = int(os.getenv("WEBHOOK_PORT", "8080"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot_app = None
DB_PATH = "subscribers.db"

# ── FORCE GOOGLE DNS FOR DODO ─────────────────────────────────────────────────
_original_getaddrinfo = socket.getaddrinfo

def _patched_getaddrinfo(host, port, *args, **kwargs):
    if host == "api.dodopayments.com":
        try:
            import urllib.request
            # resolve using Google DNS over HTTPS
            url = f"https://dns.google/resolve?name={host}&type=A"
            req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            ip = data["Answer"][0]["data"]
            log.info(f"Resolved {host} via Google DoH -> {ip}")
            return _original_getaddrinfo(ip, port, *args, **kwargs)
        except Exception as e:
            log.error(f"Google DoH resolution failed: {e}")
    return _original_getaddrinfo(host, port, *args, **kwargs)

socket.getaddrinfo = _patched_getaddrinfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


def test_network():
    log.info("=== NETWORK DIAGNOSTICS ===")
    for host in ["api.dodopayments.com", "api.telegram.org", "google.com"]:
        try:
            ip = socket.gethostbyname(host)
            log.info(f"DNS OK: {host} -> {ip}")
        except Exception as e:
            log.error(f"DNS FAILED: {host} -> {e}")
    log.info("=== END DIAGNOSTICS ===")


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS subscribers ("
        "telegram_id INTEGER PRIMARY KEY, "
        "username TEXT, "
        "subscription_id TEXT, "
        "status TEXT DEFAULT 'pending', "
        "email TEXT, "
        "joined_at TEXT, "
        "expires_at TEXT)"
    )
    con.commit()
    con.close()


def upsert_subscriber(telegram_id, username=None, subscription_id=None,
                      status=None, email=None, expires_at=None):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO subscribers (telegram_id, username, subscription_id, status, email, joined_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET "
        "username=COALESCE(excluded.username, username), "
        "subscription_id=COALESCE(excluded.subscription_id, subscription_id), "
        "status=COALESCE(excluded.status, status), "
        "email=COALESCE(excluded.email, email), "
        "expires_at=COALESCE(excluded.expires_at, expires_at)",
        (telegram_id, username, subscription_id, status, email,
         datetime.now(timezone.utc).isoformat(), expires_at)
    )
    con.commit()
    con.close()


def get_subscriber_by_sub_id(subscription_id):
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT * FROM subscribers WHERE subscription_id=?", (subscription_id,)).fetchone()
    con.close()
    return row


def set_status(subscription_id, status, expires_at=None):
    con = sqlite3.connect(DB_PATH)
    if expires_at:
        con.execute("UPDATE subscribers SET status=?, expires_at=? WHERE subscription_id=?",
                    (status, expires_at, subscription_id))
    else:
        con.execute("UPDATE subscribers SET status=? WHERE subscription_id=?",
                    (status, subscription_id))
    con.commit()
    con.close()


DODO_BASE = "https://api.dodopayments.com"


def create_payment_link(telegram_id, username):
    resp = requests.post(
        f"{DODO_BASE}/subscriptions",
        headers={"Authorization": f"Bearer {DODO_API_KEY}", "Content-Type": "application/json"},
        json={
            "product_id": DODO_PRODUCT_ID,
            "quantity": 1,
            "payment_link": True,
            "metadata": {"telegram_id": str(telegram_id), "telegram_username": username or ""},
            "return_url": "https://t.me/" + TELEGRAM_CHANNEL_ID.lstrip("@")
        },
        timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("payment_link") or data.get("url", "")


def verify_webhook_signature(raw_body, signature):
    expected = hmac.new(DODO_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.split("=")[-1])


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = (
        f"👋 Welcome, {user.first_name}!\n\n"
        "🔔 *Jobs Channel Subscription*\n\n"
        "Get access to *hand-picked job opportunities* every day.\n\n"
        "💰 *Price:* Rs.499/month (recurring)\n"
        "✅ Cancel anytime\n\n"
        "Tap below to subscribe:"
    )
    try:
        link = create_payment_link(user.id, user.username or user.first_name)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Subscribe Now — Rs.499/month", url=link)]])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=keyboard)
        upsert_subscriber(user.id, user.username, status="pending")
    except Exception as e:
        log.error(f"Error creating payment link: {e}")
        await update.message.reply_text("Sorry, something went wrong. Please try again in a moment.")


async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT status, expires_at FROM subscribers WHERE telegram_id=?", (user.id,)).fetchone()
    con.close()
    if not row:
        await update.message.reply_text("No subscription found. Use /start to subscribe!")
    elif row[0] == "active":
        await update.message.reply_text(f"✅ Subscription *active*!\nRenews: {row[1] or 'auto'}", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Status: *{row[0]}*\n\nUse /start to resubscribe.", parse_mode="Markdown")


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Jobs Channel Bot*\n\n/start — Subscribe\n/status — Check subscription\n/help — Help",
        parse_mode="Markdown"
    )


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log.info(f"Webhook: {format % args}")

    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        sig = self.headers.get("dodo-signature", "")
        if DODO_WEBHOOK_SECRET != "YOUR_WEBHOOK_SECRET":
            if not verify_webhook_signature(raw, sig):
                log.warning("Bad webhook signature")
                self.send_response(401)
                self.end_headers()
                return
        try:
            handle_dodo_event(json.loads(raw))
        except Exception as e:
            log.error(f"Webhook error: {e}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"received":true}')


def handle_dodo_event(event):
    etype  = event.get("type", "")
    data   = event.get("data", {})
    sub_id = data.get("subscription_id") or data.get("id")
    meta   = data.get("metadata", {})
    telegram_id = int(meta["telegram_id"]) if meta.get("telegram_id") else None
    log.info(f"Dodo event: {etype} | sub={sub_id} | tg={telegram_id}")

    if etype == "subscription.active":
        if telegram_id:
            upsert_subscriber(telegram_id, meta.get("telegram_username"),
                              subscription_id=sub_id, status="active",
                              email=data.get("customer", {}).get("email"),
                              expires_at=data.get("next_billing_date"))
            _send_message(telegram_id, "🎉 *Payment successful!*\n\nYou now have access. Your subscription renews monthly.")
            _create_invite_and_send(telegram_id)
    elif etype == "subscription.renewed":
        if sub_id:
            set_status(sub_id, "active", data.get("next_billing_date"))
            if telegram_id:
                _send_message(telegram_id, "✅ Subscription renewed! Enjoy continued access.")
    elif etype == "subscription.updated":
        if sub_id:
            set_status(sub_id, data.get("status", "active"), data.get("next_billing_date"))
    elif etype == "subscription.plan_changed":
        if sub_id:
            set_status(sub_id, "active", data.get("next_billing_date"))
            if telegram_id:
                _send_message(telegram_id, "🔄 Plan updated. You still have full access!")
    elif etype == "subscription.on_hold":
        if sub_id:
            set_status(sub_id, "on_hold")
            row = get_subscriber_by_sub_id(sub_id)
            if row:
                _send_message(row[0], "⚠️ Subscription *on hold*. Please update your payment method.")
    elif etype == "subscription.failed":
        if sub_id:
            set_status(sub_id, "failed")
            row = get_subscriber_by_sub_id(sub_id)
            if row:
                _send_message(row[0], "❌ Payment *failed*. Use /start to resubscribe.")
    elif etype in ("subscription.cancelled", "subscription.expired"):
        if sub_id:
            set_status(sub_id, etype.split(".")[1])
            row = get_subscriber_by_sub_id(sub_id)
            if row:
                _kick_from_channel(row[0])
                _send_message(row[0], "😔 Subscription ended. You have been removed.\n\nResubscribe anytime with /start")


def _send_message(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.error(f"Send message error: {e}")


def _create_invite_and_send(telegram_id):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/createChatInviteLink",
            json={"chat_id": TELEGRAM_CHANNEL_ID, "member_limit": 1, "name": f"sub_{telegram_id}"},
            timeout=10
        )
        invite = resp.json()["result"]["invite_link"]
        _send_message(telegram_id, f"🔗 Your invite link:\n{invite}\n\n_Do not share this link._")
    except Exception as e:
        log.error(f"Invite link error: {e}")


def _kick_from_channel(telegram_id):
    for method in ("banChatMember", "unbanChatMember"):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
                json={"chat_id": TELEGRAM_CHANNEL_ID, "user_id": telegram_id},
                timeout=10
            )
        except Exception as e:
            log.error(f"{method} error: {e}")


def start_webhook_server():
    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    log.info(f"Webhook server on port {WEBHOOK_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    init_db()
    test_network()
    t = threading.Thread(target=start_webhook_server, daemon=True)
    t.start()
    bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("status", status_cmd))
    bot_app.add_handler(CommandHandler("help", help_cmd))
    log.info("Bot is running.")
    bot_app.run_polling()
