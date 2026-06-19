"""Test face detection on actual Pi camera snapshots to see if faces are detectable."""
import cv2
from pathlib import Path

MODELS_DIR = Path(__file__).parent / "models"
SNAPSHOTS = Path(r"C:\Users\Tarik\Desktop\_Projects\pi-cam-motion-clips\snapshots")

detector = cv2.FaceDetectorYN.create(
    str(MODELS_DIR / "face_detection_yunet_2023mar.onnx"), "", (320, 320), 0.5, 0.3, 5000
)

# Test the 10 newest snapshots
snaps = sorted(SNAPSHOTS.glob("*.jpg"), key=lambda f: f.stat().st_mtime, reverse=True)[:10]
print(f"Testing {len(snaps)} newest snapshots for face detection:\n")

for snap in snaps:
    img = cv2.imread(str(snap))
    if img is None:
        print(f"  SKIP: {snap.name} (unreadable)")
        continue
    h, w = img.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)
    count = len(faces) if faces is not None else 0
    if count > 0:
        for i, face in enumerate(faces):
            conf = face[14]
            fw, fh = int(face[2]), int(face[3])
            print(f"  FACE: {snap.name} ({w}x{h}) -> face {fw}x{fh}px, conf={conf:.2f}")
    else:
        print(f"  NONE: {snap.name} ({w}x{h}) -> no faces detected")
