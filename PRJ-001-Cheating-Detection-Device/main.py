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

SERVER_IP = "192.168.1.2"
SERVER_PORT = 8080

# ==========================================
# 2. Hardware Pin Definitions
# ==========================================
# --- Indicators (LEDs & Buzzer) ---
buzzer = machine.Pin(10, machine.Pin.OUT)
red = machine.Pin(11, machine.Pin.OUT)
green = machine.Pin(12, machine.Pin.OUT)
yellow = machine.Pin(13, machine.Pin.OUT)

# --- Motor Driver (L298N) ---
ENA = machine.PWM(machine.Pin(16))
IN1 = machine.Pin(17, machine.Pin.OUT)
IN2 = machine.Pin(18, machine.Pin.OUT)

IN3 = machine.Pin(7, machine.Pin.OUT)
IN4 = machine.Pin(8, machine.Pin.OUT)
ENB = machine.PWM(machine.Pin(9))

ENA.freq(1000)
ENB.freq(1000)
DEFAULT_SPEED = 45000

# --- KY-024 Hall Effect Sensor ---
hall_analog = machine.ADC(26)  
hall_digital = machine.Pin(27, machine.Pin.IN)  
hall_baseline = 33500  

# --- HC-SR04 Ultrasonic Sensor ---
trig = machine.Pin(14, machine.Pin.OUT)  
echo = machine.Pin(15, machine.Pin.IN)   
last_valid_distance = 100.0  

# ==========================================
# 3. System State & Sensor Functions
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
    buzzer.value(1)

def calibrate_hall_sensor():
    global hall_baseline
    print("Calibrating KY-024 Hall Sensor... Keep magnets away.")
    calibration_state()
    total = 0
    for _ in range(10):
        total += hall_analog.read_u16()
        time.sleep(0.1)
    hall_baseline = int(total / 10)
    print(f"Calibration Done! Baseline: {hall_baseline}")

def get_filtered_distance():
    global last_valid_distance
    valid_readings = []
    
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
                valid_readings.append(dist)
        time.sleep_ms(10)
        
    if len(valid_readings) > 0:
        avg_dist = round(sum(valid_readings) / len(valid_readings), 1)
        last_valid_distance = avg_dist
        return avg_dist
    else:
        return last_valid_distance

# ==========================================
# 4. Motor Control Function
# ==========================================
def move_car(cmd, speed=DEFAULT_SPEED):
    ENA.duty_u16(speed)
    ENB.duty_u16(speed)
    
    if cmd == 'F':
        IN1.value(1); IN2.value(0)
        IN3.value(1); IN4.value(0)
    elif cmd == 'B':
        IN1.value(0); IN2.value(1)
        IN3.value(0); IN4.value(1)
    elif cmd == 'R':
        IN1.value(0); IN2.value(1)
        IN3.value(1); IN4.value(0)
    elif cmd == 'L':
        IN1.value(1); IN2.value(0)
        IN3.value(0); IN4.value(1)
    else:
        ENA.duty_u16(0)
        ENB.duty_u16(0)
        IN1.value(0); IN2.value(0)
        IN3.value(0); IN4.value(0)

# ==========================================
# 5. Bluetooth Radar (Background Scanner)
# ==========================================
ble = bluetooth.BLE()
ble.active(True)
scanned_devices = []

def decode_name(payload):
    i = 0
    while i < len(payload):
        length = payload[i]
        if length == 0: break
        adv_type = payload[i + 1]
        if adv_type == 0x09 or adv_type == 0x08:
            return bytes(payload[i + 2 : i + 1 + length]).decode('utf-8', 'ignore')
        i += 1 + length
    return "Unknown"

def ble_irq(event, data):
    if event == 5:
        addr_type, addr, adv_type, rssi, adv_data = data
        mac_address = ubinascii.hexlify(addr, ':').decode('utf-8')
        device_name = decode_name(adv_data)
        
        existing_device = next((d for d in scanned_devices if d['mac'] == mac_address), None)
        if existing_device:
            existing_device['rssi'] = rssi
            if device_name != "Unknown":
                existing_device['name'] = device_name
        else:
            scanned_devices.append({'mac': mac_address, 'name': device_name, 'rssi': rssi})

ble.irq(ble_irq)
ble.gap_scan(0, 30000, 30000)

# ==========================================
# 6. Wi-Fi Connection Function
# ==========================================
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    
    print(f"Connecting to {WIFI_SSID}...")
    calibration_state()
    
    while not wlan.isconnected():
        time.sleep(1)
        
    print(f"Wi-Fi Connected! IP: {wlan.ifconfig()[0]}")
    safe_state()

# ==========================================
# 7. Main Socket Client Loop
# ==========================================
def start_robot():
    calibrate_hall_sensor()
    connect_wifi()
    
    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print(f"Connecting to Laptop Server ({SERVER_IP}:{SERVER_PORT})...")
    
    try:
        client_sock.connect((SERVER_IP, SERVER_PORT))
        client_sock.settimeout(0.1) 
        print("Connected to Laptop successfully! Starting full sensor stream...")
        
        while True:
            # 1. Read Sensors
            analog_val = hall_analog.read_u16()
            digital_val = hall_digital.value()
            diff = abs(analog_val - hall_baseline)
            distance_cm = get_filtered_distance()
            
            # 2. Determine System Status
            if digital_val == 1:
                current_status = "danger_magnet"
                alert_state()  
            elif distance_cm < 15.0:
                current_status = "obstacle_close"
                calibration_state() 
            elif diff > 2000:
                current_status = "warning_field_change"
                calibration_state()  
            else:
                current_status = "safe"
                safe_state()  
                
            # 3. Package & Send Data
            sensor_data = {
                "distance_cm": distance_cm,
                "hall_analog": analog_val,
                "hall_diff": diff,
                "hall_digital": digital_val,
                "ble_devices": scanned_devices,
                "status": current_status
            }
            json_str = json.dumps(sensor_data)
            client_sock.send(json_str.encode('utf-8'))
            
            if len(scanned_devices) > 20:
                scanned_devices.clear()
            
            # 4. Receive Command & Move (With Auto-Stop Reflex)
            try:
                response = client_sock.recv(1024).decode('utf-8').strip()
                
                if response == 'F' and distance_cm < 15.0:
                    print("⚠️ AUTO-STOP: Obstacle too close! Ignoring Forward command.")
                    move_car('S')
                elif response in ['F', 'B', 'L', 'R', 'S']:
                    move_car(response)
            
            except OSError:
                
                pass
                
            
            time.sleep(0.1)
            
    except Exception as e:
        print(f"Connection Error: {e}")
        alert_state()
    finally:
        ble.gap_scan(None)
        client_sock.close()
        move_car('S')

# ==========================================
# 8. Start Execution
# ==========================================
if __name__ == "__main__":
    start_robot()


