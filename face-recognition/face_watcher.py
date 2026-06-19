"""
PiCam Face Watcher v4.0 -- REAL-TIME face recognition from Pi security camera.

Pipeline (fast, non-blocking):
  1. Polls Pi directly via SFTP every 2 seconds for new motion snapshots
  2. Downloads new snapshots instantly
  3. Runs face_recognition (dlib) for detection + recognition
  4. Known face -> plays per-person greeting (or generic welcome_home.mp3)
  5. Unknown face -> alert beep
  6. Audio is non-blocking -- polling continues during playback
  7. Deletes processed snapshots from Pi to free SD space
  8. Sends Pushover alerts gated by away_mode (auto-toggled by phone presence)

v4.0 changes:
  - Pi password no longer hardcoded; loads via picam_secrets.py
  - Singleton mutex via 127.0.0.1:51731 (prevents Startup-folder stacking)
  - Per-identity Pushover cooldowns (not per-category) -- one stranger
    lingering on the porch no longer fires every 5 min for an hour
  - Tailscale-presence away_mode auto-toggle (phone reachable = home,
    silence emergency alerts; phone gone for N polls = away, arm alerts)
  - Hot-reload of faces/ folder -- add a new person's JPEGs and the
    watcher re-encodes WITHOUT restart
  - Per-person greeting audio: prefer audio/welcome_<name>.mp3, fall
    back to the generic welcome_home.mp3
  - Local snapshot pruning -- delete motion_*.jpg older than N days
    AND keep total under N GB (Pi-side already prunes; Windows side did not)
  - Pi-alive dead-man switch: single Pushover priority 1 if SFTP has been
    silent for >15 min, single "back online" on recovery
  - cmdline-verified PID kill (no more wrong-process-with-recycled-PID risk)
"""

import sys

# -- Path bootstrap so picam_secrets.py (one level up) imports cleanly ----
from pathlib import Path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import cv2
import numpy as np
import os
import socket
import subprocess
import time
import json
import logging
from logging.handlers import RotatingFileHandler
import traceback
import atexit
import threading
import winsound
import pygame
import paramiko
import face_recognition
from collections import defaultdict

import picam_secrets

# Optional: Pushover text alerts. Guarded so a missing/broken notifier can
# never take down the core watcher (face recognition + audio still run).
try:
    import pushover_notify
    PUSHOVER_AVAILABLE = True
except Exception:
    pushover_notify = None
    PUSHOVER_AVAILABLE = False

# -- Paths --------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
FACES_DIR = SCRIPT_DIR / "faces"
AUDIO_DIR = SCRIPT_DIR / "audio"
LOG_DIR = SCRIPT_DIR / "logs"
PUSHOVER_CONFIG = SCRIPT_DIR / "pushover_config.json"

LOCAL_SNAPSHOTS = Path(r"C:\Users\Tarik\Desktop\_Projects\pi-cam-motion-clips\snapshots")

# -- Audio files ---------------------------------------------------
WELCOME_AUDIO = AUDIO_DIR / "welcome_home.mp3"  # generic fallback
UNKNOWN_AUDIO = AUDIO_DIR / "unknown_detected.wav"

# -- Settings ------------------------------------------------------
FACE_TOLERANCE = 0.6    # dlib's empirically validated default
POLL_INTERVAL = 2       # SFTP poll cadence
AUDIO_COOLDOWN = 30     # Seconds between audio plays per category
TARGET_VOLUME = 0.50
SSH_TIMEOUT = 5
RECONNECT_DELAY = 3

# Snapshot retention (Windows side). Pi side is already pruned remotely.
LOCAL_RETENTION_DAYS = 14
LOCAL_RETENTION_MAX_GB = 5.0
PRUNE_INTERVAL = 30 * 60   # run prune at most every 30 min

# Pi-alive dead-man switch
PI_DEAD_THRESHOLD = 15 * 60  # 15 min of no contact -> Pushover

# Per-identity Pushover cooldown (in addition to per-category)
DEFAULT_IDENTITY_COOLDOWN = 10 * 60  # 10 min

# Tailscale presence -> away_mode auto-toggle
PRESENCE_POLL_INTERVAL = 60        # seconds
PRESENCE_HITS_TO_HOME = 2          # consecutive hits to flip to home
PRESENCE_MISSES_TO_AWAY = 5        # consecutive misses to flip to away
TAILSCALE_PHONE_HOSTNAME = None    # set in pushover_config.json "presence_host"

# Singleton mutex port (127.0.0.1 bind)
SINGLETON_PORT = 51731

# -- Logging -------------------------------------------------------
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOCAL_SNAPSHOTS.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(
            LOG_DIR / "face_watcher.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("face_watcher")

for _noisy in ["paramiko", "paramiko.transport", "paramiko.auth", "paramiko.sftp"]:
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# -- Singleton mutex -----------------------------------------------
# Bind a port on 127.0.0.1 — OS releases on process exit/crash, so no
# stale-lock cleanup is needed. Stacks of hidden pythonw daemons from
# Startup-folder re-launches can no longer happen silently.
_SINGLETON_SOCK = None


def _acquire_singleton():
    global _SINGLETON_SOCK
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        s.bind(("127.0.0.1", SINGLETON_PORT))
        s.listen(1)
        _SINGLETON_SOCK = s
        return True
    except OSError as e:
        log.warning(f"Another face_watcher is already running (singleton port {SINGLETON_PORT} taken: {e}). Exiting.")
        return False


# -- PID management (kill verifiably-our zombies on startup) -------
PID_FILE = LOG_DIR / "face_watcher.pid"


def _proc_cmdline_contains(pid: int, needle: str) -> bool:
    """Best-effort check that PID is one of OUR Python processes before we kill it.
    Avoids the recycled-PID hazard where an unrelated process inherits an old PID."""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
            capture_output=True, text=True, timeout=5,
        )
        return needle.lower() in (out.stdout or "").lower()
    except Exception:
        return False


def _kill_old_instance():
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return
    if old_pid == os.getpid():
        return
    if not _proc_cmdline_contains(old_pid, "face_watcher.py"):
        log.info(f"Stale PID file (PID {old_pid} is not a face_watcher) — ignoring")
        return
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/PID", str(old_pid)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            log.info(f"Killed zombie face_watcher (PID {old_pid})")
            time.sleep(1)
    except Exception:
        pass


def _write_pid():
    try:
        PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass


def _cleanup_pid():
    try:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except Exception:
        pass


# -- Volume control ------------------------------------------------
def set_volume(level: float):
    try:
        from pycaw.pycaw import AudioUtilities
        devices = AudioUtilities.GetSpeakers()
        vol = devices.EndpointVolume
        vol.SetMasterVolumeLevelScalar(level, None)
    except Exception as e:
        log.warning(f"Could not set volume: {e}")


def play_audio(person: str, known_names: set):
    """Play audio for `person`. If known, prefer per-person greeting
    (audio/welcome_<name>.mp3); fall back to generic welcome_home.mp3.
    Non-blocking — returns immediately."""
    is_known = person.lower() in known_names
    if is_known:
        per_person = AUDIO_DIR / f"welcome_{person.lower()}.mp3"
        audio_path = per_person if per_person.exists() else WELCOME_AUDIO
    else:
        audio_path = UNKNOWN_AUDIO

    try:
        set_volume(TARGET_VOLUME)
    except Exception as e:
        log.warning(f"Volume set failed: {e}")

    if audio_path.exists():
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            pygame.mixer.music.load(str(audio_path))
            pygame.mixer.music.set_volume(1.0)
            pygame.mixer.music.play()
            log.info(f"Playing: {audio_path.name} for {person.upper()}")
        except Exception as e:
            log.warning(f"Audio playback failed: {e}")
            try:
                winsound.Beep(600 if is_known else 1200, 500)
            except Exception:
                pass
    else:
        try:
            winsound.Beep(600 if is_known else 1200, 300 if is_known else 500)
        except Exception:
            pass


# -- Face Recognition Engine (dlib) with hot-reload ----------------
class FaceEngine:
    """Loads reference encodings from faces/<name>/*.jpg. Reloads when
    the faces/ directory mtime changes — add a new person's JPEGs and
    the watcher picks them up without a restart."""

    def __init__(self):
        log.info("Loading face_recognition (dlib) engine...")
        self.known_encodings = []
        self.known_names = []
        self._dir_signature = None
        self.reload()

    def _signature(self):
        """Cheap fingerprint of faces/ — sum of (path, mtime, size) for every
        candidate image. Changes on add/remove/edit of any image file."""
        sig = []
        if not FACES_DIR.exists():
            return tuple()
        for person_dir in sorted(FACES_DIR.iterdir()):
            if not person_dir.is_dir():
                continue
            for img_path in sorted(person_dir.iterdir()):
                if img_path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp"):
                    continue
                try:
                    st = img_path.stat()
                    sig.append((str(img_path), int(st.st_mtime), st.st_size))
                except OSError:
                    pass
        return tuple(sig)

    def reload_if_changed(self):
        sig = self._signature()
        if sig == self._dir_signature:
            return False
        log.info("faces/ changed on disk — reloading encodings")
        self.reload()
        return True

    def reload(self):
        self.known_encodings = []
        self.known_names = []
        if not FACES_DIR.exists():
            self._dir_signature = self._signature()
            return
        for person_dir in sorted(FACES_DIR.iterdir()):
            if not person_dir.is_dir():
                continue
            name = person_dir.name.lower()
            if name == "unknown":
                log.warning("Reserved folder name 'unknown' — skipping")
                continue
            count = 0
            for img_path in sorted(person_dir.iterdir()):
                if img_path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp"):
                    continue
                try:
                    img = face_recognition.load_image_file(str(img_path))
                    encodings = face_recognition.face_encodings(img, num_jitters=10)
                except Exception as e:
                    log.warning(f"Skipping bad reference {name}/{img_path.name}: {e}")
                    continue
                if not encodings:
                    log.warning(f"No face in: {name}/{img_path.name}")
                    continue
                if len(encodings) > 1:
                    log.warning(f"{name}/{img_path.name}: {len(encodings)} faces found, using first")
                self.known_encodings.append(encodings[0])
                self.known_names.append(name)
                count += 1
            if count > 0:
                log.info(f"  {count} encoding(s) for '{name}'")
        people = sorted(set(self.known_names))
        log.info(f"Face database: {len(people)} people ({', '.join(people) or 'none'}), "
                 f"{len(self.known_encodings)} encodings")
        self._dir_signature = self._signature()

    @property
    def known_people(self) -> set:
        return set(self.known_names)

    def recognize(self, img_path: str):
        results = []
        img = face_recognition.load_image_file(img_path)
        h, w = img.shape[:2]
        small = cv2.resize(img, (w // 2, h // 2))
        face_locations_small = face_recognition.face_locations(
            small, number_of_times_to_upsample=1, model="hog"
        )
        if not face_locations_small:
            log.info(f"No face detected in {img_path}")
            return results

        face_locations = [
            (top * 2, right * 2, bottom * 2, left * 2)
            for top, right, bottom, left in face_locations_small
        ]
        face_encs = face_recognition.face_encodings(img, face_locations, num_jitters=1)

        for enc, loc in zip(face_encs, face_locations):
            top, right, bottom, left = loc
            if not self.known_encodings:
                results.append(("unknown", 0.0, 1.0, [left, top, right - left, bottom - top]))
                continue
            distances = face_recognition.face_distance(self.known_encodings, enc)
            best_idx = int(np.argmin(distances))
            best_dist = float(distances[best_idx])
            name = self.known_names[best_idx] if best_dist <= FACE_TOLERANCE else "unknown"
            score = max(0.0, 1.0 - best_dist)
            results.append((name, score, best_dist, [left, top, right - left, bottom - top]))
        return results


# -- Pi Connection Manager -----------------------------------------
class PiConnection:
    def __init__(self):
        self.ssh = None
        self.sftp = None
        self._creds = picam_secrets.load_pi_creds()
        self.host = self._creds["hostname"]

    def connect(self):
        try:
            self.close()
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(**self._creds)
            transport = self.ssh.get_transport()
            if transport:
                transport.set_keepalive(30)
            self.sftp = self.ssh.open_sftp()
            log.info(f"Connected to Pi at {self.host}")
            return True
        except Exception as e:
            log.warning(f"Pi connection failed: {e}")
            self.ssh = None
            self.sftp = None
            return False

    def close(self):
        try:
            if self.sftp:
                self.sftp.close()
            if self.ssh:
                self.ssh.close()
        except Exception:
            pass
        self.ssh = None
        self.sftp = None

    def is_alive(self):
        try:
            transport = self.ssh.get_transport() if self.ssh else None
            if transport and transport.is_active():
                transport.send_ignore()
                return True
        except Exception:
            pass
        return False

    def list_files(self, remote_dir, extensions=(".jpg", ".jpeg")):
        try:
            files = []
            for attr in self.sftp.listdir_attr(remote_dir):
                if any(attr.filename.lower().endswith(ext) for ext in extensions):
                    files.append(attr)
            return files
        except FileNotFoundError:
            return []
        except IOError as e:
            if "No such file" in str(e):
                return []
            raise

    def download(self, remote_path, local_path):
        self.sftp.get(remote_path, str(local_path))

    def delete(self, remote_path):
        self.sftp.remove(remote_path)


# -- Tailscale presence detector -----------------------------------
class PresenceDetector:
    """Tracks whether the configured 'home' device (e.g. Tarik's phone) is
    currently reachable. Used to flip pushover_config.json's away_mode."""

    def __init__(self):
        self._hits = 0
        self._misses = 0
        self.is_home = None   # None until first decision

    def _phone_reachable(self, hostname_or_ip: str) -> bool:
        if not hostname_or_ip:
            return False
        # Prefer `tailscale status --json` for an authoritative answer; fall
        # back to plain ping for LAN-only homes.
        try:
            r = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout:
                data = json.loads(r.stdout)
                peers = (data.get("Peer") or {}).values()
                for p in peers:
                    name = (p.get("HostName") or "").lower()
                    if hostname_or_ip.lower() in name:
                        return bool(p.get("Online"))
        except Exception:
            pass
        # ping fallback
        try:
            r = subprocess.run(
                ["ping", "-n", "1", "-w", "1000", hostname_or_ip],
                capture_output=True, text=True, timeout=3,
            )
            return r.returncode == 0
        except Exception:
            return False

    def poll(self, hostname_or_ip: str) -> str:
        """Returns 'home', 'away', or 'unchanged'."""
        if self._phone_reachable(hostname_or_ip):
            self._hits += 1
            self._misses = 0
            if self._hits >= PRESENCE_HITS_TO_HOME and self.is_home is not True:
                self.is_home = True
                return "home"
        else:
            self._misses += 1
            self._hits = 0
            if self._misses >= PRESENCE_MISSES_TO_AWAY and self.is_home is not False:
                self.is_home = False
                return "away"
        return "unchanged"


# -- Real-Time Watcher --------------------------------------------
class RealtimeWatcher:
    def __init__(self, engine: FaceEngine, pi: PiConnection):
        self.engine = engine
        self.pi = pi
        self.processed = set()
        self.last_audio = defaultdict(float)
        self._state_file = LOG_DIR / "processed_files.json"
        self._audio_queue = []
        self._load_state()

        # Per-(category, identity) Pushover cooldown
        self.last_notify = {}   # dict[(category, identity)] -> last_send_ts

        # Pushover config (live-reloaded by mtime)
        self.pushover_cfg = dict(pushover_notify.DEFAULTS) if PUSHOVER_AVAILABLE else {}
        self._pushover_mtime = None
        self._reload_pushover_if_changed()

        # Pi-alive watchdog
        self.last_pi_contact = time.time()
        self._pi_dead_alerted = False

        # Snapshot pruning
        self._last_prune = 0.0

    # -- state ------------------------------------------------------
    def _load_state(self):
        if self._state_file.exists():
            try:
                with open(self._state_file, "r") as f:
                    self.processed = set(json.load(f))
            except Exception:
                self.processed = set()

    def _save_state(self):
        try:
            recent = sorted(self.processed)[-20000:]   # was 5000 — file is tiny, raise cap
            tmp = self._state_file.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(recent, f)
            os.replace(tmp, self._state_file)   # atomic on Windows + POSIX
        except Exception as e:
            log.warning(f"Could not save processed state: {e}")

    def _should_play(self, person: str) -> bool:
        now = time.time()
        category = "known" if person.lower() in self.engine.known_people else "unknown"
        if now - self.last_audio[category] >= AUDIO_COOLDOWN:
            self.last_audio[category] = now
            return True
        return False

    # -- pushover ---------------------------------------------------
    def _reload_pushover_if_changed(self):
        if not PUSHOVER_AVAILABLE:
            return
        try:
            mtime = PUSHOVER_CONFIG.stat().st_mtime if PUSHOVER_CONFIG.exists() else 0
        except OSError:
            mtime = 0
        if mtime != self._pushover_mtime:
            self._pushover_mtime = mtime
            self.pushover_cfg = pushover_notify.load_config(PUSHOVER_CONFIG)
            log.info(
                f"Pushover config loaded "
                f"(configured={pushover_notify.is_configured(self.pushover_cfg)}, "
                f"away_mode={self.pushover_cfg.get('away_mode')})"
            )

    def _should_notify(self, category: str, identity: str) -> bool:
        """Per-(category, identity) cooldown. Default 10 min per identity,
        overridable via pushover_config.json 'cooldown_per_identity_minutes'."""
        now = time.time()
        per_identity = float(self.pushover_cfg.get(
            "cooldown_per_identity_minutes", DEFAULT_IDENTITY_COOLDOWN / 60
        )) * 60
        category_cd = float(self.pushover_cfg.get(f"cooldown_{category}", 60))
        cooldown = max(per_identity, category_cd)

        key = (category, identity)
        last = self.last_notify.get(key, 0.0)
        if now - last >= cooldown:
            self.last_notify[key] = now
            return True
        return False

    @staticmethod
    def _snapshot_age_seconds(fname: str) -> float:
        try:
            stamp = fname.replace("motion_", "").rsplit(".", 1)[0]
            taken = time.mktime(time.strptime(stamp, "%Y%m%d_%H%M%S"))
            return max(0.0, time.time() - taken)
        except Exception:
            return 0.0

    def _build_alert(self, category, known_names):
        ts = time.strftime("%I:%M %p").lstrip("0")
        pretty = ", ".join(n.title() for n in known_names)
        if category == "unknown":
            title = "Unknown person at home"
            extra = f" (also recognized: {pretty})" if known_names else ""
            message = f"An UNKNOWN person was detected at {ts}.{extra} Check the photo."
            priority = int(self.pushover_cfg.get("unknown_priority", 2))
        elif category == "known":
            who = pretty if known_names else "Someone"
            title = f"{who} arrived home"
            message = f"{who} was recognized at {ts}."
            priority = 0
        else:
            title = "Motion at home"
            message = f"Motion detected at {ts} -- no face clearly visible."
            priority = 0
        return title, message, priority

    def _notify_for_results(self, results, image_path, fname):
        if not PUSHOVER_AVAILABLE:
            return
        self._reload_pushover_if_changed()
        cfg = self.pushover_cfg
        if not pushover_notify.is_configured(cfg) or not cfg.get("away_mode", True):
            return

        if results:
            names = [r[0] for r in results]
            known_names = sorted({n for n in names if n != "unknown"})
            category = "unknown" if any(n == "unknown" for n in names) else "known"
            identity = "unknown" if category == "unknown" else ",".join(known_names)
        else:
            known_names = []
            category = "motion"
            identity = "motion"

        if category == "known" and not cfg.get("notify_known", True):
            return
        if category == "motion" and not cfg.get("notify_motion_no_face", True):
            return

        max_age = float(cfg.get("notify_max_age", 300))
        if max_age > 0 and self._snapshot_age_seconds(fname) > max_age:
            return

        if not self._should_notify(category, identity):
            return

        title, message, priority = self._build_alert(category, known_names)
        image = str(image_path) if cfg.get("attach_photo", True) else None

        threading.Thread(
            target=self._send_notification,
            args=(dict(cfg), title, message, priority, image, fname),
            daemon=True,
        ).start()

    def _send_notification(self, cfg, title, message, priority, image, fname):
        ok, detail = pushover_notify.send(
            cfg, title, message, priority=priority, image_path=image
        )
        if ok:
            log.info(f"Pushover sent: [{title}] (priority={priority}) for {fname}")
        else:
            log.warning(f"Pushover FAILED for {fname}: {detail}")

    # -- Pi-alive dead-man -----------------------------------------
    def check_pi_alive(self):
        """Called from the main loop. If the Pi has been silent for >15 min,
        fire ONE Pushover priority 1 alert. On recovery, fire a priority 0
        'back online' confirmation."""
        if not PUSHOVER_AVAILABLE:
            return
        cfg = self.pushover_cfg
        if not pushover_notify.is_configured(cfg):
            return
        silent_for = time.time() - self.last_pi_contact
        if silent_for > PI_DEAD_THRESHOLD and not self._pi_dead_alerted:
            self._pi_dead_alerted = True
            mins = int(silent_for / 60)
            log.warning(f"Pi has been silent for {mins} min — firing dead-man Pushover")
            threading.Thread(
                target=self._send_notification,
                args=(dict(cfg), "PiCam offline",
                      f"No SFTP contact for {mins} min. Last seen "
                      f"{time.strftime('%I:%M %p', time.localtime(self.last_pi_contact))}.",
                      1, None, "pi-dead"),
                daemon=True,
            ).start()
        elif silent_for < 60 and self._pi_dead_alerted:
            # recovery
            self._pi_dead_alerted = False
            log.info("Pi back online — firing recovery Pushover")
            threading.Thread(
                target=self._send_notification,
                args=(dict(cfg), "PiCam back online",
                      "SFTP contact restored.", 0, None, "pi-alive"),
                daemon=True,
            ).start()

    # -- Snapshot pruning ------------------------------------------
    def maybe_prune_snapshots(self):
        now = time.time()
        if now - self._last_prune < PRUNE_INTERVAL:
            return
        self._last_prune = now
        try:
            self._prune_local_snapshots()
        except Exception as e:
            log.warning(f"Snapshot prune failed: {e}")

    def _prune_local_snapshots(self):
        """Delete motion_*.jpg older than LOCAL_RETENTION_DAYS AND keep total
        size under LOCAL_RETENTION_MAX_GB. Filename timestamp is authoritative
        — no stat() calls needed for the age check."""
        if not LOCAL_SNAPSHOTS.exists():
            return
        cutoff = time.time() - LOCAL_RETENTION_DAYS * 86400
        max_bytes = int(LOCAL_RETENTION_MAX_GB * 1024 * 1024 * 1024)

        # Pass 1: age-based delete
        files = list(LOCAL_SNAPSHOTS.glob("motion_*.jpg"))
        aged_out = 0
        for p in files:
            try:
                stamp = p.stem.replace("motion_", "")
                taken = time.mktime(time.strptime(stamp, "%Y%m%d_%H%M%S"))
                if taken < cutoff:
                    p.unlink(missing_ok=True)
                    aged_out += 1
            except Exception:
                continue

        # Pass 2: size cap (oldest-first)
        survivors = []
        for p in LOCAL_SNAPSHOTS.glob("motion_*.jpg"):
            try:
                stamp = p.stem.replace("motion_", "")
                taken = time.mktime(time.strptime(stamp, "%Y%m%d_%H%M%S"))
            except Exception:
                taken = 0
            try:
                survivors.append((taken, p.stat().st_size, p))
            except OSError:
                pass
        survivors.sort(key=lambda x: x[0])   # oldest first
        total = sum(s[1] for s in survivors)
        size_evicted = 0
        for taken, sz, p in survivors:
            if total <= max_bytes:
                break
            try:
                p.unlink(missing_ok=True)
                total -= sz
                size_evicted += 1
            except Exception:
                pass

        if aged_out or size_evicted:
            log.info(f"Snapshot prune: {aged_out} aged out, {size_evicted} evicted for size, "
                     f"total now {total / (1024**3):.2f} GB")

    # -- poll cycle -------------------------------------------------
    def poll_and_process(self):
        try:
            files = self.pi.list_files(getattr(self, "_pi_snapshots_path", "/home/pi/picam-recordings/snapshots"), extensions=(".jpg", ".jpeg"))
            # successful listdir == Pi is alive
            self.last_pi_contact = time.time()
        except Exception:
            raise

        new_files = [f for f in files if f.filename not in self.processed]
        if not new_files:
            return

        log.info(f"Found {len(new_files)} new snapshot(s) on Pi")

        for file_attr in new_files:
            fname = file_attr.filename
            if file_attr.st_size == 0:
                continue
            remote_path = f"{getattr(self, '_pi_snapshots_path', '/home/pi/picam-recordings/snapshots')}/{fname}"
            local_path = LOCAL_SNAPSHOTS / fname

            try:
                self.pi.download(remote_path, local_path)
            except FileNotFoundError:
                log.warning(f"Skipped {fname} — already deleted from Pi (race)")
                self.processed.add(fname)
                continue
            except Exception as e:
                log.warning(f"Download failed for {fname}: {e}")
                continue

            try:
                self.processed.add(fname)
                results = self.engine.recognize(str(local_path))

                if results:
                    for name, score, dist, bbox in results:
                        log.info(f"FACE: {name.upper()} (dist={dist:.3f}, threshold={FACE_TOLERANCE}) in {fname}")
                        if self._should_play(name):
                            self._audio_queue.append(name)
                else:
                    log.info(f"No faces in {fname}")

                try:
                    self._notify_for_results(results, local_path, fname)
                except Exception as e:
                    log.warning(f"Notify error for {fname}: {e}")

                try:
                    self.pi.delete(remote_path)
                except Exception as e:
                    log.warning(f"Could not delete {fname} from Pi: {e}")

            except Exception as e:
                log.warning(f"Error processing {fname}: {e}")
                log.warning(traceback.format_exc())

    def play_queued_audio(self):
        if not self._audio_queue:
            return
        person = self._audio_queue.pop(0)
        self._audio_queue.clear()
        play_audio(person, self.engine.known_people)


# -- Presence watcher thread ---------------------------------------
def presence_thread(watcher: RealtimeWatcher, presence: PresenceDetector, stop_evt: threading.Event):
    """Background thread that polls Tailscale/ping for the configured 'home'
    device. When state flips, writes away_mode to pushover_config.json —
    face_watcher's existing mtime-reload picks it up on the next poll."""
    while not stop_evt.is_set():
        try:
            cfg = watcher.pushover_cfg
            if cfg.get("presence_override"):
                stop_evt.wait(PRESENCE_POLL_INTERVAL)
                continue
            host = cfg.get("presence_host") or ""
            if not host:
                stop_evt.wait(PRESENCE_POLL_INTERVAL)
                continue
            state = presence.poll(host)
            if state in ("home", "away"):
                new_away = (state == "away")
                # Read-modify-write the config so we don't clobber other keys
                try:
                    with open(PUSHOVER_CONFIG, "r", encoding="utf-8") as f:
                        on_disk = json.load(f)
                    if on_disk.get("away_mode") != new_away:
                        on_disk["away_mode"] = new_away
                        with open(PUSHOVER_CONFIG, "w", encoding="utf-8") as f:
                            json.dump(on_disk, f, indent=2)
                        log.info(f"Presence: {state.upper()} — away_mode={new_away}")
                except Exception as e:
                    log.warning(f"Presence write failed: {e}")
        except Exception as e:
            log.warning(f"Presence thread error: {e}")
        stop_evt.wait(PRESENCE_POLL_INTERVAL)


# -- Main ----------------------------------------------------------
def main():
    # 1. Singleton FIRST — refuse to do anything else if another instance owns the port
    if not _acquire_singleton():
        sys.exit(0)

    _kill_old_instance()
    _write_pid()

    print("""
    ================================================
      PiCam Face Watcher v4.0
      Singleton-locked | Hot-reload faces | Per-identity cooldown
      Tailscale presence | Pi-alive dead-man | Snapshot pruning
      Press Ctrl+C or close window to stop
    ================================================
    """)

    pygame.mixer.init()
    log.info("Pygame mixer initialized")

    engine = FaceEngine()
    if not engine.known_encodings:
        log.warning("NO KNOWN FACES — all detections will be 'unknown'")
        log.warning(f"Add photos to: {FACES_DIR}")

    pi = PiConnection()
    watcher = RealtimeWatcher(engine, pi)
    watcher._pi_snapshots_path = "/home/pi/picam-recordings/snapshots"

    # Presence thread (only starts polling if presence_host is set in config)
    presence = PresenceDetector()
    stop_evt = threading.Event()
    presence_t = threading.Thread(
        target=presence_thread, args=(watcher, presence, stop_evt), daemon=True
    )
    presence_t.start()

    def cleanup():
        log.info("Cleaning up...")
        stop_evt.set()
        try:
            watcher._save_state()
        except Exception:
            pass
        try:
            pi.close()
        except Exception:
            pass
        try:
            pygame.mixer.quit()
        except Exception:
            pass
        _cleanup_pid()
        try:
            if _SINGLETON_SOCK:
                _SINGLETON_SOCK.close()
        except Exception:
            pass

    atexit.register(cleanup)

    log.info(f"Face tolerance: {FACE_TOLERANCE}; Polling Pi at {pi.host} every {POLL_INTERVAL}s")
    if PUSHOVER_AVAILABLE and pushover_notify.is_configured(watcher.pushover_cfg):
        log.info(
            f"Pushover: ARMED (away_mode={watcher.pushover_cfg.get('away_mode')}, "
            f"stranger priority={watcher.pushover_cfg.get('unknown_priority')})"
        )
    else:
        log.warning("Pushover: NOT configured — no text alerts will be sent.")
    log.info("Ready.")

    connected = False
    last_heartbeat = time.time()
    last_state_save = time.time()
    last_faces_check = 0.0

    try:
        while True:
            if not connected or not pi.is_alive():
                if connected:
                    log.warning("Pi connection lost, reconnecting...")
                else:
                    log.info(f"Connecting to Pi at {pi.host}...")
                connected = pi.connect()
                if not connected:
                    watcher.check_pi_alive()   # may fire dead-man alert
                    time.sleep(RECONNECT_DELAY)
                    continue

            try:
                watcher.poll_and_process()
            except Exception as e:
                log.warning(f"Poll error (will reconnect): {e}")
                connected = False
                continue

            try:
                watcher.play_queued_audio()
            except Exception as e:
                log.warning(f"Audio error: {e}")

            now = time.time()

            # Hot-reload faces/ every 30s — cheap signature check
            if now - last_faces_check >= 30:
                last_faces_check = now
                try:
                    engine.reload_if_changed()
                except Exception as e:
                    log.warning(f"Faces reload error: {e}")

            # Snapshot prune
            try:
                watcher.maybe_prune_snapshots()
            except Exception:
                pass

            # Pi-alive watchdog
            try:
                watcher.check_pi_alive()
            except Exception:
                pass

            if now - last_state_save >= 30:
                last_state_save = now
                if len(watcher.processed) > 20000:
                    watcher.processed = set(sorted(watcher.processed)[-20000:])
                watcher._save_state()

            if now - last_heartbeat >= 60:
                last_heartbeat = now
                log.info(f"Heartbeat: watching ({len(watcher.processed)} processed). "
                         f"away_mode={watcher.pushover_cfg.get('away_mode')}; "
                         f"last_pi_contact={int(now - watcher.last_pi_contact)}s ago")

            time.sleep(POLL_INTERVAL)

    except (KeyboardInterrupt, SystemExit):
        log.info("Stopped.")


if __name__ == "__main__":
    main()
