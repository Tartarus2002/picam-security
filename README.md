# PiCam Security

Home security camera built on a **Raspberry Pi Zero 2 W + Arducam IMX519**, with face recognition, motion detection, RTSP/HLS/WebRTC streaming, and Pushover emergency alerts. Two halves:

- **Pi side** — motion detection + RTSP/HLS streaming, runs as a single systemd unit.
- **PC side** — Windows watcher that pulls snapshots over SFTP, runs face recognition (dlib HOG), plays a greeting on known faces, fires Pushover alerts on strangers.

## Architecture

```
  ┌──────────────────────────────┐         ┌──────────────────────────────┐
  │  Raspberry Pi Zero 2 W       │         │  Windows PC                  │
  │                              │         │                              │
  │  picam-motion.service        │         │  face_watcher.py             │
  │    motion_detector.py        │         │    SFTP poll every 2s        │
  │    └ picamera2 direct        │         │    HOG face recognition      │
  │    └ embedded mediamtx       │         │    pygame audio greeting     │
  │    └ burst-3 snapshots       │         │    Tailscale-presence        │
  │       per motion event       │         │       away_mode auto-toggle  │
  │                              │         │    Pi-alive dead-man         │
  │  picam-stream.service        │         │    Singleton mutex           │
  │    (failover when motion     │         │                              │
  │     stops, ExecStopPost)     │         │  clip-sync.py                │
  │                              │         │    SFTP every 2 min          │
  │  cron freeze-watchdog        │         │    atomic .part rename       │
  │    every 3 min               │         │                              │
  │    motion.log mtime > 150s   │         │  pushover_notify.py          │
  │    -> systemctl restart      │         │    truststore for Norton SSL │
  │                              │         │    retry queue (4h max)      │
  │                              │         │    image auto-downscale      │
  └──────────────┬───────────────┘         └──────────────┬───────────────┘
                 │                                        │
                 │       SFTP snapshots + clips           │
                 │ ──────────────────────────────────────►│
                 │       (key auth, Tailscale or LAN)     │
                 │                                        │
                 │       RTSP / HLS / WebRTC stream       │
                 │ ──────────────────────────────────────►│
                 │       (port 8554 / 8888 / 8889)        │
                 ▼                                        ▼
```

Snapshot lifecycle: Pi captures 3-frame burst on motion → SFTP-pulled by Windows → face_recognition runs on each → known faces play `welcome_home.mp3` (or per-person `welcome_<name>.mp3`) → Pushover fired with snapshot + tappable live link → Pi-side snapshot deleted to free SD space.

## Layout

```
.
├── motion-detect/motion_detector.py     # Pi: capture + stream + snapshot
├── face-recognition/                    # PC-side watcher
│   ├── face_watcher.py                  #   main daemon (singleton-locked)
│   ├── pushover_notify.py               #   Pushover send + retry queue
│   ├── pushover_config.template.json    #   copy to pushover_config.json
│   ├── pushover_test.py                 #   verify creds reach your phone
│   ├── fix_blue_snapshots.py            #   one-off R/B channel recovery
│   └── test_audio.py / test_encoding.py # diagnostics
├── clip-sync.py                         # PC: atomic SFTP pull every 2 min
├── picam_secrets.py                     # central credential loader
├── config/picam.conf.template           # copy to picam.conf
├── services/
│   ├── picam-motion.service             # the only "always on" unit
│   ├── picam-stream.service             # swap target (ExecStopPost)
│   ├── picam-freeze-watchdog.cron       # /etc/cron.d entry
│   ├── watchdog.sh                      # cron's health-check
│   └── _deprecated/                     # old units that caused the freeze
├── scripts/storage-cleanup.sh           # Pi: hourly storage trim
├── boot-config/                         # microSD bootfs helpers
└── SETUP-GUIDE.txt                      # end-to-end install walkthrough
```

## Setup

### 1. Pi-side install
Follow `SETUP-GUIDE.txt`. After first boot:

```bash
cp config/picam.conf.template config/picam.conf   # fill in WiFi creds
sudo cp services/picam-motion.service services/picam-stream.service /etc/systemd/system/
sudo cp services/picam-freeze-watchdog.cron /etc/cron.d/picam-freeze-watchdog
sudo cp services/watchdog.sh /home/pi/picam/watchdog.sh && sudo chmod +x /home/pi/picam/watchdog.sh
sudo systemctl daemon-reload && sudo systemctl enable --now picam-motion
```

### 2. PC-side install (Windows)
```bash
# Drop credentials at %USERPROFILE%\.picam_secrets.json (gitignored):
# {
#   "pi_host": "192.168.1.X",
#   "pi_user": "pi",
#   "pi_password": "<fallback>",
#   "pi_ssh_key": "C:\\Users\\YOU\\.ssh\\picam_ed25519"
# }

cp face-recognition/pushover_config.template.json face-recognition/pushover_config.json
# fill in your user_key and app_token

pip install opencv-python face_recognition pygame paramiko pycaw truststore requests
python face-recognition/face_watcher.py   # or via Start Face Watcher.bat / .vbs
```

### 3. SSH key auth (recommended)
```bash
ssh-keygen -t ed25519 -f ~/.ssh/picam_ed25519 -N ""
ssh-copy-id -i ~/.ssh/picam_ed25519.pub pi@<pi>
# `picam_secrets.py` auto-detects the key and uses it; password is fallback.
```

## Hard-won design decisions

- **picamera2 direct capture (not RTSP+OpenCV).** FFmpeg's H.264 decoder caches frames, causing motion blindness.
- **Single-shot autofocus (AfMode=1).** Continuous AF moves the lens, creating motion false positives.
- **XBGR8888 byte order is `[R, G, B, X]` on little-endian ARM.** Naïve `frame[:, :, :3]` slice swaps R and B. Use `frame[:, :, [2, 1, 0]]`. See `_to_bgr` in `motion_detector.py`.
- **Burst-3 capture (0/300ms/600ms) per motion event.** Doorway use case: subject in frame for ~1s. One frame at one moment misses too often; three angles boost recognition.
- **Pre-2026-05-31: `picam-watchdog.timer` probed mediamtx API port 9997 but the embedded mediamtx has `api: no`.** The probe always failed → it restarted `picam-stream` every 5 min → camera contention with motion-detector → 11-hour silent freeze. Replaced by a cron job that checks `motion.log` mtime > 150s. See `services/_deprecated/README.md`.
- **`face_recognition` (dlib-bin prebuilt wheel) over the CNN model.** CNN segfaulted on CPU-only Windows.
- **Pushover, not Telegram.** Image attachment + tappable live URL + emergency priority bypasses Do Not Disturb. No bot framework needed.
- **truststore.inject_into_ssl() at module import.** This PC runs Norton SSL inspection, which signs HTTPS with a private root in the Windows store but not in certifi's bundle. `truststore` routes verification through the OS trust store.

## Status & gotchas

- The system is live. Detection rate, retention policies, and Pi power supply are tuned by editing `face-recognition/pushover_config.json` and `config/picam.conf`. None require restarting either side.
- Pi Zero 2 W's USB power draw exceeds typical PC USB-A 2.0 ports (500 mA). Use a real 5V/2.5A wall adapter to avoid silent undervoltage frame drops. The brownout watcher thread in `motion_detector.py` will log `Throttle state changed: 0x...` to `motion.log` when this happens.
- `face-recognition/fix_blue_snapshots.py` rewrites pre-fix JPEGs whose R and B channels are swapped (snapshots saved before 2026-06-15). Run with `--apply` after a dry-run.

## License

Personal project, no license declared. If you'd like to reuse any of it, open an issue first.
