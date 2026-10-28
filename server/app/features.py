"""Turn raw Pico frames into the feature vector used by the ML model.

The same extractor is used live and when replaying recorded sessions for
training, so a model always sees features computed the same way.
"""
from collections import deque

import numpy as np

from . import ble as blelib

FEATURE_NAMES = [
    "hall_dev_mean",      # mean |sample - baseline| in this frame
    "hall_dev_max",       # max  |sample - baseline| in this frame
    "hall_signed_mean",   # mean (sample - baseline): pole / orientation
    "hall_frame_std",     # within-frame noise
    "hall_win_dev_mean",  # mean deviation over the ~1 s window
    "hall_win_std",       # std of frame means over the window
    "hall_win_p2p",       # peak-to-peak of frame means over the window
    "hall_slope",         # trend of frame means (counts per frame)
    "hall_digital",       # KY-024 comparator output
    "moving",             # motors running (they add magnetic noise)
    "ble_count",
    "ble_audio_count",
    "ble_audio_max_rssi",
    "ble_audio_rssi_delta",
    "ble_apple_count",
    "ble_max_rssi",
]

WINDOW = 10
NO_RSSI = -100.0


class FeatureExtractor:
    def __init__(self, window: int = WINDOW):
        self.means = deque(maxlen=window)
        self.audio_rssi = deque(maxlen=window)

    def reset(self):
        self.means.clear()
        self.audio_rssi.clear()

    def extract(self, frame: dict, baseline: float, ignored_macs=frozenset()) -> tuple[np.ndarray, dict]:
        samples = frame.get("hall") or [frame.get("hall_analog", baseline)]
        x = np.asarray(samples, dtype=float)
        dev = x - baseline
        fmean = float(x.mean())
        self.means.append(fmean)
        m = np.asarray(self.means)
        mdev = np.abs(m - baseline)
        slope = float(np.polyfit(np.arange(len(m)), m, 1)[0]) if len(m) >= 3 else 0.0

        devices = []
        for d in frame.get("ble") or []:
            if d.get("mac") in ignored_macs:
                continue
            info = blelib.classify(d)
            devices.append({**d, **info})
        audio = [d for d in devices if d["audio"]]
        audio_max = max((d["rssi"] for d in audio), default=NO_RSSI)
        self.audio_rssi.append(audio_max)
        delta = audio_max - self.audio_rssi[0] if len(self.audio_rssi) > 1 else 0.0

        f = {
            "hall_dev_mean": float(np.abs(dev).mean()),
            "hall_dev_max": float(np.abs(dev).max()),
            "hall_signed_mean": float(dev.mean()),
            "hall_frame_std": float(x.std()),
            "hall_win_dev_mean": float(mdev.mean()),
            "hall_win_std": float(m.std()),
            "hall_win_p2p": float(m.max() - m.min()),
            "hall_slope": slope,
            "hall_digital": float(frame.get("hall_digital", 0)),
            "moving": float(frame.get("motor", "S") != "S"),
            "ble_count": float(len(devices)),
            "ble_audio_count": float(len(audio)),
            "ble_audio_max_rssi": float(audio_max),
            "ble_audio_rssi_delta": float(delta),
            "ble_apple_count": float(sum(1 for d in devices if (d.get("mfg") or "").startswith("4c00"))),
            "ble_max_rssi": float(max((d["rssi"] for d in devices), default=NO_RSSI)),
        }
        vec = np.array([f[n] for n in FEATURE_NAMES], dtype=float)
        return vec, {"features": f, "devices": devices}
