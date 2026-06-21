import asyncio
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    Message, FSInputFile, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import yt_dlp

# ══════════════════════════════════════════════
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "8684337468:AAGhQ6rjhtvX-pUuYfmtnrA7SMVHIciIG6Q")
ADMIN_IDS    = [5599261398]
AUDD_TOKEN   = os.environ.get("AUDD_TOKEN", "test")
_DATA_DIR = Path("/data") if Path("/data").exists() else Path(".")
DOWNLOAD_DIR = _DATA_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
DB_PATH      = str(_DATA_DIR / "bot.db")
# Instagram'dan video yuklash uchun cookies fayli (ixtiyoriy).
# Brauzerdan Instagram'ga login qilib, "Get cookies.txt" kengaytmasi bilan
# eksport qilib, shu nomdagi faylga joylashtiring. Fayl bo'lmasa muammo emas,
# kod cookies'siz ham ishlashga harakat qiladi.
INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"
# ══════════════════════════════════════════════

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

search_cache: dict = {}
video_cache: dict = {}

def cleanup_old_files(max_age_hours: int = 1):
    """downloads/ papkasidagi eski fayllarni o'chiradi."""
    import time
    now = time.time()
    max_age = max_age_hours * 3600
    if not DOWNLOAD_DIR.exists():
        return
    for path in DOWNLOAD_DIR.rglob("*"):
        try:
            if path.is_file() and (now - path.stat().st_mtime) > max_age:
                path.unlink()
        except Exception:
            pass
    for path in sorted(DOWNLOAD_DIR.rglob("*"), reverse=True):
        try:
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except Exception:
            pass

async def periodic_cleanup():
    """Har 30 daqiqada eski fayllarni tozalaydi."""
    while True:
        try:
            cleanup_old_files(max_age_hours=1)
            stale = [k for k, p in video_cache.items() if not Path(p).exists()]
            for k in stale:
                video_cache.pop(k, None)
        except Exception as e:
            logger.warning(f"Cleanup xatosi: {e}")
        await asyncio.sleep(1800)

bot    = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp     = Dispatcher()
router = Router()
dp.include_router(router)
URL_RE = re.compile(r"https?://\S+")

# ── DATABASE ──────────────────────────────────
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
    # eski bazalarda 'banned' ustuni bo'lmasligi mumkin — qo'shamiz
    try:
        con.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
        con.commit()
    except sqlite3.OperationalError:
        pass  # ustun allaqachon bor
    con.close()

def db_add_channel(ch_id, name, link):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR REPLACE INTO required_channels VALUES (?,?,?)", (ch_id,name,link))
    con.commit(); con.close()

def db_remove_channel(ch_id):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM required_channels WHERE channel_id=?", (ch_id,))
    con.commit(); con.close()

def db_get_channels():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT channel_id,channel_name,invite_link FROM required_channels").fetchall()
    con.close(); return rows

def db_add_user(uid, uname):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR IGNORE INTO users VALUES (?,?,0)", (uid, uname))
    con.commit(); con.close()

def db_user_count():
    con = sqlite3.connect(DB_PATH)
    c = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    con.close(); return c

def db_all_users():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
    con.close(); return [r[0] for r in rows]

# ── BLOKLASH ───────────────────────────────────
def db_ban_user(uid: int):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    con.commit(); con.close()

def db_unban_user(uid: int):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    con.commit(); con.close()

def db_is_banned(uid: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT banned FROM users WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return bool(row and row[0])

def db_banned_users():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT user_id, username FROM users WHERE banned=1").fetchall()
    con.close(); return rows

def db_banned_count():
    con = sqlite3.connect(DB_PATH)
    c = con.execute("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0]
    con.close(); return c

# ── YORDAMCHI ADMINLAR ────────────────────────
def db_add_admin(uid: int, uname: str, added_by: int):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR REPLACE INTO admins VALUES (?,?,?)", (uid, uname, added_by))
    con.commit(); con.close()

def db_remove_admin(uid: int):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    con.commit(); con.close()

def db_all_admins():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT user_id, username FROM admins").fetchall()
    con.close(); return rows

def db_is_sub_admin(uid: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return bool(row)

# ── KINO KODLARI ──────────────────────────────
def db_add_movie(code: str, file_id: str, title: str, caption: str = ""):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR REPLACE INTO movies VALUES (?,?,?,?)", (code, file_id, title, caption))
    con.commit(); con.close()

def db_get_movie(code: str):
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT file_id,title,caption FROM movies WHERE code=?", (code,)).fetchone()
    con.close()
    if not row:
        return None
    return {"file_id": row[0], "title": row[1], "caption": row[2]}

def db_remove_movie(code: str):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM movies WHERE code=?", (code,))
    con.commit(); con.close()

def db_all_movies():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT code,title FROM movies ORDER BY CAST(code AS INTEGER)").fetchall()
    con.close(); return rows

def db_movie_count():
    con = sqlite3.connect(DB_PATH)
    c = con.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
    con.close(); return c

def db_next_movie_code() -> str:
    """Bo'sh raqamli kodlardan eng kichigini topadi (1 dan boshlab)."""
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

# ── MAJBURIY OBUNA ────────────────────────────
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

async def guard(message: Message) -> bool:
    if db_is_banned(message.from_user.id):
        await message.answer("🚫 Siz botdan foydalanishdan bloklangansiz.")
        return False
    nj = await check_subs(message.from_user.id)
    if nj:
        await message.answer("⚠️ Avval quyidagi kanallarga obuna bo'ling:", reply_markup=sub_kb(nj))
        return False
    return True

# ── YT-DLP UMUMIY SOZLAMALAR ──
YT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
_YT_COOKIES_SOURCE = "/etc/secrets/youtube_cookies.txt"
YOUTUBE_COOKIES_FILE = str(_DATA_DIR / "youtube_cookies.txt")

def _setup_youtube_cookies():
    """/etc/secrets/ read-only bo'lgani uchun, yt-dlp cookie faylni
    o'zi qayta yozishga harakat qilganda xato bermasligi uchun,
    faylni yoziladigan joyga (DATA_DIR) nusxalaymiz."""
    try:
        if Path(_YT_COOKIES_SOURCE).exists():
            import shutil
            shutil.copy(_YT_COOKIES_SOURCE, YOUTUBE_COOKIES_FILE)
            logger.info(f"YouTube cookies {YOUTUBE_COOKIES_FILE}ga nusxalandi")
    except Exception as e:
        logger.warning(f"YouTube cookies nusxalashda xato: {e}")

_setup_youtube_cookies()

def _yt_extra_opts() -> dict:
    """YouTube uchun qoshimcha sozlamalar."""
    opts = {
        "extractor_args": {
            "youtube": {
                "player_client": ["android"],
            }
        },
    }
    if Path(YOUTUBE_COOKIES_FILE).exists():
        opts["cookiefile"] = YOUTUBE_COOKIES_FILE
    return opts

def fmt_duration(dur) -> str:
    if not dur:
        return "?"
    try:
        dur = int(dur)
    except (ValueError, TypeError):
        return "?"
    return f"{dur // 60}:{dur % 60:02d}"

def is_instagram_url(url: str) -> bool:
    return "instagram.com" in url.lower()

# ── YUKLAB OLISH ──────────────────────────────
def search_songs(query: str, count: int = 10) -> list:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": True,
        "http_headers": YT_HEADERS,
        **_yt_extra_opts(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
        results = []
        for entry in info.get("entries", []):
            vid_id = entry.get("id", "")
            url = entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}"
            results.append({
                "title":    entry.get("title", "Noma'lum"),
                "url":      url,
                "duration": entry.get("duration") or 0,
                "uploader": entry.get("uploader", ""),
            })
        return results

def download_mp3(query: str, out_dir: Path) -> dict:
    is_url = query.startswith("http")
    search = query if is_url else f"ytsearch1:{query}"
    ydl_opts = {
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "format": "best/bestaudio",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "postprocessor_args": {
            "ffmpeg": ["-ar", "44100"],
        },
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": YT_HEADERS,
        "match_filter": yt_dlp.utils.match_filter_func("!is_live"),
    }
    if is_url and is_instagram_url(search) and Path(INSTAGRAM_COOKIES_FILE).exists():
        ydl_opts["cookiefile"] = INSTAGRAM_COOKIES_FILE
    if not is_url or not is_instagram_url(search):
        ydl_opts.update(_yt_extra_opts())
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search, download=True)
    except yt_dlp.utils.DownloadError:
        # Ffprobe audio kodekni topa olmasa — video formatdan to'g'ridan-to'g'ri MP3 chiqaramiz
        ydl_opts["format"] = "best"
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
        "title":    info.get("title", "Noma'lum"),
        "artist":   info.get("uploader", ""),
        "duration": info.get("duration", 0),
        "path":     mp3,
    }

def _probe_has_video(path: str) -> bool:
    """Faylda haqiqiy video oqimi (rasm-thumbnail emas) borligini tekshiradi."""
    try:
        import subprocess
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=10,
        )
        return "video" in probe.stdout
    except Exception as pe:
        logger.warning(f"ffprobe tekshirishda xato: {pe}")
        return True  # tekshira olmasak, bloklamaymiz

def download_video(url: str, out_dir: Path) -> str:
    instagram = is_instagram_url(url)

    base_opts = {
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": YT_HEADERS,
    }

    if instagram:
        # Instagram'da odatda video+audio bitta formatda keladi.
        # "bestvideo+bestaudio" majburlash ko'pincha mos format topa olmay,
        # yt-dlp'ni faqat audio-only formatga qaytarishga majbur qiladi.
        # Shu sababli "best" ishlatamiz va vcodec!=none bo'lgan formatlarga
        # ustunlik beramiz.
        ydl_opts = {
            **base_opts,
            "format": "best[vcodec!=none]/best",
        }
        # Agar cookies fayli mavjud bo'lsa, ulaymiz (login talab qiladigan
        # yoki cheklangan postlar uchun foydali).
        if Path(INSTAGRAM_COOKIES_FILE).exists():
            ydl_opts["cookiefile"] = INSTAGRAM_COOKIES_FILE
        else:
            logger.warning(
                f"{INSTAGRAM_COOKIES_FILE} topilmadi — cookies'siz urinilmoqda. "
                "Agar Instagram doim faqat audio bersa, cookies fayl qo'shing."
            )
    else:
        ydl_opts = {
            **base_opts,
            "format": "best[vcodec!=none]/best",
            **_yt_extra_opts(),
        }

    def _run(opts: dict):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return ydl, info, filename

    ydl, info, filename = _run(ydl_opts)
    mp4 = str(Path(filename).with_suffix(".mp4"))
    if not Path(mp4).exists():
        for f in Path(out_dir).glob("*.mp4"):
            mp4 = str(f)
            break

    has_video = _probe_has_video(mp4) if Path(mp4).exists() else False
    logger.info(f"download_video: {mp4} | instagram={instagram} | video stream bor: {has_video}")

    # Instagram uchun: agar birinchi urinish audio-only chiqsa, "best" formatdagi
    # boshqa variantlarni avtomatik qayta sinab ko'ramiz (fallback).
    if instagram and not has_video:
        logger.warning("Instagram: birinchi format audio-only chiqdi, fallback bilan qayta urinilmoqda…")
        fallback_opts = {**ydl_opts, "format": "best"}
        try:
            for old in Path(out_dir).glob("*"):
                old.unlink()
        except Exception:
            pass
        ydl, info, filename = _run(fallback_opts)
        mp4 = str(Path(filename).with_suffix(".mp4"))
        if not Path(mp4).exists():
            for f in Path(out_dir).glob("*.mp4"):
                mp4 = str(f)
                break
        has_video = _probe_has_video(mp4) if Path(mp4).exists() else False
        logger.info(f"download_video (fallback): {mp4} | video stream bor: {has_video}")

    if not has_video:
        raise yt_dlp.utils.DownloadError("Faylda video oqimi topilmadi (audio-only)")
    return mp4

async def recognize_audio(file_path: str) -> dict:
    url = "https://api.audd.io/"
    with open(file_path, "rb") as f:
        data = aiohttp.FormData()
        data.add_field("api_token", AUDD_TOKEN)
        data.add_field("file", f, filename="audio.mp3", content_type="audio/mpeg")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as resp:
                result = await resp.json()
    if result.get("status") == "success" and result.get("result"):
        r = result["result"]
        return {"title": r.get("title", ""), "artist": r.get("artist", "")}
    return {}

# ── ADMIN ─────────────────────────────────────
def is_owner(uid): return uid in ADMIN_IDS
def is_admin(uid): return uid in ADMIN_IDS or db_is_sub_admin(uid)

# Admin "kino qo'shish" jarayonidagi holatni saqlaymiz: {admin_id: {"step":..., "file_id":..., "title":...}}
admin_movie_state: dict = {}

@router.message(Command("admin"))
async def cmd_admin(message: Message):
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

@router.message(F.photo | F.video | F.audio | F.document)
async def h_admin_broadcast_catcher_media(message: Message):
    """Admin 'Reklama' rejimida media xabarlarni broadcast qiladi."""
    uid = message.from_user.id
    if not is_admin(uid):
        return
    pending = admin_pending_action.get(uid)
    if not pending or pending.get("action") != "broadcast":
        return
    admin_pending_action.pop(uid, None)
    status = await message.answer("📣 Yuborilmoqda…")
    await send_broadcast(message, status)



@router.callback_query(F.data == "adm_users")
async def cb_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    await cb.answer(
        f"👥 Jami: {db_user_count()} foydalanuvchi\n🚫 Bloklangan: {db_banned_count()}",
        show_alert=True,
    )

# Admin'dan qo'shimcha matn kutilayotgan kichik harakatlar uchun holat:
# {"action": "ban"/"unban"/"addadmin"}
admin_pending_action: dict = {}

@router.callback_query(F.data == "adm_ban")
async def cb_ban(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    admin_pending_action[cb.from_user.id] = {"action": "ban"}
    await cb.message.answer(
        "🚫 Bloklash uchun foydalanuvchi ID raqamini yuboring.\n"
        "(ID ni /admin → 👥 orqali emas, foydalanuvchi botga yozganda ko'rasiz, "
        "yoki @userinfobot orqali bilib olishingiz mumkin)\n\n❌ Bekor: /cancel"
    )
    await cb.answer()

@router.callback_query(F.data == "adm_unban")
async def cb_unban(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    banned = db_banned_users()
    if not banned:
        await cb.answer("Bloklangan foydalanuvchilar yo'q", show_alert=True); return
    btns = [[InlineKeyboardButton(text=f"✅ {uname or uid}", callback_data=f"unban_{uid}")] for uid, uname in banned[:60]]
    await cb.message.answer("Blokdan chiqarish:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

@router.callback_query(F.data.startswith("unban_"))
async def cb_do_unban(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    target = int(cb.data.removeprefix("unban_"))
    db_unban_user(target)
    await cb.answer("✅ Blokdan chiqarildi", show_alert=True)
    await cb.message.delete()

@router.callback_query(F.data == "adm_addadmin")
async def cb_addadmin(cb: CallbackQuery):
    if not is_owner(cb.from_user.id): return
    admin_pending_action[cb.from_user.id] = {"action": "addadmin"}
    await cb.message.answer(
        "🛡 Yangi yordamchi admin qo'shish uchun uning ID raqamini yuboring.\n\n❌ Bekor: /cancel"
    )
    await cb.answer()

@router.callback_query(F.data == "adm_listadmins")
async def cb_listadmins(cb: CallbackQuery):
    if not is_owner(cb.from_user.id): return
    admins = db_all_admins()
    if not admins:
        await cb.answer("Yordamchi adminlar yo'q", show_alert=True); return
    btns = [[InlineKeyboardButton(text=f"🗑 {uname or uid}", callback_data=f"rmadmin_{uid}")] for uid, uname in admins]
    await cb.message.answer(
        "🛡 <b>Yordamchi adminlar</b> (bosish — o'chirish):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
    )
    await cb.answer()

@router.callback_query(F.data.startswith("rmadmin_"))
async def cb_rmadmin(cb: CallbackQuery):
    if not is_owner(cb.from_user.id): return
    target = int(cb.data.removeprefix("rmadmin_"))
    db_remove_admin(target)
    await cb.answer("✅ O'chirildi", show_alert=True)
    await cb.message.delete()

@router.callback_query(F.data == "adm_list")
async def cb_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    chs = db_get_channels()
    if not chs:
        await cb.answer("Kanallar yo'q", show_alert=True); return
    text = "📋 <b>Kanallar:</b>\n" + "\n".join(f"• {n} ({i})" for i, n, l in chs)
    await cb.message.answer(text); await cb.answer()

@router.callback_query(F.data == "adm_add")
async def cb_add(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    await cb.message.answer(
        "Format:\n<code>/addch @username Kanal nomi https://t.me/link</code>"
    ); await cb.answer()

@router.callback_query(F.data == "adm_del")
async def cb_del(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    chs = db_get_channels()
    if not chs:
        await cb.answer("Kanallar yo'q", show_alert=True); return
    btns = [[InlineKeyboardButton(text=f"🗑 {n}", callback_data=f"rmch_{i}")] for i, n, l in chs]
    await cb.message.answer("O'chirish:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

@router.callback_query(F.data.startswith("rmch_"))
async def cb_rmch(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    db_remove_channel(cb.data.removeprefix("rmch_"))
    await cb.answer("✅ O'chirildi", show_alert=True)
    await cb.message.delete()

@router.callback_query(F.data == "adm_ads")
async def cb_ads(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    admin_pending_action[cb.from_user.id] = {"action": "broadcast"}
    await cb.message.answer(
        "📣 Reklama uchun xabar yuboring — matn, rasm, video yoki audio bo'lishi mumkin "
        "(rasm/video/audio'ga izoh/caption ham qo'shsangiz bo'ladi).\n\n❌ Bekor: /cancel"
    )
    await cb.answer()

# ── KINO QO'SHISH / O'CHIRISH / RO'YXAT ───────
@router.callback_query(F.data == "adm_movie_add")
async def cb_movie_add(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    admin_movie_state[cb.from_user.id] = {"step": "wait_file"}
    await cb.message.answer(
        "🎬 Kino faylini (video yoki video-document) menga forward qiling yoki yuboring.\n\n"
        "❌ Bekor qilish uchun /cancel yozing."
    )
    await cb.answer()

@router.callback_query(F.data == "adm_movie_list")
async def cb_movie_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    movies = db_all_movies()
    if not movies:
        await cb.answer("Kinolar yo'q", show_alert=True); return
    text = f"📋 <b>Kinolar ({len(movies)} ta):</b>\n" + "\n".join(f"• #{code} — {title}" for code, title in movies)
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await cb.message.answer(text); await cb.answer()

@router.callback_query(F.data == "adm_movie_del")
async def cb_movie_del(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    movies = db_all_movies()
    if not movies:
        await cb.answer("Kinolar yo'q", show_alert=True); return
    btns = []
    row = []
    for code, title in movies[:60]:
        row.append(InlineKeyboardButton(text=f"#{code}", callback_data=f"rmmovie_{code}"))
        if len(row) == 5:
            btns.append(row); row = []
    if row:
        btns.append(row)
    await cb.message.answer(
        "🗑 O'chirish uchun kodni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
    )
    await cb.answer()

@router.callback_query(F.data.startswith("rmmovie_"))
async def cb_rmmovie(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    code = cb.data.removeprefix("rmmovie_")
    db_remove_movie(code)
    await cb.answer(f"✅ Kino {code} o'chirildi", show_alert=True)
    await cb.message.delete()

@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if not is_admin(message.from_user.id): return
    cleared = bool(admin_movie_state.pop(message.from_user.id, None)) or \
              bool(admin_pending_action.pop(message.from_user.id, None))
    if cleared:
        await message.answer("❌ Bekor qilindi.")

@router.message(F.video | (F.document & F.document.mime_type.startswith("video")))
async def h_admin_movie_file(message: Message):
    """Admin kino qo'shish jarayonida bo'lsa, video faylni qabul qiladi."""
    uid = message.from_user.id
    if not is_admin(uid):
        return
    state = admin_movie_state.get(uid)
    if not state or state.get("step") != "wait_file":
        return  # admin kino qo'shish jarayonida emas -> boshqa handlerga o'tadi (h_video/h_doc)

    file_id = message.video.file_id if message.video else message.document.file_id
    title = (message.caption or "Noma'lum film").split("\n")[0][:80]
    state["file_id"] = file_id
    state["title"] = title
    state["caption"] = message.caption or ""
    state["step"] = "wait_code"

    suggested = db_next_movie_code()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ #{suggested} (taklif)", callback_data=f"usecode_{suggested}")
    ]])
    await message.answer(
        f"🎬 Fayl qabul qilindi: <b>{title}</b>\n\n"
        f"Endi shu kino uchun kod kiriting (faqat raqam, masalan: 4131),\n"
        f"yoki taklif qilingan kodni tanlang. Foydalanuvchilar uni <code>#{suggested}</code> deb yozadi:",
        reply_markup=kb,
    )

@router.callback_query(F.data.startswith("usecode_"))
async def cb_usecode(cb: CallbackQuery):
    uid = cb.from_user.id
    state = admin_movie_state.get(uid)
    if not is_admin(uid) or not state or state.get("step") != "wait_code":
        await cb.answer("❌ Jarayon topilmadi.", show_alert=True); return
    code = cb.data.removeprefix("usecode_")
    db_add_movie(code, state["file_id"], state["title"], state.get("caption", ""))
    admin_movie_state.pop(uid, None)
    await cb.answer("✅ Saqlandi!", show_alert=True)
    await cb.message.answer(f"✅ Kino <b>{state['title']}</b> kod <b>#{code}</b> bilan saqlandi.")

@router.message(F.text.regexp(r"^#\d{1,10}$"))
async def h_movie_code_lookup(message: Message):
    """Foydalanuvchi #raqam yozganda kino yuboradi (masalan: #4131)."""
    uid = message.from_user.id
    code = message.text.strip().removeprefix("#")

    if not await guard(message): return
    db_add_user(uid, message.from_user.username or "")
    movie = db_get_movie(code)
    if not movie:
        await message.answer("❓ Bu kodga mos kino topilmadi. Kodni tekshirib qayta yuboring.\nMasalan: <code>#4131</code>")
        return
    try:
        await message.answer_video(
            movie["file_id"],
            caption=movie.get("caption") or f"🎬 <b>{movie['title']}</b>",
        )
    except Exception as e:
        logger.exception(e)
        await message.answer("❌ Kinoni yuborishda xatolik yuz berdi.")

@router.message(F.text.regexp(r"^\d{1,15}$"))
async def h_admin_numeric_input(message: Message):
    """Admindan kutilayotgan raqamli kiritishlar: kino kodi, ban/unban/addadmin ID."""
    uid = message.from_user.id
    text = message.text.strip()

    if not is_admin(uid):
        return  # oddiy foydalanuvchidan kelgan raqam — e'tiborsiz qoldiramiz

    # 1) Kino kodi kiritish jarayoni
    movie_state = admin_movie_state.get(uid)
    if movie_state and movie_state.get("step") == "wait_code":
        if db_get_movie(text):
            await message.answer(f"⚠️ Kod {text} band. Boshqa kod kiriting yoki taklif qilinganini tanlang.")
            return
        db_add_movie(text, movie_state["file_id"], movie_state["title"], movie_state.get("caption", ""))
        admin_movie_state.pop(uid, None)
        await message.answer(f"✅ Kino <b>{movie_state['title']}</b> kod <b>#{text}</b> bilan saqlandi.")
        return

    # 2) Ban / Unban / Admin qo'shish jarayoni
    pending = admin_pending_action.get(uid)
    if pending:
        action = pending["action"]
        target = int(text)
        admin_pending_action.pop(uid, None)
        if action == "ban":
            if target in ADMIN_IDS:
                await message.answer("❌ Bosh adminni bloklab bo'lmaydi."); return
            db_ban_user(target)
            await message.answer(f"🚫 Foydalanuvchi <code>{target}</code> bloklandi.")
        elif action == "unban":
            db_unban_user(target)
            await message.answer(f"✅ Foydalanuvchi <code>{target}</code> blokdan chiqarildi.")
        elif action == "addadmin":
            if not is_owner(uid):
                return
            db_add_admin(target, "", uid)
            await message.answer(f"🛡 Foydalanuvchi <code>{target}</code> endi yordamchi admin.")
        return

@router.message(Command("addch"))
async def cmd_addch(message: Message):
    if not is_admin(message.from_user.id): return
    p = message.text.split(maxsplit=3)
    if len(p) < 4:
        await message.answer("❗ <code>/addch @id Nomi https://link</code>"); return
    db_add_channel(p[1], p[2], p[3])
    await message.answer(f"✅ <b>{p[2]}</b> qo'shildi")

async def send_broadcast(source_message: Message, status_message: Message):
    """Berilgan xabarni (matn/rasm/video/audio — istalgan turi) barcha foydalanuvchilarga nusxalaydi."""
    users = db_all_users()
    ok = 0
    for uid in users:
        try:
            await source_message.copy_to(uid)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await status_message.edit_text(f"✅ Yuborildi: {ok}/{len(users)}")

@router.message(Command("ads"))
async def cmd_ads(message: Message):
    if not is_admin(message.from_user.id): return
    text = message.text.removeprefix("/ads").strip()
    if not text:
        await message.answer("❗ Matn bo'sh! Yoki /admin → 📣 Reklama orqali rasm/video/audio ham yuborishingiz mumkin."); return
    users = db_all_users()
    msg = await message.answer(f"📣 {len(users)} ta foydalanuvchiga yuborilmoqda…")
    ok = 0
    for uid in users:
        try:
            await bot.send_message(uid, text)
            ok += 1
            await asyncio.sleep(0.05)
        except: pass
    await msg.edit_text(f"✅ Yuborildi: {ok}/{len(users)}")

# ── START ─────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(message: Message):
    db_add_user(message.from_user.id, message.from_user.username or "")
    nj = await check_subs(message.from_user.id)
    if nj:
        await message.answer("⚠️ Avval obuna bo'ling:", reply_markup=sub_kb(nj)); return
    await message.answer(
        "👋 <b>Salom!</b>\n\n"
        "🎵 Qo'shiq/xonanda nomi yozing → MP3 yuklab beraman\n"
        "🔗 Link yuboring → video + MP3 yuklab beraman\n"
        "🎤 Audio/video yuboring → qo'shiqni tanib yuklab beraman"
    )

@router.callback_query(F.data == "check_sub")
async def cb_check(cb: CallbackQuery):
    nj = await check_subs(cb.from_user.id)
    if nj:
        await cb.answer("❌ Hali obuna bo'lmadingiz!", show_alert=True)
    else:
        await cb.answer("✅ Rahmat!")
        await cb.message.delete()
        await cb.message.answer(
            "✅ <b>Obuna tasdiqlandi!</b>\n\n"
            "🎵 Qo'shiq nomi yozing → MP3\n"
            "🔗 Link → video + MP3\n"
            "🎤 Audio → tanib yuklab beraman"
        )

# ── QIDIRUV NATIJALARI (raqamli ro'yxat uslubida) ──
def build_results_text(query: str, results: list) -> str:
    lines = [f"🔍 {query}", ""]
    for i, r in enumerate(results, start=1):
        dur_str = fmt_duration(r["duration"])
        lines.append(f"{i}. {r['title']}  {dur_str}")
    return "\n".join(lines)

def build_results_kb(count: int) -> InlineKeyboardMarkup:
    num_rows = []
    row = []
    for i in range(1, count + 1):
        row.append(InlineKeyboardButton(text=str(i), callback_data=f"dl_{i-1}"))
        if len(row) == 5:
            num_rows.append(row)
            row = []
    if row:
        num_rows.append(row)
    num_rows.append([InlineKeyboardButton(text="❌ Yopish", callback_data="nav_close")])
    return InlineKeyboardMarkup(inline_keyboard=num_rows)

@router.callback_query(F.data == "nav_close")
async def cb_nav_close(cb: CallbackQuery):
    await cb.answer()
    await cb.message.delete()

@router.callback_query(F.data.startswith("findsong_"))
async def cb_find_song(cb: CallbackQuery):
    """Video tarkibidagi qo'shiqni tanib, MP3 yuklab beradi."""
    try:
        msg_id = int(cb.data.removeprefix("findsong_"))
    except ValueError:
        await cb.answer("❌ Noto'g'ri so'rov.", show_alert=True)
        return
    vid_path = video_cache.get(msg_id)
    if not vid_path or not Path(vid_path).exists():
        await cb.answer("❌ Video topilmadi, qayta yuboring.", show_alert=True)
        return

    await cb.answer("🎧 Tekshirilmoqda…")
    info_msg = await cb.message.answer("🎧 Qo'shiq tanib olinmoqda…")
    try:
        # Videodan audio chiqarib, tanib olishga yuboramiz
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = str(Path(tmpdir) / "extract.mp3")
            ydl_opts = {
                "quiet": True, "no_warnings": True,
            }
            # ffmpeg orqali videodan audio ajratamiz
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-i", vid_path, "-vn", "-acodec", "libmp3lame", audio_path],
                check=True, capture_output=True,
            )
            result = await recognize_audio(audio_path)

        if result and result.get("title"):
            q = f"{result['title']} {result['artist']}"
            await info_msg.edit_text(
                f"🎵 <b>{result['title']}</b>\n🎤 {result['artist']}\n\n⬇️ Yuklanmoqda…"
            )
            with tempfile.TemporaryDirectory() as tmpdir2:
                aud = download_mp3(q, Path(tmpdir2))
                await cb.message.answer_audio(
                    FSInputFile(aud["path"]),
                    title=result["title"],
                    performer=result["artist"],
                )
            await info_msg.delete()
        else:
            await info_msg.edit_text("❓ Qo'shiq tanib olinmadi.")
    except Exception as e:
        logger.exception(e)
        await info_msg.edit_text("❌ Xatolik yuz berdi.")

@router.callback_query(F.data.startswith("dl_"))
async def cb_download_song(cb: CallbackQuery):
    uid = cb.from_user.id
    try:
        idx = int(cb.data.removeprefix("dl_"))
    except ValueError:
        await cb.answer("❌ Noto'g'ri so'rov.", show_alert=True)
        return
    urls = search_cache.get(uid, [])
    if not urls or idx >= len(urls):
        await cb.answer("❌ Natija eskirdi. Qayta qidiring.", show_alert=True)
        return
    url = urls[idx]
    await cb.answer("⬇️ Yuklanmoqda…")
    msg = await cb.message.answer("⬇️ Yuklanmoqda…")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            aud = download_mp3(url, Path(tmpdir))
            await msg.edit_text(f"📤 <b>{aud['title']}</b> yuborilmoqda…")
            await cb.message.answer_audio(
                FSInputFile(aud["path"]),
                title=aud["title"],
                performer=aud["artist"],
            )
            await msg.delete()
    except Exception as e:
        logger.exception(e)
        await msg.edit_text("❌ Yuklab bo'lmadi.")

# ── ASOSIY HANDLERLAR ─────────────────────────
@router.message(F.text)
async def h_text(message: Message):
    uid = message.from_user.id
    text = message.text.strip()
    # Admin broadcast rejimida bo'lsa va command bo'lmasa — broadcast qilamiz
    if is_admin(uid) and not text.startswith("/"):
        pending = admin_pending_action.get(uid)
        if pending and pending.get("action") == "broadcast":
            admin_pending_action.pop(uid, None)
            status = await message.answer("📣 Yuborilmoqda…")
            await send_broadcast(message, status)
            return
    if not await guard(message): return
    db_add_user(uid, message.from_user.username or "")
    url  = URL_RE.search(text)

    if url:
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

                # 1) Video yuklash (alohida papkada)
                try:
                    vid_path = download_video(url.group(), vid_dir)
                except Exception as ve:
                    logger.exception(f"Video yuklanmadi: {ve}")

                # 2) Audio (MP3) yuklash (alohida papkada)
                try:
                    aud = download_mp3(url.group(), aud_dir)
                    aud_path = aud["path"]
                    title = aud["title"]
                except Exception as ae:
                    logger.warning(f"Audio yuklanmadi: {ae}")

                if not vid_path and not aud_path:
                    await msg.edit_text("❌ Yuklab bo'lmadi.")
                    return

                await msg.delete()

                # Videoni doimiy joyga ko'chiramiz (callback uchun kerak bo'lishi mumkin)
                permanent_path = None
                if vid_path and Path(vid_path).exists():
                    permanent_dir = DOWNLOAD_DIR / str(message.from_user.id)
                    permanent_dir.mkdir(parents=True, exist_ok=True)
                    permanent_path = str(permanent_dir / Path(vid_path).name)
                    if Path(vid_path).resolve() != Path(permanent_path).resolve():
                        import shutil
                        shutil.copy(vid_path, permanent_path)

                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🎵 Qo'shiqni topish", callback_data=f"findsong_{message.message_id}")
                ]])

                if permanent_path:
                    video_cache[message.message_id] = permanent_path

                sent = None
                if vid_path and Path(vid_path).exists():
                    size = os.path.getsize(vid_path) / (1024 * 1024)
                    logger.info(f"Video yuborishga tayyor: {vid_path} ({size:.2f} MB)")
                    if size <= 50:
                        sent = await message.answer_video(
                            FSInputFile(vid_path),
                            caption=f"🎬 <b>{title}</b>",
                            reply_markup=kb if permanent_path else None,
                        )
                    else:
                        logger.warning(f"Video juda katta ({size:.2f} MB), yuborilmadi")
                if aud_path and Path(aud_path).exists():
                    await message.answer_audio(
                        FSInputFile(aud_path),
                        title=title,
                        performer="",
                    )
                if not sent and permanent_path:
                    # Video yuborilmagan bo'lsa ham tugmani audio xabariga bog'laymiz
                    await message.answer("🎵 Videodagi qo'shiqni topish:", reply_markup=kb)

        except Exception as e:
            logger.exception(e)
            await msg.edit_text("❌ Yuklab bo'lmadi.")
    else:
        msg = await message.answer(f"🔍 <b>{text}</b> qidirilmoqda…")
        try:
            results = search_songs(text, count=10)
            if not results:
                await msg.edit_text("❌ Qo'shiq topilmadi.")
                return

            body = build_results_text(text, results)
            kb = build_results_kb(len(results))
            await msg.edit_text(body, reply_markup=kb)

            search_cache[message.from_user.id] = [r["url"] for r in results]

        except Exception as e:
            logger.exception(e)
            await msg.edit_text("❌ Qo'shiq topilmadi.")

async def _handle_audio(message: Message, file_id: str):
    msg = await message.answer("🎵 Qo'shiq tanib olinmoqda…")
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        f = await bot.get_file(file_id)
        await bot.download_file(f.file_path, tmp_path)
        result = await recognize_audio(tmp_path)
        os.unlink(tmp_path)
        if result and result["title"]:
            q = f"{result['title']} {result['artist']}"
            await msg.edit_text(f"🎵 <b>{result['title']}</b>\n🎤 {result['artist']}\n\n⬇️ Yuklanmoqda…")
            with tempfile.TemporaryDirectory() as tmpdir:
                aud = download_mp3(q, Path(tmpdir))
                await message.answer_audio(FSInputFile(aud["path"]), title=result["title"], performer=result["artist"])
            await msg.delete()
        else:
            await msg.edit_text("❓ Qo'shiq tanib olinmadi.")
    except Exception as e:
        logger.exception(e)
        await msg.edit_text("❌ Xatolik.")

@router.message(F.audio)
async def h_audio(message: Message):
    if not await guard(message): return
    await _handle_audio(message, message.audio.file_id)

@router.message(F.voice)
async def h_voice(message: Message):
    if not await guard(message): return
    await _handle_audio(message, message.voice.file_id)

@router.message(F.video)
async def h_video(message: Message):
    if not await guard(message): return
    await _handle_audio(message, message.video.file_id)

@router.message(F.document)
async def h_doc(message: Message):
    if not await guard(message): return
    mime = message.document.mime_type or ""
    if mime.startswith("audio") or mime.startswith("video"):
        await _handle_audio(message, message.document.file_id)

# ── ISHGA TUSHIRISH ───────────────────────────
async def _start_dummy_web_server():
    """Render.com Web Service uchun: portni tinglovchi minimal HTTP server.
    Bot ishi polling orqali davom etadi, bu server faqat 'tirikman' signali beradi."""
    from aiohttp import web

    async def health(_request):
        return web.Response(text="Bot ishlayapti ✅")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Dummy web server {port}-portda ishga tushdi (Render health check uchun)")

async def main():
    db_init()
    cleanup_old_files(max_age_hours=1)  # boshlanishda bir marta tozalash
    asyncio.create_task(periodic_cleanup())
    asyncio.create_task(_start_dummy_web_server())
    logger.info("Bot ishga tushdi ✅")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
