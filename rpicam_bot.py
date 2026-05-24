import os
import re
import time
import shutil
import asyncio
import threading
import subprocess

import cv2
import psutil

from functools import wraps
from flask import Flask, Response, request
from picamera2 import Picamera2
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN       = os.environ["BOT_TOKEN"]
STREAM_USERNAME = os.environ.get("STREAM_USERNAME", "admin")
STREAM_PASSWORD = os.environ.get("STREAM_PASSWORD", "changeme")

_raw_ids   = os.environ.get("ALLOWED_IDS", "")
ALLOWED_IDS: set[int] = (
    {int(uid.strip()) for uid in _raw_ids.split(",") if uid.strip()}
    if _raw_ids
    else set()
)

MAX_VIDEO_DURATION = 600   # seconds


picam2      = Picamera2()
cam_lock    = threading.Lock()

def _configure_and_start_camera(cam: Picamera2) -> None:
    """Apply standard video config and start the camera."""
    cfg = cam.create_video_configuration(main={"size": (1920, 1080)})
    cam.configure(cfg)
    cam.start()


_configure_and_start_camera(picam2)


def restart_camera() -> None:
    """
    Stop, close and recreate the global picam2 instance.
    Must be called while cam_lock is held by the caller,
    or from a context where concurrent access is impossible.
    """
    global picam2

    for step in ("stop", "close"):
        try:
            getattr(picam2, step)()
        except Exception:
            pass

    try:
        picam2 = Picamera2()
        _configure_and_start_camera(picam2)
    except Exception as exc:
        print(f"[camera] restart error: {exc}")

photo_task: asyncio.Task | None = None
video_task: asyncio.Task | None = None

single_shot_lock: asyncio.Lock


def camera_busy() -> bool:
    """Return True if a live loop task is currently running."""
    return (
        (photo_task is not None and not photo_task.done()) or
        (video_task is not None and not video_task.done())
    )

latest_frame: bytes | None = None
frame_lock   = threading.Lock()

cloudflare_url:     str | None                = None
cloudflare_process: subprocess.Popen | None   = None
stream_started                                = False
stream_stop_event  = threading.Event()

# FLASK

app_flask = Flask(__name__)


def check_auth(username: str, password: str) -> bool:
    return username == STREAM_USERNAME and password == STREAM_PASSWORD


def authenticate() -> Response:
    return Response(
        "Authentication Required",
        401,
        {"WWW-Authenticate": 'Basic realm="Live Stream"'},
    )


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated


@app_flask.route("/")
@requires_auth
def video_feed() -> Response:
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app_flask.route("/_shutdown", methods=["POST"])
def flask_shutdown_route() -> Response:
    func = request.environ.get("werkzeug.server.shutdown")
    if func:
        func()
    return Response("OK", 200)


def shutdown_flask() -> None:
    try:
        import urllib.request
        urllib.request.urlopen(
            urllib.request.Request(
                "http://localhost:5000/_shutdown",
                method="POST"
            ),
            timeout=3,
        )
    except Exception as exc:
        print(f"[flask] shutdown error: {exc}")


def start_flask() -> None:
    app_flask.run(host="0.0.0.0", port=5000, threaded=True, debug=False)

def capture_frames() -> None:
    global latest_frame

    stream_stop_event.clear()

    while not stream_stop_event.is_set():
        try:
            with cam_lock:
                frame = picam2.capture_array()

            _, buffer = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95]
            )

            with frame_lock:
                latest_frame = buffer.tobytes()

        except Exception as exc:
            print(f"[stream] frame error: {exc}")
            time.sleep(0.1)

    with frame_lock:
        latest_frame = None

    print("[stream] capture thread stopped.")


def generate_frames():
    while True:
        with frame_lock:
            frame = latest_frame

        if frame is None:
            time.sleep(0.01)
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            + frame
            + b"\r\n"
        )
        time.sleep(0.03)

# CLOUDFLARE TUNNEL

def start_cloudflare() -> None:
    global cloudflare_url, cloudflare_process

    cloudflare_process = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", "http://localhost:5000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    for line in cloudflare_process.stdout:
        print(line, end="")
        match = re.search(r"https://[^\s]+\.trycloudflare\.com", line)
        if match:
            cloudflare_url = match.group(0)
            print(f"[cloudflare] tunnel URL: {cloudflare_url}")
            break

# AUTHORIZATION

def authorized(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if ALLOWED_IDS and update.effective_user.id not in ALLOWED_IDS:
            await update.message.reply_text("⛔ Unauthorized.")
            return
        return await handler(update, context)
    return wrapper

async def send_photo(update: Update) -> None:
    image_path = "capture.jpg"
    try:
        with cam_lock:
            picam2.capture_file(image_path)

        with open(image_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=120,
                pool_timeout=120,
            )
    except Exception as exc:
        print(f"[photo] error: {exc}")
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)


async def record_video(update: Update, duration: int) -> None:
    video_path = "video.mp4"

    try:
        with cam_lock:
            try:
                picam2.stop()
                picam2.close()
            except Exception:
                pass

        await update.message.reply_text(f"⏺ Recording {duration}s video…")

        result = subprocess.run(
            [
                "rpicam-vid", # IF libcam WORK IN YOUR CASE THEN REPLACE rpicam TO libcamera
                "-t", str(duration * 1000),
                "--nopreview",
                "--width", "1280",
                "--height", "720",
                "--framerate", "24",
                "--bitrate", "2000000",
                "--codec", "libav",
                "-o", video_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        with cam_lock:
            restart_camera()

        if result.returncode != 0 or not os.path.exists(video_path):
            await update.message.reply_text("❌ Video recording failed.")
            return

        with open(video_path, "rb") as f:
            await update.message.reply_video(
                video=f,
                read_timeout=600,
                write_timeout=600,
                connect_timeout=600,
                pool_timeout=600,
            )

    except Exception as exc:
        print(f"[video] error: {exc}")
        await update.message.reply_text(f"❌ Error: {exc}")

    finally:
        if os.path.exists(video_path):
            os.remove(video_path)

        with cam_lock:
            try:
                if not picam2.started:
                    restart_camera()
            except Exception:
                restart_camera()


async def livephoto_loop(update: Update, count: int | None) -> None:
    sent = 0
    try:
        while True:
            await send_photo(update)
            sent += 1
            if count and sent >= count:
                break
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        pass
    finally:
        await update.message.reply_text("📷 Live photo stopped.")


async def livevideo_loop(
    update: Update, duration: int, count: int | None
) -> None:
    sent = 0
    try:
        while True:
            await record_video(update, duration)
            sent += 1
            if count and sent >= count:
                break
    except asyncio.CancelledError:
        pass
    finally:
        await update.message.reply_text("🎥 Live video stopped.")


async def livephoto_interval_loop(
    update: Update, interval_secs: int, label: str
) -> None:
    """Send a photo immediately, then repeat every interval_secs."""
    try:
        while True:
            await send_photo(update)
            await asyncio.sleep(interval_secs)   # CancelledError lands here
    except asyncio.CancelledError:
        pass
    finally:
        await update.message.reply_text("📷 Interval photo stopped.")


async def livevideo_interval_loop(
    update: Update, interval_secs: int, duration: int, label: str
) -> None:
    """Record a clip immediately, then repeat every interval_secs."""
    try:
        while True:
            await record_video(update, duration)
            await asyncio.sleep(interval_secs)   # CancelledError lands here
    except asyncio.CancelledError:
        pass
    finally:
        await update.message.reply_text("🎥 Interval video stopped.")


def parse_interval(value: str, unit: str) -> tuple[int, str]:
    """
    Convert (value, unit) → (seconds, human-readable label).
    unit is 'm' (minutes) or 'h' (hours).
    """
    n = int(value)
    if unit == "m":
        return n * 60, f"{n} min"
    else:
        return n * 3600, f"{n} h"


# COMMAND HANDLERS

@authorized
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *RpiCam Bot*\n\n"
        "Commands:\n"
        "/photo — take a single photo\n"
        "/video — record a 4 s clip\n"
        "/livephoto — stream photos every 2 s\n"
        "/livephotoN — stream N photos (e.g. /livephoto5)\n"
        "/stopphoto — stop live photos\n"
        "/livevideo10s — record clips of 10 s each\n"
        "/livevideo10s5 — record 5 clips of 10 s each\n"
        "/stopvideo — stop live video\n"
        "\n"
        "📸 *Interval photo (minutes or hours):*\n"
        "/livephotoevery10m — photo every 10 minutes\n"
        "/livephotoevery1h — photo every 1 hour\n"
        "➡️ stop with /stopphoto\n"
        "\n"
        "🎥 *Interval video (minutes or hours):*\n"
        "/livevideoevery10m30s — 30 s clip every 10 minutes\n"
        "/livevideoevery1h10s — 10 s clip every 1 hour\n"
        "➡️ stop with /stopvideo\n"
        "\n"
        "/live — start MJPEG stream via Cloudflare\n"
        "/stopstream — stop the live stream\n"
        "/status — CPU temp, RAM, storage",
        parse_mode="Markdown",
    )


@authorized
async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if camera_busy():
        await update.message.reply_text("⏳ Camera busy with another task.")
        return

    async with single_shot_lock:
        await send_photo(update)


@authorized
async def video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if camera_busy():
        await update.message.reply_text("⏳ Camera busy with another task.")
        return

    async with single_shot_lock:
        await record_video(update, 4)


@authorized
async def livephoto(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global photo_task

    if camera_busy():
        await update.message.reply_text("⏳ Camera busy with another task.")
        return

    text  = update.message.text.strip()
    match = re.match(r"/livephoto(\d+)?", text)
    count = int(match.group(1)) if match and match.group(1) else None

    photo_task = asyncio.create_task(livephoto_loop(update, count))
    await update.message.reply_text("📷 Live photo started.")


@authorized
async def stopphoto(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global photo_task

    if photo_task and not photo_task.done():
        photo_task.cancel()
        await update.message.reply_text("🛑 Stopping live photo…")
    else:
        await update.message.reply_text("No live photo is running.")


@authorized
async def livevideo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global video_task

    if camera_busy():
        await update.message.reply_text("⏳ Camera busy with another task.")
        return

    text  = update.message.text.strip()
    match = re.match(r"/livevideo(\d+)s(\d+)?", text)

    if not match:
        await update.message.reply_text(
            "Usage examples:\n/livevideo10s\n/livevideo10s5"
        )
        return

    duration = min(int(match.group(1)), MAX_VIDEO_DURATION)
    count    = int(match.group(2)) if match.group(2) else None

    video_task = asyncio.create_task(livevideo_loop(update, duration, count))
    await update.message.reply_text(
        f"🎥 Live video started ({duration}s per clip)."
    )


@authorized
async def stopvideo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global video_task

    if video_task and not video_task.done():
        video_task.cancel()
        await update.message.reply_text("🛑 Stopping live video…")
    else:
        await update.message.reply_text("No live video is running.")


@authorized
async def livephotoevery(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global photo_task

    if camera_busy():
        await update.message.reply_text("⏳ Camera busy with another task.")
        return

    text  = update.message.text.strip()
    match = re.match(r"^/livephotoevery(\d+)(m|h)$", text)
    if not match:
        await update.message.reply_text(
            "Usage:\n"
            "/livephotoevery10m — photo every 10 minutes\n"
            "/livephotoevery1h  — photo every 1 hour"
        )
        return

    interval_secs, label = parse_interval(match.group(1), match.group(2))

    photo_task = asyncio.create_task(
        livephoto_interval_loop(update, interval_secs, label)
    )
    await update.message.reply_text(
        f"📷 Interval photo started — every {label}.\n"
        "Use /stopphoto to stop."
    )


@authorized
async def livevideoevery(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global video_task

    if camera_busy():
        await update.message.reply_text("⏳ Camera busy with another task.")
        return

    text  = update.message.text.strip()
    match = re.match(r"^/livevideoevery(\d+)(m|h)(\d+)s$", text)
    if not match:
        await update.message.reply_text(
            "Usage:\n"
            "/livevideoevery10m30s — 30 s clip every 10 minutes\n"
            "/livevideoevery1h10s  — 10 s clip every 1 hour"
        )
        return

    interval_secs, label = parse_interval(match.group(1), match.group(2))
    duration = min(int(match.group(3)), MAX_VIDEO_DURATION)

    video_task = asyncio.create_task(
        livevideo_interval_loop(update, interval_secs, duration, label)
    )
    await update.message.reply_text(
        f"🎥 Interval video started — {duration}s clip every {label}.\n"
        "Use /stopvideo to stop."
    )


@authorized
async def stopstream(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global stream_started, cloudflare_url, cloudflare_process

    if not stream_started:
        await update.message.reply_text("No live stream is running.")
        return

    await update.message.reply_text("🛑 Stopping live stream…")

    stream_stop_event.set()

    if cloudflare_process and cloudflare_process.poll() is None:
        cloudflare_process.terminate()
        try:
            cloudflare_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cloudflare_process.kill()
        print("[cloudflare] tunnel terminated.")

    threading.Thread(target=shutdown_flask, daemon=True).start()

    stream_started     = False
    cloudflare_url     = None
    cloudflare_process = None

    await update.message.reply_text("✅ Live stream stopped.")


@authorized
async def status(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp = int(f.read()) / 1000
    except Exception:
        temp = "Unknown"

    ram  = psutil.virtual_memory()
    disk = shutil.disk_usage("/")

    await update.message.reply_text(
        f"🌡 CPU Temp:      {temp}°C\n"
        f"💾 RAM Usage:     {ram.percent}%\n"
        f"💿 Storage Free:  {disk.free // (1024 ** 3)} GB"
    )


@authorized
async def live(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global stream_started, cloudflare_url

    if not stream_started:
        await update.message.reply_text("📡 Starting live stream…")

        threading.Thread(target=capture_frames, daemon=True).start()
        threading.Thread(target=start_flask,    daemon=True).start()
        time.sleep(3)
        threading.Thread(target=start_cloudflare, daemon=True).start()

        deadline = time.time() + 30
        while cloudflare_url is None:
            if time.time() > deadline:
                await update.message.reply_text("❌ Tunnel startup timed out.")
                return
            await asyncio.sleep(1)

        stream_started = True

    await update.message.reply_text(
        f"📺 Live Stream:\n{cloudflare_url}\n\n"
        f"Login: `{STREAM_USERNAME}` / `{STREAM_PASSWORD}`",
        parse_mode="Markdown",
    )

# APPLICATION ENTRY POINT

def main() -> None:
    global single_shot_lock
    single_shot_lock = asyncio.Lock()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Single-shot commands
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("photo",      photo))
    app.add_handler(CommandHandler("video",      video))
    app.add_handler(CommandHandler("stopphoto",  stopphoto))
    app.add_handler(CommandHandler("stopvideo",  stopvideo))
    app.add_handler(CommandHandler("status",     status))
    app.add_handler(CommandHandler("live",       live))
    app.add_handler(CommandHandler("stopstream", stopstream))

    # Live loops  (/livephotoN, /livevideo10s, /livevideo10s5)
    app.add_handler(
        MessageHandler(filters.Regex(r"^/livephoto\d*$"), livephoto)
    )
    app.add_handler(
        MessageHandler(filters.Regex(r"^/livevideo\d+s\d*$"), livevideo)
    )

    # Interval loops — minutes OR hours
    app.add_handler(
        MessageHandler(filters.Regex(r"^/livephotoevery\d+(m|h)$"), livephotoevery)
    )
    app.add_handler(
        MessageHandler(filters.Regex(r"^/livevideoevery\d+(m|h)\d+s$"), livevideoevery)
    )

    print("Bot running…")
    app.run_polling()


if __name__ == "__main__":
    main()
