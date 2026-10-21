import base64
import json
import os
import re
from urllib.parse import quote

from anthropic import AsyncAnthropic
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      MenuButtonWebApp, Update, WebAppInfo)
from telegram.constants import ChatAction
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

TOKEN = os.environ["BOT_TOKEN"]          # токен от @BotFather
WEBAPP_URL = os.environ["WEBAPP_URL"]    # https-ссылка на index.html
MODEL = "claude-sonnet-5-5"
claude = AsyncAnthropic()                # ключ берётся из ANTHROPIC_API_KEY

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
    r = await claude.messages.create(
        model=MODEL, max_tokens=1500, system=SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    text = r.content[0].text
    return json.loads(re.search(r"\{.*\}", text, re.S).group(0))


def app_url(d: dict) -> str:
    q = "f=" + quote("\n".join(d["formulas"][:4]))
    for k in ("x0", "x1", "seconds"):
        if k in d:
            q += f"&{k}={d[k]}"
    return f"{WEBAPP_URL}?{q}"


def open_kb(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎹 Открыть редактор", web_app=WebAppInfo(url=url))
    ]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Опиши, какую музыку хочешь («быстрая бочка и грустная мелодия»), "
        "пришли свою формулу или скриншот-пример — я соберу формулы и открою редактор.\n"
        "Или открой пустой редактор и пиши формулы сам 👇",
        reply_markup=open_kb(WEBAPP_URL),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await respond(update, update.message.text)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    f = await msg.photo[-1].get_file()
    data = base64.b64encode(bytes(await f.download_as_bytearray())).decode()
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
        {"type": "text", "text": msg.caption or "Сделай такую же музыку формулами по этому примеру."},
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
        menu_button=MenuButtonWebApp(text="Музыка", web_app=WebAppInfo(url=WEBAPP_URL))
    )


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
