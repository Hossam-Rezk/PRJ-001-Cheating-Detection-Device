"""Paths, labels and default settings shared across the server."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DATASET_DIR = DATA_DIR / "dataset"
MODELS_DIR = DATA_DIR / "models"
EVAL_DIR = DATA_DIR / "evaluation"
STATIC_DIR = ROOT / "server" / "static"

for d in (DATA_DIR, DATASET_DIR, MODELS_DIR, EVAL_DIR):
    d.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DATA_DIR / "settings.json"
CALIBRATION_FILE = DATA_DIR / "calibrations.json"
ALERTS_FILE = DATA_DIR / "alerts.json"

# Classes the model distinguishes. "earphones" is the target class; the
# "other_magnetic" class teaches the model what is *not* a target.
LABELS = ["clear", "earphones", "other_magnetic"]
TARGET_LABEL = "earphones"

ORIENTATIONS = ["facing", "side", "flipped", "inside_case", "n/a"]
ENVIRONMENTS = ["lab_bench", "exam_desk", "metal_desk", "near_laptop", "floor", "other"]

DEFAULT_SETTINGS = {
    # detection
    "mode": "fusion",              # threshold | ml | fusion
    "alert_threshold": 0.60,       # fused score to raise an alert
    "clear_threshold": 0.40,       # fused score to clear it (hysteresis)
    "alert_frames": 3,             # consecutive frames above threshold
    "clear_frames": 10,
    "w_ml": 0.60, "w_ble": 0.25, "w_rule": 0.15,
    "ble_alert_rssi": -65,         # audio device louder than this counts as near
    "server_alert_led": True,      # light the car's red LED on alerts
    "buzzer": False,               # silent by default: do not disturb students
    # safety
    "obstacle_stop_cm": 20.0,
    # dead reckoning (positions are metres from where the car started)
    "speed_mps_at_100": 0.45,      # measured forward speed at 100% PWM
    "turn_dps_at_100": 180.0,      # measured spin rate at 100% PWM
    "ble_tx_power": -59,           # RSSI at 1 m
    "ble_path_loss_n": 2.2,
}
