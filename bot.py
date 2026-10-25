import os
import random
import string
import threading
import asyncio
import time
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

LETTERS = string.ascii_lowercase
DIGITS = string.digits

# وقت انتهاء الحظر المؤقت لكل برنامج (يحدده ديسكورد لما يقول "استنى")
RATE = {}

# ---------------------------------------------------------------
# كل دالة ترجع: True = متاح ، False = غير متاح ، None = ما قدرت أتحقق
# الشرط صارم: ما نقول "متاح" إلا لو كل الإشارات تتفق
# ---------------------------------------------------------------


async def check_discord(client, name):
    try:
        r = await client.post(
            "https://discord.com/api/v9/unique-username/username-attempt-unauthed",
            json={"username": name},
            headers={"User-Agent": HEADERS["User-Agent"]},
        )
    except Exception:
        return None
    if r.status_code == 429:
        # ديسكورد يقول لنا كم ننتظر، نحترم الوقت ونكمل
        wait = 5.0
        try:
            wait = float(r.json().get("retry_after", wait))
        except Exception:
            pass
        RATE["Discord"] = time.time() + wait + 0.5
        return None
    if r.status_code == 200:
        try:
            data = r.json()
            if "taken" in data:
                return not data["taken"]
        except Exception:
            return None
    return None


async def check_psn(client, name):
    try:
        r = await client.post(
            "https://accounts.api.playstation.com/api/v1/accounts/onlineIds",
            json={"onlineId": name, "reserveIfAvailable": False},
            headers={"User-Agent": HEADERS["User-Agent"]},
        )
    except Exception:
        return None
    if r.status_code in (200, 201, 204):
        return True
    if r.status_code == 400:
        return False
    return None


# اسم البرنامج: (دالة الفحص، عدد الفحوصات المتزامنة، تأخير صغير بين كل فحص)
SERVICES = {
    "Discord": (check_discord, 3, 0.25),
    "PlayStation": (check_psn, 8, 0.0),
}

# يوزر معروف إنه محجوز في كل برنامج، نستخدمه نختبر فيه الفحص
CANARY_TAKEN = {
    "Discord": "discord",
    "PlayStation": "john",
}

# سوني ما تقبل يوزر يبدأ بـ _ أو - ، لو تبي تجربه غيّرها إلى True
PSN_ALLOW_SYMBOL_FIRST = False

PAUSE_AFTER_FAILS = 20
PAUSE_SECONDS = 60


def free_name():
    """يوزر طويل عشوائي، أكيد غير موجود. نستخدمه نتأكد إن الفحص يقول 'متاح' لما يكون متاح"""
    mid = "".join(random.choice(LETTERS) for _ in range(13))
    return random.choice(LETTERS) + mid + random.choice(LETTERS)


def random_name(svc=None):
    """يوزر رباعي حسب شروط كل برنامج"""
    if svc == "PlayStation":
        # 4 خانات، فيها رمز واحد بس (_ أو -) في أي مكان، والباقي حروف وأرقام
        positions = [0, 1, 2, 3] if PSN_ALLOW_SYMBOL_FIRST else [1, 2, 3]
        pos = random.choice(positions)
        symbol = random.choice("_-")
        chars = []
        for i in range(4):
            if i == pos:
                chars.append(symbol)
            elif i == 0:
                chars.append(random.choice(LETTERS))
            else:
                chars.append(random.choice(LETTERS + DIGITS))
        return "".join(chars)

    # ديسكورد: رباعي، يبدأ وينتهي بحرف أو رقم، وأحياناً نقطة أو _ في النص
    alphabet = LETTERS + DIGITS
    chars = [random.choice(alphabet) for _ in range(4)]
    if random.random() < 0.4:
        chars[random.choice([1, 2])] = random.choice("._")
    return "".join(chars)


async def run_health(client, svc):
    """يتأكد إن الفحص سليم: المحجوز يطلع محجوز، والحر يطلع حر"""
    checker = SERVICES[svc][0]
    taken_res = await checker(client, CANARY_TAKEN[svc])
    free_res = await checker(client, free_name())
    return taken_res is False and free_res is True


class Session:
    def __init__(self):
        self.started = time.time()
        self.queue = asyncio.Queue()
        self.seen = {s: set() for s in SERVICES}
        self.stats = {
            s: {
                "checked": 0, "found": 0, "unknown": 0, "rejected": 0,
                "streak": 0, "pause_until": 0.0, "ok": None,
            }
            for s in SERVICES
        }

    def next_name(self, svc):
        seen = self.seen[svc]
        if len(seen) > 300000:
            seen.clear()
        for _ in range(30):
            n = random_name(svc)
            if n not in seen:
                seen.add(n)
                return n
        return None


async def health_loop(sess, svc, client):
    st = sess.stats[svc]
    while True:
        if RATE.get(svc, 0) > time.time():
            await asyncio.sleep(1)
            continue
        ok = await run_health(client, svc)
        st["ok"] = ok
        await asyncio.sleep(60 if ok else 20)


async def worker(sess, svc, client):
    checker, _, delay = SERVICES[svc]
    st = sess.stats[svc]
    while True:
        now = time.time()
        if st["ok"] is not True or st["pause_until"] > now or RATE.get(svc, 0) > now:
            await asyncio.sleep(0.5)
            continue

        name = sess.next_name(svc)
        if name is None:
            await asyncio.sleep(0.05)
            continue

        res = await checker(client, name)
        st["checked"] += 1

        if res is None:
            st["unknown"] += 1
            st["streak"] += 1
            if st["streak"] >= PAUSE_AFTER_FAILS:
                st["pause_until"] = time.time() + PAUSE_SECONDS
                st["streak"] = 0
        else:
            st["streak"] = 0
            if res is True:
                # تأكيد مرتين إضافيتين، ثم نتأكد إن الفحص نفسه سليم لحظة الإرسال
                confirmed = True
                for _ in range(2):
                    await asyncio.sleep(0.7)
                    if await checker(client, name) is not True:
                        confirmed = False
                        break
                if confirmed and await run_health(client, svc):
                    st["found"] += 1
                    await sess.queue.put((name, svc))
                else:
                    st["rejected"] += 1
                    if confirmed:
                        st["ok"] = False  # الفحص صار يعطي نتائج مشكوك فيها

        if delay:
            await asyncio.sleep(delay)


async def sender(sess, bot, chat_id):
    while True:
        first = await sess.queue.get()
        items = [first]
        await asyncio.sleep(0.3)
        while not sess.queue.empty():
            items.append(sess.queue.get_nowait())
        grouped = {}
        for n, s in items:
            grouped.setdefault(n, []).append(s)
        text = "\n".join(f"✅ {n} — {' ، '.join(ss)}" for n, ss in grouped.items())
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            await asyncio.sleep(2)
        await asyncio.sleep(0.5)


async def run_session(sess, bot, chat_id):
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=40)
    async with httpx.AsyncClient(timeout=8, follow_redirects=True, limits=limits) as client:
        coros = [sender(sess, bot, chat_id)]
        for svc, (_, workers, _) in SERVICES.items():
            coros.append(health_loop(sess, svc, client))
            for _ in range(workers):
                coros.append(worker(sess, svc, client))
        await asyncio.gather(*coros)


sessions = {}  # chat_id -> (Session, Task)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in sessions and not sessions[chat_id][1].done():
        await update.message.reply_text("البحث شغال أصلاً 🔍\n/status للتفاصيل\n/stop للإيقاف")
        return
    sess = Session()
    task = asyncio.create_task(run_session(sess, context.bot, chat_id))
    sessions[chat_id] = (sess, task)
    await update.message.reply_text(
        "بديت البحث الصارم 🔍\n"
        "• Discord: يوزرات رباعية\n"
        "• PlayStation: رباعية فيها _ أو - في مكان واحد\n\n"
        "قبل ما أرسل أي يوزر:\n"
        "• أفحصه 3 مرات\n"
        "• أتأكد إن الفحص نفسه سليم (يعرف المحجوز من المتاح)\n\n"
        "/status لمعرفة حالة كل برنامج\n"
        "/stop للإيقاف"
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    item = sessions.get(chat_id)
    if item and not item[1].done():
        item[1].cancel()
        await update.message.reply_text("وقفت البحث ⏹")
    else:
        await update.message.reply_text("ما في بحث شغال.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    item = sessions.get(chat_id)
    if not item or item[1].done():
        await update.message.reply_text("ما في بحث شغال. أرسل /start")
        return
    sess = item[0]
    mins = max((time.time() - sess.started) / 60, 0.01)
    now = time.time()
    lines = [f"⏱ شغال من {mins:.1f} دقيقة\n"]
    total = 0
    for svc, st in sess.stats.items():
        total += st["checked"]
        if st["ok"] is True:
            state = "✅ سليم"
        elif st["ok"] is False:
            state = "⛔ الفحص غير موثوق (غالباً محظور)"
        else:
            state = "… يختبر"
        if st["pause_until"] > now or RATE.get(svc, 0) > now:
            state += " ⏸"
        lines.append(
            f"{svc}: {state}\n   فحص {st['checked']} | متاح {st['found']} | "
            f"مرفوض بعد التأكيد {st['rejected']} | فشل {st['unknown']}"
        )
    lines.append(f"\nالسرعة: {total / (mins * 60):.1f} فحص/ثانية")
    await update.message.reply_text("\n".join(lines))


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


async def main():
    threading.Thread(target=run_ping, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("status", status))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
