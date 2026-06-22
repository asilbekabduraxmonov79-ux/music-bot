import asyncio
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

import yt_dlp
import aiohttp
from aiohttp import web

# ══════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8684337468:AAH0DdUJZ0L90-aEcx7sFH0pFzsfiDTH__0")
ADMIN_IDS = [5599261398]
_DATA_DIR = Path(".")
DOWNLOAD_DIR = _DATA_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
DB_PATH = str(_DATA_DIR / "bot.db")

# Cookies fayl yo'llari
COOKIES_PATHS = [
    "/etc/secrets/youtube_cookies.txt",  # Render Secret File
    "youtube_cookies.txt",                # GitHub'dagi fayl
    "cookies.txt",                        # Umumiy nom
]
# ══════════════════════════════════════════════

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

search_cache = {}
video_cache = {}
URL_RE = re.compile(r"https?://\S+")

def find_cookies_file() -> str:
    """Cookies faylni topish"""
    for path in COOKIES_PATHS:
        if os.path.exists(path):
            print(f"✅ Cookies fayl topildi: {path}")
            return path
    print("⚠️ Cookies fayl topilmadi!")
    return None

COOKIES_FILE = find_cookies_file()

def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS required_channels (
        channel_id TEXT PRIMARY KEY, channel_name TEXT, invite_link TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, banned INTEGER DEFAULT 0)""")
    con.execute("""CREATE TABLE IF NOT EXISTS movies (
        code TEXT PRIMARY KEY, file_id TEXT, title TEXT, caption TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY, username TEXT, added_by INTEGER)""")
    con.commit()
    try:
        con.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
        con.commit()
    except:
        pass
    con.close()

def db_add_channel(ch_id, name, link):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR REPLACE INTO required_channels VALUES (?,?,?)", (ch_id, name, link))
    con.commit()
    con.close()

def db_remove_channel(ch_id):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM required_channels WHERE channel_id=?", (ch_id,))
    con.commit()
    con.close()

def db_get_channels():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT channel_id,channel_name,invite_link FROM required_channels").fetchall()
    con.close()
    return rows

def db_add_user(uid, uname):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR IGNORE INTO users VALUES (?,?,0)", (uid, uname))
    con.commit()
    con.close()

def db_user_count():
    con = sqlite3.connect(DB_PATH)
    c = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    con.close()
    return c

def db_all_users():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
    con.close()
    return [r[0] for r in rows]

def db_ban_user(uid):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    con.commit()
    con.close()

def db_unban_user(uid):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    con.commit()
    con.close()

def db_is_banned(uid):
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT banned FROM users WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return bool(row and row[0])

def db_banned_users():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT user_id, username FROM users WHERE banned=1").fetchall()
    con.close()
    return rows

def db_banned_count():
    con = sqlite3.connect(DB_PATH)
    c = con.execute("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0]
    con.close()
    return c

def db_add_admin(uid, uname, added_by):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR REPLACE INTO admins VALUES (?,?,?)", (uid, uname, added_by))
    con.commit()
    con.close()

def db_remove_admin(uid):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    con.commit()
    con.close()

def db_all_admins():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT user_id, username FROM admins").fetchall()
    con.close()
    return rows

def db_is_sub_admin(uid):
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return bool(row)

def db_add_movie(code, file_id, title, caption=""):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR REPLACE INTO movies VALUES (?,?,?,?)", (code, file_id, title, caption))
    con.commit()
    con.close()

def db_get_movie(code):
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT file_id,title,caption FROM movies WHERE code=?", (code,)).fetchone()
    con.close()
    if not row:
        return None
    return {"file_id": row[0], "title": row[1], "caption": row[2]}

def db_remove_movie(code):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM movies WHERE code=?", (code,))
    con.commit()
    con.close()

def db_all_movies():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT code,title FROM movies ORDER BY CAST(code AS INTEGER)").fetchall()
    con.close()
    return rows

def db_next_movie_code():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT code FROM movies").fetchall()
    con.close()
    used = set()
    for (c,) in rows:
        if c.isdigit():
            used.add(int(c))
    n = 1
    while n in used:
        n += 1
    return str(n)

async def check_subs(uid):
    not_joined = []
    for ch_id, name, link in db_get_channels():
        try:
            m = await bot.get_chat_member(ch_id, uid)
            if m.status in ("left", "kicked"):
                not_joined.append((name, link))
        except:
            not_joined.append((name, link))
    return not_joined

def sub_kb(not_joined):
    btns = [[InlineKeyboardButton(text=f"📢 {n}", url=l)] for n, l in not_joined]
    btns.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

async def guard(message: types.Message) -> bool:
    if db_is_banned(message.from_user.id):
        await message.answer("🚫 Siz botdan foydalanishdan bloklangansiz.")
        return False
    nj = await check_subs(message.from_user.id)
    if nj:
        await message.answer("⚠️ Avval quyidagi kanallarga obuna bo'ling:", reply_markup=sub_kb(nj))
        return False
    return True

def fmt_duration(dur):
    if not dur:
        return "?"
    try:
        dur = int(dur)
    except:
        return "?"
    return f"{dur // 60}:{dur % 60:02d}"

def search_songs(query: str, count: int = 10) -> list:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
                "skip": ["hls", "dash"],
            }
        }
    }
    if COOKIES_FILE:
        ydl_opts["cookiefile"] = COOKIES_FILE
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
        results = []
        for entry in info.get("entries", []):
            vid_id = entry.get("id", "")
            url = entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}"
            results.append({
                "title": entry.get("title", "Noma'lum"),
                "url": url,
                "duration": entry.get("duration") or 0,
                "uploader": entry.get("uploader", ""),
            })
        return results

def download_mp3(query: str, out_dir: Path) -> dict:
    is_url = query.startswith("http")
    search = query if is_url else f"ytsearch1:{query}"
    
    ydl_opts = {
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "postprocessor_args": {"ffmpeg": ["-ar", "44100"]},
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
                "skip": ["hls", "dash"],
            }
        }
    }
    
    # Cookies qo'shish
    if COOKIES_FILE:
        ydl_opts["cookiefile"] = COOKIES_FILE
        print(f"✅ Cookies ishlatilmoqda: {COOKIES_FILE}")
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(search, download=True)
    if "entries" in info:
        info = info["entries"][0]
    filename = ydl.prepare_filename(info)
    mp3 = str(Path(filename).with_suffix(".mp3"))
    if not Path(mp3).exists():
        for f in Path(out_dir).glob("*.mp3"):
            mp3 = str(f)
            break
    return {
        "title": info.get("title", "Noma'lum"),
        "artist": info.get("uploader", ""),
        "duration": info.get("duration", 0),
        "path": mp3,
    }

def download_video(url: str, out_dir: Path) -> str:
    ydl_opts = {
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
        "format": "best[height<=480][ext=mp4]/best[ext=mp4]/best",
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
                "skip": ["hls", "dash"],
            }
        }
    }
    
    # Cookies qo'shish
    if COOKIES_FILE:
        ydl_opts["cookiefile"] = COOKIES_FILE
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        mp4 = str(Path(filename).with_suffix(".mp4"))
        if not Path(mp4).exists():
            for f in Path(out_dir).glob("*.mp4"):
                mp4 = str(f)
                break
        return mp4

async def recognize_audio(file_path: str) -> dict:
    url = "https://api.audd.io/"
    with open(file_path, "rb") as f:
        data = aiohttp.FormData()
        data.add_field("api_token", "test")
        data.add_field("file", f, filename="audio.mp3", content_type="audio/mpeg")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as resp:
                result = await resp.json()
    if result.get("status") == "success" and result.get("result"):
        r = result["result"]
        return {"title": r.get("title", ""), "artist": r.get("artist", "")}
    return {}

def is_owner(uid): return uid in ADMIN_IDS
def is_admin(uid): return uid in ADMIN_IDS or db_is_sub_admin(uid)

admin_movie_state = {}
admin_pending_action = {}

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    db_add_user(message.from_user.id, message.from_user.username or "")
    nj = await check_subs(message.from_user.id)
    if nj:
        await message.answer("⚠️ Avval obuna bo'ling:", reply_markup=sub_kb(nj))
        return
    await message.answer(
        "👋 <b>Salom!</b>\n\n"
        "🎵 Qo'shiq/xonanda nomi yozing → MP3\n"
        "🔗 YouTube link yuboring → Video + Audio\n"
        "🎤 Audio/video yuboring → Qo'shiqni aniqlaydi"
    )

@dp.message_handler(commands=['admin'])
async def cmd_admin(message: types.Message):
    uid = message.from_user.id
    if not is_admin(uid): return
    btns = [
        [InlineKeyboardButton(text="📢 Kanal qo'shish", callback_data="adm_add")],
        [InlineKeyboardButton(text="🗑 Kanal o'chirish", callback_data="adm_del")],
        [InlineKeyboardButton(text="📋 Kanallar", callback_data="adm_list")],
        [InlineKeyboardButton(text="📣 Reklama", callback_data="adm_ads")],
        [InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="adm_users")],
        [InlineKeyboardButton(text="🚫 Bloklash", callback_data="adm_ban"),
         InlineKeyboardButton(text="✅ Blokdan chiqarish", callback_data="adm_unban")],
        [InlineKeyboardButton(text="🎬 Kino qo'shish", callback_data="adm_movie_add")],
        [InlineKeyboardButton(text="🗑 Kino o'chirish", callback_data="adm_movie_del")],
        [InlineKeyboardButton(text="📋 Kinolar ro'yxati", callback_data="adm_movie_list")],
    ]
    if is_owner(uid):
        btns.append([InlineKeyboardButton(text="🛡 Admin qo'shish", callback_data="adm_addadmin"),
                     InlineKeyboardButton(text="🛡 Adminlar ro'yxati", callback_data="adm_listadmins")])
    await message.answer("🔧 <b>Admin panel</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.message_handler(commands=['addch'])
async def cmd_addch(message: types.Message):
    if not is_admin(message.from_user.id): return
    p = message.text.split(maxsplit=3)
    if len(p) < 4:
        await message.answer("❗ <code>/addch @id Nomi https://link</code>")
        return
    db_add_channel(p[1], p[2], p[3])
    await message.answer(f"✅ <b>{p[2]}</b> qo'shildi")

@dp.message_handler(commands=['ads'])
async def cmd_ads(message: types.Message):
    if not is_admin(message.from_user.id): return
    text = message.text.removeprefix("/ads").strip()
    if not text:
        await message.answer("❗ Matn bo'sh!")
        return
    users = db_all_users()
    msg = await message.answer(f"📣 {len(users)} ta foydalanuvchiga yuborilmoqda…")
    ok = 0
    for uid in users:
        try:
            await bot.send_message(uid, text)
            ok += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await msg.edit_text(f"✅ Yuborildi: {ok}/{len(users)}")

@dp.message_handler(commands=['cancel'])
async def cmd_cancel(message: types.Message):
    if not is_admin(message.from_user.id): return
    cleared = bool(admin_movie_state.pop(message.from_user.id, None)) or bool(admin_pending_action.pop(message.from_user.id, None))
    if cleared:
        await message.answer("❌ Bekor qilindi.")

@dp.message_handler(lambda m: m.text and m.text.startswith("#"))
async def h_movie_code(message: types.Message):
    uid = message.from_user.id
    code = message.text.strip().removeprefix("#")
    if not await guard(message): return
    db_add_user(uid, message.from_user.username or "")
    movie = db_get_movie(code)
    if not movie:
        await message.answer("❓ Bu kodga mos kino topilmadi.")
        return
    try:
        await message.answer_video(movie["file_id"], caption=movie.get("caption") or f"🎬 <b>{movie['title']}</b>")
    except Exception as e:
        logger.exception(e)
        await message.answer("❌ Kinoni yuborishda xatolik yuz berdi.")

@dp.message_handler(lambda m: m.text and URL_RE.search(m.text))
async def h_url(message: types.Message):
    uid = message.from_user.id
    if not await guard(message): return
    db_add_user(uid, message.from_user.username or "")
    url = URL_RE.search(message.text).group()
    msg = await message.answer("⬇️ Yuklanmoqda…")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            vid_dir = Path(tmpdir) / "vid"
            aud_dir = Path(tmpdir) / "aud"
            vid_dir.mkdir(exist_ok=True)
            aud_dir.mkdir(exist_ok=True)
            vid_path = None
            aud_path = None
            title = "Noma'lum"
            try:
                vid_path = download_video(url, vid_dir)
                print(f"✅ Video yuklandi: {vid_path}")
            except Exception as e:
                logger.exception(f"Video yuklanmadi: {e}")
            try:
                aud = download_mp3(url, aud_dir)
                aud_path = aud["path"]
                title = aud["title"]
                print(f"✅ Audio yuklandi: {aud_path}")
            except Exception as e:
                logger.exception(f"Audio yuklanmadi: {e}")
            if not vid_path and not aud_path:
                await msg.edit_text("❌ Yuklab bo'lmadi.")
                return
            await msg.delete()
            if vid_path and Path(vid_path).exists():
                size = os.path.getsize(vid_path) / (1024 * 1024)
                if size <= 50:
                    await message.answer_video(types.InputFile(vid_path), caption=f"🎬 <b>{title}</b>")
            if aud_path and Path(aud_path).exists():
                await message.answer_audio(types.InputFile(aud_path), title=title)
    except Exception as e:
        logger.exception(e)
        await msg.edit_text("❌ Yuklab bo'lmadi.")

@dp.message_handler(content_types=['text'])
async def h_text(message: types.Message):
    uid = message.from_user.id
    text = message.text.strip()
    if not await guard(message): return
    db_add_user(uid, message.from_user.username or "")
    msg = await message.answer(f"🔍 <b>{text}</b> qidirilmoqda…")
    try:
        results = search_songs(text, count=10)
        if not results:
            await msg.edit_text("❌ Qo'shiq topilmadi.")
            return
        lines = [f"🔍 {text}", ""]
        for i, r in enumerate(results, start=1):
            lines.append(f"{i}. {r['title']}  {fmt_duration(r['duration'])}")
        kb = InlineKeyboardMarkup(row_width=5)
        buttons = []
        for i in range(1, len(results) + 1):
            buttons.append(InlineKeyboardButton(text=str(i), callback_data=f"dl_{i-1}"))
            if len(buttons) == 5:
                kb.row(*buttons)
                buttons = []
        if buttons:
            kb.row(*buttons)
        kb.row(InlineKeyboardButton(text="❌ Yopish", callback_data="nav_close"))
        await msg.edit_text("\n".join(lines), reply_markup=kb)
        search_cache[uid] = [r["url"] for r in results]
    except Exception as e:
        logger.exception(e)
        await msg.edit_text("❌ Qo'shiq topilmadi.")

@dp.message_handler(content_types=['audio', 'voice', 'video', 'document'])
async def h_audio(message: types.Message):
    if not await guard(message): return
    msg = await message.answer("🎵 Qo'shiq tanib olinmoqda…")
    try:
        if message.audio:
            file_id = message.audio.file_id
        elif message.voice:
            file_id = message.voice.file_id
        elif message.video:
            file_id = message.video.file_id
        elif message.document and message.document.mime_type.startswith(("audio", "video")):
            file_id = message.document.file_id
        else:
            await msg.edit_text("❓ Bu fayl turi qo'llab-quvvatlanmaydi.")
            return
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        file = await bot.get_file(file_id)
        await file.download(tmp_path)
        result = await recognize_audio(tmp_path)
        os.unlink(tmp_path)
        if result and result.get("title"):
            q = f"{result['title']} {result['artist']}"
            await msg.edit_text(f"🎵 <b>{result['title']}</b>\n🎤 {result['artist']}\n\n⬇️ Yuklanmoqda…")
            with tempfile.TemporaryDirectory() as tmpdir:
                aud = download_mp3(q, Path(tmpdir))
                await message.answer_audio(types.InputFile(aud["path"]), title=result["title"], performer=result["artist"])
            await msg.delete()
        else:
            await msg.edit_text("❓ Qo'shiq tanib olinmadi.")
    except Exception as e:
        logger.exception(e)
        await msg.edit_text("❌ Xatolik.")

@dp.callback_query_handler(lambda c: c.data.startswith("dl_"))
async def cb_download(callback_query: types.CallbackQuery):
    uid = callback_query.from_user.id
    try:
        idx = int(callback_query.data.replace("dl_", ""))
    except:
        await callback_query.answer("❌ Xato!", show_alert=True)
        return
    urls = search_cache.get(uid, [])
    if not urls or idx >= len(urls):
        await callback_query.answer("❌ Natija eskirdi!", show_alert=True)
        return
    url = urls[idx]
    await callback_query.answer("⬇️ Yuklanmoqda…")
    msg = await callback_query.message.answer("⬇️ Yuklanmoqda…")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            aud = download_mp3(url, Path(tmpdir))
            await msg.edit_text(f"📤 <b>{aud['title']}</b> yuborilmoqda…")
            await callback_query.message.answer_audio(types.InputFile(aud["path"]), title=aud["title"], performer=aud["artist"])
            await msg.delete()
    except Exception as e:
        logger.exception(e)
        await msg.edit_text("❌ Yuklab bo'lmadi.")

@dp.callback_query_handler(lambda c: c.data == "check_sub")
async def cb_check(callback_query: types.CallbackQuery):
    nj = await check_subs(callback_query.from_user.id)
    if nj:
        await callback_query.answer("❌ Hali obuna bo'lmadingiz!", show_alert=True)
    else:
        await callback_query.answer("✅ Rahmat!")
        await callback_query.message.delete()
        await callback_query.message.answer("✅ <b>Obuna tasdiqlandi!</b>")

@dp.callback_query_handler(lambda c: c.data == "nav_close")
async def cb_close(callback_query: types.CallbackQuery):
    await callback_query.answer()
    await callback_query.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith("adm_"))
async def cb_admin(callback_query: types.CallbackQuery):
    uid = callback_query.from_user.id
    data = callback_query.data
    if not is_admin(uid):
        await callback_query.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    await callback_query.answer()
    if data == "adm_list":
        chs = db_get_channels()
        if not chs:
            await callback_query.message.answer("Kanallar yo'q")
            return
        text = "📋 <b>Kanallar:</b>\n" + "\n".join(f"• {n} ({i})" for i, n, l in chs)
        await callback_query.message.answer(text)
    elif data == "adm_add":
        await callback_query.message.answer("Format:\n<code>/addch @username Kanal nomi https://t.me/link</code>")
    elif data == "adm_del":
        chs = db_get_channels()
        if not chs:
            await callback_query.message.answer("Kanallar yo'q")
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for ch_id, name, link in chs:
            kb.add(InlineKeyboardButton(text=f"🗑 {name}", callback_data=f"rmch_{ch_id}"))
        await callback_query.message.answer("O'chirish:", reply_markup=kb)
    elif data == "adm_ads":
        admin_pending_action[uid] = {"action": "broadcast"}
        await callback_query.message.answer("📣 Reklama xabarini yuboring.\n❌ Bekor: /cancel")
    elif data == "adm_users":
        await callback_query.answer(f"👥 Jami: {db_user_count()}\n🚫 Bloklangan: {db_banned_count()}", show_alert=True)
    elif data == "adm_ban":
        admin_pending_action[uid] = {"action": "ban"}
        await callback_query.message.answer("🚫 Bloklash uchun foydalanuvchi ID raqamini yuboring.\n❌ Bekor: /cancel")
    elif data == "adm_unban":
        banned = db_banned_users()
        if not banned:
            await callback_query.answer("Bloklangan foydalanuvchilar yo'q", show_alert=True)
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for uid_b, uname in banned:
            kb.add(InlineKeyboardButton(text=f"✅ {uname or uid_b}", callback_data=f"unban_{uid_b}"))
        await callback_query.message.answer("Blokdan chiqarish:", reply_markup=kb)
    elif data == "adm_addadmin":
        if not is_owner(uid):
            return
        admin_pending_action[uid] = {"action": "addadmin"}
        await callback_query.message.answer("🛡 Admin ID raqamini yuboring.\n❌ Bekor: /cancel")
    elif data == "adm_listadmins":
        if not is_owner(uid):
            return
        admins = db_all_admins()
        if not admins:
            await callback_query.answer("Yordamchi adminlar yo'q", show_alert=True)
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for aid, uname in admins:
            kb.add(InlineKeyboardButton(text=f"🗑 {uname or aid}", callback_data=f"rmadmin_{aid}"))
        await callback_query.message.answer("🛡 <b>Yordamchi adminlar</b> (bosish — o'chirish):", reply_markup=kb)
    elif data == "adm_movie_add":
        admin_movie_state[uid] = {"step": "wait_file"}
        await callback_query.message.answer("🎬 Kino faylini yuboring.\n❌ Bekor: /cancel")
    elif data == "adm_movie_list":
        movies = db_all_movies()
        if not movies:
            await callback_query.answer("Kinolar yo'q", show_alert=True)
            return
        text = f"📋 <b>Kinolar ({len(movies)} ta):</b>\n" + "\n".join(f"• #{code} — {title}" for code, title in movies)
        await callback_query.message.answer(text[:4000])
    elif data == "adm_movie_del":
        movies = db_all_movies()
        if not movies:
            await callback_query.answer("Kinolar yo'q", show_alert=True)
            return
        kb = InlineKeyboardMarkup(row_width=5)
        buttons = []
        for code, title in movies[:60]:
            buttons.append(InlineKeyboardButton(text=f"#{code}", callback_data=f"rmmovie_{code}"))
        kb.add(*buttons)
        await callback_query.message.answer("🗑 O'chirish uchun kodni tanlang:", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("rmch_"))
async def cb_rmch(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id): return
    db_remove_channel(callback_query.data.replace("rmch_", ""))
    await callback_query.answer("✅ O'chirildi", show_alert=True)
    await callback_query.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith("unban_"))
async def cb_unban(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id): return
    target = int(callback_query.data.replace("unban_", ""))
    db_unban_user(target)
    await callback_query.answer("✅ Blokdan chiqarildi", show_alert=True)
    await callback_query.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith("rmadmin_"))
async def cb_rmadmin(callback_query: types.CallbackQuery):
    if not is_owner(callback_query.from_user.id): return
    target = int(callback_query.data.replace("rmadmin_", ""))
    db_remove_admin(target)
    await callback_query.answer("✅ O'chirildi", show_alert=True)
    await callback_query.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith("rmmovie_"))
async def cb_rmmovie(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id): return
    code = callback_query.data.replace("rmmovie_", "")
    db_remove_movie(code)
    await callback_query.answer(f"✅ Kino {code} o'chirildi", show_alert=True)
    await callback_query.message.delete()

@dp.message_handler(content_types=['video', 'document'])
async def h_admin_movie_file(message: types.Message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    state = admin_movie_state.get(uid)
    if not state or state.get("step") != "wait_file":
        return
    file_id = message.video.file_id if message.video else message.document.file_id
    title = (message.caption or "Noma'lum film").split("\n")[0][:80]
    state["file_id"] = file_id
    state["title"] = title
    state["caption"] = message.caption or ""
    state["step"] = "wait_code"
    suggested = db_next_movie_code()
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(text=f"✅ #{suggested} (taklif)", callback_data=f"usecode_{suggested}"))
    await message.answer(f"🎬 Fayl qabul qilindi: <b>{title}</b>\n\nKod kiriting:", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("usecode_"))
async def cb_usecode(callback_query: types.CallbackQuery):
    uid = callback_query.from_user.id
    state = admin_movie_state.get(uid)
    if not is_admin(uid) or not state or state.get("step") != "wait_code":
        await callback_query.answer("❌ Jarayon topilmadi.", show_alert=True)
        return
    code = callback_query.data.replace("usecode_", "")
    db_add_movie(code, state["file_id"], state["title"], state.get("caption", ""))
    admin_movie_state.pop(uid, None)
    await callback_query.answer("✅ Saqlandi!", show_alert=True)
    await callback_query.message.answer(f"✅ Kino <b>{state['title']}</b> kod <b>#{code}</b> bilan saqlandi.")

@dp.message_handler(lambda m: m.text and m.text.isdigit())
async def h_admin_numeric(message: types.Message):
    uid = message.from_user.id
    text = message.text.strip()
    if not is_admin(uid):
        return
    movie_state = admin_movie_state.get(uid)
    if movie_state and movie_state.get("step") == "wait_code":
        if db_get_movie(text):
            await message.answer(f"⚠️ Kod {text} band.")
            return
        db_add_movie(text, movie_state["file_id"], movie_state["title"], movie_state.get("caption", ""))
        admin_movie_state.pop(uid, None)
        await message.answer(f"✅ Kino <b>{movie_state['title']}</b> kod <b>#{text}</b> bilan saqlandi.")
        return
    pending = admin_pending_action.get(uid)
    if pending:
        action = pending["action"]
        target = int(text)
        admin_pending_action.pop(uid, None)
        if action == "ban":
            if target in ADMIN_IDS:
                await message.answer("❌ Bosh adminni bloklab bo'lmaydi.")
                return
            db_ban_user(target)
            await message.answer(f"🚫 Foydalanuvchi <code>{target}</code> bloklandi.")
        elif action == "unban":
            db_unban_user(target)
            await message.answer(f"✅ Foydalanuvchi <code>{target}</code> blokdan chiqarildi.")
        elif action == "addadmin":
            if not is_owner(uid):
                return
            db_add_admin(target, "", uid)
            await message.answer(f"🛡 Foydalanuvchi <code>{target}</code> yordamchi admin.")
        return

# ══════════════════════════════════════════════
# Render uchun web server (port binding)
# ══════════════════════════════════════════════
async def start_web_server():
    try:
        app = web.Application()
        async def health(request):
            return web.Response(text="Bot ishlayapti ✅")
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 10000))
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"✅ Web server {port}-portda ishga tushdi")
    except Exception as e:
        print(f"⚠️ Web server ishga tushmadi: {e}")

if __name__ == "__main__":
    db_init()
    logger.info("Bot ishga tushdi ✅")
    loop = asyncio.get_event_loop()
    loop.create_task(start_web_server())
    executor.start_polling(dp, skip_updates=True)
