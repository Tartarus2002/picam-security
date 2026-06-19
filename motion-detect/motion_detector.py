#!/usr/bin/env python3
"""
PiCam Motion Detector v4.1
==========================
Direct camera motion detection for Raspberry Pi Zero 2 W using picamera2.
Also streams live H264 via its own mediamtx instance for phone viewing.

Changes from v4.0:
  - Single-shot autofocus (continuous AF caused constant false positives)
  - Camera warmup period to skip auto-exposure/AWB stabilization
  - RTSP streaming: picamera2 H264 encoder -> ffmpeg -> mediamtx

Usage:
    python3 motion_detector.py
    python3 motion_detector.py --config /home/pi/picam/config/picam.conf
"""

import argparse
import datetime
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FileOutput


# Minimal mediamtx config (same ports as picam-stream, no rpicam-vid).
# webrtcICEHostNAT1To1IPs lists the host's reachable IPs so WebRTC ICE can
# advertise them — required when the Pi is behind NAT and accessed via a
# Tailscale 100.x address. Set PICAM_ICE_HOST_IPS in the env to override
# (comma-separated). If unset, mediamtx auto-detects local interfaces.
_ICE_IPS = os.environ.get("PICAM_ICE_HOST_IPS", "").strip()
_ice_line = f"webrtcICEHostNAT1To1IPs: [{_ICE_IPS}]\n" if _ICE_IPS else ""
MEDIAMTX_CONFIG = f"""\
logLevel: warn
logDestinations: [stdout]
api: no
rtsp: yes
rtspAddress: :8554
rtspTransports: [tcp, udp]
webrtc: yes
webrtcAddress: :8889
{_ice_line}hls: yes
hlsAddress: :8888
paths:
  cam: {{}}
"""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "storage_dir": "/home/pi/picam-recordings",
    "log_dir": "/home/pi/picam-logs",
    "sensitivity": 25,
    "min_area": 5000,
    "cooldown": 5,
    "snapshot_on_motion": True,
    "max_storage_gb": 8,
    "camera_width": 1280,
    "camera_height": 720,
}


def load_config(config_path=None):
    """Load configuration from picam.conf shell-style config file."""
    config = DEFAULT_CONFIG.copy()
    if config_path and os.path.exists(config_path):
        with open(config_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if '#' in value:
                    value = value[:value.index('#')].strip()

                mapping = {
                    "MOTION_SENSITIVITY": ("sensitivity", int),
                    "MOTION_MIN_AREA": ("min_area", int),
                    "MOTION_COOLDOWN": ("cooldown", int),
                    "STORAGE_DIR": ("storage_dir", str),
                    "LOG_DIR": ("log_dir", str),
                    "SNAPSHOT_ON_MOTION": ("snapshot_on_motion", lambda v: v.lower() == "true"),
                    "MAX_STORAGE_GB": ("max_storage_gb", int),
                    "CAMERA_WIDTH": ("camera_width", int),
                    "CAMERA_HEIGHT": ("camera_height", int),
                }
                if key in mapping:
                    conf_key, conv = mapping[key]
                    config[conf_key] = conv(value)

    return config


# ---------------------------------------------------------------------------
# Storage Manager
# ---------------------------------------------------------------------------
class StorageManager:
    def __init__(self, storage_dir, max_gb):
        self.storage_dir = Path(storage_dir)
        self.max_bytes = max_gb * 1024 * 1024 * 1024
        self.snapshots_dir = self.storage_dir / "snapshots"
        self.events_dir = self.storage_dir / "events"
        for d in [self.snapshots_dir, self.events_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def get_storage_used(self):
        total = 0
        for f in self.storage_dir.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
        return total

    def cleanup_old_files(self):
        used = self.get_storage_used()
        if used <= self.max_bytes:
            return
        files = []
        for f in self.storage_dir.rglob("*"):
            if f.is_file() and f.suffix in (".jpg", ".json"):
                files.append((f.stat().st_mtime, f))
        files.sort()
        for mtime, filepath in files:
            if used <= self.max_bytes * 0.8:
                break
            size = filepath.stat().st_size
            filepath.unlink()
            used -= size
            logging.info(f"Cleaned up: {filepath.name} ({size // 1024}KB)")

    def get_timestamp_str(self):
        return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    def save_snapshot(self, frame, suffix=""):
        # Re-create directory if it was deleted (e.g., by external cleanup)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        ts = self.get_timestamp_str()
        # suffix like "_1" lets a burst share a base timestamp while keeping
        # the legacy filename pattern parseable by face_watcher.
        path = self.snapshots_dir / f"motion_{ts}{suffix}.jpg"
        ok = cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            logging.info(f"Snapshot saved: {path.name}")
        else:
            logging.warning(f"Snapshot write FAILED: {path}")
        return path

    def save_event(self, event_data):
        ts = self.get_timestamp_str()
        path = self.events_dir / f"event_{ts}.json"
        with open(path, "w") as f:
            json.dump(event_data, f, indent=2, default=str)
        return path


# ---------------------------------------------------------------------------
# RTSP Streamer (picamera2 H264 -> ffmpeg -> mediamtx)
# ---------------------------------------------------------------------------
class RTSPStreamer:
    """Manages mediamtx + ffmpeg for RTSP streaming from picamera2."""

    def __init__(self):
        self.mediamtx_proc = None
        self.ffmpeg_proc = None
        self.encoder = None
        self.output = None
        self.config_path = "/tmp/mediamtx_motion.yml"

    def start(self, picam):
        """Start RTSP streaming pipeline."""
        try:
            # Write minimal mediamtx config
            with open(self.config_path, "w") as f:
                f.write(MEDIAMTX_CONFIG)

            # Start mediamtx
            self.mediamtx_proc = subprocess.Popen(
                ["/home/pi/picam/bin/mediamtx", self.config_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(2)  # Let mediamtx bind ports

            # Start ffmpeg to push H264 to mediamtx
            self.ffmpeg_proc = subprocess.Popen(
                [
                    "ffmpeg", "-nostdin",
                    "-f", "h264",
                    "-i", "pipe:0",
                    "-c:v", "copy",
                    "-f", "rtsp", "-rtsp_transport", "tcp",
                    "rtsp://127.0.0.1:8554/cam",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Start H264 hardware encoding from picamera2
            self.encoder = H264Encoder(bitrate=1500000)
            self.output = FileOutput(self.ffmpeg_proc.stdin)
            picam.start_recording(self.encoder, self.output)

            logging.info("RTSP streaming active (picamera2 -> mediamtx :8554)")
            return True

        except Exception as e:
            logging.warning(f"RTSP streaming failed to start: {e}")
            self.stop(picam)
            return False

    def stop(self, picam):
        """Stop RTSP streaming pipeline."""
        try:
            picam.stop_recording()
        except Exception:
            pass

        for proc, name in [(self.ffmpeg_proc, "ffmpeg"), (self.mediamtx_proc, "mediamtx")]:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        self.ffmpeg_proc = None
        self.mediamtx_proc = None
        self.encoder = None
        self.output = None


# ---------------------------------------------------------------------------
# Motion Detector (picamera2 direct capture)
# ---------------------------------------------------------------------------
class MotionDetector:
    def __init__(self, config):
        self.config = config
        self.sensitivity = config["sensitivity"]
        self.min_area = config["min_area"]
        self.cooldown = config["cooldown"]
        self.running = Event()
        self.running.set()
        self.storage = StorageManager(config["storage_dir"], config["max_storage_gb"])
        self.last_motion_time = 0
        self.picam = None
        self.streamer = RTSPStreamer()

    def _free_camera(self):
        """Kill rpicam-vid to free camera for picamera2."""
        for proc_name in ["rpicam-vid", "rpicam-still"]:
            result = subprocess.run(
                ["pkill", "-f", proc_name],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                logging.info(f"Stopped {proc_name} to free camera")
        time.sleep(1)

    def start_camera(self):
        """Initialize picamera2 for direct frame capture + RTSP streaming."""
        self._free_camera()

        self.picam = Picamera2()

        # Use video configuration for simultaneous capture + H264 encoding
        cam_config = self.picam.create_video_configuration(
            main={
                "size": (self.config["camera_width"], self.config["camera_height"]),
            },
        )
        self.picam.configure(cam_config)
        self.picam.start()
        time.sleep(2)  # Camera sensor warm-up

        # Log the actual pixel format for debugging
        main_cfg = self.picam.camera_configuration()["main"]
        logging.info(f"Camera format: {main_cfg.get('format', 'unknown')}")

        # Single-shot autofocus (Arducam IMX519)
        # Continuous AF caused massive false positives (max_pixel_diff=230+)
        try:
            self.picam.set_controls({"AfMode": 1})  # Auto (single-shot)
            time.sleep(0.5)
            self.picam.set_controls({"AfTrigger": 0})  # Trigger AF
            logging.info("Autofocus: single-shot triggered, waiting to settle...")
            time.sleep(3)  # Wait for AF to complete
            logging.info("Autofocus: complete, lens locked")
        except Exception as e:
            logging.warning(f"Autofocus setup: {e}")

        # Start RTSP streaming (non-critical, motion detection works without it)
        self.streamer.start(self.picam)

        logging.info(
            f"Camera started: {self.config['camera_width']}x{self.config['camera_height']} "
            f"(picamera2 direct capture)"
        )

    def stop_camera(self):
        if self.picam:
            self.streamer.stop(self.picam)
            try:
                self.picam.stop()
                self.picam.close()
            except Exception:
                pass
            self.picam = None
            logging.info("Camera stopped")

    def _to_bgr(self, frame):
        """Convert capture_array output to BGR for OpenCV.

        picamera2's default for create_video_configuration is XBGR8888.
        The format NAME describes the 32-bit word from MSB to LSB
        (X|B|G|R), but on little-endian ARM the bytes in memory are
        actually [R, G, B, X]. numpy channel index 0 = R, 1 = G, 2 = B,
        3 = X (pad). cv2 expects [B, G, R], so we reorder + drop X.

        Pre-fix code did frame[:, :, :3] which kept [R, G, B] and cv2
        interpreted it as BGR -> R/B swapped -> blue skin. Fixed 2026-06-15.
        """
        if frame.ndim == 2:
            return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        channels = frame.shape[2]
        if channels == 4:
            # [R, G, B, X] -> [B, G, R] = numpy fancy-index [2, 1, 0]
            return frame[:, :, [2, 1, 0]].copy()
        # 3-channel: picamera2 returns RGB-as-named (bytes [R, G, B] on LE).
        return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

    def detect_motion(self, prev_gray, curr_gray):
        frame_diff = cv2.absdiff(prev_gray, curr_gray)
        threshold = max(5, 50 - self.sensitivity)
        _, thresh = cv2.threshold(frame_diff, threshold, 255, cv2.THRESH_BINARY)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        thresh = cv2.dilate(thresh, kernel, iterations=2)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        significant = [c for c in contours if cv2.contourArea(c) >= self.min_area]
        diff_score = sum(cv2.contourArea(c) for c in significant)
        return len(significant) > 0, significant, diff_score

    def handle_motion(self, frame, contours, diff_score):
        now = time.time()
        if now - self.last_motion_time < self.cooldown:
            return
        self.last_motion_time = now

        logging.info(f"MOTION DETECTED | contours={len(contours)} score={diff_score:.0f}")

        event = {
            "timestamp": datetime.datetime.now().isoformat(),
            "contours": len(contours),
            "diff_score": diff_score,
            "action": [],
        }

        if self.config["snapshot_on_motion"]:
            # Burst capture: 3 frames at ~0 / 300ms / 600ms after motion.
            # Doorway scenes: subject is in frame ~1s, multiple angles let
            # face_recognition find at least one clear pose.
            snap_path = self.storage.save_snapshot(frame)
            event["action"].append(f"snapshot:{snap_path.name}")
            for i in (1, 2):
                time.sleep(0.3)
                try:
                    extra_raw = self.picam.capture_array()
                    extra = self._to_bgr(extra_raw)
                    extra_path = self.storage.save_snapshot(extra, suffix=f"_{i}")
                    event["action"].append(f"snapshot:{extra_path.name}")
                except Exception as e:
                    logging.warning(f"Burst frame {i} failed: {e}")

        self.storage.save_event(event)
        self.storage.cleanup_old_files()

    def run(self):
        self.start_camera()

        # Warmup: skip first 10 frames (5 seconds) to let AE/AWB stabilize
        logging.info("Camera warmup (5 seconds)...")
        for _ in range(10):
            self.picam.capture_array()
            time.sleep(0.5)
        logging.info("Warmup complete, starting motion detection")

        # First real frame
        frame_raw = self.picam.capture_array()
        frame = self._to_bgr(frame_raw)
        prev_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        prev_gray = cv2.GaussianBlur(prev_gray, (21, 21), 0)

        processed_count = 0
        max_diff_seen = 0
        max_pixel_seen = 0
        last_status_log = time.time()
        compare_interval = 0.5

        logging.info("Motion detection running (picamera2 direct). Ctrl+C to stop.")

        while self.running.is_set():
            time.sleep(compare_interval)

            try:
                frame_raw = self.picam.capture_array()
                frame = self._to_bgr(frame_raw)
            except Exception as e:
                logging.warning(f"Frame capture failed: {e}")
                continue

            processed_count += 1

            curr_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            curr_gray = cv2.GaussianBlur(curr_gray, (21, 21), 0)

            motion, contours, score = self.detect_motion(prev_gray, curr_gray)

            if score > max_diff_seen:
                max_diff_seen = score
            raw_diff = cv2.absdiff(prev_gray, curr_gray)
            raw_max = int(raw_diff.max())
            if raw_max > max_pixel_seen:
                max_pixel_seen = raw_max

            if motion:
                self.handle_motion(frame, contours, score)

            prev_gray = curr_gray

            now = time.time()
            if now - last_status_log >= 30:
                logging.info(
                    f"STATUS | processed={processed_count} "
                    f"max_diff_score={max_diff_seen:.0f} max_pixel_diff={max_pixel_seen} "
                    f"(threshold={max(5, 50 - self.sensitivity)} min_area={self.min_area})"
                )
                last_status_log = now
                max_diff_seen = 0
                max_pixel_seen = 0

        self.stop_camera()
        logging.info("Motion detector stopped.")

    def stop(self):
        self.running.clear()


# ---------------------------------------------------------------------------
# Main

def _brownout_watcher():
    """Log whenever vcgencmd reports under-voltage / throttling.
    Pi Zero 2 W on a PC USB port frequently brownouts because PC ports
    deliver only 500 mA. This surfaces silent failures as WARNING lines."""
    import subprocess, time as _t
    last = 0x0
    while True:
        try:
            out = subprocess.run(
                ["vcgencmd", "get_throttled"],
                capture_output=True, text=True, timeout=3,
            ).stdout
            val = int(out.strip().split("=")[-1], 16)
            if val != last:
                bits = []
                if val & 0x1:     bits.append("UNDERVOLTAGE_NOW")
                if val & 0x2:     bits.append("FREQ_CAPPED_NOW")
                if val & 0x4:     bits.append("THROTTLED_NOW")
                if val & 0x8:     bits.append("SOFT_TEMP_LIMIT_NOW")
                if val & 0x10000: bits.append("undervoltage_since_boot")
                if val & 0x20000: bits.append("freq_capped_since_boot")
                if val & 0x40000: bits.append("throttled_since_boot")
                if val & 0x80000: bits.append("soft_temp_since_boot")
                logging.warning(f"Throttle state changed: 0x{val:x} ({', '.join(bits) or 'clear'})")
                last = val
        except Exception:
            pass
        _t.sleep(30)


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="PiCam Motion Detector")
    parser.add_argument(
        "--config",
        default="/home/pi/picam/config/picam.conf",
        help="Path to picam.conf",
    )
    args = parser.parse_args()
    config = load_config(args.config)

    log_dir = Path(config["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "motion.log"),
        ],
    )
    import threading as _th
    _th.Thread(target=_brownout_watcher, daemon=True).start()

    detector = MotionDetector(config)

    def shutdown(signum, frame):
        logging.info("Shutdown signal received...")
        detector.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        detector.run()
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
