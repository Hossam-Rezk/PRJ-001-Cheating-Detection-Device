"""Classify BLE advertisements into device kinds (AirPods, earbuds, phones...).

Signals used, strongest first:
  * Apple Continuity "proximity pairing" (company 0x004C, type 0x07) is what
    AirPods / Beats broadcast; bytes 3-4 carry the model id.
  * GAP Appearance 0x0940-0x097F = "Wearable Audio Device" (earbud/headset).
  * Advertised name keywords (Galaxy Buds, FreeBuds, JBL, Sony WF-...).
"""
import math

APPLE_MODELS = {
    0x0220: "AirPods", 0x0F20: "AirPods (2nd gen)", 0x1320: "AirPods (3rd gen)",
    0x1920: "AirPods (4th gen)", 0x0E20: "AirPods Pro", 0x1420: "AirPods Pro (2nd gen)",
    0x2420: "AirPods Pro (2nd gen, USB-C)", 0x0A20: "AirPods Max",
    0x0520: "BeatsX", 0x0620: "Beats Solo3", 0x0B20: "Powerbeats Pro",
    0x0C20: "Beats Solo Pro", 0x1020: "Beats Flex", 0x1120: "Beats Studio Buds",
    0x1220: "Beats Fit Pro",
}
APPLE_TYPES = {0x07: "proximity_pairing", 0x10: "nearby_info", 0x0F: "nearby_action",
               0x12: "find_my", 0x0C: "handoff", 0x09: "airplay", 0x05: "airdrop"}

AUDIO_KEYWORDS = ("airpod", "buds", "earbud", "earphone", "headphone", "headset",
                  "beats", "jbl", "soundcore", "freebuds", "wf-", "wh-", "tws",
                  "jabra", "bose", "pods", "sennheiser", "skullcandy", "oneplus buds",
                  "nothing ear", "redmi buds", "qcy", "anker")
PHONE_KEYWORDS = ("iphone", "galaxy", "pixel", "redmi", "xiaomi", "oppo", "vivo",
                  "huawei", "honor", "realme", "phone")
WATCH_KEYWORDS = ("watch", "band", "fitbit", "mi band", "amazfit")

KIND_AUDIO = {"airpods", "earbuds"}


def classify(dev: dict) -> dict:
    """Return {kind, model, confidence, audio} for one device dict from the Pico."""
    name = (dev.get("name") or "").lower()
    mfg = (dev.get("mfg") or "").lower()
    app = dev.get("app", -1)
    if app is None:
        app = -1

    if mfg.startswith("4c00") and len(mfg) >= 6:
        apple_type = int(mfg[4:6], 16)
        if apple_type == 0x07:
            model = "AirPods / Beats"
            if len(mfg) >= 14:
                model_id = int(mfg[10:12] + mfg[12:14], 16)
                model = APPLE_MODELS.get(model_id, model)
            return _r("airpods", model, 0.95)
        if apple_type == 0x12:
            # Find My network: AirTag, or AirPods/case in offline mode
            return _r("apple_findmy", "Apple Find My accessory", 0.4)
        if apple_type == 0x10:
            return _r("phone", "Apple device (iPhone/iPad/Mac)", 0.6)
        return _r("apple", "Apple " + APPLE_TYPES.get(apple_type, "device"), 0.5)

    if 0x0940 <= app <= 0x097F:
        sub = {0x0941: "Earbud", 0x0942: "Headset", 0x0943: "Headphones",
               0x0944: "Neck band"}.get(app, "Wearable audio")
        return _r("earbuds", sub, 0.9)

    if any(k in name for k in AUDIO_KEYWORDS):
        return _r("earbuds", dev.get("name"), 0.85)
    if any(k in name for k in WATCH_KEYWORDS):
        return _r("watch", dev.get("name"), 0.7)
    if any(k in name for k in PHONE_KEYWORDS):
        return _r("phone", dev.get("name"), 0.7)
    if mfg.startswith("7500"):
        return _r("samsung", "Samsung device", 0.4)
    if mfg.startswith("0600"):
        return _r("microsoft", "Microsoft device", 0.4)
    return _r("unknown", dev.get("name") or "Unknown", 0.2)


def _r(kind, model, conf):
    return {"kind": kind, "model": model, "confidence": conf, "audio": kind in KIND_AUDIO}


def rssi_to_distance(rssi: float, tx_power: float = -59, n: float = 2.2) -> float:
    """Log-distance path-loss model -> metres (rough; walls/bodies add error)."""
    return round(10 ** ((tx_power - rssi) / (10 * n)), 2)


def proximity_score(rssi: float, near_rssi: float = -70) -> float:
    """0..1 logistic score, 0.5 at near_rssi, ~0.95 at near_rssi + 12 dB."""
    return 1 / (1 + math.exp(-(rssi - near_rssi) / 4.0))
