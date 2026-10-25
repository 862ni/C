import os
import random
import string
import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from psnawp_api import PSNAWP


# =========================================================
# إعدادات Render
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
NPSSO = os.environ.get("NPSSO", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN غير موجود في Environment")

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

LETTERS = string.ascii_lowercase
DIGITS = string.digits
SYMBOLS = "_-"

# ملاحظة: البحث عن الحساب لا يؤكد إمكانية حجز الاسم.
CHECK_INTERVAL = 3.5
MAX_ERRORS = 5

# =========================================================
# حالة PlayStation
# =========================================================

_psn = None
_psn_lock = threading.Lock()

PSN_STATE = {
    "auth_error": None,
    "last_error": None,
    "last_check": None,
}


def get_psn():
    global _psn

    if not NPSSO:
        raise RuntimeError(
            "NPSSO غير موجود في Environment"
        )

    if _psn is None:
        _psn = PSNAWP(NPSSO)

    return _psn


def lookup_psn(name):
    """
    النتائج:
      True  = الاسم غير موجود في بحث الحسابات.
      False = الحساب موجود.
      None  = تعذر التحقق.

    True لا تعني أن الاسم قابل للحجز.
    """

    with _psn_lock:
        try:
            psn = get_psn()
            psn.user(online_id=name)

            PSN_STATE["auth_error"] = None
            PSN_STATE["last_error"] = None
            PSN_STATE["last_check"] = time.time()

            return False

        except Exception as exc:
            error_type = type(exc).__name__

            PSN_STATE["last_error"] = error_type
            PSN_STATE["last_check"] = time.time()

            if (
                error_type == "PSNAWPNotFoundError"
            ):
                PSN_STATE["auth_error"] = None
                return True

            if (
                "Authentication" in error_type
                or "Unauthorized" in error_type
            ):
                PSN_STATE["auth_error"] = error_type

            # لا نعتبر أي خطأ آخر دليل توفر.
            return None


async def check_psn(name):
    return await asyncio.to_thread(
        lookup_psn,
        name,
    )


# =========================================================
# توليد الأسماء
# =========================================================

def random_name():
    """
    يولد أسماء من 4 أو 5 خانات،
    تحتوي على _ أو - مرة واحدة.

    هذه الأسماء مرشحة للبحث فقط،
    وليست مضمونة القبول لدى PlayStation.
    """

    length = random.choice((4, 5))
    symbol_position = random.randrange(1, length)

    result = []

    for position in range(length):
        if position == symbol_position:
            result.append(random.choice(SYMBOLS))

        elif position == 0:
            result.append(random.choice(LETTERS))

        else:
            result.append(
                random.choice(LETTERS + DIGITS)
            )

    return "".join(result)


def random_control_name():
    """اسم عشوائي طويل لاستخدامه في اختبار البحث."""

    return (
        random.choice(LETTERS)
        + "".join(
            random.choice(LETTERS + DIGITS)
            for _ in range(17)
        )
    )


# =========================================================
# إدارة جلسات البحث
# =========================================================

sessions = {}


class Session:
    def __init__(self):
        self.started = time.time()
        self.task = None
        self.seen = set()
        self.running = True

        self.stats = {
            "checked": 0,
            "not_found": 0,
            "existing": 0,
            "errors": 0,
        }

    def next_name(self):
        for _ in range(100):
            name = random_name()

            if name not in self.seen:
                self.seen.add(name)
                return name

        return None


# =========================================================
# البحث
# =========================================================

async def search_loop(sess, bot, chat_id):
    consecutive_errors = 0

    try:
        while sess.running:

            name = sess.next_name()

            if name is None:
                await asyncio.sleep(1)
                continue

            result = await check_psn(name)

            if not sess.running:
                break

            if result is None:
                sess.stats["errors"] += 1
                consecutive_errors += 1

                if consecutive_errors >= MAX_ERRORS:
                    await bot.send_message(
                        chat_id,
                        "⚠️ تعذر التحقق من PlayStation "
                        "عدة مرات متتالية.\n"
                        "أوقفت البحث مؤقتًا لتجنب "
                        "إرسال نتائج غير موثوقة.\n\n"
                        f"آخر خطأ: "
                        f"{PSN_STATE['last_error']}"
                    )

                    await asyncio.sleep(60)
                    consecutive_errors = 0

                else:
                    await asyncio.sleep(CHECK_INTERVAL)

                continue

            consecutive_errors = 0
            sess.stats["checked"] += 1

            if result is False:
                sess.stats["existing"] += 1
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            sess.stats["not_found"] += 1

            # لا نكرر إرسال الاسم دون إعادة التحقق.
            await asyncio.sleep(1)

            confirm = await check_psn(name)

            if not sess.running:
                break

            if confirm is not True:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # تنبيه واضح: نتيجة البحث لا تثبت قابلية الحجز.
            await bot.send_message(
                chat_id,
                "🔎 نتيجة بحث PlayStation\n\n"
                f"الاسم: {name}\n"
                "الحالة: لم يُعثر على الحساب في البحث.\n\n"
                "⚠️ هذا لا يؤكد أن الاسم متاح "
                "للتسجيل أو تغيير Online ID.\n"
                "تحقق من الاسم عبر PlayStation "
                "قبل الاعتماد عليه."
            )

            await asyncio.sleep(CHECK_INTERVAL)

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        PSN_STATE["last_error"] = type(exc).__name__

        try:
            await bot.send_message(
                chat_id,
                "❌ توقف البحث بسبب خطأ.\n"
                f"النوع: {type(exc).__name__}"
            )
        except Exception:
            pass

    finally:
        sess.running = False


# =========================================================
# أوامر Telegram
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id

    current = sessions.get(chat_id)

    if current and current.running:
        await update.effective_message.reply_text(
            "🔍 البحث يعمل بالفعل.\n"
            "/status — الإحصائيات\n"
            "/stop — إيقاف البحث"
        )
        return

    if not NPSSO:
        await update.effective_message.reply_text(
            "❌ لم يتم ضبط NPSSO.\n"
            "أضفه في Environment داخل Render."
        )
        return

    sess = Session()

    sess.task = asyncio.create_task(
        search_loop(
            sess,
            context.bot,
            chat_id,
        )
    )

    sessions[chat_id] = sess

    await update.effective_message.reply_text(
        "🔍 بدأ البحث عن أسماء PlayStation.\n\n"
        "• الأسماء: 4 أو 5 خانات.\n"
        "• تحتوي على _ أو -.\n"
        "• يتم استبعاد النتائج غير المؤكدة.\n\n"
        "⚠️ البحث عن الحساب لا يثبت إمكانية حجز الاسم.\n\n"
        "/status — الإحصائيات\n"
        "/stop — إيقاف البحث\n"
        "/test — اختبار الاتصال"
    )


async def stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id
    sess = sessions.get(chat_id)

    if not sess or not sess.running:
        await update.effective_message.reply_text(
            "لا يوجد بحث يعمل حاليًا."
        )
        return

    sess.running = False

    if sess.task and not sess.task.done():
        sess.task.cancel()

    await update.effective_message.reply_text(
        "⏹ تم طلب إيقاف البحث."
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id
    sess = sessions.get(chat_id)

    if not sess:
        await update.effective_message.reply_text(
            "لا توجد جلسة بحث.\nأرسل /start"
        )
        return

    elapsed = max(time.time() - sess.started, 1)

    state = (
        "🟢 يعمل"
        if sess.running
        else "⏹ متوقف"
    )

    stats = sess.stats

    message = (
        f"الحالة: {state}\n"
        f"المدة: {elapsed / 60:.1f} دقيقة\n\n"
        f"إجمالي الفحوصات: {stats['checked']}\n"
        f"حسابات موجودة: {stats['existing']}\n"
        f"لم يُعثر عليها: {stats['not_found']}\n"
        f"أخطاء: {stats['errors']}\n\n"
        f"آخر خطأ: {PSN_STATE['last_error']}\n"
        f"مشكلة مصادقة: {PSN_STATE['auth_error']}"
    )

    await update.effective_message.reply_text(message)


async def test_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not NPSSO:
        await update.effective_message.reply_text(
            "❌ NPSSO غير موجود في Environment."
        )
        return

    await update.effective_message.reply_text(
        "⏳ أختبر الاتصال مع PlayStation..."
    )

    result = await check_psn("john")

    if result is False:
        message = (
            "✅ تم العثور على الحساب التجريبي.\n"
            "الاتصال نجح لهذا الطلب."
        )

    elif result is True:
        message = (
            "⚠️ لم يُعثر على الحساب التجريبي.\n"
            "لا يمكن اعتبار هذا الاختبار تأكيدًا "
            "لصحة فحص توفر الأسماء."
        )

    else:
        message = (
            "❌ تعذر التحقق من الحساب.\n"
            f"الخطأ: {PSN_STATE['last_error']}\n"
            f"المصادقة: {PSN_STATE['auth_error']}"
        )

    await update.effective_message.reply_text(message)


# =========================================================
# خادم Render
# =========================================================

class Ping(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8",
        )
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def run_ping():
    port = int(os.environ.get("PORT", "10000"))

    server = HTTPServer(
        ("0.0.0.0", port),
        Ping,
    )

    server.serve_forever()


# =========================================================
# تشغيل البوت
# =========================================================

async def main():
    threading.Thread(
        target=run_ping,
        daemon=True,
    ).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("stop", stop)
    )

    app.add_handler(
        CommandHandler("status", status)
    )

    app.add_handler(
        CommandHandler("test", test_cmd)
    )

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    try:
        await asyncio.Event().wait()

    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
