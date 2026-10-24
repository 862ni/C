import os
import random
import string
import threading
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

CHARS = string.ascii_lowercase + string.digits + "_"

# المهام الشغالة لكل شات
tasks = {}


async def check_username(client, name):
    """True = متاح، False = غير متاح، None = ما قدرت أتحقق"""
    try:
        r = await client.get(f"https://www.tiktok.com/@{name}", headers=HEADERS)
    except Exception:
        return None
    if r.status_code == 404:
        return True
    if r.status_code == 200:
        text = r.text
        if '"statusCode":10221' in text or '"statusCode":10202' in text:
            return True
        if '"uniqueId"' in text or '"statusCode":0' in text:
            return False
    return None


def random_name():
    return "".join(random.choice(CHARS) for _ in range(4))


async def search_loop(bot, chat_id):
    checked = set()
    fails = 0
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        while True:
            name = random_name()
            if name in checked:
                continue
            checked.add(name)
            res = await check_username(client, name)
            if res is True:
                fails = 0
                await bot.send_message(chat_id, f"✅ @{name} متاح")
            elif res is None:
                fails += 1
                if fails >= 10:
                    # تيك توك غالباً حاظر الطلبات، نرتاح شوي
                    await asyncio.sleep(60)
                    fails = 0
            else:
                fails = 0
            await asyncio.sleep(1.5)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in tasks and not tasks[chat_id].done():
        await update.message.reply_text("البحث شغال أصلاً 🔍\nلإيقافه أرسل /stop")
        return
    tasks[chat_id] = asyncio.create_task(search_loop(context.bot, chat_id))
    await update.message.reply_text("بديت البحث عن يوزرات رباعية 🔍\nما راح أرسل لك إلا المتاح.\nلإيقافه أرسل /stop")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    t = tasks.get(chat_id)
    if t and not t.done():
        t.cancel()
        await update.message.reply_text("وقفت البحث ⏹")
    else:
        await update.message.reply_text("ما في بحث شغال.")


# سيرفر صغير عشان Render يشوف إن الخدمة شغالة
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def run_ping():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


def main():
    threading.Thread(target=run_ping, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.run_polling()


if __name__ == "__main__":
    main()

