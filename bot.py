import hashlib
import hmac
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, quote

import httpx

from google import genai
from google.genai import types
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      MenuButtonWebApp, Update, WebAppInfo)
from telegram.constants import ChatAction
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

TOKEN = os.environ["BOT_TOKEN"]          # токен от @BotFather
WEBAPP_URL = os.environ["WEBAPP_URL"]    # https-ссылка на index.html
PUBLIC_URL = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])  # ключ из Google AI Studio

SYSTEM = """Ты пишешь музыку математическими формулами для приложения-секвенсора.
Каждая формула — один голос: y = f(x). x — время, y — высота тона в октавах
(частота = 660 * 2^y Гц; y=0 ≈ 660 Гц, y=-2.5 ≈ 117 Гц, y=1 = 1320 Гц; ноту MIDI m даёт y = (m-72)/12 - 0.33496, функция note(m)).
Звук — синус. Если y не число (rest) — пауза.

Разрешено: + - * / ^ (), x, pi, e, rest, sin cos tan atan asin acos abs floor ceil round
sqrt exp ln log2 log10 max min sign mod(a,b) frac(a), note(m), pick(i,a,b,c,...) (берёт элемент по номеру i циклически; rest можно передавать как аргумент), if(усл,а,б), сравнения < > <= >= ==,
условие `усл ? а : б`. Каждая формула — ОДНА строка, без "y =".
По умолчанию окно x от -4.25 до 4.25 играет 8 секунд (шаг 1 по x ≈ 0.94 с).

Приёмы:
- Бочка (повтор каждые 1): -2.5+3.9/(1+19*mod(x+0.8,1))+0.85*sin(72*pi*mod(x+0.8,1))/(1+12*mod(x+0.8,1))
- Мелодия по ступеням с паузами: frac(48*(x+4.25)/8.5)<0.84 ? -0.502+1.8-log2(1+mod(floor(48*(x+4.25)/8.5)+2,6)) : rest
- Ритм/шум: -1.06+mod(91357/(2+abs(mod(2*(x+8.4),8))),16)/7.9+1.3*max(0,1-abs(mod(x+8.4,2)-0.75)/0.13)
- Ноты по MIDI: note(pick(floor(16*(x+4.25)/8.5),72,76,79,84,79,76,72,rest,...)) — 16 шагов за окно
- Ступенчатые ноты: floor(...) и mod(...); затухание: 2^(-24*mod(...,1)); тремоло/вибрато: sin(...)

Если пользователь прислал свою формулу или картинку с формулой/графиком — перепиши её в этот
синтаксис: ⌊a⌋ → floor(a), дроби → /, arctan → atan, кусочные функции → тернарный оператор,
неявное умножение допустимо, но лучше писать *. Если прислан только пример звучания словами —
подбери похожие голоса. Желание пользователя (темп, настроение, инструменты) важнее примера.

Ответ — ТОЛЬКО JSON без пояснений вокруг:
{"comment": "1-2 предложения по-русски, что получилось и что можно подкрутить",
 "formulas": ["...", "..."], "x0": -4.25, "x1": 4.25, "seconds": 8}
Не больше 4 формул. x0, x1, seconds — необязательные."""


async def generate(content) -> dict:
    r = await client.aio.models.generate_content(
        model=MODEL,
        contents=content,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            max_output_tokens=2000,
        ),
    )
    return json.loads(re.search(r"\{.*\}", r.text, re.S).group(0))


def app_url(d: dict) -> str:
    q = "f=" + quote("\n".join(d["formulas"][:4]))
    for k in ("x0", "x1", "seconds"):
        if k in d:
            q += f"&{k}={d[k]}"
    return f"{WEBAPP_URL}?{q}"


def with_api(url: str) -> str:
    # приложению нужен адрес сервера бота, чтобы слать файлы в чат
    if not PUBLIC_URL:
        return url
    return url + ("&" if "?" in url else "?") + "api=" + quote(PUBLIC_URL, safe="")


def open_kb(url: str) -> InlineKeyboardMarkup:
    url = with_api(url)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎹 Открыть редактор", web_app=WebAppInfo(url=url))
    ]])


BOT_NAME = "Synex²"

WELCOME = (
    f"<b>🎛 {BOT_NAME}</b>\n"
    "<i>музыка, которую пишут формулами</i>\n"
    "━━━━━━━━━━━━━━━\n\n"
    "Я превращаю математику в звук. Каждая формула <code>y = f(x)</code> — отдельный голос: "
    "x — это время, y — высота тона.\n\n"
    "<b>Что я умею</b>\n"
    "🎹 <b>Собирать музыку по описанию</b> — напиши «быстрая бочка и грустная мелодия», "
    "и я подберу формулы\n"
    "🖼 <b>Разбирать примеры</b> — пришли скриншот графика или формулу текстом, "
    "я перепишу её под редактор\n"
    "🎚 <b>Редактор голосов</b> — графики, масштаб, сдвиг каждого голоса по тону, "
    "громкость, удаление в одно касание\n"
    "🎧 <b>Своя песня на дорожке</b> — загрузи трек, выдели нужный фрагмент "
    "(хоть 15 секунд) и двигай его по таймлайну, накладывая на формулы\n"
    "📤 <b>Сохранять результат в чат</b> — аудио, видео с графиком или картинку\n"
    "🎨 Тема «Ч / Б» и своё фото на фон\n\n"
    "<b>С чего начать</b>\n"
    "Просто напиши, какую музыку хочешь, или открой пустой редактор 👇"
)

EXAMPLES = (
    "<b>💡 Попробуй написать</b>\n\n"
    "• <i>«Бодрый бит: бочка на каждую долю и быстрый хэт»</i>\n"
    "• <i>«Грустная мелодия в миноре и глубокий бас»</i>\n"
    "• <i>«Сделай арпеджио, как в играх на 8-бит»</i>\n"
    "• <i>«Сирена, которая медленно меняет высоту»</i>\n\n"
    "Или пришли свою формулу, например <code>sin(x*3)</code>, "
    "или скриншот графика — я переведу его в звук."
)


def welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎹 Открыть редактор", web_app=WebAppInfo(url=with_api(WEBAPP_URL)))],
        [InlineKeyboardButton("💡 Примеры запросов", callback_data="examples")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        WELCOME, parse_mode=ParseMode.HTML, reply_markup=welcome_kb()
    )


async def on_examples(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(EXAMPLES, parse_mode=ParseMode.HTML)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await respond(update, update.message.text)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    f = await msg.photo[-1].get_file()
    data = bytes(await f.download_as_bytearray())
    content = [
        types.Part.from_bytes(data=data, mime_type="image/jpeg"),
        msg.caption or "Сделай такую же музыку формулами по этому примеру.",
    ]
    await respond(update, content)


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Видео я не разбираю. Пришли скриншот кадра с формулой или напиши формулу текстом."
    )


async def respond(update: Update, content):
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        d = await generate(content)
        await update.message.reply_text(
            d.get("comment", "Готово!"), reply_markup=open_kb(app_url(d))
        )
    except Exception as e:
        await update.message.reply_text(f"Не получилось собрать формулы ({e}). Попробуй описать иначе.")


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
            f"{BOT_NAME} — музыка из формул: собираю звук по описанию и открываю редактор."
        )
        await app.bot.set_my_description(
            f"{BOT_NAME} — музыка, которую пишут формулами.\n\n"
            "• собираю формулы по твоему описанию или скриншоту\n"
            "• редактор голосов с графиком\n"
            "• своя песня на таймлайне — двигай и накладывай на формулы\n"
            "• сохранение аудио, видео и картинки в чат\n\n"
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
    app.add_handler(CallbackQueryHandler(on_examples, pattern="^examples$"))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
