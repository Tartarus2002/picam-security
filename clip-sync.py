"""
PiCam Clip Sync — pulls motion clips from Pi to Windows PC.
Runs as a scheduled task every 2 minutes.

Flow:
  1. Connect to Pi via SFTP (creds + key from picam_secrets.py)
  2. Skip files modified within last 10s (still being written)
  3. Download to .part, then atomic os.replace to final name
  4. Delete from Pi only AFTER local rename succeeded
  5. Promote Pi-unreachable logs to WARNING after 3 consecutive failures
"""

import os
import socket
import sys
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# -- Path bootstrap so picam_secrets.py imports cleanly --------------
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import paramiko
import picam_secrets

# -- Configuration --------------------------------------------------
PI_CLIPS_DIR = "/home/pi/picam-recordings/clips"
PI_SNAPSHOTS_DIR = "/home/pi/picam-recordings/snapshots"

LOCAL_CLIPS_DIR = Path(r"C:\Users\Tarik\Desktop\_Projects\pi-cam-motion-clips")
LOCAL_SNAPSHOTS_DIR = LOCAL_CLIPS_DIR / "snapshots"

SYNC_SNAPSHOTS = False
DELETE_AFTER_SYNC = True
MTIME_QUARANTINE_SECONDS = 10   # skip files modified more recently than this
SINGLETON_PORT = 51732
UNREACHABLE_STATE_FILE = LOCAL_CLIPS_DIR / ".unreachable_count"
UNREACHABLE_WARN_THRESHOLD = 3

LOG_FILE = LOCAL_CLIPS_DIR / "sync.log"

# -- Logging setup --------------------------------------------------
LOCAL_CLIPS_DIR.mkdir(parents=True, exist_ok=True)
LOCAL_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("clip-sync")
for _noisy in ["paramiko", "paramiko.transport", "paramiko.auth", "paramiko.sftp"]:
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def _acquire_singleton():
    """Bind 127.0.0.1:SINGLETON_PORT — second clip-sync exits cleanly.
    OS releases the port on crash/exit, no stale lock file."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLETON_PORT))
        s.listen(1)
        return s
    except OSError:
        log.warning(f"Another clip-sync is already running (port {SINGLETON_PORT} taken). Exiting.")
        return None


def _read_unreachable_count() -> int:
    try:
        return int(UNREACHABLE_STATE_FILE.read_text().strip())
    except Exception:
        return 0


def _write_unreachable_count(n: int):
    try:
        if n <= 0:
            if UNREACHABLE_STATE_FILE.exists():
                UNREACHABLE_STATE_FILE.unlink()
        else:
            UNREACHABLE_STATE_FILE.write_text(str(n))
    except Exception:
        pass


def sync_directory(sftp, remote_dir, local_dir, delete_remote=True):
    try:
        remote_files = sftp.listdir_attr(remote_dir)
    except FileNotFoundError:
        log.warning(f"Remote directory not found: {remote_dir}")
        return 0, 0

    synced = 0
    deleted = 0
    now = time.time()

    for file_attr in remote_files:
        fname = file_attr.filename

        if not fname.endswith((".mp4", ".jpg", ".json")):
            continue

        remote_path = f"{remote_dir}/{fname}"
        local_path = local_dir / fname

        # Skip if size 0 (still creating) OR mtime too recent (still writing).
        # mtime guard was documented in v1 but never implemented; this is the fix.
        if file_attr.st_size == 0:
            continue
        if file_attr.st_mtime and (now - file_attr.st_mtime) < MTIME_QUARANTINE_SECONDS:
            log.debug(f"Skipping {fname} (mtime too recent, still writing)")
            continue

        # Already-synced shortcut (size match only — fine, we verify post-rename anyway).
        if local_path.exists() and local_path.stat().st_size == file_attr.st_size:
            if delete_remote:
                try:
                    sftp.remove(remote_path)
                    deleted += 1
                except Exception as e:
                    log.warning(f"Failed to delete {fname} from Pi: {e}")
            continue

        # Atomic download: get to .part, os.replace to final name.
        # If get() crashes, the .part is orphaned (cleaned on next pass) and
        # the remote copy is NOT deleted, so retry is safe.
        part_path = local_path.with_suffix(local_path.suffix + ".part")
        try:
            sftp.get(remote_path, str(part_path))
            # Verify size matches BEFORE rename
            if part_path.stat().st_size != file_attr.st_size:
                log.warning(f"Size mismatch for {fname} — discarding partial")
                part_path.unlink(missing_ok=True)
                continue
            os.replace(part_path, local_path)
            synced += 1
            log.info(f"Downloaded: {fname} ({file_attr.st_size / 1024:.0f} KB)")

            if delete_remote:
                try:
                    sftp.remove(remote_path)
                    deleted += 1
                except Exception as e:
                    log.warning(f"Downloaded but failed to delete {fname}: {e}")

        except Exception as e:
            log.error(f"Failed to download {fname}: {e}")
            try:
                part_path.unlink(missing_ok=True)
            except Exception:
                pass

    # Pass 2: clean up any orphaned .part files older than 10 min
    cutoff = now - 600
    for orphan in local_dir.glob("*.part"):
        try:
            if orphan.stat().st_mtime < cutoff:
                orphan.unlink()
                log.info(f"Cleaned orphan .part: {orphan.name}")
        except Exception:
            pass

    return synced, deleted


def main():
    singleton = _acquire_singleton()
    if singleton is None:
        return

    try:
        creds = picam_secrets.load_pi_creds()
        # clip-sync uses a longer timeout than the watcher (it's tolerant of brief network blips)
        creds["timeout"] = 10
    except Exception as e:
        log.error(f"Could not load Pi credentials: {e}")
        return

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(**creds)
    except Exception as e:
        # Track consecutive failures; surface at WARNING after 3 (visible in default INFO logs).
        n = _read_unreachable_count() + 1
        _write_unreachable_count(n)
        if n >= UNREACHABLE_WARN_THRESHOLD:
            log.warning(f"Pi unreachable for {n} consecutive runs: {e}")
        else:
            log.info(f"Pi unreachable (attempt {n}): {e}")
        return

    # On successful connect, reset the failure counter
    _write_unreachable_count(0)

    try:
        sftp = ssh.open_sftp()

        clips_synced, _ = sync_directory(sftp, PI_CLIPS_DIR, LOCAL_CLIPS_DIR, DELETE_AFTER_SYNC)
        snaps_synced = 0
        if SYNC_SNAPSHOTS:
            snaps_synced, _ = sync_directory(sftp, PI_SNAPSHOTS_DIR, LOCAL_SNAPSHOTS_DIR, DELETE_AFTER_SYNC)

        if clips_synced or snaps_synced:
            log.info(f"Sync complete: {clips_synced} clips, {snaps_synced} snapshots transferred")

        sftp.close()
    except Exception as e:
        log.error(f"Sync error: {e}")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
