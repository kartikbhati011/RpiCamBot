# 📷 RpiCam Bot

A Telegram bot for your Raspberry Pi camera — take photos, record videos and schedule interval captures, all from Telegram.

---

## Features

| Command | Description |
|---|---|
| `/start` | Show all commands |
| `/photo` | Take a single photo |
| `/video` | Record a 4s clip |
| `/livephoto` | Send photos every 2s continuously |
| `/livephotoN` | Send N photos (e.g. `/livephoto5`) |
| `/stopphoto` | Stop live / interval photos |
| `/livevideo10s` | Record 10s clips continuously |
| `/livevideo10s5` | Record 5 clips of 10s each |
| `/stopvideo` | Stop live / interval videos |
| `/livephotoevery10m` | Photo every 10 minutes |
| `/livephotoevery1h` | Photo every 1 hour |
| `/livevideoevery10m30s` | 30s clip every 10 minutes |
| `/livevideoevery1h10s` | 10s clip every 1 hour |
| `/status` | CPU temp, RAM usage, storage |

---

## Requirements

- Raspberry Pi with camera module ([Amazon](https://amzn.in/d/0ea5sp6H))
- Python 3.10+

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/kartikbhati011/RpiCamBot.git
cd RpiCamBot
```

### 2. Install dependencies

```bash
pip3 install -r requirements.txt --break-system-packages
```

> **Note:** Install these all packages if missing
> ```bash
> sudo apt install python3-opencv rpicam-apps python3-picamera2 libcamera-apps ffmpeg
> ```

### 3. Run the bot

```bash
python3 rpicam_bot.py
```

---

## Notes

- Multiple user ID's support separate with comma

## 📸 Photo Quality Options

| # | Resolution | Size Name    | JPEG Quality |
|---|------------|--------------|---------------|
| 1 | 640×360    | Low          | 50            |
| 2 | 1280×720   | HD           | 75            |
| 3 | 1920×1080  | Full HD      | 85            |

## 🎥 Video Quality Options

| # | Resolution | Size Name   | Framerate | Bitrate | Est. Size/min |
|---|------------|-------------|------------|----------|----------------|
| 1 | 640×360    | Low         | 24 fps     | 1 Mbps   | ~7 MB          |
| 2 | 1280×720   | HD          | 24 fps     | 2 Mbps   | ~15 MB         |
| 4 | 1920×1080  | FullHD      | 24 fps     | 8 Mbps   | ~60 MB         |

## Security

- Only users listed in `ALLOWED_IDS` can operate the bot

## License

Under MIT
