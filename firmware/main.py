# ExamGuard Rover - Raspberry Pi Pico 2 W firmware (protocol v2)
#
# Streams newline-delimited JSON frames to the laptop server and receives
# newline-delimited commands back. Backwards compatible with the server's
# legacy mode (single-letter commands).
#
# Frame (Pico -> server), one JSON object per line:
#   {"fw":"2.0","seq":n,"t":ticks_ms,"hall":[8 x u16],"hall_analog":u16,
#    "hall_baseline":u16,"hall_digital":0|1,"distance_cm":float,
#    "motor":"F|B|L|R|S","speed":0-100,"status":"...",
#    "ble":[{"mac":..,"name":..,"rssi":..,"mfg":"hex","app":int,"age":ms}]}
#
# Commands (server -> Pico), one per line:
#   F|B|L|R|S [speed%]   drive (deadman: auto-stop if not refreshed in 600 ms)
#   C                    re-calibrate hall baseline (keep magnets away)
#   A 0|1                server-side alert indicator (red LED)
#   Z 0|1                enable buzzer (default off = silent exam mode)

import machine
import network
import socket
import time
import json
import bluetooth
import ubinascii

# ==========================================
# 1. Network & Server Configuration
# ==========================================
WIFI_SSID = "YOUR_WIFI_NAME"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"

SERVER_IP = "192.168.1.2"        # laptop address, printed by server/run.py
SERVER_PORT = 8080

FW_VERSION = "2.0"
FRAME_PERIOD_MS = 100        # ~10 frames per second
HALL_BURST = 8               # hall samples per frame (for variance features)
DEADMAN_MS = 600             # stop motors if no drive command for this long
OBSTACLE_CM = 15.0           # local reflex: refuse to drive forward closer than this
BLE_STALE_MS = 8000          # forget BLE devices not heard for this long
BLE_MAX_REPORT = 12          # strongest N devices per frame (keeps frames small)

# ==========================================
# 2. Hardware Pin Definitions
# ==========================================
buzzer = machine.Pin(10, machine.Pin.OUT)
red = machine.Pin(11, machine.Pin.OUT)
green = machine.Pin(12, machine.Pin.OUT)
yellow = machine.Pin(13, machine.Pin.OUT)

# Motor Driver (L298N)
ENA = machine.PWM(machine.Pin(16))
IN1 = machine.Pin(17, machine.Pin.OUT)
IN2 = machine.Pin(18, machine.Pin.OUT)
IN3 = machine.Pin(7, machine.Pin.OUT)
IN4 = machine.Pin(8, machine.Pin.OUT)
ENB = machine.PWM(machine.Pin(9))
ENA.freq(1000)
ENB.freq(1000)
DEFAULT_SPEED_PCT = 70       # 70% of 65535 ~= 45000 (the original default)

# KY-024 Hall Effect Sensor
hall_analog = machine.ADC(26)
hall_digital = machine.Pin(27, machine.Pin.IN)
hall_baseline = 33500

# HC-SR04 Ultrasonic Sensor
trig = machine.Pin(14, machine.Pin.OUT)
echo = machine.Pin(15, machine.Pin.IN)
last_valid_distance = 100.0

# Runtime state
motor_cmd = 'S'
motor_speed = 0
last_drive_ms = 0
server_alert = False
buzzer_enabled = False

# ==========================================
# 3. Indicators & Sensors
# ==========================================
def turn_off_all():
    buzzer.value(0)
    red.value(0)
    green.value(0)
    yellow.value(0)

def calibration_state():
    turn_off_all()
    yellow.value(1)

def safe_state():
    turn_off_all()
    green.value(1)

def alert_state():
    turn_off_all()
    red.value(1)
    if buzzer_enabled:
        buzzer.value(1)

def calibrate_hall_sensor(samples=20):
    global hall_baseline
    print("Calibrating KY-024 Hall Sensor... Keep magnets away.")
    calibration_state()
    total = 0
    for _ in range(samples):
        total += hall_analog.read_u16()
        time.sleep(0.05)
    hall_baseline = total // samples
    print("Calibration done. Baseline:", hall_baseline)

def read_hall_burst(n=HALL_BURST):
    out = []
    for _ in range(n):
        out.append(hall_analog.read_u16())
        time.sleep_us(1500)
    return out

def get_filtered_distance():
    global last_valid_distance
    valid = []
    for _ in range(3):
        trig.low()
        time.sleep_us(2)
        trig.high()
        time.sleep_us(10)
        trig.low()
        duration = machine.time_pulse_us(echo, 1, 30000)
        if duration > 0:
            dist = (duration / 2) / 29.1
            if 2.0 <= dist <= 400.0:
                valid.append(dist)
        time.sleep_ms(5)
    if valid:
        last_valid_distance = round(sum(valid) / len(valid), 1)
    return last_valid_distance

# ==========================================
# 4. Motor Control
# ==========================================
def move_car(cmd, speed_pct=DEFAULT_SPEED_PCT):
    global motor_cmd, motor_speed
    speed_pct = max(0, min(100, speed_pct))
    duty = int(65535 * speed_pct / 100)
    if cmd not in ('F', 'B', 'L', 'R'):
        cmd, duty, speed_pct = 'S', 0, 0
    ENA.duty_u16(duty)
    ENB.duty_u16(duty)
    if cmd == 'F':
        IN1.value(1); IN2.value(0); IN3.value(1); IN4.value(0)
    elif cmd == 'B':
        IN1.value(0); IN2.value(1); IN3.value(0); IN4.value(1)
    elif cmd == 'R':
        IN1.value(0); IN2.value(1); IN3.value(1); IN4.value(0)
    elif cmd == 'L':
        IN1.value(1); IN2.value(0); IN3.value(0); IN4.value(1)
    else:
        IN1.value(0); IN2.value(0); IN3.value(0); IN4.value(0)
    motor_cmd = cmd
    motor_speed = speed_pct

# ==========================================
# 5. Bluetooth Radar (background scanner)
# ==========================================
_IRQ_SCAN_RESULT = 5
ble = bluetooth.BLE()
ble.active(True)
ble_devices = {}   # mac -> dict(name, rssi, mfg, app, seen)

def parse_adv(payload):
    """Return (name, manufacturer-data hex, appearance) from an AD payload."""
    name = None
    mfg = None
    app = -1
    i = 0
    n = len(payload)
    while i + 1 < n:
        length = payload[i]
        if length == 0 or i + 1 + length > n:
            break
        ad_type = payload[i + 1]
        value = payload[i + 2:i + 1 + length]
        if ad_type in (0x08, 0x09):
            name = bytes(value).decode('utf-8', 'ignore')
        elif ad_type == 0xFF and len(value) >= 2:
            # company id (little endian) + first bytes of payload
            mfg = ubinascii.hexlify(bytes(value[:16])).decode()
        elif ad_type == 0x19 and len(value) >= 2:
            app = value[0] | (value[1] << 8)
        i += 1 + length
    return name, mfg, app

def ble_irq(event, data):
    if event != _IRQ_SCAN_RESULT:
        return
    addr_type, addr, adv_type, rssi, adv_data = data
    mac = ubinascii.hexlify(bytes(addr), ':').decode()
    name, mfg, app = parse_adv(adv_data)
    dev = ble_devices.get(mac)
    if dev is None:
        if len(ble_devices) >= 40:
            return  # protect RAM in crowded rooms; prune frees slots
        dev = {'name': 'Unknown', 'mfg': '', 'app': -1}
        ble_devices[mac] = dev
    dev['rssi'] = rssi
    dev['seen'] = time.ticks_ms()
    if name:
        dev['name'] = name
    if mfg:
        dev['mfg'] = mfg
    if app >= 0:
        dev['app'] = app

def ble_snapshot():
    now = time.ticks_ms()
    out = []
    for mac in list(ble_devices.keys()):
        d = ble_devices.get(mac)
        if d is None:
            continue
        age = time.ticks_diff(now, d['seen'])
        if age > BLE_STALE_MS:
            del ble_devices[mac]
            continue
        out.append({'mac': mac, 'name': d['name'], 'rssi': d['rssi'],
                    'mfg': d['mfg'], 'app': d['app'], 'age': age})
    out.sort(key=lambda x: -x['rssi'])
    return out[:BLE_MAX_REPORT]

ble.irq(ble_irq)
ble.gap_scan(0, 30000, 30000)

# ==========================================
# 6. Wi-Fi
# ==========================================
wlan = network.WLAN(network.STA_IF)

def connect_wifi():
    wlan.active(True)
    if wlan.isconnected():
        return
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    print("Connecting to", WIFI_SSID)
    calibration_state()
    while not wlan.isconnected():
        time.sleep(0.5)
    print("Wi-Fi connected, IP:", wlan.ifconfig()[0])
    safe_state()

# ==========================================
# 7. Command handling
# ==========================================
def handle_command(line, distance_cm):
    global last_drive_ms, server_alert, buzzer_enabled
    parts = line.split()
    if not parts:
        return
    op = parts[0]
    arg = None
    if len(parts) > 1:
        try:
            arg = int(parts[1])
        except ValueError:
            arg = None
    if op in ('F', 'B', 'L', 'R', 'S'):
        last_drive_ms = time.ticks_ms()
        speed = DEFAULT_SPEED_PCT if arg is None else arg
        if op == 'F' and distance_cm < OBSTACLE_CM:
            move_car('S')   # auto-stop reflex
        else:
            move_car(op, speed)
    elif op == 'C':
        move_car('S')
        calibrate_hall_sensor()
    elif op == 'A':
        server_alert = bool(arg)
    elif op == 'Z':
        buzzer_enabled = bool(arg)

def update_indicators(status):
    if server_alert or status == "danger_magnet":
        alert_state()
    elif status in ("obstacle_close", "warning_field_change"):
        calibration_state()
    else:
        safe_state()

# ==========================================
# 8. Main loop (auto-reconnects)
# ==========================================
def run_session():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print("Connecting to server %s:%d" % (SERVER_IP, SERVER_PORT))
    sock.connect((SERVER_IP, SERVER_PORT))
    sock.settimeout(0)  # non-blocking receive
    print("Connected. Streaming frames.")
    rx_buf = b''
    seq = 0
    try:
        while True:
            t0 = time.ticks_ms()
            hall = read_hall_burst()
            analog_val = hall[-1]
            digital_val = hall_digital.value()
            mean_hall = sum(hall) // len(hall)
            diff = abs(mean_hall - hall_baseline)
            distance_cm = get_filtered_distance()

            if digital_val == 1:
                status = "danger_magnet"
            elif distance_cm < OBSTACLE_CM:
                status = "obstacle_close"
            elif diff > 2000:
                status = "warning_field_change"
            else:
                status = "safe"

            frame = {
                "fw": FW_VERSION, "seq": seq, "t": t0,
                "hall": hall, "hall_analog": analog_val,
                "hall_baseline": hall_baseline, "hall_diff": diff,
                "hall_digital": digital_val, "distance_cm": distance_cm,
                "motor": motor_cmd, "speed": motor_speed,
                "status": status, "ble": ble_snapshot(),
            }
            sock.sendall((json.dumps(frame) + "\n").encode())
            seq += 1

            # Drain any commands the server sent
            try:
                while True:
                    chunk = sock.recv(512)
                    if not chunk:
                        raise OSError("server closed connection")
                    rx_buf += chunk
            except OSError as e:
                if e.args and e.args[0] not in (11, 110):  # EAGAIN / ETIMEDOUT
                    raise
            while b'\n' in rx_buf:
                line, rx_buf = rx_buf.split(b'\n', 1)
                handle_command(line.decode().strip(), distance_cm)
            if len(rx_buf) > 256:
                rx_buf = b''

            # Safety: deadman + obstacle reflex while already moving forward
            if motor_cmd != 'S' and time.ticks_diff(time.ticks_ms(), last_drive_ms) > DEADMAN_MS:
                move_car('S')
            if motor_cmd == 'F' and distance_cm < OBSTACLE_CM:
                move_car('S')

            update_indicators(status)

            elapsed = time.ticks_diff(time.ticks_ms(), t0)
            if elapsed < FRAME_PERIOD_MS:
                time.sleep_ms(FRAME_PERIOD_MS - elapsed)
    finally:
        move_car('S')
        sock.close()

def start_robot():
    move_car('S')
    calibrate_hall_sensor()
    while True:
        try:
            connect_wifi()
            run_session()
        except Exception as e:
            print("Connection error:", e)
            move_car('S')
            calibration_state()
            time.sleep(2)  # retry

if __name__ == "__main__":
    start_robot()
