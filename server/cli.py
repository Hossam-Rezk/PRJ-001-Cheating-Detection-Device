"""Command-line tool for the jobs that are not on the Monitor page.

Talks to the running server (server/run.py) over its REST API, so the car
must be connected for recording, calibration and trials.

    python server/cli.py status
    python server/cli.py calibrate
    python server/cli.py record earphones --seconds 20 --distance 5 --orientation facing --env exam_desk
    python server/cli.py dataset                 # class balance
    python server/cli.py dataset --synthetic     # add simulator demo data
    python server/cli.py export dataset.csv
    python server/cli.py train --no-synthetic
    python server/cli.py models
    python server/cli.py activate v20261028-165629
    python server/cli.py trial --present --condition "AirPods in pocket"
    python server/cli.py report report.json
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8000"


def call(path, method="GET", body=None, raw=False):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"error: {json.loads(e.read() or b'{}').get('detail', e.reason)}")
    except urllib.error.URLError:
        sys.exit(f"error: server not reachable at {BASE} (start it with: python server/run.py)")
    return data if raw else json.loads(data or b"{}")


def pct(v):
    return "–" if v is None else f"{v * 100:.1f}%"


def countdown(seconds, label):
    for left in range(int(seconds), 0, -1):
        print(f"\r  {label}: {left:3d} s left ", end="", flush=True)
        time.sleep(1)
    print()


def cmd_status(a):
    st = call("/api/state")
    m, c = st["model"], st["calibration"]
    print(f"car:         {'connected ' + str(st['peer']) if st['connected'] else 'offline'}")
    print(f"model:       {m['version'] + ' ' + m['algorithm'] + ' ' + pct(m['test_accuracy']) if m else 'none (magnet rule + Bluetooth only)'}")
    print(f"calibration: {'baseline ' + str(c['baseline']) + ', threshold +/-' + str(c['threshold']) if c else 'not calibrated'}")
    print(f"alerts:      {len(st['alerts'])} recent")


def cmd_calibrate(a):
    call("/api/calibrate", "POST", {"seconds": a.seconds, "k": a.k, "note": a.note})
    print("Calibrating: keep magnets, phones and metal away from the car.")
    countdown(a.seconds + 2, "calibrating")
    c = call("/api/state")["calibration"]
    print(f"baseline {c['baseline']}, noise {c['frame_mean_std']}, threshold +/-{c['threshold']}")


def cmd_record(a):
    s = call("/api/dataset/record/start", "POST", {"label": a.label, "target_distance_cm": a.distance,
                                                  "orientation": a.orientation, "environment": a.env, "notes": a.notes})
    print(f"Recording session {s['id']} ({a.label}). Hold the condition still. Ctrl+C stops early.")
    try:
        countdown(a.seconds, "recording")
    except KeyboardInterrupt:
        print()
    s = call("/api/dataset/record/stop", "POST")
    print(f"saved {s.get('frames', 0)} frames")


def cmd_dataset(a):
    if a.synthetic:
        print(f"added {call('/api/dataset/synthetic', 'POST', {})['created_sessions']} synthetic sessions")
    if a.delete_synthetic:
        call("/api/dataset/synthetic", "DELETE")
        print("removed synthetic sessions")
    st = call("/api/dataset")["stats"]
    print(f"{'class':<16}{'sessions':>9}{'frames':>8}{'real':>8}")
    for label, b in st["by_label"].items():
        print(f"{label:<16}{b['sessions']:>9}{b['frames']:>8}{b['real_frames']:>8}")
    print(f"total: {st['total_sessions']} sessions, {st['total_frames']} frames ({st['synthetic_sessions']} synthetic sessions)")


def cmd_export(a):
    q = "" if a.synthetic else "?include_synthetic=false"
    with open(a.file, "wb") as fh:
        fh.write(call("/api/dataset/export.csv" + q, raw=True))
    print(f"wrote {a.file}")


def cmd_train(a):
    print("Training (compares 4 algorithms, about 10-40 s)...")
    m = call("/api/models/train", "POST", {"include_synthetic": a.synthetic})
    t, cv = m.get("target", {}), m.get("cross_validation", {})
    print(f"model {m['version']} ({m['algorithm']}) is now active")
    print(f"  test accuracy   {pct(m['test_accuracy'])}  (requirement 80%: {'pass' if m['meets_requirement'] else 'FAIL'})")
    print(f"  false positives {pct(t.get('fpr'))}   earphone recall {pct(t.get('recall'))}")
    print(f"  cross-val       {pct(cv.get('mean'))} ± {pct(cv.get('std'))}")
    if m["synthetic_share"] > 0:
        print(f"  note: {m['synthetic_share'] * 100:.0f}% synthetic data, not a real-world result")


def cmd_models(a):
    for m in call("/api/models")["models"]:
        print(f"{'*' if m['active'] else ' '} {m['version']}  {m['algorithm']:<20} test {pct(m['test_accuracy']):>6}  "
              f"FPR {pct(m['fpr']):>6}  synthetic {m['synthetic_share'] * 100:.0f}%")


def cmd_activate(a):
    m = call(f"/api/models/{a.version}/activate", "POST")
    print(f"active: {m['version']} ({m['algorithm']})")


def cmd_trial(a):
    t = call("/api/evaluation/start", "POST", {"target_present": a.present, "distance_cm": a.distance,
                                              "condition": a.condition})
    print(f"Trial #{t['id']} started ({'target present' if a.present else 'no target'}). "
          "Drive the car past the desk, then press Enter.")
    try:
        input()
    except (KeyboardInterrupt, EOFError):
        pass
    t = call("/api/evaluation/stop", "POST")
    verdict = "correct" if t["correct"] else ("missed" if t["ground_truth"] else "false alarm")
    print(f"result: {verdict}" + (f", first alert after {t['first_alert_s']} s" if t["first_alert_s"] is not None else ""))


def cmd_report(a):
    e = call("/api/evaluation")
    print(f"{'requirement':<36}{'target':<14}{'measured':<16}status")
    for r in e["requirements"]:
        val = "–" if r["value"] is None else f"{r['value']} {r['unit']}"
        print(f"{r['name']:<36}{r['target']:<14}{val:<16}{r['status']}")
    c = e["confusion"]
    print(f"trials: {len(e['trials'])}  (TP {c['tp']}, FN {c['fn']}, FP {c['fp']}, TN {c['tn']})")
    if a.file:
        with open(a.file, "wb") as fh:
            fh.write(call("/api/evaluation/report.json", raw=True))
        print(f"wrote {a.file}")


def main():
    p = argparse.ArgumentParser(description="ExamGuard Rover command-line tool")
    p.add_argument("--server", default="http://localhost:8000", help="dashboard address (default %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="car, model and calibration status")
    c = sub.add_parser("calibrate", help="calibrate the car's magnetic sensor")
    c.add_argument("--seconds", type=float, default=4); c.add_argument("--k", type=float, default=5)
    c.add_argument("--note", default="")
    r = sub.add_parser("record", help="record one labelled session")
    r.add_argument("label", choices=["clear", "earphones", "other_magnetic"])
    r.add_argument("--seconds", type=int, default=20); r.add_argument("--distance", type=float, default=0)
    r.add_argument("--orientation", default="n/a", choices=["facing", "side", "flipped", "inside_case", "n/a"])
    r.add_argument("--env", default="exam_desk", choices=["lab_bench", "exam_desk", "metal_desk", "near_laptop", "floor", "other"])
    r.add_argument("--notes", default="")
    d = sub.add_parser("dataset", help="show class balance; add or remove synthetic data")
    d.add_argument("--synthetic", action="store_true"); d.add_argument("--delete-synthetic", action="store_true")
    x = sub.add_parser("export", help="export features as CSV")
    x.add_argument("file"); x.add_argument("--no-synthetic", dest="synthetic", action="store_false")
    t = sub.add_parser("train", help="train, evaluate and activate a model")
    t.add_argument("--no-synthetic", dest="synthetic", action="store_false")
    sub.add_parser("models", help="list model versions")
    v = sub.add_parser("activate", help="switch to another model version"); v.add_argument("version")
    tr = sub.add_parser("trial", help="run one evaluation trial")
    g = tr.add_mutually_exclusive_group(required=True)
    g.add_argument("--present", action="store_true"); g.add_argument("--absent", dest="present", action="store_false")
    tr.add_argument("--distance", type=float, default=None); tr.add_argument("--condition", default="")
    rp = sub.add_parser("report", help="requirements table; optionally save the full JSON report")
    rp.add_argument("file", nargs="?")
    a = p.parse_args()
    global BASE
    BASE = a.server.rstrip("/")
    globals()["cmd_" + a.cmd](a)


if __name__ == "__main__":
    main()
