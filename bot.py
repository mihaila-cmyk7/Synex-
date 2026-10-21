import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, quote

import httpx

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      MenuButtonWebApp, Update, WebAppInfo)
from telegram.constants import ParseMode
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

TOKEN = os.environ["BOT_TOKEN"]          # токен от @BotFather
WEBAPP_URL = os.environ["WEBAPP_URL"]    # https-ссылка на index.html
PUBLIC_URL = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")


def with_api(url: str) -> str:
    # приложению нужен адрес сервера бота, чтобы слать файлы в чат
    if not PUBLIC_URL:
        return url
    return url + ("&" if "?" in url else "?") + "api=" + quote(PUBLIC_URL, safe="")


BOT_NAME = "Synex²"

WELCOME = (
    f"<b>🎛 {BOT_NAME}</b>\n"
    "<i>музыка, которую пишут формулами</i>\n"
    "━━━━━━━━━━━━━━━\n\n"
    "Каждая формула <code>y = f(x)</code> — отдельный голос: "
    "x — это время, y — высота тона. Всё происходит в приложении внутри бота.\n\n"
    "<b>Что умеет приложение</b>\n"
    "🎹 <b>Редактор голосов</b> — пиши формулы, смотри график, двигай тон и громкость каждого голоса\n"
    "🧩 <b>Готовые заготовки</b> — бочка, бас, мелодия, хэт и другие, добавляются одним касанием\n"
    "🎧 <b>Своя песня на дорожке</b> — загрузи трек, выдели фрагмент (хоть 15 секунд) "
    "и накладывай его на формулы\n"
    "📤 <b>Сохранение в чат</b> — аудио, видео с графиком или картинка\n"
    "🎨 Тема «Ч / Б» и своё фото на фон\n\n"
    "✨ <b>А ещё внутри спрятаны две прикольные функции</b> — попробуй найти их сам 👀\n\n"
    "Жми кнопку ниже и заходи в приложение 👇"
)


def welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎹 Открыть приложение", web_app=WebAppInfo(url=with_api(WEBAPP_URL)))],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        WELCOME, parse_mode=ParseMode.HTML, reply_markup=welcome_kb()
    )


async def post_init(app: Application):
    await app.bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Музыка", web_app=WebAppInfo(url=with_api(WEBAPP_URL)))
    )
    # карточка бота до первого запуска и список команд
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "О боте и что он умеет"),
            BotCommand("help", "О боте и что он умеет"),
        ])
        await app.bot.set_my_short_description(
            f"{BOT_NAME} — музыка из формул. Открой приложение внутри бота."
        )
        await app.bot.set_my_description(
            f"{BOT_NAME} — музыка, которую пишут формулами.\n\n"
            "• редактор голосов с графиком\n"
            "• своя песня на таймлайне — двигай и накладывай на формулы\n"
            "• сохранение аудио, видео и картинки в чат\n"
            "• внутри спрятаны две прикольные функции 👀\n\n"
            "Нажми «Запустить»."
        )
    except Exception as e:
        print("не удалось обновить описание бота:", e)


MAX_BYTES = 49 * 1024 * 1024
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "X-Init-Data, X-Kind, X-Filename, Content-Type",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
}


def user_id_from_init(init_data: str):
    """Проверяем подпись Telegram и достаём id пользователя."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > 86400:
            return None
        return json.loads(pairs["user"])["id"]
    except Exception:
        return None


class Ping(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", with_body=True):
        self.send_response(code)
        for k, v in CORS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def do_GET(self):
        self._send(200, b"ok")

    def do_HEAD(self):
        self._send(200, b"ok", with_body=False)

    def do_OPTIONS(self):
        self._send(204)

    def do_POST(self):
        if self.path != "/upload":
            return self._send(404)
        uid = user_id_from_init(self.headers.get("X-Init-Data", ""))
        n = int(self.headers.get("Content-Length", 0))
        if not uid:
            return self._send(403, b"bad auth")
        if n <= 0 or n > MAX_BYTES:
            return self._send(413, b"size")
        data = self.rfile.read(n)
        kind = self.headers.get("X-Kind", "file")
        name = os.path.basename(self.headers.get("X-Filename", "mathmusic"))
        method, field = {"video": ("sendVideo", "video"),
                         "photo": ("sendPhoto", "photo")}.get(kind, ("sendDocument", "document"))
        if name.endswith(".webm"):
            method, field = "sendDocument", "document"
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{TOKEN}/{method}",
                data={"chat_id": uid, "caption": "🎹 Math Music"},
                files={field: (name, data)},
                timeout=120,
            )
            ok = r.is_success
        except Exception:
            ok = False
        self._send(200 if ok else 502, b"ok" if ok else b"telegram error")

    def log_message(self, *args):
        pass


def keep_alive():
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), Ping).serve_forever()


def main():
    threading.Thread(target=keep_alive, daemon=True).start()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    # любое другое сообщение — снова показываем, что умеет бот, и ведём в приложение
    app.add_handler(MessageHandler(~filters.COMMAND, start))
    app.run_polling()


if __name__ == "__main__":
    main()
