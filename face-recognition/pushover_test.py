"""
Quick Pushover verification for PiCam.

Run this AFTER pasting your app token into pushover_config.json to confirm
a text actually reaches your phone. It only sends -- it does NOT touch the
running face_watcher (no PID kill), so it's safe to run anytime.

    C:\\Python313\\python.exe pushover_test.py
"""

import sys
from pathlib import Path

import pushover_notify

CONFIG = Path(__file__).parent / "pushover_config.json"
SNAP_DIR = Path(r"C:\Users\Tarik\Desktop\_Projects\pi-cam-motion-clips\snapshots")


def main():
    cfg = pushover_notify.load_config(CONFIG)

    token_set = cfg.get("app_token") not in ("", pushover_notify.PLACEHOLDER_TOKEN)
    print(f"Config file:   {CONFIG}")
    print(f"  user_key set:  {bool(cfg.get('user_key'))}")
    print(f"  app_token set: {token_set}")
    print(f"  away_mode:     {cfg.get('away_mode')}")

    if not pushover_notify.is_configured(cfg):
        print("\n[X] Not configured yet.")
        print("    1. Go to https://pushover.net/apps/build and create an app.")
        print("    2. Copy its API Token/Key.")
        print("    3. Paste it as \"app_token\" in pushover_config.json.")
        sys.exit(1)

    # Attach the most recent real snapshot if one exists, so you also confirm
    # that photo attachments come through.
    image = None
    if SNAP_DIR.exists():
        snaps = sorted(SNAP_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
        if snaps:
            image = str(snaps[0])
            print(f"  attaching latest snapshot: {snaps[0].name}")

    print("\nSending test notification (normal priority)...")
    ok, detail = pushover_notify.send(
        cfg,
        title="PiCam test alert",
        message="If you can read this on your phone, PiCam notifications are working.",
        priority=0,
        image_path=image,
    )
    if ok:
        print("[OK] Sent. Check your phone.")
    else:
        print(f"[X] Failed: {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
