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

# ==================== SOZLAMALAR ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8684337468:AAH0DdUJZ0L90-aEcx7sFH0pFzsfiDTH__0")
ADMIN_IDS = [5599261398]
_DATA_DIR = Path(".")
DOWNLOAD_DIR = _DATA_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
DB_PATH = str(_DATA_DIR / "bot.db")

# Logging sozlamalari
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

search_cache = {}
video_cache = {}
URL_RE = re.compile(r"https?://\S+")

# ==================== DATABASE ====================
def db_init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS required_channels (
        channel_id TEXT PRIMARY KEY, channel_name TEXT, invite_link TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, banned INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS movies (
        code TEXT PRIMARY KEY, file_id TEXT, title TEXT, caption TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY, username TEXT, added_by INTEGER)""")
    conn.commit()
    try:
        conn.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
        conn.commit()
    except:
        pass
    conn.close()

def db_add_channel(ch_id, name, link):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO required_channels VALUES (?,?,?)", (ch_id, name, link))
    conn.commit()
    conn.close()

def db_remove_channel(ch_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM required_channels WHERE channel_id=?", (ch_id,))
    conn.commit()
    conn.close()

def db_get_channels():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT channel_id,channel_name,invite_link FROM required_channels").fetchall()
    conn.close()
    return rows

def db_add_user(uid, uname):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO users VALUES (?,?,0)", (uid, uname))
    conn.commit()
    conn.close()

def db_user_count():
    conn = sqlite3.connect(DB_PATH)
    c = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return c

def db_all_users():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
    conn.close()
    return [r[0] for r in rows]

def db_ban_user(uid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def db_unban_user(uid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def db_is_banned(uid):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT banned FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return bool(row and row[0])

def db_banned_users():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT user_id, username FROM users WHERE banned=1").fetchall()
    conn.close()
    return rows

def db_banned_count():
    conn = sqlite3.connect(DB_PATH)
    c = conn.execute("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0]
    conn.close()
    return c

def db_add_admin(uid, uname, added_by):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO admins VALUES (?,?,?)", (uid, uname, added_by))
    conn.commit()
    conn.close()

def db_remove_admin(uid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def db_all_admins():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT user_id, username FROM admins").fetchall()
    conn.close()
    return rows

def db_is_admin(uid):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return bool(row)

def db_add_movie(code, file_id, title, caption=""):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO movies VALUES (?,?,?,?)", (code, file_id, title, caption))
    conn.commit()
    conn.close()

def db_get_movie(code):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT file_id,title,caption FROM movies WHERE code=?", (code,)).fetchone()
    conn.close()
    if not row:
        return None
    return {"file_id": row[0], "title": row[1], "caption": row[2]}

def db_remove_movie(code):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM movies WHERE code=?", (code,))
    conn.commit()
    conn.close()

def db_all_movies():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT code,title FROM movies ORDER BY CAST(code AS INTEGER)").fetchall()
    conn.close()
    return rows

def db_movie_count():
    conn = sqlite3.connect(DB_PATH)
    c = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
    conn.close()
    return c

def db_next_movie_code():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT code FROM movies").fetchall()
    conn.close()
    used = set()
    for (c,) in rows:
        if c.isdigit():
            used.add(int(c))
    n = 1
    while n in used:
        n += 1
    return str(n)

# ==================== SUBSCRIPTION ====================
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
        await message.answer("🚫 Siz bloklangansiz.")
        return False
    nj = await check_subs(message.from_user.id)
    if nj:
        await message.answer("⚠️ Kanallarga obuna bo'ling:", reply_markup=sub_kb(nj))
        return False
    return True

# ==================== YOUTUBE FUNKSIYALARI ====================
def search_songs(query: str, count: int = 10) -> list:
    """YouTube dan qo'shiq qidirish"""
    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
            results = []
            for entry in info.get("entries", []):
                if entry:
                    dur = entry.get("duration", 0)
                    results.append({
                        "title": entry.get("title", "Noma'lum"),
                        "url": f"https://youtube.com/watch?v={entry.get('id', '')}",
                        "duration": dur,
                        "duration_str": f"{dur//60}:{dur%60:02d}" if dur else "?",
                    })
            return results
    except Exception as e:
        print(f"Qidiruv xatosi: {e}")
        return []

def download_audio(url: str, out_dir: Path) -> str:
    """YouTube dan audio yuklash"""
    ydl_opts = {
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "quiet": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return str(Path(filename).with_suffix(".mp3"))

def download_video(url: str, out_dir: Path) -> str:
    """YouTube dan video yuklash"""
    ydl_opts = {
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "format": "best[height<=480][ext=mp4]/best",
        "quiet": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        mp4 = str(Path(filename).with_suffix(".mp4"))
        if not Path(mp4).exists():
            for f in Path(out_dir).glob("*.mp4"):
                mp4 = str(f)
                break
        return mp4

def download_instagram(url: str, out_dir: Path) -> str:
    """Instagram dan video yuklash"""
    ydl_opts = {
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "format": "best",
        "quiet": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        mp4 = str(Path(filename).with_suffix(".mp4"))
        if not Path(mp4).exists():
            for f in Path(out_dir).glob("*.mp4"):
                mp4 = str(f)
                break
        return mp4

# ==================== AUDIO RECOGNITION ====================
async def recognize_audio(file_path: str) -> dict:
    """Audio fayldan qo'shiq nomini aniqlash (Shazam)"""
    try:
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
            return {
                "title": r.get("title", ""),
                "artist": r.get("artist", ""),
                "album": r.get("album", ""),
            }
    except Exception as e:
        print(f"Recognition xatosi: {e}")
    return {}

# ==================== ADMIN ====================
def is_admin(uid):
    return uid in ADMIN_IDS or db_is_admin(uid)

admin_movie_state = {}
admin_pending_action = {}

# ==================== START ====================
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    db_add_user(uid, message.from_user.username or "")
    if not await guard(message):
        return
    await message.answer(
        "👋 <b>Salom! Music Bot</b>\n\n"
        "🎵 <b>Qo'shiq nomi</b> yozing → MP3\n"
        "🔗 <b>YouTube havola</b> → Audio + Video\n"
        "📸 <b>Instagram havola</b> → Video\n"
        "🎤 <b>Audio/video</b> yuboring → Qo'shiqni aniqlayman\n"
        "🎬 <b>#kod</b> yozing → Kinoni yuboraman\n\n"
        "📝 Misol: <code>Jaloliddin Ahmadaliyev Sog'indim</code>"
    )

# ==================== ADMIN PANEL ====================
@dp.message_handler(commands=['admin'])
async def cmd_admin(message: types.Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("❌ Ruxsat yo'q!")
        return
    
    btns = [
        [InlineKeyboardButton("📢 Kanal qo'shish", callback_data="adm_add")],
        [InlineKeyboardButton("🗑 Kanal o'chirish", callback_data="adm_del")],
        [InlineKeyboardButton("📋 Kanallar", callback_data="adm_list")],
        [InlineKeyboardButton("📣 Reklama", callback_data="adm_ads")],
        [InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="adm_users")],
        [InlineKeyboardButton("🚫 Bloklash", callback_data="adm_ban")],
        [InlineKeyboardButton("✅ Blokdan chiqarish", callback_data="adm_unban")],
        [InlineKeyboardButton("🎬 Kino qo'shish", callback_data="adm_movie_add")],
        [InlineKeyboardButton("🗑 Kino o'chirish", callback_data="adm_movie_del")],
        [InlineKeyboardButton("📋 Kinolar", callback_data="adm_movie_list")],
    ]
    await message.answer("🔧 <b>Admin panel</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

# ==================== QO'SHIQ QIDIRISH ====================
@dp.message_handler(content_types=['text'])
async def h_text(message: types.Message):
    uid = message.from_user.id
    text = message.text.strip()
    
    if text.startswith('/'):
        return
    
    if not await guard(message):
        return
    
    db_add_user(uid, message.from_user.username or "")
    
    # URL tekshirish
    url_match = URL_RE.search(text)
    if url_match:
        url = url_match.group()
        
        # Instagram
        if 'instagram.com' in url:
            msg = await message.answer("⬇️ Instagram video yuklanmoqda...")
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    vid = download_instagram(url, Path(tmpdir))
                    await msg.edit_text("📤 Yuborilmoqda...")
                    with open(vid, 'rb') as f:
                        await message.answer_video(f)
                    await msg.delete()
            except Exception as e:
                await msg.edit_text(f"❌ Xato: {str(e)[:100]}")
            return
        
        # YouTube
        if 'youtube.com' in url or 'youtu.be' in url:
            btns = [
                [InlineKeyboardButton("🎵 Audio", callback_data=f"audio_{url}")],
                [InlineKeyboardButton("🎬 Video", callback_data=f"video_{url}")]
            ]
            await message.answer("Yuklab olish turini tanlang:", reply_markup=InlineKeyboardMarkup(btns))
            return
    
    # Qo'shiq qidirish (10 ta natija)
    msg = await message.answer(f"🔍 '{text}' qidirilmoqda...")
    try:
        results = search_songs(text, count=10)
        if not results:
            await msg.edit_text("❌ Qo'shiq topilmadi.")
            return
        
        lines = [f"🔍 <b>{text}</b>\n"]
        for i, r in enumerate(results, start=1):
            lines.append(f"{i}. {r['title']}  <i>{r['duration_str']}</i>")
        
        # Tugmalar (10 ta raqam)
        kb = InlineKeyboardMarkup(row_width=5)
        btns = []
        for i in range(1, len(results) + 1):
            btns.append(InlineKeyboardButton(text=str(i), callback_data=f"dl_{i-1}"))
            if len(btns) == 5:
                kb.row(*btns)
                btns = []
        if btns:
            kb.row(*btns)
        kb.row(InlineKeyboardButton("❌ Yopish", callback_data="nav_close"))
        
        # Keyingi sahifa tugmasi (agar 10 ta bo'lsa)
        if len(results) == 10:
            kb.row(InlineKeyboardButton("➡️ Keyingi 10 ta", callback_data="next_10"))
        
        await msg.edit_text("\n".join(lines), reply_markup=kb, parse_mode=ParseMode.HTML)
        search_cache[uid] = results
        
    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:100]}")

# ==================== CALLBACK ====================
@dp.callback_query_handler(lambda c: c.data.startswith("dl_"))
async def cb_download(callback_query: types.CallbackQuery):
    uid = callback_query.from_user.id
    try:
        idx = int(callback_query.data.replace("dl_", ""))
    except:
        await callback_query.answer("❌ Xato!", show_alert=True)
        return
    
    results = search_cache.get(uid, [])
    if not results or idx >= len(results):
        await callback_query.answer("❌ Natija eskirdi!", show_alert=True)
        return
    
    url = results[idx]['url']
    await callback_query.answer("⬇️ Yuklanmoqda...")
    msg = await callback_query.message.answer("⬇️ Yuklanmoqda...")
    
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            aud = download_audio(url, Path(tmpdir))
            await msg.edit_text("📤 Yuborilmoqda...")
            with open(aud, 'rb') as f:
                await callback_query.message.answer_audio(f)
            await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:100]}")

@dp.callback_query_handler(lambda c: c.data == "next_10")
async def cb_next_10(callback_query: types.CallbackQuery):
    """Keyingi 10 ta natija"""
    uid = callback_query.from_user.id
    await callback_query.answer("🔍 Qidirilmoqda...")
    
    # Eski natijalardan keyingi qismni olish
    old_results = search_cache.get(uid, [])
    if not old_results:
        await callback_query.answer("❌ Natija yo'q!", show_alert=True)
        return
    
    # Yangi qidiruv
    query = old_results[0].get('query', '')
    if not query:
        await callback_query.answer("❌ Xato!", show_alert=True)
        return
    
    msg = await callback_query.message.answer(f"🔍 '{query}' qidirilmoqda...")
    try:
        results = search_songs(query, count=20)  # 20 ta natija
        if not results:
            await msg.edit_text("❌ Qo'shiq topilmadi.")
            return
        
        # Natijalarni saqlash
        search_cache[uid] = results
        
        lines = [f"🔍 <b>{query}</b>\n"]
        for i, r in enumerate(results, start=1):
            lines.append(f"{i}. {r['title']}  <i>{r['duration_str']}</i>")
        
        kb = InlineKeyboardMarkup(row_width=5)
        btns = []
        for i in range(1, len(results) + 1):
            btns.append(InlineKeyboardButton(text=str(i), callback_data=f"dl_{i-1}"))
            if len(btns) == 5:
                kb.row(*btns)
                btns = []
        if btns:
            kb.row(*btns)
        kb.row(InlineKeyboardButton("❌ Yopish", callback_data="nav_close"))
        
        await msg.edit_text("\n".join(lines), reply_markup=kb, parse_mode=ParseMode.HTML)
        
    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:100]}")

@dp.callback_query_handler(lambda c: c.data.startswith("audio_"))
async def cb_audio(callback_query: types.CallbackQuery):
    await callback_query.answer("⬇️ Yuklanmoqda...")
    url = callback_query.data.replace("audio_", "")
    msg = await callback_query.message.answer("⬇️ Yuklanmoqda...")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            aud = download_audio(url, Path(tmpdir))
            await msg.edit_text("📤 Yuborilmoqda...")
            with open(aud, 'rb') as f:
                await callback_query.message.answer_audio(f)
            await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:100]}")

@dp.callback_query_handler(lambda c: c.data.startswith("video_"))
async def cb_video(callback_query: types.CallbackQuery):
    await callback_query.answer("⬇️ Yuklanmoqda...")
    url = callback_query.data.replace("video_", "")
    msg = await callback_query.message.answer("⬇️ Yuklanmoqda...")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            vid = download_video(url, Path(tmpdir))
            await msg.edit_text("📤 Yuborilmoqda...")
            with open(vid, 'rb') as f:
                await callback_query.message.answer_video(f)
            await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:100]}")

@dp.callback_query_handler(lambda c: c.data == "nav_close")
async def cb_close(callback_query: types.CallbackQuery):
    await callback_query.answer()
    await callback_query.message.delete()

# ==================== ADMIN CALLBACKS ====================
@dp.callback_query_handler(lambda c: c.data.startswith("adm_"))
async def cb_admin(callback_query: types.CallbackQuery):
    uid = callback_query.from_user.id
    data = callback_query.data
    
    if not is_admin(uid):
        await callback_query.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    
    await callback_query.answer()
    
    if data == "adm_add":
        await callback_query.message.answer("Format: /addch @username Nomi https://t.me/link")
    elif data == "adm_del":
        chs = db_get_channels()
        if not chs:
            await callback_query.message.answer("Kanallar yo'q")
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for ch_id, name, link in chs:
            kb.add(InlineKeyboardButton(text=f"🗑 {name}", callback_data=f"rmch_{ch_id}"))
        await callback_query.message.answer("O'chirish:", reply_markup=kb)
    elif data == "adm_list":
        chs = db_get_channels()
        if not chs:
            await callback_query.message.answer("Kanallar yo'q")
            return
        text = "📋 Kanallar:\n" + "\n".join(f"• {name} ({ch_id})" for ch_id, name, link in chs)
        await callback_query.message.answer(text)
    elif data == "adm_users":
        await callback_query.answer(f"👥 Foydalanuvchilar: {db_user_count()}\n🚫 Bloklangan: {db_banned_count()}", show_alert=True)
    elif data == "adm_ban":
        admin_pending_action[uid] = {"action": "ban"}
        await callback_query.message.answer("🚫 Bloklash uchun foydalanuvchi ID raqamini yuboring.")
    elif data == "adm_unban":
        banned = db_banned_users()
        if not banned:
            await callback_query.answer("Bloklangan foydalanuvchilar yo'q", show_alert=True)
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for uid_b, uname in banned:
            kb.add(InlineKeyboardButton(text=f"✅ {uname or uid_b}", callback_data=f"unban_{uid_b}"))
        await callback_query.message.answer("Blokdan chiqarish:", reply_markup=kb)
    elif data == "adm_ads":
        admin_pending_action[uid] = {"action": "broadcast"}
        await callback_query.message.answer("📣 Reklama xabarini yuboring (matn, rasm, video, audio)")
    elif data == "adm_movie_add":
        admin_movie_state[uid] = {"step": "wait_file"}
        await callback_query.message.answer("🎬 Kino faylini yuboring (video fayl)")
    elif data == "adm_movie_del":
        movies = db_all_movies()
        if not movies:
            await callback_query.answer("Kinolar yo'q", show_alert=True)
            return
        kb = InlineKeyboardMarkup(row_width=5)
        btns = []
        for code, title in movies[:60]:
            btns.append(InlineKeyboardButton(text=f"#{code}", callback_data=f"rmmovie_{code}"))
        kb.add(*btns)
        await callback_query.message.answer("🗑 O'chirish:", reply_markup=kb)
    elif data == "adm_movie_list":
        movies = db_all_movies()
        if not movies:
            await callback_query.message.answer("Kinolar yo'q")
            return
        text = f"📋 Kinolar ({len(movies)}):\n" + "\n".join(f"• #{code} — {title}" for code, title in movies)
        await callback_query.message.answer(text[:4000])

@dp.callback_query_handler(lambda c: c.data.startswith("rmch_"))
async def cb_rmch(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return
    db_remove_channel(callback_query.data.replace("rmch_", ""))
    await callback_query.answer("✅ O'chirildi", show_alert=True)
    await callback_query.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith("unban_"))
async def cb_unban(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return
    target = int(callback_query.data.replace("unban_", ""))
    db_unban_user(target)
    await callback_query.answer("✅ Blokdan chiqarildi", show_alert=True)
    await callback_query.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith("rmmovie_"))
async def cb_rmmovie(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return
    code = callback_query.data.replace("rmmovie_", "")
    db_remove_movie(code)
    await callback_query.answer(f"✅ Kino {code} o'chirildi", show_alert=True)
    await callback_query.message.delete()

# ==================== ADMIN BUYRUQLARI ====================
@dp.message_handler(commands=['addch'])
async def cmd_addch(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    p = message.text.split(maxsplit=3)
    if len(p) < 4:
        await message.answer("❗ Format: /addch @id Nomi https://link")
        return
    db_add_channel(p[1], p[2], p[3])
    await message.answer(f"✅ {p[2]} qo'shildi")

# ==================== REKLAMA ====================
@dp.message_handler(content_types=['text', 'photo', 'video', 'audio', 'document'])
async def h_broadcast(message: types.Message):
    uid = message.from_user.id
    pending = admin_pending_action.get(uid)
    if not pending or pending.get("action") != "broadcast":
        return
    
    if not is_admin(uid):
        return
    
    admin_pending_action.pop(uid, None)
    users = db_all_users()
    msg = await message.answer(f"📣 {len(users)} ta foydalanuvchiga yuborilmoqda...")
    ok = 0
    for uid_user in users:
        try:
            await message.copy_to(uid_user)
            ok += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await msg.edit_text(f"✅ Yuborildi: {ok}/{len(users)}")

# ==================== KINO QO'SHISH ====================
@dp.message_handler(content_types=['video', 'document'])
async def h_movie_file(message: types.Message):
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
    await message.answer(f"🎬 Fayl qabul qilindi: {title}\n\nKod kiriting:", reply_markup=kb)

@dp.message_handler(lambda m: m.text and m.text.isdigit())
async def h_movie_code(message: types.Message):
    uid = message.from_user.id
    text = message.text.strip()
    
    if not is_admin(uid):
        return
    
    state = admin_movie_state.get(uid)
    if state and state.get("step") == "wait_code":
        if db_get_movie(text):
            await message.answer(f"⚠️ Kod {text} band.")
            return
        db_add_movie(text, state["file_id"], state["title"], state.get("caption", ""))
        admin_movie_state.pop(uid, None)
        await message.answer(f"✅ Kino #{text} saqlandi!")
        return
    
    pending = admin_pending_action.get(uid)
    if pending:
        action = pending["action"]
        target = int(text)
        admin_pending_action.pop(uid, None)
        if action == "ban":
            db_ban_user(target)
            await message.answer(f"🚫 Foydalanuvchi {target} bloklandi.")
        elif action == "unban":
            db_unban_user(target)
            await message.answer(f"✅ Foydalanuvchi {target} blokdan chiqarildi.")
        return

@dp.callback_query_handler(lambda c: c.data.startswith("usecode_"))
async def cb_usecode(callback_query: types.CallbackQuery):
    uid = callback_query.from_user.id
    state = admin_movie_state.get(uid)
    if not state or state.get("step") != "wait_code":
        await callback_query.answer("❌ Xato!", show_alert=True)
        return
    code = callback_query.data.replace("usecode_", "")
    db_add_movie(code, state["file_id"], state["title"], state.get("caption", ""))
    admin_movie_state.pop(uid, None)
    await callback_query.answer("✅ Saqlandi!", show_alert=True)
    await callback_query.message.answer(f"✅ Kino #{code} saqlandi!")

# ==================== KINO QIDIRISH ====================
@dp.message_handler(lambda m: m.text and m.text.startswith("#"))
async def h_movie_search(message: types.Message):
    uid = message.from_user.id
    code = message.text.strip().replace("#", "")
    
    if not await guard(message):
        return
    
    movie = db_get_movie(code)
    if not movie:
        await message.answer(f"❓ #{code} kino topilmadi.")
        return
    
    try:
        await message.answer_video(
            movie["file_id"],
            caption=movie.get("caption") or f"🎬 {movie['title']}"
        )
    except Exception as e:
        await message.answer(f"❌ Xato: {str(e)[:100]}")

# ==================== AUDIO RECOGNITION ====================
@dp.message_handler(content_types=['audio', 'voice'])
async def h_audio(message: types.Message):
    if not await guard(message):
        return
    
    msg = await message.answer("🎵 Qo'shiq tanib olinmoqda...")
    try:
        if message.audio:
            file_id = message.audio.file_id
        elif message.voice:
            file_id = message.voice.file_id
        else:
            return
        
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        
        file = await bot.get_file(file_id)
        await file.download(tmp_path)
        
        result = await recognize_audio(tmp_path)
        os.unlink(tmp_path)
        
        if result and result.get("title"):
            q = f"{result['title']} {result['artist']}"
            await msg.edit_text(
                f"🎵 <b>{result['title']}</b>\n"
                f"🎤 {result['artist']}\n\n"
                f"⬇️ Yuklanmoqda..."
            )
            with tempfile.TemporaryDirectory() as tmpdir:
                aud = download_audio(q, Path(tmpdir))
                with open(aud, 'rb') as f:
                    await message.answer_audio(f, title=result['title'], performer=result['artist'])
            await msg.delete()
        else:
            await msg.edit_text("❓ Qo'shiq tanib olinmadi.")
    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:100]}")

# ==================== WEB SERVER ====================
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

# ==================== MAIN ====================
if __name__ == "__main__":
    db_init()
    print("="*50)
    print("✅ MUSIC BOT ISHGA TUSHDI!")
    print("🎵 Qo'shiq nomi yozing → 10 ta natija")
    print("🔗 YouTube havola → Audio + Video")
    print("📸 Instagram havola → Video")
    print("🎤 Audio yuboring → Qo'shiqni aniqlaydi")
    print("🎬 #kod → Kinoni yuboradi")
    print("👑 /admin → Admin panel")
    print("="*50)
    
    loop = asyncio.get_event_loop()
    loop.create_task(start_web_server())
    executor.start_polling(dp, skip_updates=True)
