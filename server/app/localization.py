"""Approximate target location, in metres from where the car started.

Rooms differ in size and layout, so nothing here assumes a room or a seat
grid. The start point is (0, 0) with the car facing +x: x = metres ahead of
the start, y = metres to the left of it. Positions are reported as
"2.4 m ahead, 1.3 m left of start" and as a distance from the car.

The car has no wheel encoders, so its pose is dead-reckoned from the drive
commands and calibrated speeds (drifts; the supervisor can re-anchor it on
the map). Two estimators:
  * magnetic/fused hotspot: the hall sensor only reacts within centimetres,
    so the target is where the car was when the fused score peaked;
  * BLE multilateration: RSSI -> distance (path-loss model) at several car
    positions, solved by least squares. Needs the car to move around.
Estimates describe a place, never a student.
"""
import math
from collections import deque

import numpy as np

from . import ble as blelib

CELL = 0.25  # metres per heat-map cell


def describe(x, y):
    """Plain-words position relative to the start point."""
    fwd = f"{abs(x):.1f} m {'ahead' if x >= 0 else 'behind'}"
    side = f"{abs(y):.1f} m {'left' if y >= 0 else 'right'}"
    return f"{fwd}, {side} of start"


class Localizer:
    def __init__(self, s=None):
        self.reset()

    def reset(self, x=0.0, y=0.0, th=0.0):
        self.x, self.y, self.th = x, y, th
        self.trail = deque(maxlen=1500)
        self.heat = {}      # (i, j) cell -> highest fused score seen there
        self.driven = 0.0   # metres travelled since reset
        self.ble_obs = {}   # mac -> deque[(x, y, d, rssi)]

    def set_pose(self, x, y, th=None):
        self.x, self.y = x, y
        if th is not None:
            self.th = th

    def step(self, cmd, speed, dt, s):
        dt = min(dt, 0.5)
        v = s["speed_mps_at_100"] * speed / 100
        w = math.radians(s["turn_dps_at_100"] * speed / 100)
        px, py = self.x, self.y
        if cmd == "F":
            self.x += v * dt * math.cos(self.th); self.y += v * dt * math.sin(self.th)
        elif cmd == "B":
            self.x -= v * dt * math.cos(self.th); self.y -= v * dt * math.sin(self.th)
        elif cmd == "L":
            self.th = (self.th + w * dt) % (2 * math.pi)
        elif cmd == "R":
            self.th = (self.th - w * dt) % (2 * math.pi)
        self.driven += math.hypot(self.x - px, self.y - py)
        if not self.trail or math.hypot(self.trail[-1][0] - self.x, self.trail[-1][1] - self.y) > 0.03:
            self.trail.append((round(self.x, 3), round(self.y, 3)))

    def observe(self, score, devices, s):
        sx, sy = self.x + 0.1 * math.cos(self.th), self.y + 0.1 * math.sin(self.th)  # sensor at the nose
        key = (math.floor(sx / CELL), math.floor(sy / CELL))
        self.heat[key] = max(self.heat.get(key, 0.0), score)
        for d in devices:
            if not d.get("audio"):
                continue
            dist = blelib.rssi_to_distance(d["rssi"], s["ble_tx_power"], s["ble_path_loss_n"])
            q = self.ble_obs.setdefault(d["mac"], deque(maxlen=300))
            q.append((self.x, self.y, dist, d["rssi"]))

    def where(self, x=None, y=None):
        return describe(self.x if x is None else x, self.y if y is None else y)

    def _ble_fix(self, obs):
        P = np.array([(o[0], o[1]) for o in obs]); D = np.array([o[2] for o in obs])
        spread = float(np.sqrt(P.var(axis=0).sum()))
        # Positions along a straight line leave a mirror ambiguity: require
        # spread in both directions (smallest axis >= 20 cm std).
        min_axis = float(np.sqrt(max(0.0, np.linalg.eigvalsh(np.cov(P.T))[0]))) if len(obs) >= 3 else 0.0
        if len(obs) < 8 or spread < 0.4 or min_axis < 0.2:
            return None
        wts = 1 / np.maximum(D, 0.1) ** 2
        p = (P * wts[:, None]).sum(0) / wts.sum()
        for _ in range(30):  # Gauss-Newton on range residuals
            diff = p - P
            r = np.linalg.norm(diff, axis=1) + 1e-6
            res = r - D
            J = diff / r[:, None]
            Wt = 1 / np.maximum(D, 0.3)
            A = J.T @ (J * Wt[:, None]); b = J.T @ (res * Wt)
            try:
                dp = np.linalg.solve(A + 1e-3 * np.eye(2), b)
            except np.linalg.LinAlgError:
                break
            p = p - dp
            if np.linalg.norm(dp) < 1e-3:
                break
        rms = float(np.sqrt(np.mean((np.linalg.norm(p - P, axis=1) - D) ** 2)))
        if rms > 1.5:
            return None  # ranges inconsistent (multipath, device moved, MAC reused)
        return float(p[0]), float(p[1]), rms, spread

    def _entry(self, method, x, y, radius, conf, **extra):
        return {"method": method, "x": round(x, 2), "y": round(y, 2), "radius_m": round(radius, 2),
                "confidence": round(conf, 2), "where": describe(x, y),
                "from_car_m": round(math.hypot(x - self.x, y - self.y), 2), **extra}

    def estimate(self, s, min_score=0.5):
        est = []
        hot = [(k, v) for k, v in self.heat.items() if v >= min_score]
        if hot:
            hot.sort(key=lambda kv: -kv[1])
            top = hot[:6]
            w = np.array([v for _, v in top])
            xs = np.array([(k[0] + 0.5) * CELL for k, _ in top]); ys = np.array([(k[1] + 0.5) * CELL for k, _ in top])
            x, y = float((xs * w).sum() / w.sum()), float((ys * w).sum() / w.sum())
            est.append(self._entry("magnetic_fused_hotspot", x, y, 0.35, min(1.0, len(hot) / 6) * float(w.max())))
        for mac, obs in self.ble_obs.items():
            fix = self._ble_fix(list(obs))
            if fix:
                x, y, rms, spread = fix
                est.append(self._entry("ble_multilateration", x, y, max(0.5, rms),
                                       max(0.05, min(0.9, spread / (rms + 0.5))), mac=mac, samples=len(obs)))
        est.sort(key=lambda e: -e["confidence"])
        return est

    def snapshot(self, s):
        return {"cell": CELL, "pose": [round(self.x, 3), round(self.y, 3), round(self.th, 3)],
                "trail": list(self.trail), "where": self.where(), "driven_m": round(self.driven, 1),
                "heat": [[k[0], k[1], round(v, 2)] for k, v in self.heat.items() if v > 0.05],
                "estimates": self.estimate(s)}
