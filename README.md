# 📷 RpiCam Bot

A Telegram bot for your Raspberry Pi camera — take photos, record videos, stream live, and schedule interval captures, all from Telegram.

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
| `/live` | Start MJPEG stream via Cloudflare tunnel |
| `/stopstream` | Stop the live stream |
| `/status` | CPU temp, RAM usage, storage |

---

## Requirements

- Raspberry Pi with camera module
- Python 3.10+
- `cloudflared` installed ([download](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/))

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

> **Note:** `picamera2` is pre-installed on Raspberry Pi OS. If missing:
> ```bash
> sudo apt install python3-picamera2
> ```

### 3. Run the bot

```bash
export BOT_TOKEN="your_telegram_token_here"
export ALLOWED_IDS="telegram_user_id_here"
export STREAM_PASSWORD="stream_passwd_here"
python3 rpicam_bot.py
```

---

## Notes

- Multiple user ID's support separate with comma
- If rpicam does not work in your case see line 264 in code


## Security

- Only users listed in `ALLOWED_IDS` can operate the bot
- The MJPEG stream is protected by HTTP Basic Auth

---

## License

MIT
