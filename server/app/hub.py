"""Central state: receives Pico frames, runs features -> model -> fusion ->
alerts -> localization, records datasets / evaluation trials, and pushes
updates to dashboard websockets."""
import asyncio
import json
import time
from collections import deque

import numpy as np

from . import ble as blelib
from .config import ALERTS_FILE, CALIBRATION_FILE, DEFAULT_SETTINGS, EVAL_DIR, SETTINGS_FILE
from .dataset import Dataset
from .detector import Detector
from .features import FeatureExtractor
from .localization import Localizer
from .model import ModelStore

TRIALS_FILE = EVAL_DIR / "trials.json"
DRIVE_HOLD_S = 0.4


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


class Hub:
    def __init__(self):
        self.settings = {**DEFAULT_SETTINGS, **_load(SETTINGS_FILE, {})}
        self.calibrations = _load(CALIBRATION_FILE, [])
        self.ignored_macs = set(self.settings.get("ignored_macs", []))
        self.fx = FeatureExtractor()
        self.detector = Detector()
        self.detector.load_history(_load(ALERTS_FILE, []))
        self.loc = Localizer(self.settings)
        self.models = ModelStore()
        self.dataset = Dataset()
        self.trials = _load(TRIALS_FILE, [])
        self.trial = None
        self.calib_job = None
        self.clients = set()
        self.loop = None

        self.connected = False
        self.peer = None
        self.legacy = False
        self.last_frame = None
        self.last_payload = None
        self.last_frame_time = 0.0
        self.arrivals = deque(maxlen=60)
        self.history = deque(maxlen=600)  # compact time series for charts
        self.frames_total = 0
        self.drive = ("S", 0, 0.0)
        self.pending = []  # one-shot commands for the Pico
        self.led_state = None
        self.blocked = False
        self.drift = deque(maxlen=300)
        self.last_map_push = 0.0
        self.started = time.time()

    # -- persistence -----------------------------------------------------
    def save_settings(self):
        self.settings["ignored_macs"] = sorted(self.ignored_macs)
        SETTINGS_FILE.write_text(json.dumps(self.settings, indent=1))

    def update_settings(self, patch):
        for k, v in patch.items():
            if k in DEFAULT_SETTINGS:
                self.settings[k] = type(DEFAULT_SETTINGS[k])(v)
        if "buzzer" in patch:
            self.pending.append(f"Z {int(self.settings['buzzer'])}")
        self.save_settings()

    def save_alerts(self):
        ALERTS_FILE.write_text(json.dumps(self.detector.history[-500:], indent=1))

    # -- calibration --------------------------------------------------------
    @property
    def calibration(self):
        return self.calibrations[-1] if self.calibrations else None

    def baseline(self):
        if self.calibration:
            return self.calibration["baseline"]
        return (self.last_frame or {}).get("hall_baseline", 33500)

    def threshold_counts(self):
        return self.calibration["threshold"] if self.calibration else 500.0

    def start_calibration(self, seconds=4.0, k=5.0, note=""):
        self.pending.append("C")  # firmware re-baselines too
        self.calib_job = {"until": time.time() + 1.5 + seconds, "skip_until": time.time() + 1.5,
                          "samples": [], "frame_means": [], "k": k, "note": note, "seconds": seconds}

    def _calib_frame(self, frame):
        job = self.calib_job
        now = time.time()
        if now < job["skip_until"]:
            return
        hall = frame.get("hall") or [frame.get("hall_analog", 0)]
        job["samples"].extend(hall)
        job["frame_means"].append(float(np.mean(hall)))
        if now >= job["until"]:
            self.calib_job = None
            s = np.array(job["samples"], float); fm = np.array(job["frame_means"])
            if len(s) < 8:
                return self.broadcast({"type": "toast", "level": "error", "text": "Calibration failed: no data"})
            prev = self.calibration
            noise = float(fm.std())
            rec = {
                "time": now, "baseline": round(float(s.mean()), 1), "sample_std": round(float(s.std()), 1),
                "frame_mean_std": round(noise, 1), "min": int(s.min()), "max": int(s.max()),
                "n_samples": int(len(s)), "n_frames": int(len(fm)), "k": job["k"],
                "threshold": round(max(150.0, job["k"] * max(noise, float(s.std()) / 2)), 1),
                "drift_from_previous": round(float(s.mean()) - prev["baseline"], 1) if prev else None,
                "pico_baseline": frame.get("hall_baseline"), "note": job["note"],
            }
            self.calibrations.append(rec)
            CALIBRATION_FILE.write_text(json.dumps(self.calibrations, indent=1))
            self.drift.clear()
            self.fx.reset()
            self.broadcast({"type": "calibration", "record": rec})

    # -- driving ------------------------------------------------------------
    def set_drive(self, cmd, speed):
        cmd = cmd if cmd in ("F", "B", "L", "R", "S") else "S"
        self.drive = (cmd, int(max(0, min(100, speed))), time.time() + DRIVE_HOLD_S)

    def drive_reply(self, distance):
        cmd, speed, until = self.drive
        if time.time() > until:
            cmd, speed = "S", 0
        self.blocked = cmd == "F" and distance < self.settings["obstacle_stop_cm"]
        if self.blocked:
            cmd, speed = "S", 0
        return cmd, speed

    # -- frame pipeline -----------------------------------------------------
    def on_connect(self, peer):
        self.connected, self.peer = True, peer
        self.fx.reset()
        self.led_state = None
        self.pending.append(f"Z {int(self.settings['buzzer'])}")
        self.broadcast({"type": "toast", "level": "ok", "text": f"Car connected from {peer}"})

    def on_disconnect(self, peer):
        if self.peer == peer:
            self.connected = False
            self.broadcast({"type": "toast", "level": "error", "text": "Car disconnected"})

    def on_frame(self, frame, arrival):
        """Process one frame; returns the reply lines for the Pico."""
        dt = arrival - self.last_frame_time if self.last_frame_time else 0.1
        self.last_frame_time = arrival
        self.arrivals.append(arrival)
        self.frames_total += 1
        self.legacy = "fw" not in frame
        self.last_frame = frame
        s = self.settings

        self.loc.step(frame.get("motor", "S"), frame.get("speed", 70 if frame.get("motor", "S") != "S" else 0), dt, s)
        if self.calib_job:
            self._calib_frame(frame)
        self.dataset.add(frame)

        base = self.baseline()
        vec, info = self.fx.extract(frame, base, frozenset(self.ignored_macs))
        feats, devices = info["features"], info["devices"]
        ml = self.models.predict(vec)
        raw, comps, best_ble = self.detector.score(feats, ml, devices, self.threshold_counts(),
                                                   self.models.threshold, s)
        comps["raw"] = raw
        score = self.detector.smooth_score(raw)
        self.loc.observe(score, devices, s)

        if frame.get("motor", "S") == "S" and score < s["clear_threshold"]:
            self.drift.append(float(np.mean(frame.get("hall") or [frame.get("hall_analog", base)])) - base)

        where = self.loc.where()
        # How far the possible device is from the car: Bluetooth gives a rough
        # range; without an audio device the evidence is magnetic (cm range).
        if best_ble:
            rng = blelib.rssi_to_distance(best_ble["rssi"], s["ble_tx_power"], s["ble_path_loss_n"])
            range_text = "right next to the car" if rng < 0.2 else f"≈{rng:.1f} m from the car"
        else:
            rng, range_text = 0.1, "within ~10 cm of the car"
        ctx = {"where": where, "range_m": rng, "range_text": range_text,
               "pose": [round(self.loc.x, 2), round(self.loc.y, 2)],
               "hall_dev": round(feats["hall_win_dev_mean"], 1),
               "ml_label": ml["label"] if ml else None,
               "ml_p_target": round(ml["p_target"], 3) if ml else None,
               "ble_device": None if not best_ble else {
                   "model": best_ble["model"], "rssi": best_ble["rssi"], "mac": best_ble["mac"],
                   "est_distance_m": blelib.rssi_to_distance(best_ble["rssi"], s["ble_tx_power"], s["ble_path_loss_n"])}}
        event, alert = self.detector.update(score, comps, ctx, arrival, s)
        if event:
            self.save_alerts()
            self.broadcast({"type": "alert", "event": event, "alert": alert})

        if self.trial:
            self.trial["frames"] += 1
            self.trial["frame_positive"] += int(score >= s["alert_threshold"])
            if event in ("raised", "reopened") and self.trial["first_alert_s"] is None:
                self.trial["first_alert_s"] = round(arrival - self.trial["started"], 2)
                self.trial["alert_ids"].append(alert["id"])

        # Reply to the Pico
        cmd, speed = self.drive_reply(frame.get("distance_cm", 999))
        lines = []
        led = bool(self.detector.active) and s["server_alert_led"]
        if self.legacy:
            lines.append(cmd)
        else:
            lines.extend(self.pending); self.pending.clear()
            if led != self.led_state:
                lines.append(f"A {int(led)}"); self.led_state = led
            lines.append(f"{cmd} {speed}" if cmd != "S" else "S")

        hall = frame.get("hall") or [frame.get("hall_analog", base)]
        hmean = float(np.mean(hall))
        self.history.append([round(arrival, 2), round(hmean), round(hmean - base, 1), frame.get("distance_cm"),
                             score, comps["ml"], comps["ble"], comps["rule"]])
        payload = {
            "type": "frame", "t": arrival, "seq": frame.get("seq"), "fw": frame.get("fw", "legacy"),
            "hall_mean": round(hmean), "hall_dev": round(hmean - base, 1), "baseline": base,
            "hall_digital": frame.get("hall_digital", 0), "distance_cm": frame.get("distance_cm"),
            "motor": frame.get("motor", "?"), "speed": frame.get("speed"), "pico_status": frame.get("status"),
            "features": {k: round(v, 3) for k, v in feats.items()}, "ml": ml, "score": score, "components": comps,
            "alert": self.detector.active, "blocked": self.blocked,
            "pose": [round(self.loc.x, 3), round(self.loc.y, 3), round(self.loc.th, 3)], "where": where,
            "driven_m": round(self.loc.driven, 1),
            "devices": [{"mac": d["mac"], "name": d.get("name"), "rssi": d["rssi"], "kind": d["kind"],
                         "model": d["model"], "audio": d["audio"], "confidence": d["confidence"],
                         "est_m": blelib.rssi_to_distance(d["rssi"], s["ble_tx_power"], s["ble_path_loss_n"])}
                        for d in devices],
            "ignored_count": len(self.ignored_macs),
            "rate_hz": self.rate_hz(), "recording": self.dataset.active, "trial": self.trial,
            "calibrating": bool(self.calib_job),
        }
        self.last_payload = payload
        self.broadcast(payload)
        if arrival - self.last_map_push > 1.0:
            self.last_map_push = arrival
            self.broadcast({"type": "map", **self.loc.snapshot(s)})
        return lines

    def rate_hz(self):
        a = self.arrivals
        if len(a) < 2:
            return 0.0
        return round((len(a) - 1) / max(1e-6, a[-1] - a[0]), 2)

    # -- alerts -------------------------------------------------------------
    def alert_ack(self, aid, client_ms=None):
        a = self.detector.find(aid)
        if a and a["display_latency_ms"] is None:
            a["display_latency_ms"] = round((time.time() - a["t_condition"]) * 1000, 1)
            self.save_alerts()

    def alert_feedback(self, aid, verdict):
        a = self.detector.find(aid)
        if not a:
            raise KeyError(aid)
        a["feedback"] = verdict  # confirmed | false_positive | null
        self.save_alerts()
        return a

    # -- evaluation trials -------------------------------------------------
    def start_trial(self, meta):
        if self.trial:
            self.stop_trial()
        self.trial = {"id": len(self.trials) + 1, "ground_truth": bool(meta.get("target_present")),
                      "condition": meta.get("condition", ""), "distance_cm": meta.get("distance_cm"),
                      "notes": meta.get("notes", ""), "started": time.time(), "frames": 0, "frame_positive": 0,
                      "first_alert_s": None, "alert_ids": [], "mode": self.settings["mode"],
                      "model": self.models.meta["version"] if self.models.meta else None}
        if self.detector.active:  # an alert already open counts from the start
            self.trial["first_alert_s"] = 0.0
            self.trial["alert_ids"].append(self.detector.active["id"])
        return self.trial

    def stop_trial(self):
        t, self.trial = self.trial, None
        if not t:
            return None
        t["ended"] = time.time()
        t["predicted"] = t["first_alert_s"] is not None
        t["correct"] = t["predicted"] == t["ground_truth"]
        fp = t["frame_positive"] / max(1, t["frames"])
        t["frame_accuracy"] = round(fp if t["ground_truth"] else 1 - fp, 4)
        self.trials.append(t)
        TRIALS_FILE.write_text(json.dumps(self.trials, indent=1))
        return t

    def delete_trial(self, tid):
        self.trials = [t for t in self.trials if t["id"] != tid]
        TRIALS_FILE.write_text(json.dumps(self.trials, indent=1))

    def evaluation_summary(self):
        tr = self.trials
        pos = [t for t in tr if t["ground_truth"]]; neg = [t for t in tr if not t["ground_truth"]]
        tp = sum(t["predicted"] for t in pos); fn = len(pos) - tp
        fp = sum(t["predicted"] for t in neg); tn = len(neg) - fp
        acc = (tp + tn) / len(tr) if tr else None
        fpr = fp / len(neg) if neg else None
        fnr = fn / len(pos) if pos else None
        alerts = self.detector.history
        disp = [a["display_latency_ms"] for a in alerts if a.get("display_latency_ms") is not None]
        det = [a["detect_latency_ms"] for a in alerts if a.get("detect_latency_ms") is not None]
        lat = disp or det
        p95 = float(np.percentile(lat, 95)) / 1000 if lat else None
        fb = [a["feedback"] for a in alerts if a.get("feedback")]
        model_acc = self.models.meta["test_accuracy"] if self.models.meta else None
        synthetic = self.models.meta.get("synthetic_share", 0) > 0 if self.models.meta else False
        rate = self.rate_hz()

        def req(name, target, value, ok, unit="", note=""):
            return {"name": name, "target": target, "value": value, "unit": unit,
                    "status": "no data" if value is None else ("pass" if ok else "fail"), "note": note}

        reqs = [
            req("System detection accuracy", ">= 85%", None if acc is None else round(acc * 100, 1),
                acc is not None and acc >= 0.85, "%", f"{len(tr)} trials"),
            req("AI model accuracy (held-out test)", ">= 80%", None if model_acc is None else round(model_acc * 100, 1),
                model_acc is not None and model_acc >= 0.80, "%",
                "includes synthetic data - not valid for reporting" if synthetic else ""),
            req("Alert response time (p95)", "<= 2 s", None if p95 is None else round(p95, 2), p95 is not None and p95 <= 2.0,
                "s", "condition -> dashboard display" if disp else "condition -> alert raised"),
            req("False positive rate", "<= 10%", None if fpr is None else round(fpr * 100, 1),
                fpr is not None and fpr <= 0.10, "%", f"{len(neg)} negative trials"),
            req("Sensor update rate", ">= 1 Hz", rate if self.connected else None, rate >= 1.0, "Hz", "live"),
            req("Sensor calibration documented", ">= 1 record", len(self.calibrations) or None,
                len(self.calibrations) >= 1, "records"),
        ]
        return {"trials": tr[::-1], "confusion": {"tp": tp, "fn": fn, "fp": fp, "tn": tn},
                "accuracy": acc, "fpr": fpr, "fnr": fnr,
                "latency_ms": {"n": len(lat), "mean": round(float(np.mean(lat)), 1) if lat else None,
                               "p95": round(p95 * 1000, 1) if p95 is not None else None,
                               "max": round(float(np.max(lat)), 1) if lat else None},
                "feedback": {"confirmed": fb.count("confirmed"), "false_positive": fb.count("false_positive")},
                "requirements": reqs}

    # -- misc ---------------------------------------------------------------
    def state(self):
        drift = list(self.drift)
        return {
            "settings": self.settings, "connected": self.connected, "peer": self.peer, "legacy": self.legacy,
            "calibration": self.calibration, "calibrations": self.calibrations[::-1],
            "drift": {"mean": round(float(np.mean(drift)), 1) if drift else None,
                      "std": round(float(np.std(drift)), 1) if drift else None, "n": len(drift)},
            "model": self.models.meta, "frames_total": self.frames_total, "uptime_s": round(time.time() - self.started),
            "ignored_macs": sorted(self.ignored_macs), "recording": self.dataset.active, "trial": self.trial,
            "alerts": self.detector.history[-100:][::-1], "history": list(self.history),
        }

    def broadcast(self, msg):
        if not self.clients:
            return
        data = json.dumps(msg, default=float)
        for q in list(self.clients):
            if q.qsize() < 200:
                q.put_nowait(data)
