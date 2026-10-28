# PRJ-001 Cheating Detection Device: ExamGuard Rover

A remote-controlled car that drives along exam desks and flags a **possible** earphone (AirPods, earbuds), with an
estimate of how far it is from the car. All sensors sit on the car; a laptop runs the detection, the machine
learning model and a web dashboard.

Alerts are indications, not proof. The system never identifies or accuses a student; the supervisor decides what
to check.

## How it works

```
 Car (on board)                         Laptop: server/run.py                     Browser
 ───────────────                        ──────────────────────                    ───────
 Pico 2 W controller   readings  ──▶    Car link (TCP 8080)                       Monitor page
 KY-024 hall sensor    ~10 / s          → 16 features → ML model        live ──▶  (http://localhost:8000)
 HC-SR04 ultrasonic                     → fusion + alerts               data
 Bluetooth scanner     ◀── commands     → location estimate             ◀── drive / calibrate
 Motors, LEDs, buzzer                   → saved data and models
```

1. **Car:** the Pico reads the hall sensor (8 samples per frame), the ultrasonic sensor and nearby Bluetooth
   adverts, and streams a JSON frame about 10 times a second over Wi-Fi. It executes drive commands and stops by
   itself if commands stop for 0.6 s or an obstacle is closer than 15 cm.
2. **Features:** each frame becomes 16 features: magnetic deviation and its trend over a 1 s window, Bluetooth
   audio devices (AirPods are recognised from Apple's proximity-pairing adverts, other earbuds from their
   Bluetooth appearance or name) and their signal strength, and whether the motors are running.
3. **Model:** a classifier predicts `clear`, `earphones` or `other_magnetic`. The third class teaches it what is
   *not* a target (phones, magnets, metal).
4. **Fusion:** the earphone probability, a calibrated magnetic threshold rule and Bluetooth proximity combine into
   one score. An alert is raised after 3 frames above the threshold and cleared after 10 below it.
5. **Location:** positions are in metres from where the car started (no room size or seat layout needed). An
   alert reports the device's distance from the car: from Bluetooth signal strength when an audio device is heard
   (about ±0.5–1 m), otherwise "within ~10 cm" (the hall sensor's range). Driving in two directions also gives a
   Bluetooth position fix on the map.

## Hardware (all on the car)

| Part | Role | Pico pins |
| --- | --- | --- |
| Raspberry Pi Pico 2 W | Controller, Wi-Fi, Bluetooth scanner | — |
| KY-024 hall sensor | Magnetic field | GP26 analog, GP27 digital |
| HC-SR04 ultrasonic | Obstacle distance | GP14 trigger, GP15 echo |
| L298N motor driver | Left motors / right motors | GP16 ENA, GP17 IN1, GP18 IN2 / GP9 ENB, GP7 IN3, GP8 IN4 |
| LEDs | Green safe, yellow calibrating, red alert | GP12, GP13, GP11 |
| Buzzer | Off by default (silent exam mode) | GP10 |

Mount the hall sensor at the front and low: it only reacts within a few centimetres.

## Repository layout

```
firmware/main.py        MicroPython firmware for the Pico 2 W
server/run.py           starts the car link (port 8080) and the dashboard (port 8000)
server/cli.py           command-line tool: record, train, evaluate, report
server/app/             features, model training, fusion, location, dataset, simulator, API
server/static/          the Monitor page (no external libraries, works offline)
data/                   created at run time: recordings, models, calibrations, trials (not committed)
```

## Setup

**Laptop** (Windows, Python 3.12+):

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Allow Python through Windows Firewall on private networks so the car can reach port 8080.

**Car:**

1. Flash MicroPython for the Pico 2 W (`RPI_PICO2_W` build).
2. In `firmware/main.py`, set `WIFI_SSID`, `WIFI_PASSWORD` and `SERVER_IP` (the laptop's address; the server
   prints it at start-up). The Wi-Fi must be 2.4 GHz and shared with the laptop.
3. Copy `firmware/main.py` to the Pico as `main.py` (for example with Thonny).

## Running

```bash
.venv\Scripts\python server\run.py          # real car
.venv\Scripts\python server\run.py --sim    # virtual car in a simulated room, no hardware needed
```

Open http://localhost:8000, then switch on the car. The top bar shows **Car connected** and the update rate.

The **Monitor** page has the detection status, the car's sensor readings, the drive pad (W A S D or arrows,
Space stops), the map, alerts with *Device found / False alarm* feedback, live charts and Bluetooth devices, and
a **Calibration & settings** panel.

## Command-line tool

Run while the server is running. Each command starts with `.venv\Scripts\python server\cli.py`.

| Command | What it does |
| --- | --- |
| `status` | Car, model and calibration status |
| `calibrate` | Calibrate the hall sensor (car parked away from magnets) |
| `record earphones --seconds 20 --distance 5 --orientation facing --env exam_desk` | Record one labelled session (`clear`, `earphones`, `other_magnetic`) |
| `dataset` / `dataset --synthetic` / `dataset --delete-synthetic` | Class balance / add or remove simulator demo data |
| `export data.csv` | Export the features as CSV |
| `train --no-synthetic` | Train, evaluate and activate a model |
| `models` / `activate <version>` | List model versions / switch version |
| `trial --present` / `trial --absent` | Run one evaluation trial; press Enter when done |
| `report [report.json]` | Requirements table; optionally save the full report |

## Workflow

1. **Calibrate** before each exam.
2. **Record** short sessions per class across distances (2–20 cm), orientations and desks.
3. **Train** with `train --no-synthetic` once real data exists.
4. **Monitor** during the exam and mark each alert.
5. **Evaluate** with present and absent trials, then `report`.

## Machine learning

- **Split by session:** whole recording sessions go to train / validation / test (70 / 15 / 15 per class), so
  near-identical frames never appear on both sides.
- **Model selection:** random forest, gradient boosting, logistic regression and a depth-5 decision tree are
  compared; the best on validation is activated and only its held-out test result is reported.
- **Threshold:** the alert probability threshold is tuned on validation with a false-positive cap of 10%.
- **Reported:** accuracy, macro F1, per-class precision and recall, confusion matrix, ROC and AUC, 5-fold grouped
  cross-validation, permutation feature importance, learning curve and accuracy by distance, orientation and
  environment.
- **On the Pico:** a depth-4 decision tree is exported as MicroPython (`data/models/<version>/pico_model.py`).

### Current results (synthetic data only)

These come from the built-in simulator (90 sessions, 5,400 frames, 1,800 per class), not from real recordings.
They show the pipeline works; re-measure with the real car before reporting.

| Metric | Result | Requirement |
| --- | --- | --- |
| Model accuracy, held-out test | 92.0% | ≥ 80% |
| Earphones: false positive rate | 0.0% | ≤ 10% |
| 5-fold cross-validation | 98.5% ± 1.1% | — |
| Alert response time, 95th percentile | 0.81 s | ≤ 2 s |
| Sensor update rate | about 8 Hz | ≥ 1 Hz |

The only test errors were magnets 8 cm from the sensor read as `clear`: the magnetic signal is near noise at
that distance.

## Car ↔ laptop protocol

Car → laptop, one JSON object per line:

```json
{"fw":"2.0","seq":12,"t":51234,"hall":[33512,33498,...],"hall_baseline":33500,"hall_digital":0,
 "distance_cm":84.2,"motor":"S","speed":0,"status":"safe",
 "ble":[{"mac":"aa:bb:...","name":"Unknown","rssi":-58,"mfg":"4c0007190114...","app":-1,"age":120}]}
```

Laptop → car, one command per line: `F|B|L|R|S [speed%]` drive, `C` recalibrate, `A 0|1` alert LED,
`Z 0|1` buzzer. The server also accepts the original firmware's format (single-letter commands).

## Known limits

- The hall sensor reacts only within about 10 cm; farther earphones are found through Bluetooth, which misses
  earbuds that are connected and not advertising.
- Position is dead-reckoned from drive commands (no wheel encoders) and drifts; re-anchor the car on the map.
- Bluetooth distance is rough (about ±0.5–1 m).
