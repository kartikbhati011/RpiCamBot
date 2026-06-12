import asyncio
import logging
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# Configuration
BOT_TOKEN     = "Bot_TOKEN"             # Enter your Bot TOKEN here
ALLOWED_USERS: list[int] = []           # leave empty to allow everyone
MEDIA_DIR     = Path("/tmp/rpi_cam")
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
MAX_VIDEO_S   = 300

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Quality presets
QUALITY_PRESETS = {
    "LOW":    {"label": "LOW  640×480",    "w": 640,  "h": 480},
    "MEDIUM": {"label": "MEDIUM 1280×720", "w": 1280, "h": 720},
    "HIGH":   {"label": "HIGH  1920×1080", "w": 1920, "h": 1080},
}

_stop_flags: dict[str, threading.Event] = {}

# Helpers

def _allowed(uid: int) -> bool:
    return not ALLOWED_USERS or uid in ALLOWED_USERS

def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def _cap(seconds: int) -> int:
    if seconds > MAX_VIDEO_S:
        logger.warning("Requested %ds capped to %ds", seconds, MAX_VIDEO_S)
        return MAX_VIDEO_S
    return seconds

def _quality_keyboard(payload: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(v["label"], callback_data=f"{payload}|{k}")
        for k, v in QUALITY_PRESETS.items()
    ]
    return InlineKeyboardMarkup([buttons])

async def _deny(update: Update) -> None:
    await update.message.reply_text("Not authorised.")

def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    logger.info("CMD: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


async def send_photo_safe(bot, chat_id: int, path: Path, caption: str) -> None:
    """Send photo, retrying forever on network/timeout errors."""
    while True:
        try:
            with open(path, "rb") as f:
                await bot.send_photo(
                    chat_id,
                    photo=f,
                    caption=caption,
                    read_timeout=None,
                    write_timeout=None,
                    connect_timeout=None,
                    pool_timeout=None,
                )
            return
        except (TimedOut, NetworkError) as e:
            logger.warning("Photo send failed (%s) — retrying in 5s …", e)
            await asyncio.sleep(5)


async def send_video_safe(bot, chat_id: int, path: Path, caption: str) -> None:
    """Send video, retrying forever on network/timeout errors."""
    while True:
        try:
            with open(path, "rb") as f:
                await bot.send_video(
                    chat_id,
                    video=f,
                    caption=caption,
                    supports_streaming=True,
                    read_timeout=None,
                    write_timeout=None,
                    connect_timeout=None,
                    pool_timeout=None,
                )
            return
        except (TimedOut, NetworkError) as e:
            logger.warning("Video send failed (%s) — retrying in 5s …", e)
            await asyncio.sleep(5)


def capture_photo(w: int, h: int) -> Path:
    out = MEDIA_DIR / f"photo_{_ts()}.jpg"
    _run(["rpicam-still",
          "--width", str(w), "--height", str(h),
          "--output", str(out), "--nopreview", "-t", "500"])
    return out


def capture_video(w: int, h: int, duration_s: int) -> Path:
    duration_s = _cap(duration_s)
    raw = MEDIA_DIR / f"raw_{_ts()}.h264"
    out = MEDIA_DIR / f"video_{_ts()}.mp4"
    _run(["rpicam-vid",
          "--width", str(w), "--height", str(h),
          "--timeout", str(duration_s * 1000),
          "--output", str(raw), "--nopreview"])
    _run(["ffmpeg", "-y", "-framerate", "30",
          "-i", str(raw), "-c:v", "copy", str(out)])
    raw.unlink(missing_ok=True)
    return out

# /start

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update.effective_user.id):
        return await _deny(update)

    text = """
RPi Camera Bot

Photo commands
/start - Show all commands
/photo — single photo
/livephoto — photos every 2 s continuously
/livephotoN — send N photos (e.g. /livephoto5)
/stopphoto — stop live / interval photos
/livephotoevery10m — photo every 10 min
/livephotoevery1h — photo every 1 hour

Video commands
/video — record a 4 s clip
/livevideo10s — record 10 s clips continuously
/livevideo10s5 — record 5 clips of 10 s each
/stopvideo — stop live / interval videos
/livevideoevery10m30s — 30 s clip every 10 min
/livevideoevery1h10s — 10 s clip every 1 hour

Any problem DM @HappyBoyKartik here
"""
    await update.message.reply_text(text)

# Universal command parser

_RE_PHOTO_LIVE_N  = re.compile(r"^livephoto(\d+)$")
_RE_PHOTO_M       = re.compile(r"^livephotoevery(\d+)m$")
_RE_PHOTO_H       = re.compile(r"^livephotoevery(\d+)h$")
_RE_VIDEO_SINGLE  = re.compile(r"^video(\d+)s$")
_RE_VIDEO_LIVE    = re.compile(r"^livevideo(\d+)s$")
_RE_VIDEO_LIVE_N  = re.compile(r"^livevideo(\d+)s(\d+)$")
_RE_VIDEO_M       = re.compile(r"^livevideoevery(\d+)m(\d+)s$")
_RE_VIDEO_H       = re.compile(r"^livevideoevery(\d+)h(\d+)s$")


async def handle_all_commands(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not _allowed(update.effective_user.id):
        return await _deny(update)

    raw  = update.message.text.lstrip("/").split()[0].lower()
    data = ctx.user_data

    # /photo
    if raw == "photo":
        return await update.message.reply_text(
            "Select quality:", reply_markup=_quality_keyboard("photo"))

    # /video  (4 s default)
    if raw == "video":
        data["vid_dur"] = 4
        return await update.message.reply_text(
            "Select quality for a 4 s clip:",
            reply_markup=_quality_keyboard("video_single"))

    m = _RE_VIDEO_SINGLE.match(raw)
    if m:
        data["vid_dur"] = _cap(int(m.group(1)))
        return await update.message.reply_text(
            f"Select quality for a {data['vid_dur']}s clip:",
            reply_markup=_quality_keyboard("video_single"))

    # /livephoto
    if raw == "livephoto":
        data["lp_count"] = None
        return await update.message.reply_text(
            "Select quality — live photos every 2 s (∞):",
            reply_markup=_quality_keyboard("livephoto"))

    m = _RE_PHOTO_LIVE_N.match(raw)
    if m:
        data["lp_count"] = int(m.group(1))
        return await update.message.reply_text(
            f"Select quality — {data['lp_count']} photos every 2 s:",
            reply_markup=_quality_keyboard("livephoto"))

    m = _RE_PHOTO_M.match(raw)
    if m:
        data["lp_interval"] = int(m.group(1)) * 60
        return await update.message.reply_text(
            f"Select quality — photo every {m.group(1)} min:",
            reply_markup=_quality_keyboard("livephoto_interval"))

    m = _RE_PHOTO_H.match(raw)
    if m:
        data["lp_interval"] = int(m.group(1)) * 3600
        return await update.message.reply_text(
            f"Select quality — photo every {m.group(1)} h:",
            reply_markup=_quality_keyboard("livephoto_interval"))

    # /stopphoto
    if raw == "stopphoto":
        flag = _stop_flags.pop(f"photo_{update.effective_chat.id}", None)
        if flag:
            flag.set()
            return await update.message.reply_text("Photo task stopped.")
        return await update.message.reply_text("No active photo task.")

    m = _RE_VIDEO_LIVE.match(raw)
    if m:
        data["lv_dur"]   = _cap(int(m.group(1)))
        data["lv_count"] = None
        return await update.message.reply_text(
            f"Select quality — {data['lv_dur']}s clips (∞):",
            reply_markup=_quality_keyboard("livevideo"))

    m = _RE_VIDEO_LIVE_N.match(raw)
    if m:
        data["lv_dur"]   = _cap(int(m.group(1)))
        data["lv_count"] = int(m.group(2))
        return await update.message.reply_text(
            f"Select quality — {data['lv_count']} × {data['lv_dur']}s clips:",
            reply_markup=_quality_keyboard("livevideo"))

    m = _RE_VIDEO_M.match(raw)
    if m:
        data["lv_interval"] = int(m.group(1)) * 60
        data["lv_dur"]      = _cap(int(m.group(2)))
        return await update.message.reply_text(
            f"Select quality — {data['lv_dur']}s clip every {m.group(1)} min:",
            reply_markup=_quality_keyboard("livevideo_interval"))

    m = _RE_VIDEO_H.match(raw)
    if m:
        data["lv_interval"] = int(m.group(1)) * 3600
        data["lv_dur"]      = _cap(int(m.group(2)))
        return await update.message.reply_text(
            f"Select quality — {data['lv_dur']}s clip every {m.group(1)} h:",
            reply_markup=_quality_keyboard("livevideo_interval"))

    # /stopvideo
    if raw == "stopvideo":
        flag = _stop_flags.pop(f"video_{update.effective_chat.id}", None)
        if flag:
            flag.set()
            return await update.message.reply_text("Video task stopped.")
        return await update.message.reply_text("No active video task.")

# Callback — quality selected

async def on_quality(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    payload, qkey = query.data.split("|")
    preset  = QUALITY_PRESETS[qkey]
    w, h    = preset["w"], preset["h"]
    chat_id = query.message.chat_id
    data    = ctx.user_data
    bot     = ctx.bot
    loop    = asyncio.get_event_loop()

    await query.edit_message_text(f"{preset['label'].strip()} — starting…")

    # Single photo
    if payload == "photo":
        path = await loop.run_in_executor(None, capture_photo, w, h)
        await send_photo_safe(bot, chat_id, path, f"{qkey} {w}×{h}")
        path.unlink(missing_ok=True)

    # Single video
    elif payload == "video_single":
        dur  = data.pop("vid_dur", 4)
        path = await loop.run_in_executor(None, capture_video, w, h, dur)
        await send_video_safe(bot, chat_id, path, f"{qkey} {w}×{h} · {dur}s")
        path.unlink(missing_ok=True)

    # Live photos
    elif payload == "livephoto":
        count    = data.pop("lp_count", None)
        stop_key = f"photo_{chat_id}"
        if stop_key in _stop_flags:
            _stop_flags[stop_key].set()
        flag = threading.Event()
        _stop_flags[stop_key] = flag

        label = f"{count} photos" if count else "∞ photos"
        await bot.send_message(chat_id, f"{label} every 2 s — /stopphoto to stop.")

        async def _photo_loop(flag=flag, count=count, w=w, h=h, qkey=qkey):
            sent = 0
            while not flag.is_set():
                if count is not None and sent >= count:
                    break
                path = await loop.run_in_executor(None, capture_photo, w, h)
                cap  = f"#{sent+1} {qkey} {w}×{h}"
                await send_photo_safe(bot, chat_id, path, cap)
                path.unlink(missing_ok=True)
                sent += 1
                if (count is None or sent < count) and not flag.is_set():
                    await asyncio.sleep(2)
            _stop_flags.pop(stop_key, None)
            if count and sent >= count:
                await bot.send_message(chat_id, f"Done — sent {sent} photos.")

        asyncio.create_task(_photo_loop())

    # Interval photos
    elif payload == "livephoto_interval":
        interval = data.pop("lp_interval", 600)
        stop_key = f"photo_{chat_id}"
        if stop_key in _stop_flags:
            _stop_flags[stop_key].set()
        flag = threading.Event()
        _stop_flags[stop_key] = flag

        mins = interval // 60
        unit = f"{mins // 60}h" if mins >= 60 else f"{mins}m"
        await bot.send_message(chat_id, f" Photo every {unit} — /stopphoto to stop.")

        async def _photo_interval_loop(flag=flag, interval=interval, w=w, h=h, qkey=qkey):
            while not flag.is_set():
                path = await loop.run_in_executor(None, capture_photo, w, h)
                await send_photo_safe(bot, chat_id, path, f"Interval {qkey} {w}×{h}")
                path.unlink(missing_ok=True)
                # sleep in small chunks so /stopphoto responds quickly
                for _ in range(interval):
                    if flag.is_set():
                        break
                    await asyncio.sleep(1)
            _stop_flags.pop(stop_key, None)

        asyncio.create_task(_photo_interval_loop())

    # Live videos
    elif payload == "livevideo":
        dur      = data.pop("lv_dur", 10)
        count    = data.pop("lv_count", None)
        stop_key = f"video_{chat_id}"
        if stop_key in _stop_flags:
            _stop_flags[stop_key].set()
        flag = threading.Event()
        _stop_flags[stop_key] = flag

        label = f"{count} clips" if count else "∞ clips"
        await bot.send_message(chat_id, f"{label} of {dur}s — /stopvideo to stop.")

        async def _video_loop(flag=flag, count=count, dur=dur, w=w, h=h, qkey=qkey):
            sent = 0
            while not flag.is_set():
                if count is not None and sent >= count:
                    break
                path = await loop.run_in_executor(None, capture_video, w, h, dur)
                cap  = f"#{sent+1} {qkey} {w}×{h} · {dur}s"
                await send_video_safe(bot, chat_id, path, cap)
                path.unlink(missing_ok=True)
                sent += 1
            _stop_flags.pop(stop_key, None)
            if count and sent >= count:
                await bot.send_message(chat_id, f"Done — sent {sent} clips.")

        asyncio.create_task(_video_loop())

    # Interval videos
    elif payload == "livevideo_interval":
        interval = data.pop("lv_interval", 600)
        dur      = data.pop("lv_dur", 10)
        stop_key = f"video_{chat_id}"
        if stop_key in _stop_flags:
            _stop_flags[stop_key].set()
        flag = threading.Event()
        _stop_flags[stop_key] = flag

        mins = interval // 60
        unit = f"{mins // 60}h" if mins >= 60 else f"{mins}m"
        await bot.send_message(chat_id, f" {dur}s video every {unit} — /stopvideo to stop.")

        async def _video_interval_loop(flag=flag, interval=interval, dur=dur, w=w, h=h, qkey=qkey):
            while not flag.is_set():
                path = await loop.run_in_executor(None, capture_video, w, h, dur)
                await send_video_safe(bot, chat_id, path, f"Interval {qkey} {w}×{h} · {dur}s")
                path.unlink(missing_ok=True)
                for _ in range(interval):
                    if flag.is_set():
                        break
                    await asyncio.sleep(1)
            _stop_flags.pop(stop_key, None)

        asyncio.create_task(_video_interval_loop())


def main() -> None:
    request = HTTPXRequest(
        connect_timeout=None,
        read_timeout=None,
        write_timeout=None,
        pool_timeout=None,
    )
    app = Application.builder().token(BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_quality))
    app.add_handler(MessageHandler(filters.TEXT & filters.COMMAND, handle_all_commands))

    logger.info("Bot is running …")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
