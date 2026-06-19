"""
One-off: recover colors in snapshots saved with the pre-2026-06-15 bug.

Old motion_detector.py did `frame[:, :, :3]` on XBGR8888 frames, keeping
[R, G, B] (because XBGR8888 is little-endian: bytes are R, G, B, X in
memory). cv2.imwrite then interpreted those as BGR, so saved JPEGs had
R and B channels swapped — skin tones look blue.

This script reverses that on existing JPEGs:
  bad_jpeg (read as BGR by cv2) actually contains [R-orig, G-orig, B-orig]
  swap channels 0 and 2 -> [B-orig, G-orig, R-orig] = correct BGR
  re-save

Run:
  python fix_blue_snapshots.py               # dry-run, prints what it'd do
  python fix_blue_snapshots.py --apply       # actually rewrite the JPEGs

Only affects snapshots whose mtime is BEFORE the patch (2026-06-15 22:09)
so new correct ones aren't double-flipped. Override with --all.
"""

import argparse, sys, time
from pathlib import Path

PATCH_TS = time.mktime(time.strptime("2026-06-15 22:09:00", "%Y-%m-%d %H:%M:%S"))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually rewrite files")
    parser.add_argument("--all", action="store_true", help="also process post-patch files")
    parser.add_argument("--dir", default=r"C:\Users\Tarik\Desktop\_Projects\pi-cam-motion-clips\snapshots")
    args = parser.parse_args()

    import cv2

    root = Path(args.dir)
    files = list(root.glob("motion_*.jpg"))
    if args.all:
        targets = files
    else:
        targets = [f for f in files if f.stat().st_mtime < PATCH_TS]

    print(f"Found {len(files)} snapshots; {len(targets)} pre-patch (cutoff {time.ctime(PATCH_TS)})")
    if not args.apply:
        print("Dry run — no files modified. Re-run with --apply to actually fix.")
        return

    fixed = 0
    failed = 0
    for i, f in enumerate(targets):
        try:
            img = cv2.imread(str(f))
            if img is None or img.shape[2] != 3:
                failed += 1
                continue
            corrected = img[:, :, ::-1].copy()   # swap channels 0 and 2
            cv2.imwrite(str(f), corrected, [cv2.IMWRITE_JPEG_QUALITY, 85])
            fixed += 1
            if i % 500 == 0:
                print(f"  ... {i}/{len(targets)}")
        except Exception as e:
            print(f"  FAIL {f.name}: {e}")
            failed += 1
    print(f"Done. Fixed {fixed}, failed {failed}.")

if __name__ == "__main__":
    main()
