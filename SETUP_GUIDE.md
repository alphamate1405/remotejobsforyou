# 🤖 Telegram Jobs Channel Subscription Bot
## Complete Setup Guide (No Coding Required)

---

## What This Bot Does

- User sends `/start` to your bot
- Bot generates a **Dodo Payments checkout link** (₹499/month recurring)
- After payment, user gets a **private invite link** to your channel
- Subscription **renews automatically** every month
- If they cancel or payment fails → they are **automatically kicked** from the channel

---

## Step 1: Create Your Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Give it a name (e.g. "Jobs Channel Bot")
4. Give it a username ending in `bot` (e.g. `my_jobs_channel_bot`)
5. BotFather will give you a **token** — copy it, you'll need it

---

## Step 2: Get Your Channel ID

1. Add your bot as an **Admin** of your Telegram channel
   - Go to your channel → Edit → Administrators → Add Administrator → search your bot
   - Give it these permissions: **Invite Users**, **Ban Users**
2. To get your channel's numeric ID:
   - Forward any message from your channel to **@userinfobot**
   - It will show you the ID (a number like `-1001234567890`)

---

## Step 3: Set Up Dodo Payments

1. Log in to your [Dodo Payments Dashboard](https://dashboard.dodopayments.com)
2. Go to **Products** → Create a new product
   - Type: **Subscription**
   - Price: ₹499
   - Billing interval: **Monthly**
   - Copy the **Product ID**
3. Go to **API Keys** → copy your **API Key**
4. Go to **Webhooks** → Add Endpoint
   - URL: `http://YOUR_SERVER_IP:8080/webhook`
   - Enable these events:
     - `subscription.created`
     - `subscription.renewed`
     - `subscription.updated`
     - `subscription.cancelled`
     - `subscription.expired`
     - `payment.failed`
   - Copy the **Webhook Signing Secret**

---

## Step 4: Install & Run the Bot

### Option A — Run on your PC / server (Python required)

1. Install Python from https://python.org (download & install)
2. Open a terminal / command prompt in the bot folder
3. Copy `.env.example` to `.env` and fill in your values:
   ```
   TELEGRAM_BOT_TOKEN=7123456789:AAF...
   TELEGRAM_CHANNEL_ID=-1001234567890
   DODO_API_KEY=sk_live_...
   DODO_PRODUCT_ID=prod_...
   DODO_WEBHOOK_SECRET=whsec_...
   WEBHOOK_PORT=8080
   ```
4. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
5. Run the bot:
   ```
   python bot.py
   ```

### Option B — Deploy to Railway (easiest, runs 24/7 for free)

1. Go to https://railway.app and sign up
2. Click **New Project** → **Deploy from GitHub**
   - Upload these files to a GitHub repo first, OR use Railway's file upload
3. Add the environment variables in Railway's dashboard (Variables tab)
4. Railway gives you a public URL — use that as your Dodo webhook URL:
   `https://your-app.railway.app/webhook`

---

## Step 5: Make Your Channel Private

1. Go to your Telegram channel settings
2. Set it to **Private** (so only invited members can join)
3. Remove any existing public invite links

---

## How the Flow Works

```
User → /start → Bot sends payment link
         ↓
User pays on Dodo → Dodo sends webhook to your server
         ↓
Bot sends user a private invite link to join channel
         ↓
Every month Dodo charges automatically
         ↓
If payment fails or user cancels → Bot kicks them from channel
```

---

## Commands Available to Users

| Command | What it does |
|---------|-------------|
| `/start` | Get a subscription payment link |
| `/status` | Check if subscription is active |
| `/help` | Show available commands |

---

## Files in This Project

| File | Purpose |
|------|---------|
| `bot.py` | The entire bot code |
| `requirements.txt` | Python packages needed |
| `.env.example` | Template for your secret keys |
| `subscribers.db` | Auto-created database of subscribers |

---

## Need Help?

If you get stuck at any step, just come back and ask!
