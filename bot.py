import os
import sqlite3
import hmac
import hashlib
import json
import logging
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import urllib.request

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "@your_channel")
DODO_API_KEY        = os.getenv("DODO_API_KEY", "YOUR_DODO_API_KEY")
DODO_PRODUCT_ID     = os.getenv("DODO_PRODUCT_ID", "YOUR_DODO_PRODUCT_ID")
DODO_WEBHOOK_SECRET = os.getenv("DODO_WEBHOOK_SECRET", "YOUR_WEBHOOK_SECRET")
WEBHOOK_PORT        = int(os.getenv("WEBHOOK_PORT", "8080"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot_app = None

# ── DATABASE ──────────────────────────────────────────────────────────────────
DB_PATH = "subscribers.db"

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            telegram_id     INTEGER PRIMARY KEY,
            username        TEXT,
            subscription_id TEXT,
            status          TEXT DEFAULT 'pending',
            email           TEXT,
            joined_at       TEXT,
            expires_at      TEXT
        )
    """)
    con.commit()
    con.close()

def upsert_subscriber(telegram_id, username=None, subscription_id=None,
                      status=None, email=None, expires_at=None):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO subscribers (telegram_id, username, subscription_id, status, email, joined_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username        = COALESCE(excluded.username, username),
            subscription_id = COALESCE(excluded.subscription_id, subscription_id),
            status          = COALESCE(excluded.status, status),
            email           = COALESCE(excluded.email, email),
            expires_at      = COALESCE(excluded.expires_at, expires_at)
    """, (telegram_id, username, subscription_id, status, email,
          datetime.now(timezone.utc).isoformat(), expires_at))
    con.commit()
    con.close()

def get_subscriber_by_sub_id(subscription_id):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT * FROM subscribers WHERE subscription_id = ?", (subscription_id,)
    ).fetchone()
    con.close()
    return row

def set_status(subscription_id, status, expires_at=None):
    con = sqlite3.connect(DB_PATH)
    if expires_at:
        con.execute(
            "UPDATE subscribers SET status=?, expires_at=? WHERE subscription_id=?",
            (status, expires_at, subscription_id)
        )
    else:
        con.execute(
            "UPDATE subscribers SET status=? WHERE subscription_id=?",
            (status, subscription_id)
        )
    con.commit()
    con.close()

# ── DODO PAYMENTS ─────────────────────────────────────────────────────────────
DODO_BASE = "https://api.dodopayments.com"

def create_payment_link(telegram_id, username):
    url = f"{DODO_BASE}/subscriptions"
    payload = json.dumps({
        "product_id": DODO_PRODUCT_ID,
        "quantity": 1,
        "payment_link": True,
        "metadata": {
            "telegram_id": str(telegram_id),
            "telegram_username": username or ""
        },
        "return_url": "https://t.me/" + TELEGRAM_CHANNEL_ID.lstrip("@")
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Authorization": f"Bearer {DODO_API_KEY}", "Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data.get("payment_link") or data.get("url", "")

def verify_webhook_signature(raw_body, signature):
    expected = hmac.new(
        DODO_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.split("=")[-1])

# ── TELEGRAM COMMANDS ─────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = (
        f"👋 Welcome, {user.first_name}!\n\n"
        "🔔 *Jobs Channel Subscription*\n\n"
        "Get access to *hand-picked job opportunities* delivered directly to your Telegram every day.\n\n"
        "💰 *Price:* ₹499/month (recurring)\n"
        "✅ Cancel anytime\n\n"
        "Tap the button below to subscribe:"
    )
    try:
        link = create_payment_link(user.id, user.username or user.first_name)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💳 Subscribe Now — ₹499/month", url=link)
        ]])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=keyboard)
        upsert_subscriber(user.id, user.username, status="pending")
    except Exception as e:
        log.error(f"Error creating payment link: {e}")
        await update.message.reply_text(
            "Sorry, something went wrong. Please try again in a moment."
        )

async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT status, expires_at FROM subscribers WHERE telegram_id=?", (user.id,)
    ).fetchone()
    con.close()
    if not row:
        await update.message.reply_text("You don't have a subscription yet. Use /start to subscribe!")
    elif row[0] == "active":
        await update.message.reply_text(
            f"✅ Your subscription is *active*!\nRenews on: {row[1] or 'auto-renew'}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ Subscription status: *{row[0]}*\n\nUse /start to resubscribe.",
            parse_mode="Markdown"
        )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Jobs Channel Bot*\n\n"
        "/start — Subscribe to the channel\n"
        "/status — Check your subscription\n"
        "/help — Show this message",
        parse_mode="Markdown"
    )

# ── WEBHOOK SERVER ────────────────────────────────────────────────────────────
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
                log.warning("Invalid webhook signature — rejected")
                self.send_response(401)
                self.end_headers()
                return

        try:
            event = json.loads(raw)
            handle_dodo_event(event)
        except Exception as e:
            log.error(f"Webhook handling error: {e}")

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
        # Fired when a new subscription is created and payment succeeds
        if telegram_id:
            upsert_subscriber(
                telegram_id,
                meta.get("telegram_username"),
                subscription_id=sub_id,
                status="active",
                email=data.get("customer", {}).get("email"),
                expires_at=data.get("next_billing_date")
            )
            _send_message(telegram_id,
                "🎉 *Payment successful!*\n\n"
                "You now have full access to the Jobs Channel.\n"
                "Your subscription renews automatically every month."
            )
            _create_invite_and_send(telegram_id)

    elif etype == "subscription.renewed":
        # Fired on every successful recurring charge
        if sub_id:
            set_status(sub_id, "active", data.get("next_billing_date"))
            if telegram_id:
                _send_message(telegram_id,
                    "✅ Your subscription has been renewed! Enjoy continued access."
                )

    elif etype == "subscription.updated":
        # General update — sync status in case it changed
        if sub_id:
            new_status = data.get("status", "active")
            set_status(sub_id, new_status, data.get("next_billing_date"))

    elif etype == "subscription.plan_changed":
        # Plan was upgraded/downgraded — keep them active
        if sub_id:
            set_status(sub_id, "active", data.get("next_billing_date"))
            if telegram_id:
                _send_message(telegram_id,
                    "🔄 Your subscription plan has been updated. You still have full access!"
                )

    elif etype == "subscription.on_hold":
        # Payment issue — warn user but don't kick yet
        if sub_id:
            set_status(sub_id, "on_hold")
            row = get_subscriber_by_sub_id(sub_id)
            if row:
                _send_message(row[0],
                    "⚠️ Your subscription is *on hold* due to a payment issue.\n\n"
                    "Please update your payment method soon or you'll lose access.\n"
                    "Use /start to get a new payment link.",
                )

    elif etype == "subscription.failed":
        # Payment failed — warn user
        if sub_id:
            set_status(sub_id, "failed")
            row = get_subscriber_by_sub_id(sub_id)
            if row:
                _send_message(row[0],
                    "❌ Your subscription payment *failed*.\n\n"
                    "Please resubscribe to keep access to the Jobs Channel.\n"
                    "Use /start to get a new payment link."
                )

    elif etype in ("subscription.cancelled", "subscription.expired"):
        # Kick user from channel
        if sub_id:
            set_status(sub_id, etype.split(".")[1])
            row = get_subscriber_by_sub_id(sub_id)
            if row:
                tg_id = row[0]
                _kick_from_channel(tg_id)
                _send_message(tg_id,
                    "😔 Your subscription has ended and you've been removed from the channel.\n\n"
                    "You can resubscribe anytime with /start"
                )

def _send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req)
    except Exception as e:
        log.error(f"Send message error: {e}")

def _create_invite_and_send(telegram_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/createChatInviteLink"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHANNEL_ID,
        "member_limit": 1,
        "name": f"sub_{telegram_id}"
    }).encode()
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        invite = data["result"]["invite_link"]
        _send_message(telegram_id,
            f"🔗 Your private invite link:\n{invite}\n\n"
            "_This link is for you only — do not share it._"
        )
    except Exception as e:
        log.error(f"Invite link error: {e}")

def _kick_from_channel(telegram_id):
    for method in ("banChatMember", "unbanChatMember"):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
        payload = json.dumps({"chat_id": TELEGRAM_CHANNEL_ID, "user_id": telegram_id}).encode()
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req)
        except Exception as e:
            log.error(f"{method} error: {e}")

def start_webhook_server():
    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    log.info(f"Webhook server listening on port {WEBHOOK_PORT}")
    server.serve_forever()

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    global bot_app
    init_db()

    t = threading.Thread(target=start_webhook_server, daemon=True)
    t.start()

    bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start",  start))
    bot_app.add_handler(CommandHandler("status", status_cmd))
    bot_app.add_handler(CommandHandler("help",   help_cmd))

    log.info("Bot is running... Press Ctrl+C to stop.")
    bot_app.run_polling()
