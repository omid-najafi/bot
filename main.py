import asyncio
import json
import logging
import os
import re
import time

import httpx
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("selfbot")

# ---------- تنظیمات (Environment Variables) ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# هوش مصنوعی (اختیاری - هر سرویس سازگار با OpenAI)
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")

REPLY_COOLDOWN = 20  # ثانیه، حداقل فاصله پاسخ در هر چت

# ---------- ذخیره وضعیت ----------
DEFAULT_STATE = {
    "session": "",
    "autoreply": {"enabled": False, "ai": False, "text": ""},
    "jobs": [],  # {"chat": int, "word": str, "minutes": int}
}


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULT_STATE.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return json.loads(json.dumps(DEFAULT_STATE))


def save_state():
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


state = load_state()

bot = TelegramClient(StringSession(), API_ID, API_HASH)
user: TelegramClient | None = None
tasks: dict[tuple, asyncio.Task] = {}
last_reply: dict[int, float] = {}
login = {"step": None, "client": None, "phone": None, "hash": None}


# ---------- اسپم زمان‌بندی‌شده ----------
async def run_job(client, chat, word, minutes):
    while True:
        try:
            await client.send_message(chat, word)
        except errors.FloodWaitError as ex:
            await asyncio.sleep(ex.seconds + 1)
            continue
        except Exception as ex:
            log.warning("send failed in %s: %s", chat, ex)
        await asyncio.sleep(minutes * 60)


def start_job(client, chat, word, minutes, persist=True):
    key = (chat, word)
    if key in tasks:
        tasks[key].cancel()
    tasks[key] = asyncio.create_task(run_job(client, chat, word, minutes))
    if persist:
        state["jobs"] = [j for j in state["jobs"] if (j["chat"], j["word"]) != key]
        state["jobs"].append({"chat": chat, "word": word, "minutes": minutes})
        save_state()


def stop_job(chat, word=None):
    removed = 0
    for key in list(tasks):
        if key[0] == chat and (word is None or key[1] == word):
            tasks.pop(key).cancel()
            removed += 1
    state["jobs"] = [
        j for j in state["jobs"]
        if not (j["chat"] == chat and (word is None or j["word"] == word))
    ]
    save_state()
    return removed


# ---------- هوش مصنوعی ----------
async def ai_reply(instruction: str, incoming: str) -> str:
    async with httpx.AsyncClient(timeout=40) as http:
        r = await http.post(
            f"{AI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {AI_API_KEY}"},
            json={
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": incoming},
                ],
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


# ---------- هندلرهای اکانت کاربر ----------
def setup_user(client: TelegramClient):
    @client.on(events.NewMessage(outgoing=True))
    async def commands(e):
        t = (e.raw_text or "").strip()
        if not t:
            return
        ar = state["autoreply"]

        # توقف
        if t == "توقف همه":
            n = stop_job(e.chat_id)
            await e.delete()
            await client.send_message("me", f"{n} اسپم متوقف شد.")
            return
        m = re.fullmatch(r"توقف\s+(\S+)", t)
        if m:
            n = stop_job(e.chat_id, m.group(1))
            await e.delete()
            await client.send_message("me", f"{n} اسپم متوقف شد.")
            return

        # لیست
        if t == "لیست":
            await e.delete()
            lines = [f"{j['chat']} | {j['word']} | هر {j['minutes']} دقیقه" for j in state["jobs"]]
            await client.send_message("me", "\n".join(lines) or "هیچ اسپمی فعال نیست.")
            return

        # پاسخ خودکار
        if t == "پاسخ روشن":
            ar["enabled"] = True; save_state(); await e.delete()
            await client.send_message("me", "پاسخ خودکار روشن شد ✅"); return
        if t == "پاسخ خاموش":
            ar["enabled"] = False; save_state(); await e.delete()
            await client.send_message("me", "پاسخ خودکار خاموش شد ❌"); return
        if t == "هوش روشن":
            ar["ai"] = True; save_state(); await e.delete()
            await client.send_message("me", "هوش مصنوعی روشن شد 🤖"); return
        if t == "هوش خاموش":
            ar["ai"] = False; save_state(); await e.delete()
            await client.send_message("me", "هوش مصنوعی خاموش شد"); return
        if t.startswith("متن پاسخ "):
            ar["text"] = t[len("متن پاسخ "):].strip(); save_state(); await e.delete()
            await client.send_message("me", "متن پاسخ ذخیره شد ✅"); return

        # دستور اسپم: «میو 60» فقط داخل گروه
        m = re.fullmatch(r"(\S+)\s+(\d+)", t)
        if m and e.is_group:
            word, minutes = m.group(1), max(1, int(m.group(2)))
            await e.delete()
            start_job(client, e.chat_id, word, minutes)
            await client.send_message("me", f"«{word}» هر {minutes} دقیقه در گروه ارسال می‌شود.")

    @client.on(events.NewMessage(incoming=True))
    async def auto_reply(e):
        ar = state["autoreply"]
        if not ar["enabled"] or not ar["text"]:
            return
        if not (e.is_private or e.is_group):
            return
        sender = await e.get_sender()
        if sender is None or getattr(sender, "bot", False):
            return

        # در گروه فقط وقتی منشن شدم یا روی پیام من ریپلای شد
        if e.is_group:
            replied_to_me = False
            if e.is_reply:
                rm = await e.get_reply_message()
                replied_to_me = bool(rm and rm.out)
            if not (e.mentioned or replied_to_me):
                return

        now = time.time()
        if now - last_reply.get(e.chat_id, 0) < REPLY_COOLDOWN:
            return
        last_reply[e.chat_id] = now

        try:
            if ar["ai"] and AI_API_KEY:
                if not e.raw_text:
                    return
                text = await ai_reply(ar["text"], e.raw_text)
            else:
                text = ar["text"]
            if text:
                await e.reply(text)
        except Exception as ex:
            log.warning("auto reply failed: %s", ex)


async def start_user(session_str):
    global user
    user = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await user.connect()
    if not await user.is_user_authorized():
        return False
    setup_user(user)
    for j in state["jobs"]:
        start_job(user, j["chat"], j["word"], j["minutes"], persist=False)
    return True


# ---------- ربات مدیریتی برای لاگین ----------
@bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def manager(e):
    global user
    if e.sender_id != OWNER_ID:
        return
    t = (e.raw_text or "").strip()

    if t == "/start":
        if user and await user.is_user_authorized():
            await e.respond("✅ اکانت متصل است.\nبرای خروج: /logout")
            return
        login.update(step="phone", client=None)
        await e.respond("شماره را با کد کشور بفرست. مثال: +989123456789")
        return

    if t == "/logout":
        for k in list(tasks):
            tasks.pop(k).cancel()
        if user:
            try:
                await user.log_out()
            except Exception:
                pass
            user = None
        state["session"] = ""; state["jobs"] = []; save_state()
        login.update(step=None, client=None)
        await e.respond("خارج شدی. برای ورود دوباره /start")
        return

    step = login["step"]
    try:
        if step == "phone":
            c = TelegramClient(StringSession(), API_ID, API_HASH)
            await c.connect()
            sent = await c.send_code_request(t.replace(" ", ""))
            login.update(client=c, phone=t.replace(" ", ""), hash=sent.phone_code_hash, step="code")
            await e.respond(
                "کد ارسال شد. ⚠️ کد را با خط‌تیره یا فاصله بفرست (مثلاً 1-2-3-4-5) "
                "وگرنه تلگرام آن را باطل می‌کند."
            )

        elif step == "code":
            code = re.sub(r"\D", "", t)
            try:
                await login["client"].sign_in(login["phone"], code, phone_code_hash=login["hash"])
            except errors.SessionPasswordNeededError:
                login["step"] = "password"
                await e.respond("رمز دومرحله‌ای (2FA) را بفرست:")
                return
            await finish_login(e)

        elif step == "password":
            await login["client"].sign_in(password=t)
            try:
                await e.delete()
            except Exception:
                pass
            await finish_login(e)

    except errors.PhoneCodeInvalidError:
        await e.respond("کد اشتباه است. دوباره بفرست.")
    except errors.PhoneCodeExpiredError:
        login.update(step=None)
        await e.respond("کد منقضی شد. /start بزن.")
    except errors.PasswordHashInvalidError:
        await e.respond("رمز اشتباه است. دوباره بفرست.")
    except Exception as ex:
        login.update(step=None)
        await e.respond(f"خطا: {ex}\nدوباره /start بزن.")


async def finish_login(e):
    global user
    state["session"] = login["client"].session.save()
    save_state()
    await login["client"].disconnect()
    login.update(step=None, client=None)
    await start_user(state["session"])
    await e.respond(
        "✅ وصل شد!\n\n"
        "دستورات (در هر چت از اکانت خودت بنویس):\n"
        "• `میو 60` ← (در گروه) هر 60 دقیقه «میو» ارسال شود\n"
        "• `توقف میو` / `توقف همه` ← توقف اسپم در همان گروه\n"
        "• `لیست` ← لیست اسپم‌های فعال\n"
        "• `متن پاسخ ...` ← تعیین متن پاسخ خودکار\n"
        "• `پاسخ روشن` / `پاسخ خاموش`\n"
        "• `هوش روشن` / `هوش خاموش` ← متن پاسخ به‌عنوان دستور برای AI",
    )


async def main():
    await bot.start(bot_token=BOT_TOKEN)
    if state["session"]:
        ok = await start_user(state["session"])
        log.info("user session restored: %s", ok)
    log.info("Bot is running")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
