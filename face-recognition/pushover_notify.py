"""
Pushover notification helper for PiCam.

Single source of truth for sending Pushover alerts. Imported by both
face_watcher.py (the live watcher) and pushover_test.py (manual verification),
so what you test is exactly what runs in production.

Pushover requires TWO credentials, kept in pushover_config.json (not in code):
  - user_key:  identifies the recipient (your account / phone)
  - app_token: identifies the sending application
               (create one at https://pushover.net/apps/build)
"""

import io
import json
import logging
import os
import threading
import time
from pathlib import Path

# This machine runs Norton, which does SSL/TLS inspection: it re-signs all
# HTTPS with a private root that lives in the Windows cert store but NOT in
# certifi's bundle, so plain requests/certifi verification fails. truststore
# routes verification through the OS (Windows) trust store, which already
# trusts the Norton root -- keeping verification ON (never disabled) and
# surviving Norton root rotation. Guarded so the module still imports if
# truststore is ever missing.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import requests

log = logging.getLogger("pushover")

PUSHOVER_API = "https://api.pushover.net/1/messages.json"
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # Pushover image attachment hard limit
PLACEHOLDER_TOKEN = "PASTE_YOUR_APP_TOKEN_HERE"

# Retry queue: append-only JSONL of failed sends. A background flusher
# retries with exponential backoff so a pushover.net outage or transient SSL
# failure during a stranger-at-door alert doesn't silently lose the event.
_QUEUE_PATH = Path(__file__).parent / "logs" / "pushover_queue.jsonl"
_FLUSHER_STARTED = False
_FLUSHER_LOCK = threading.Lock()
RETRY_SCHEDULE_SECONDS = [60, 300, 900, 3600]   # 1m, 5m, 15m, 60m
QUEUE_MAX_AGE_SECONDS = 4 * 3600                # drop after 4h

# Defaults double as the schema: load_config only accepts keys that appear here.
DEFAULTS = {
    "user_key": "",
    "app_token": "",
    "away_mode": True,            # master switch: no texts sent when False
    "notify_known": True,         # text when you/Mariana are recognized
    "notify_motion_no_face": True,  # text on motion even if no face is visible
    "attach_photo": True,         # attach the snapshot image to each alert
    "unknown_priority": 2,        # 2 = emergency (repeats until acknowledged)
    "emergency_retry": 60,        # seconds between emergency repeats (min 30)
    "emergency_expire": 3600,     # stop repeating after N seconds (max 10800)
    "notify_max_age": 300,        # don't text on snapshots older than N seconds
                                  # (suppresses stale-backlog bursts on restart)
    # Pushover can't ATTACH video (images only). Instead, an optional tappable
    # link to the live stream. Adds zero latency (just message metadata). Needs
    # the phone on Tailscale to reach the Pi's 100.x address while away.
    "live_url": "",
    "live_url_title": "View live camera",
    "cooldown_known": 60,         # min seconds between "known person" texts
    "cooldown_unknown": 300,      # min seconds between "stranger" texts
    "cooldown_motion": 120,       # min seconds between "motion, no face" texts
}


def load_config(path):
    """Load config JSON, filling defaults for any missing keys.
    Never raises -- returns defaults (with empty creds) if the file is
    missing or malformed, so a bad config can never crash the watcher."""
    cfg = dict(DEFAULTS)
    try:
        p = Path(path)
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            # Only copy known keys -- ignore typos/extras silently
            for k in DEFAULTS:
                if k in user_cfg:
                    cfg[k] = user_cfg[k]
    except Exception as e:
        log.warning(f"Could not read Pushover config {path}: {e}")
    return cfg


def is_configured(cfg):
    """True only if both credentials are present and the token has been
    changed from the placeholder."""
    token = cfg.get("app_token", "")
    return bool(cfg.get("user_key")) and bool(token) and token != PLACEHOLDER_TOKEN


def _downscale_jpeg(src: Path, max_bytes: int):
    """Return a BytesIO of `src` re-encoded under max_bytes, or None on any
    failure (caller falls back to skipping the attachment). Avoids losing
    the photo on alerts that happen to capture a high-res frame."""
    try:
        import cv2
        img = cv2.imread(str(src))
        if img is None:
            return None
        h, w = img.shape[:2]
        # Step the longer edge down until we fit. Pushover's 5MB limit is
        # very forgiving — for 1280x720 JPEGs we almost never need this.
        for target_long_edge in (1280, 1024, 800, 640, 480):
            scale = target_long_edge / max(h, w)
            if scale >= 1.0:
                continue
            new_w, new_h = int(w * scale), int(h * scale)
            resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok and len(buf) <= max_bytes:
                return io.BytesIO(bytes(buf))
        return None
    except Exception:
        return None


def send(cfg, title, message, priority=0, image_path=None, timeout=15):
    """Send one Pushover notification. Returns (ok: bool, detail: str).

    This is a blocking network call. Callers that must stay responsive
    (e.g. the poll loop) should run it on a background thread.

    On failure, the payload is appended to the retry queue and a background
    flusher will retry it with exponential backoff. The return value still
    reflects the immediate attempt, so callers can log it normally.
    """
    if not is_configured(cfg):
        return False, "Pushover not configured (missing user_key or app_token)"

    _ensure_flusher_started(cfg)

    ok, detail = _send_now(cfg, title, message, priority, image_path, timeout)
    if not ok:
        _enqueue(cfg, title, message, priority, image_path)
    return ok, detail


def _send_now(cfg, title, message, priority, image_path, timeout):
    data = {
        "token": cfg["app_token"],
        "user": cfg["user_key"],
        "title": title,
        "message": message,
        "priority": int(priority),
    }

    if int(priority) == 2:
        data["retry"] = max(30, int(cfg.get("emergency_retry", 60)))
        data["expire"] = min(10800, int(cfg.get("emergency_expire", 3600)))

    live_url = cfg.get("live_url")
    if live_url:
        data["url"] = str(live_url)[:512]
        data["url_title"] = str(cfg.get("live_url_title") or "View live camera")[:100]

    files = None
    fh = None
    try:
        if image_path:
            ip = Path(image_path)
            if ip.exists():
                size = ip.stat().st_size
                if size <= MAX_ATTACHMENT_BYTES:
                    fh = open(ip, "rb")
                    files = {"attachment": (ip.name, fh, "image/jpeg")}
                else:
                    # Try to downscale instead of silently dropping the photo.
                    buf = _downscale_jpeg(ip, MAX_ATTACHMENT_BYTES)
                    if buf is not None:
                        files = {"attachment": (ip.name, buf, "image/jpeg")}
                        log.info(f"Downscaled {ip.name} from {size/1e6:.1f}MB to fit Pushover 5MB cap")

        resp = requests.post(PUSHOVER_API, data=data, files=files, timeout=timeout)
        if resp.status_code == 200:
            return True, "sent"
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return False, f"request failed: {e}"
    finally:
        if fh:
            try:
                fh.close()
            except Exception:
                pass


# -- Retry queue ----------------------------------------------------
def _enqueue(cfg, title, message, priority, image_path):
    """Append a failed send to the retry queue. Stores user_key/app_token
    inline so the flusher can run even if the watcher's cfg has rotated."""
    try:
        _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.time(),
            "attempts": 0,
            "next_retry_at": time.time() + RETRY_SCHEDULE_SECONDS[0],
            "user_key": cfg.get("user_key", ""),
            "app_token": cfg.get("app_token", ""),
            "title": title,
            "message": message,
            "priority": int(priority),
            "image_path": str(image_path) if image_path else None,
            "live_url": cfg.get("live_url", ""),
            "live_url_title": cfg.get("live_url_title", ""),
            "emergency_retry": cfg.get("emergency_retry", 60),
            "emergency_expire": cfg.get("emergency_expire", 3600),
        }
        with open(_QUEUE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        log.warning(f"Could not enqueue Pushover retry: {e}")


def _read_queue():
    if not _QUEUE_PATH.exists():
        return []
    try:
        with open(_QUEUE_PATH, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception:
        return []


def _write_queue(entries):
    try:
        tmp = _QUEUE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        os.replace(tmp, _QUEUE_PATH)
    except Exception as e:
        log.warning(f"Could not rewrite Pushover queue: {e}")


def _flush_queue_once():
    entries = _read_queue()
    if not entries:
        return
    now = time.time()
    remaining = []
    sent_any = False
    for e in entries:
        # Drop if too old
        if now - e["ts"] > QUEUE_MAX_AGE_SECONDS:
            log.warning(f"Dropping stale Pushover (age {(now-e['ts'])/60:.0f}min): {e['title']}")
            continue
        if e["next_retry_at"] > now:
            remaining.append(e)
            continue
        cfg_like = {
            "user_key": e["user_key"],
            "app_token": e["app_token"],
            "live_url": e.get("live_url", ""),
            "live_url_title": e.get("live_url_title", ""),
            "emergency_retry": e.get("emergency_retry", 60),
            "emergency_expire": e.get("emergency_expire", 3600),
        }
        ok, detail = _send_now(
            cfg_like, e["title"], e["message"], e["priority"], e.get("image_path"), 15,
        )
        if ok:
            log.info(f"Pushover retry succeeded: {e['title']} (attempt {e['attempts']+1})")
            sent_any = True
            continue
        # Schedule next attempt
        e["attempts"] += 1
        if e["attempts"] >= len(RETRY_SCHEDULE_SECONDS):
            log.warning(f"Dropping Pushover after {e['attempts']} retries: {e['title']} ({detail})")
            continue
        e["next_retry_at"] = now + RETRY_SCHEDULE_SECONDS[e["attempts"]]
        remaining.append(e)

    _write_queue(remaining)
    if sent_any and remaining:
        # If something recovered AND there's still backlog, log the state
        log.info(f"Pushover queue: {len(remaining)} entries pending")


def _flusher_thread_target():
    """Wake every 30s and flush any due entries. Quiet when queue is empty."""
    while True:
        try:
            _flush_queue_once()
        except Exception as e:
            log.warning(f"Pushover flusher error: {e}")
        time.sleep(30)


def _ensure_flusher_started(cfg):
    """Start the background flusher exactly once per process."""
    global _FLUSHER_STARTED
    with _FLUSHER_LOCK:
        if _FLUSHER_STARTED:
            return
        _FLUSHER_STARTED = True
        t = threading.Thread(target=_flusher_thread_target, daemon=True)
        t.start()
