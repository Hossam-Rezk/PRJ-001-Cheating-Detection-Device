"""Sensor physics used for (a) synthetic demo datasets and (b) a virtual Pico.

The virtual Pico speaks the exact same TCP protocol as firmware/main.py, so
the whole server + dashboard can be exercised without hardware. Everything
generated here is tagged synthetic and must be replaced by real recordings
before reporting model accuracy.
"""
import asyncio
import json
import math
import random
import time

BASELINE = 33500
HALL_NOISE = 35
ADC_MAX = 65535

# Peak deviation (ADC counts) at 2 cm, falling off with 1/r^3 (magnetic dipole)
DIPOLE = {"earphones": 1400.0, "other_magnetic": 3800.0, "clear": 0.0}
ORIENT_GAIN = {"facing": 1.0, "side": 0.45, "flipped": -1.0, "inside_case": 2.2, "n/a": 1.0}
ENV_OFFSET = {"lab_bench": 0, "exam_desk": 40, "metal_desk": 450, "near_laptop": 220, "floor": 80, "other": 60}

AIRPODS_IDS = ["0220", "0f20", "1320", "0e20", "1420", "2420"]


def hall_dev(kind: str, dist_cm: float, orientation: str, rng: random.Random, scale=1.0) -> float:
    a = DIPOLE.get(kind, 0.0) * scale * ORIENT_GAIN.get(orientation, 1.0)
    return a * (2.0 / max(dist_cm, 1.0)) ** 3


def hall_samples(dev: float, env: str, moving: bool, rng: random.Random, n=8):
    noise = HALL_NOISE + (140 if moving else 0)
    off = ENV_OFFSET.get(env, 0) + (rng.gauss(0, 120) if moving else 0)
    return [int(min(ADC_MAX, max(0, BASELINE + off + dev + rng.gauss(0, noise)))) for _ in range(n)]


def rssi_at(dist_m: float, rng: random.Random, tx=-59, n=2.2) -> int:
    r = tx - 10 * n * math.log10(max(dist_m, 0.05)) + rng.gauss(0, 4)
    return int(max(-100, min(-28, r)))


def rand_mac(rng):
    return ":".join("%02x" % rng.randrange(256) for _ in range(6))


def airpods_adv(rng, mac, rssi):
    mid = rng.choice(AIRPODS_IDS)
    mfg = "4c00" + "0719" + "01" + mid + "".join("%02x" % rng.randrange(256) for _ in range(5))
    return {"mac": mac, "name": "Unknown", "rssi": rssi, "mfg": mfg, "app": -1, "age": rng.randrange(0, 900)}


def buds_adv(rng, mac, rssi):
    name = rng.choice(["Galaxy Buds2 Pro", "HUAWEI FreeBuds 5i", "JBL Tune 230NC", "Redmi Buds 4", "soundcore Life P3"])
    return {"mac": mac, "name": name, "rssi": rssi, "mfg": "", "app": 0x0941, "age": rng.randrange(0, 900)}


def phone_adv(rng, mac, rssi):
    if rng.random() < 0.6:
        mfg = "4c001005" + "".join("%02x" % rng.randrange(256) for _ in range(5))
        return {"mac": mac, "name": "Unknown", "rssi": rssi, "mfg": mfg, "app": -1, "age": rng.randrange(0, 2000)}
    return {"mac": mac, "name": rng.choice(["Galaxy A54", "Redmi Note 12", "Unknown"]), "rssi": rssi,
            "mfg": "7500" + "0102", "app": -1, "age": rng.randrange(0, 2000)}


class BleCache:
    """Mirror of the firmware's device table: keeps a device (with its last
    RSSI and age) until it has not been heard for 8 s."""

    def __init__(self, stale_frames=80):
        self.stale = stale_frames
        self.devs = {}

    def frame(self, heard, seq, period_ms=100):
        for d in heard:
            self.devs[d["mac"]] = (d, seq)
        out = []
        for mac, (d, seen) in list(self.devs.items()):
            if seq - seen > self.stale:
                del self.devs[mac]
                continue
            out.append({**d, "age": (seq - seen) * period_ms})
        out.sort(key=lambda x: -x["rssi"])
        return out[:12]


class SyntheticSession:
    """Stationary recording of one labelled condition, as the Pico would stream it."""

    def __init__(self, label, dist_cm, orientation, env, seed=None):
        self.rng = random.Random(seed)
        self.label, self.dist_cm, self.orientation, self.env = label, dist_cm, orientation, env
        rng = self.rng
        self.scale = rng.uniform(0.6, 1.5)
        self.moving_p = rng.choice([0.0, 0.0, 0.25])
        self.ambient = [(rand_mac(rng), rng.uniform(2.5, 9)) for _ in range(rng.randrange(0, 5))]
        self.target_ble = None
        if label == "earphones":
            r = rng.random()
            self.target_ble = "airpods" if r < 0.65 else "buds" if r < 0.9 else None  # silent when in use
        elif label == "other_magnetic" and rng.random() < 0.5:
            self.target_ble = "phone"
        # Each recording is preceded by a calibration in that environment, which
        # absorbs the desk's static offset (up to a small residual).
        self.calibrated_baseline = BASELINE + ENV_OFFSET.get(env, 0) + int(rng.gauss(0, 10))
        self.target_mac = rand_mac(rng)
        # Other people's earbuds elsewhere in the room are not a target at this desk
        self.far_buds = label == "clear" and rng.random() < 0.35
        self.far_buds_m = rng.uniform(1.2, 6.0)
        self.ble = BleCache()
        self.seq = 0

    def frame(self):
        rng = self.rng
        moving = rng.random() < self.moving_p
        dev = hall_dev(self.label, self.dist_cm, self.orientation, rng, self.scale)
        hall = hall_samples(dev, self.env, moving, rng)
        mean = sum(hall) // len(hall)
        ble = [phone_adv(rng, mac, rssi_at(d, rng)) for mac, d in self.ambient if rng.random() < 0.3]
        d_m = self.dist_cm / 100.0
        if self.target_ble and rng.random() < 0.5:
            mk = {"airpods": airpods_adv, "buds": buds_adv, "phone": phone_adv}[self.target_ble]
            ble.append(mk(rng, self.target_mac, rssi_at(d_m, rng)))
        if self.far_buds and rng.random() < 0.4:
            ble.append(airpods_adv(rng, self.target_mac, rssi_at(self.far_buds_m, rng)))
        ble = self.ble.frame(ble, self.seq)
        f = {
            "fw": "sim", "seq": self.seq, "t": self.seq * 100, "hall": hall, "hall_analog": hall[-1],
            "hall_baseline": self.calibrated_baseline, "hall_diff": abs(mean - self.calibrated_baseline),
            "hall_digital": int(abs(mean - self.calibrated_baseline) > 6000), "distance_cm": 120.0,
            "motor": "F" if moving else "S", "speed": 60 if moving else 0, "status": "safe", "ble": ble,
        }
        self.seq += 1
        return f


# ---------------------------------------------------------------------------
# Virtual Pico: moves around a simulated exam room
# ---------------------------------------------------------------------------
class SimWorld:
    def __init__(self, seed=7, room_w=8.0, room_h=6.0):
        self.rng = random.Random(seed)
        self.room_w, self.room_h = room_w, room_h
        self.x, self.y, self.th = 0.6, 0.6, 0.0  # true pose (m, m, rad)
        self.cmd, self.speed = "S", 0
        self.baseline = BASELINE
        rng = self.rng
        # Hidden objects: (kind, x, y, orientation, ble-type, mac)
        self.objects = [
            ("earphones", 2.4, 1.8, "facing", "airpods", rand_mac(rng)),
            ("earphones", 5.6, 4.2, "inside_case", "buds", rand_mac(rng)),
            ("other_magnetic", 4.0, 3.0, "side", "phone", rand_mac(rng)),
            ("other_magnetic", 6.5, 1.2, "facing", None, None),
        ] + [("phone", rng.uniform(1, 7), rng.uniform(1, 5), "n/a", "phone", rand_mac(rng)) for _ in range(4)]
        self.ble = BleCache()
        self.seq = 0
        self.last = time.monotonic()

    def apply(self, line: str):
        parts = line.split()
        if not parts:
            return
        if parts[0] in "FBLRS":
            self.cmd = parts[0]
            self.speed = int(parts[1]) if len(parts) > 1 else 70
        elif parts[0] == "C":
            self.baseline = BASELINE

    def step(self, dt):
        v = 0.45 * 0.93 * self.speed / 100  # slightly slower than the server assumes
        w = math.radians(180 * self.speed / 100)
        if self.cmd == "F":
            self.x += v * dt * math.cos(self.th); self.y += v * dt * math.sin(self.th)
        elif self.cmd == "B":
            self.x -= v * dt * math.cos(self.th); self.y -= v * dt * math.sin(self.th)
        elif self.cmd == "L":
            self.th += w * dt
        elif self.cmd == "R":
            self.th -= w * dt
        self.x = min(max(self.x, 0.15), self.room_w - 0.15)
        self.y = min(max(self.y, 0.15), self.room_h - 0.15)

    def wall_distance_cm(self):
        c, s = math.cos(self.th), math.sin(self.th)
        ts = []
        if c > 1e-6: ts.append((self.room_w - self.x) / c)
        if c < -1e-6: ts.append(-self.x / c)
        if s > 1e-6: ts.append((self.room_h - self.y) / s)
        if s < -1e-6: ts.append(-self.y / s)
        return round(min(400.0, max(2.0, min(ts) * 100 + self.rng.gauss(0, 1))), 1)

    def frame(self):
        now = time.monotonic()
        self.step(min(0.3, now - self.last))
        self.last = now
        rng = self.rng
        sx, sy = self.x + 0.1 * math.cos(self.th), self.y + 0.1 * math.sin(self.th)  # sensor at the nose
        dev, ble = 0.0, []
        for kind, ox, oy, orient, bt, mac in self.objects:
            d_m = math.hypot(ox - sx, oy - sy)
            if kind in DIPOLE:
                dev += hall_dev(kind, max(d_m * 100, 2.0), orient, rng)
            if bt and rng.random() < 0.5:
                mk = {"airpods": airpods_adv, "buds": buds_adv, "phone": phone_adv}[bt]
                ble.append(mk(rng, mac, rssi_at(d_m, rng)))
        moving = self.cmd != "S"
        hall = hall_samples(dev, "exam_desk", moving, rng)
        mean = sum(hall) // len(hall)
        ble = self.ble.frame(ble, self.seq)
        f = {
            "fw": "2.0-sim", "seq": self.seq, "t": int(now * 1000) & 0x3FFFFFFF, "hall": hall,
            "hall_analog": hall[-1], "hall_baseline": self.baseline, "hall_diff": abs(mean - self.baseline),
            "hall_digital": int(abs(mean - self.baseline) > 6000), "distance_cm": self.wall_distance_cm(),
            "motor": self.cmd, "speed": self.speed, "status": "safe", "ble": ble,
        }
        self.seq += 1
        return f


async def run_virtual_pico(host="127.0.0.1", port=8080, period=0.1):
    """Connect to the server like the real Pico and stream simulated frames forever."""
    world = SimWorld()
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            last_drive = time.monotonic()
            while True:
                writer.write((json.dumps(world.frame()) + "\n").encode())
                await writer.drain()
                await asyncio.sleep(period)
                while True:
                    try:
                        line = await asyncio.wait_for(reader.readline(), timeout=0.001)
                    except asyncio.TimeoutError:
                        break
                    if not line:
                        raise ConnectionError("server closed")
                    text = line.decode().strip()
                    if text[:1] in ("F", "B", "L", "R", "S"):
                        last_drive = time.monotonic()
                    world.apply(text)
                if world.cmd != "S" and time.monotonic() - last_drive > 0.6:
                    world.apply("S")  # same deadman as the firmware
        except (ConnectionError, OSError):
            await asyncio.sleep(1.0)
