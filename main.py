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
BOT_TOKEN    = "8684337468:AAGhQ6rjhtvX-pUuYfmtnrA7SMVHIciIG6Q"
ADMIN_IDS    = [5599261398]
AUDD_TOKEN   = "test"
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
DB_PATH      = "bot.db"
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
        user_id INTEGER PRIMARY KEY, username TEXT)""")
    con.commit(); con.close()

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
    con.execute("INSERT OR IGNORE INTO users VALUES (?,?)", (uid, uname))
    con.commit(); con.close()

def db_user_count():
    con = sqlite3.connect(DB_PATH)
    c = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    con.close(); return c

def db_all_users():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT user_id FROM users").fetchall()
    con.close(); return [r[0] for r in rows]

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
    nj = await check_subs(message.from_user.id)
    if nj:
        await message.answer("⚠️ Avval quyidagi kanallarga obuna bo'ling:", reply_markup=sub_kb(nj))
        return False
    return True

# ── YT-DLP UMUMIY SOZLAMALAR ──
YT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def fmt_duration(dur) -> str:
    if not dur:
        return "?"
    try:
        dur = int(dur)
    except (ValueError, TypeError):
        return "?"
    return f"{dur // 60}:{dur % 60:02d}"

# ── YUKLAB OLISH ──────────────────────────────
def search_songs(query: str, count: int = 10) -> list:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": True,
        "http_headers": YT_HEADERS,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
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
        "format": "bestaudio/best",
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
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "match_filter": yt_dlp.utils.match_filter_func("!is_live"),
    }
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

def download_video(url: str, out_dir: Path) -> str:
    ydl_opts = {
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "format": "(bestvideo+bestaudio/best)[vcodec!=none]",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": YT_HEADERS,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        mp4 = str(Path(filename).with_suffix(".mp4"))
        if not Path(mp4).exists():
            for f in Path(out_dir).glob("*.mp4"):
                mp4 = str(f)
                break
        # Faylda video oqimi borligini tekshiramiz
        has_video = True
        if Path(mp4).exists():
            try:
                import subprocess
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v",
                     "-show_entries", "stream=codec_type", "-of", "csv=p=0", mp4],
                    capture_output=True, text=True, timeout=10,
                )
                has_video = "video" in probe.stdout
                logger.info(f"download_video: {mp4} | video stream bor: {has_video}")
            except Exception as pe:
                logger.warning(f"ffprobe tekshirishda xato: {pe}")
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
def is_admin(uid): return uid in ADMIN_IDS

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id): return
    btns = [
        [InlineKeyboardButton(text="📢 Kanal qo'shish", callback_data="adm_add")],
        [InlineKeyboardButton(text="🗑 Kanal o'chirish", callback_data="adm_del")],
        [InlineKeyboardButton(text="📋 Kanallar", callback_data="adm_list")],
        [InlineKeyboardButton(text="📣 Reklama", callback_data="adm_ads")],
        [InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="adm_users")],
    ]
    await message.answer("🔧 <b>Admin panel</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data == "adm_users")
async def cb_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    await cb.answer(f"👥 Jami: {db_user_count()} foydalanuvchi", show_alert=True)

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
    await cb.message.answer("Format:\n<code>/ads Reklama matni</code>"); await cb.answer()

@router.message(Command("addch"))
async def cmd_addch(message: Message):
    if not is_admin(message.from_user.id): return
    p = message.text.split(maxsplit=3)
    if len(p) < 4:
        await message.answer("❗ <code>/addch @id Nomi https://link</code>"); return
    db_add_channel(p[1], p[2], p[3])
    await message.answer(f"✅ <b>{p[2]}</b> qo'shildi")

@router.message(Command("ads"))
async def cmd_ads(message: Message):
    if not is_admin(message.from_user.id): return
    text = message.text.removeprefix("/ads").strip()
    if not text:
        await message.answer("❗ Matn bo'sh!"); return
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
    msg_id = int(cb.data.removeprefix("findsong_"))
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


    uid = cb.from_user.id
    idx = int(cb.data.removeprefix("dl_"))
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
    if not await guard(message): return
    db_add_user(message.from_user.id, message.from_user.username or "")
    text = message.text.strip()
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
async def main():
    db_init()
    cleanup_old_files(max_age_hours=1)  # boshlanishda bir marta tozalash
    asyncio.create_task(periodic_cleanup())
    logger.info("Bot ishga tushdi ✅")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
