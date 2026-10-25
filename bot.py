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

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

LETTERS = string.ascii_lowercase
DIGITS = string.digits

# وقت انتهاء الانتظار لكل برنامج (لما البرنامج يقول "استنى شوي")
RATE = {}

# سوني ما تقبل يوزر يبدأ بـ _ أو - ، لو تبي تجربه غيّرها إلى True
PSN_ALLOW_SYMBOL_FIRST = False

# ---------------------------------------------------------------
# كل دالة ترجع: True = متاح ، False = غير متاح ، None = ما قدرت أتحقق
# هذي نفس طلبات "فحص التوفر" اللي يستخدمها البرنامج وقت التسجيل،
# فاليوزر المحظور أو المحجوز أو غير المقبول يطلع غير متاح.
# ---------------------------------------------------------------


def _retry_after(r, default=3.0):
    try:
        return float(r.json().get("retry_after", default))
    except Exception:
        pass
    try:
        return float(r.headers.get("Retry-After", default))
    except Exception:
        return default


async def check_psn(client, name):
    try:
        r = await client.post(
            "https://accounts.api.playstation.com/api/v1/accounts/onlineIds",
            json={"onlineId": name, "reserveIfAvailable": False},
            headers={"User-Agent": UA},
        )
    except Exception:
        return None
    if r.status_code == 429:
        RATE["PlayStation"] = time.time() + _retry_after(r) + 0.2
        return None
    if r.status_code in (200, 201, 204):
        return True
    if r.status_code == 400:
        return False
    return None


# اسم البرنامج: (دالة الفحص، عدد الفحوصات المتزامنة)
SERVICES = {
    "PlayStation": (check_psn, 20),
}

# يوزر معروف إنه محجوز، نستخدمه نختبر فيه الفحص
CANARY_TAKEN = {
    "PlayStation": "john",
}

PAUSE_AFTER_FAILS = 30
PAUSE_SECONDS = 30


def free_name():
    """يوزر طويل عشوائي، أكيد غير موجود"""
    mid = "".join(random.choice(LETTERS) for _ in range(13))
    return random.choice(LETTERS) + mid + random.choice(LETTERS)


def random_name(svc=None):
    """4 خانات فيها رمز واحد (_ أو -) في أي مكان، والباقي حروف وأرقام"""
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


async def conclusive(client, svc, name, tries=6):
    """يكرر الفحص لين يجي جواب واضح (ينتظر لو البرنامج قال استنى)"""
    checker = SERVICES[svc][0]
    for _ in range(tries):
        wait = RATE.get(svc, 0) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        res = await checker(client, name)
        if res is not None:
            return res
        await asyncio.sleep(0.3)
    return None


async def run_health(client, svc):
    """
    يتأكد إن الفحص سليم: المحجوز يطلع محجوز، والحر يطلع حر.
    True = سليم ، False = غلط أكيد ، None = ما قدر يتأكد (حظر مؤقت مثلاً)
    """
    taken_res = await conclusive(client, svc, CANARY_TAKEN[svc], tries=3)
    free_res = await conclusive(client, svc, free_name(), tries=3)
    if taken_res is None or free_res is None:
        return None
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
        ok = await run_health(client, svc)
        if ok is not None:          # لو ما قدر يتأكد نخلي الحالة السابقة
            st["ok"] = ok
        await asyncio.sleep(120 if st["ok"] else 30)


async def worker(sess, svc, client):
    checker, _ = SERVICES[svc]
    st = sess.stats[svc]
    while True:
        now = time.time()
        if st["pause_until"] > now:
            await asyncio.sleep(min(2, st["pause_until"] - now))
            continue
        wait = RATE.get(svc, 0) - now
        if wait > 0:
            await asyncio.sleep(min(2, wait))
            continue
        if st["ok"] is False:
            # الفحص غير سليم، نرتاح ونتأكد كل شوي من health_loop
            await asyncio.sleep(1)
            continue

        name = sess.next_name(svc)
        if name is None:
            await asyncio.sleep(0.05)
            continue

        res = await checker(client, name)

        if res is None:
            if RATE.get(svc, 0) > time.time():
                sess.seen[svc].discard(name)   # البرنامج قال استنى، نعيد المحاولة لاحقاً
                continue
            st["checked"] += 1
            st["unknown"] += 1
            st["streak"] += 1
            if st["streak"] >= PAUSE_AFTER_FAILS:
                st["pause_until"] = time.time() + PAUSE_SECONDS
                st["streak"] = 0
            continue

        st["checked"] += 1
        st["streak"] = 0
        if res is not True:
            continue

        # لقينا يوزر متاح: نأكده مرتين ونتأكد إن الفحص سليم قبل الإرسال
        confirmed = True
        for _ in range(2):
            await asyncio.sleep(0.4)
            if await conclusive(client, svc, name) is not True:
                confirmed = False
                break
        healthy = await run_health(client, svc) if confirmed else None
        if confirmed and healthy:
            st["found"] += 1
            await sess.queue.put((name, svc))
        else:
            st["rejected"] += 1
            if confirmed and healthy is False:
                st["ok"] = False


async def sender(sess, bot, chat_id):
    while True:
        first = await sess.queue.get()
        items = [first]
        await asyncio.sleep(0.2)
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
        await asyncio.sleep(0.4)


async def run_session(sess, bot, chat_id):
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=40)
    async with httpx.AsyncClient(timeout=6, follow_redirects=True, limits=limits) as client:
        coros = [sender(sess, bot, chat_id)]
        for svc, (_, workers) in SERVICES.items():
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
        "بديت البحث السريع في PlayStation 🔍\n"
        "يوزرات رباعية فيها _ أو - في مكان واحد\n\n"
        "ما أرسل لك يوزر إلا بعد ما أفحصه 3 مرات وأتأكد إن الفحص سليم.\n\n"
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
    secs = max(time.time() - sess.started, 1)
    now = time.time()
    lines = [f"⏱ شغال من {secs / 60:.1f} دقيقة\n"]
    for svc, st in sess.stats.items():
        if st["ok"] is True:
            state = "✅ سليم"
        elif st["ok"] is False:
            state = "⛔ الفحص غير موثوق (غالباً محظور)"
        else:
            state = "… يختبر"
        if RATE.get(svc, 0) > now:
            state += " ⏳ البرنامج يطلب ننتظر"
        elif st["pause_until"] > now:
            state += " ⏸"
        lines.append(
            f"{svc}: {state}\n   فحص {st['checked']} ({st['checked'] / secs:.1f}/ثانية) | "
            f"متاح {st['found']} | مرفوض {st['rejected']} | فشل {st['unknown']}"
        )
    await update.message.reply_text("\n".join(lines))


async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يجرب طلب واحد على سوني ويطلع لك الرد الخام، عشان نعرف وش المشكلة"""
    lines = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for name in ("john", free_name()):
            try:
                r = await client.post(
                    "https://accounts.api.playstation.com/api/v1/accounts/onlineIds",
                    json={"onlineId": name, "reserveIfAvailable": False},
                    headers={"User-Agent": UA},
                )
                body = r.text.strip().replace("\n", " ")[:160]
                lines.append(f"{name}\nالحالة: {r.status_code}\nالرد: {body}")
            except Exception as e:
                lines.append(f"{name}\nخطأ: {type(e).__name__}: {str(e)[:120]}")
    await update.message.reply_text("\n\n".join(lines))


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
    app.add_handler(CommandHandler("test", test_cmd))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
