"""Quick test to verify face detection and encoding on Tarik's reference photos."""
import cv2
import sys
from pathlib import Path

MODELS_DIR = Path(__file__).parent / "models"
FACES_DIR = Path(__file__).parent / "faces"

DETECTION_MODEL = str(MODELS_DIR / "face_detection_yunet_2023mar.onnx")
RECOGNITION_MODEL = str(MODELS_DIR / "face_recognition_sface_2021dec.onnx")

# Load models
print("Loading models...")
detector = cv2.FaceDetectorYN.create(DETECTION_MODEL, "", (320, 320), 0.7, 0.3, 5000)
recognizer = cv2.FaceRecognizerSF.create(RECOGNITION_MODEL, "")
print("Models loaded!\n")

# Process each person
for person_dir in sorted(FACES_DIR.iterdir()):
    if not person_dir.is_dir():
        continue
    print(f"=== {person_dir.name.upper()} ===")

    embeddings = []
    for img_path in sorted(person_dir.iterdir()):
        if img_path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp"):
            continue

        img = cv2.imread(str(img_path))
        if img is None:
            print(f"  FAIL: Could not read {img_path.name}")
            continue

        h, w = img.shape[:2]
        detector.setInputSize((w, h))
        _, faces = detector.detect(img)

        if faces is None or len(faces) == 0:
            print(f"  WARN: No face in {img_path.name} ({w}x{h})")
            continue

        # Get embedding for first face
        face = faces[0]
        conf = face[14]  # Detection confidence
        aligned = recognizer.alignCrop(img, face)
        embedding = recognizer.feature(aligned)
        embeddings.append(embedding)

        bbox = face[:4].astype(int)
        print(f"  OK: {img_path.name} | face at [{bbox[0]},{bbox[1]} {bbox[2]}x{bbox[3]}] conf={conf:.2f}")

    # Cross-check similarity between this person's photos
    if len(embeddings) >= 2:
        print(f"\n  Self-similarity check ({len(embeddings)} photos):")
        for i in range(len(embeddings)):
            for j in range(i+1, len(embeddings)):
                score = recognizer.match(embeddings[i], embeddings[j], cv2.FaceRecognizerSF_FR_COSINE)
                status = "MATCH" if score >= 0.36 else "NO MATCH"
                print(f"    Photo {i+1} vs Photo {j+1}: {score:.4f} [{status}]")

    print()

print("Encoding test complete!")
