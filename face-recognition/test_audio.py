"""Quick test: load face engine, test recognition on a reference photo, play audio."""
import cv2
import pygame
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
MODELS_DIR = SCRIPT_DIR / "models"
FACES_DIR = SCRIPT_DIR / "faces"
AUDIO_DIR = SCRIPT_DIR / "audio"

DETECTION_MODEL = str(MODELS_DIR / "face_detection_yunet_2023mar.onnx")
RECOGNITION_MODEL = str(MODELS_DIR / "face_recognition_sface_2021dec.onnx")
WELCOME_AUDIO = AUDIO_DIR / "welcome_home.mp3"

# Set volume to 80%
try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL
    from ctypes import cast, POINTER

    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume = cast(interface, POINTER(IAudioEndpointVolume))
    volume.SetMasterVolumeLevelScalar(0.80, None)
    print("Volume set to 80%")
except Exception as e:
    print(f"Could not set volume: {e}")

# Load models
print("Loading models...")
detector = cv2.FaceDetectorYN.create(DETECTION_MODEL, "", (320, 320), 0.7, 0.3, 5000)
recognizer = cv2.FaceRecognizerSF.create(RECOGNITION_MODEL, "")

# Load Tarik's embeddings
print("Loading Tarik's face encodings...")
tarik_embeddings = []
tarik_dir = FACES_DIR / "tarik"
for img_path in tarik_dir.iterdir():
    if img_path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        continue
    img = cv2.imread(str(img_path))
    if img is None:
        continue
    h, w = img.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)
    if faces is not None and len(faces) > 0:
        aligned = recognizer.alignCrop(img, faces[0])
        tarik_embeddings.append(recognizer.feature(aligned))

print(f"Loaded {len(tarik_embeddings)} embeddings for Tarik")

# Test: use one of Tarik's own photos (should match)
test_img_path = list(tarik_dir.glob("*.jpeg"))[0]
print(f"\nTesting recognition on: {test_img_path.name}")
test_img = cv2.imread(str(test_img_path))
h, w = test_img.shape[:2]
detector.setInputSize((w, h))
_, faces = detector.detect(test_img)

if faces is not None and len(faces) > 0:
    aligned = recognizer.alignCrop(test_img, faces[0])
    test_embedding = recognizer.feature(aligned)

    best_score = max(
        recognizer.match(test_embedding, ref, cv2.FaceRecognizerSF_FR_COSINE)
        for ref in tarik_embeddings
    )
    print(f"Best match score: {best_score:.4f} (threshold: 0.36)")
    print(f"Result: {'TARIK RECOGNIZED!' if best_score >= 0.36 else 'NOT RECOGNIZED'}")

    # Play Iron Man audio
    if WELCOME_AUDIO.exists():
        print(f"\nPlaying: {WELCOME_AUDIO.name}")
        pygame.mixer.init()
        pygame.mixer.music.load(str(WELCOME_AUDIO))
        pygame.mixer.music.set_volume(1.0)
        pygame.mixer.music.play()

        # Let it play for 8 seconds
        time.sleep(8)
        pygame.mixer.music.stop()
        pygame.mixer.quit()
        print("Audio test complete!")
    else:
        print(f"Audio file not found: {WELCOME_AUDIO}")
else:
    print("No face detected in test image!")
