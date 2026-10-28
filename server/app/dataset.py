"""Labelled recording sessions: raw Pico frames stored as JSONL + metadata.

Raw frames (not features) are stored so features can be recomputed if the
feature set changes. Each session is one condition (label, distance,
orientation, environment); training splits by session to avoid leakage
between near-identical consecutive frames.
"""
import csv
import io
import json
import random
import threading
import time
import uuid

import numpy as np

from .config import DATASET_DIR, LABELS
from .features import FEATURE_NAMES, FeatureExtractor
from .simulator import SyntheticSession

INDEX = DATASET_DIR / "sessions.json"


class Dataset:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = json.loads(INDEX.read_text()) if INDEX.exists() else []
        self.active = None
        self._fh = None

    def _save(self):
        INDEX.write_text(json.dumps(self.sessions, indent=1))

    # -- live recording -----------------------------------------------------
    def start(self, meta: dict, baseline: float) -> dict:
        if meta.get("label") not in LABELS:
            raise ValueError(f"label must be one of {LABELS}")
        self.stop()
        sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        s = {
            "id": sid, "label": meta["label"],
            "target_distance_cm": float(meta.get("target_distance_cm") or 0),
            "orientation": meta.get("orientation", "n/a"),
            "environment": meta.get("environment", "other"),
            "notes": meta.get("notes", ""), "synthetic": False,
            "baseline": baseline, "started": time.time(), "frames": 0,
        }
        self._fh = open(DATASET_DIR / f"{sid}.jsonl", "w")
        self.active = s
        return s

    def add(self, frame: dict):
        if self.active and self._fh:
            self._fh.write(json.dumps(frame) + "\n")
            self.active["frames"] += 1

    def stop(self):
        if not self.active:
            return None
        self._fh.close()
        s, self.active, self._fh = self.active, None, None
        s["ended"] = time.time()
        with self.lock:
            if s["frames"] > 0:
                self.sessions.append(s)
                self._save()
            else:
                (DATASET_DIR / f"{s['id']}.jsonl").unlink(missing_ok=True)
        return s

    def delete(self, sid: str):
        with self.lock:
            self.sessions = [s for s in self.sessions if s["id"] != sid]
            (DATASET_DIR / f"{sid}.jsonl").unlink(missing_ok=True)
            self._save()

    def delete_synthetic(self):
        for s in [s for s in self.sessions if s.get("synthetic")]:
            self.delete(s["id"])

    # -- synthetic bootstrap data ------------------------------------------
    def generate_synthetic(self, sessions_per_class=30, frames=60, seed=None):
        rng = random.Random(seed)
        made = 0
        for label in LABELS:
            for _ in range(sessions_per_class):
                # The hall sensor only sees magnets within ~10 cm; beyond that only BLE helps.
                dist = rng.choice([2, 3, 5, 8, 12, 20]) if label == "earphones" else                     rng.choice([2, 3, 5, 8]) if label == "other_magnetic" else 0
                orient = rng.choice(["facing", "side", "flipped", "inside_case"]) if label == "earphones" else \
                    rng.choice(["facing", "side", "flipped"]) if label == "other_magnetic" else "n/a"
                env = rng.choice(["lab_bench", "exam_desk", "metal_desk", "near_laptop", "floor"])
                sim = SyntheticSession(label, dist, orient, env, seed=rng.random())
                sid = "syn-" + uuid.uuid4().hex[:8]
                with open(DATASET_DIR / f"{sid}.jsonl", "w") as fh:
                    for _ in range(frames):
                        fh.write(json.dumps(sim.frame()) + "\n")
                self.sessions.append({
                    "id": sid, "label": label, "target_distance_cm": dist, "orientation": orient,
                    "environment": env, "notes": "synthetic demo data", "synthetic": True,
                    "baseline": sim.calibrated_baseline, "started": time.time(), "ended": time.time(), "frames": frames,
                })
                made += 1
        with self.lock:
            self._save()
        return made

    # -- reading back -------------------------------------------------------
    def frames(self, sid: str):
        p = DATASET_DIR / f"{sid}.jsonl"
        if not p.exists():
            return []
        with open(p) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def build_matrix(self, include_synthetic=True, ignored_macs=frozenset()):
        """Replay sessions through the feature extractor -> X, y, groups, session rows."""
        X, y, groups, used = [], [], [], []
        for s in self.sessions:
            if s.get("synthetic") and not include_synthetic:
                continue
            fx = FeatureExtractor()
            frames = self.frames(s["id"])
            for i, fr in enumerate(frames):
                vec, _ = fx.extract(fr, s.get("baseline") or fr.get("hall_baseline", 33500), ignored_macs)
                if i < 3:
                    continue  # let the window fill
                X.append(vec); y.append(s["label"]); groups.append(s["id"])
            used.append(s)
        return np.array(X), np.array(y), np.array(groups), used

    def stats(self):
        by_label = {l: {"sessions": 0, "frames": 0, "real_frames": 0} for l in LABELS}
        by_env, by_orient, by_dist = {}, {}, {}
        for s in self.sessions:
            b = by_label.setdefault(s["label"], {"sessions": 0, "frames": 0, "real_frames": 0})
            b["sessions"] += 1; b["frames"] += s["frames"]
            if not s.get("synthetic"):
                b["real_frames"] += s["frames"]
            by_env[s["environment"]] = by_env.get(s["environment"], 0) + s["frames"]
            by_orient[s["orientation"]] = by_orient.get(s["orientation"], 0) + s["frames"]
            k = str(int(s.get("target_distance_cm") or 0))
            by_dist[k] = by_dist.get(k, 0) + s["frames"]
        return {"by_label": by_label, "by_environment": by_env, "by_orientation": by_orient,
                "by_distance_cm": by_dist, "total_sessions": len(self.sessions),
                "total_frames": sum(s["frames"] for s in self.sessions),
                "synthetic_sessions": sum(1 for s in self.sessions if s.get("synthetic"))}

    def export_csv(self, include_synthetic=True) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["session_id", "label", "target_distance_cm", "orientation", "environment", "synthetic",
                    "frame_index"] + FEATURE_NAMES)
        for s in self.sessions:
            if s.get("synthetic") and not include_synthetic:
                continue
            fx = FeatureExtractor()
            for i, fr in enumerate(self.frames(s["id"])):
                vec, _ = fx.extract(fr, s.get("baseline") or 33500)
                w.writerow([s["id"], s["label"], s["target_distance_cm"], s["orientation"], s["environment"],
                            int(bool(s.get("synthetic"))), i] + [round(v, 3) for v in vec])
        return buf.getvalue()
