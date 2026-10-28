"""Fuse threshold rule + ML probability + BLE evidence into one score and
raise / clear alerts with debounce and hysteresis.

Alerts are *indications of a possible target* near the car - never an
identification of a person (privacy requirement).
"""
import itertools
import math
import time

from . import ble as blelib

_ids = itertools.count(1)
REOPEN_S = 30.0   # a cleared alert re-raised within this time...
REOPEN_M = 0.75   # ...and this distance of where it was is the same alert


def ml_adjusted(p, thr):
    """Rescale model probability so the model's tuned threshold maps to 0.5."""
    if p is None:
        return None
    thr = min(max(thr, 0.05), 0.95)
    return 0.5 * p / thr if p < thr else 0.5 + 0.5 * (p - thr) / (1 - thr)


class Detector:
    def __init__(self):
        self.above = 0
        self.below = 0
        self.streak_start = None
        self.active = None
        self.history = []  # past alerts (newest last)
        self.smooth = None
        self.raw_start = None  # first frame whose *unsmoothed* score met the condition

    def load_history(self, alerts):
        for a in alerts:  # an alert cannot still be open after a restart
            if a.get("status") == "active":
                a["status"] = "cleared"
        self.history = alerts
        global _ids
        _ids = itertools.count(max((a["id"] for a in alerts), default=0) + 1)

    def score(self, feats, ml, devices, calib_threshold, model_thr, s):
        dev = feats["hall_win_dev_mean"]
        rule = min(1.0, 0.5 * dev / max(calib_threshold, 1.0))
        if feats["hall_digital"]:
            rule = 1.0
        best = None
        ble_score = 0.0
        for d in devices:
            if not d.get("audio"):
                continue
            sc = d["confidence"] * blelib.proximity_score(d["rssi"], s["ble_alert_rssi"])
            if sc > ble_score:
                ble_score, best = sc, d
        p_ml = ml_adjusted(ml["p_target"], model_thr) if ml else None

        mode = s["mode"]
        if mode == "threshold" or (mode == "ml" and p_ml is None):
            total = rule
        elif mode == "ml":
            total = p_ml
        else:
            parts = [(s["w_rule"], rule), (s["w_ble"], ble_score)]
            if p_ml is not None:
                parts.append((s["w_ml"], p_ml))
            wsum = sum(w for w, _ in parts) or 1.0
            total = sum(w * v for w, v in parts) / wsum
        comps = {"rule": round(rule, 3), "ble": round(ble_score, 3),
                 "ml": None if p_ml is None else round(p_ml, 3)}
        return round(min(1.0, max(0.0, total)), 4), comps, best

    def smooth_score(self, score, alpha=0.5):
        """EMA over frames: single-frame flicker (a missed BLE advert, a tree
        split boundary) should not decide an alert on its own."""
        self.smooth = score if self.smooth is None else alpha * score + (1 - alpha) * self.smooth
        return round(self.smooth, 4)

    def update(self, score, comps, context, arrival, s):
        """Returns ('raised'|'cleared'|None, alert)."""
        now = time.time()
        if self.active is None:
            if comps.get("raw", score) >= s["alert_threshold"]:
                self.raw_start = self.raw_start or arrival
            elif score < s["clear_threshold"]:
                self.raw_start = None
        if score >= s["alert_threshold"]:
            if self.above == 0:
                self.streak_start = arrival
            self.above += 1
            self.below = 0
        elif score < s["clear_threshold"]:
            self.below += 1
            self.above = 0
        else:
            self.below = 0
        if self.active is None and self.above >= s["alert_frames"]:
            last = self.history[-1] if self.history else None
            if (last and last["status"] == "cleared" and last.get("pose") and context.get("pose")
                    and math.dist(last["pose"], context["pose"]) < REOPEN_M
                    and now - (last["cleared_at"] or 0) < REOPEN_S):
                # same place, moments later: one alert that flickered, not a new one
                last.update(status="active", cleared_at=None, reopened=last.get("reopened", 0) + 1)
                self.raw_start = None
                self.active = last
                return "reopened", last
            t_cond = min(self.raw_start or self.streak_start, self.streak_start)
            self.raw_start = None
            a = {"id": next(_ids), "t_condition": t_cond, "t_alert": now,
                 "detect_latency_ms": round((now - t_cond) * 1000, 1),
                 "display_latency_ms": None, "peak_score": score, "components": comps,
                 "status": "active", "feedback": None, "cleared_at": None, **context}
            self.active = a
            self.history.append(a)
            self.history = self.history[-500:]
            return "raised", a
        if self.active is not None:
            if score > self.active["peak_score"]:
                self.active["peak_score"] = score
                self.active.update(context)
            if self.below >= s["clear_frames"]:
                a = self.active
                a["status"] = "cleared"; a["cleared_at"] = now
                self.active = None
                return "cleared", a
        return None, self.active

    def find(self, aid):
        return next((a for a in self.history if a["id"] == aid), None)
