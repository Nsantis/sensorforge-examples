#!/usr/bin/env python3
"""Generate a synthetic SensorForge log for trying out the analysis notebook.

The folder layout, file names, columns and session.json follow exactly what the
app writes (see DESIGN-Logger.md). Two sensors are logged side by side:

  * Polar H10 (chest)          hr, rr, ecg (130 Hz), acc (50 Hz)
  * Polar Verity Sense (arm)   ppg (55 Hz, 3 ch + ambient), acc (52 Hz), ppi

Four minutes: rest -> activity -> recovery, one marker per phase, one short
Verity Sense dropout, and a sensor clock that is off by ~1.8 s on the Verity
so the notebook's clock alignment has something to do.

    python3 make_sample_log.py [output_dir]
"""

import csv
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

POLAR_EPOCH_UNIX_S = 946_684_800
RNG = np.random.default_rng(7)

START = datetime(2026, 10, 30, 14, 5, 12, tzinfo=timezone.utc)
DURATION_S = 240
H10 = "A1B2C3D4"
VERITY = "0E1F2A3B"

# Sensor clock = phone clock + offset (ms). Positive offset: sensor behind.
CLOCK_OFFSET_MS = {H10: 240.0, VERITY: 1830.0}
BLE_DELAY_MS = (15, 60)
DROPOUT = (150.0, 156.0)  # Verity Sense disconnected [s from start]


def unix_ms(t_s):
    return START.timestamp() * 1000 + t_s * 1000


def sensor_ns(t_s, device):
    """Sensor timestamp (ns since 2000-01-01) for phone-clock time t_s."""
    phone_unix_ms = unix_ms(t_s)
    sensor_unix_ms = phone_unix_ms - CLOCK_OFFSET_MS[device]
    return int(round((sensor_unix_ms / 1000 - POLAR_EPOCH_UNIX_S) * 1e9))


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def hr_profile(t):
    """Target heart rate: rest 62, activity ramp to ~128, recovery."""
    if t < 90:
        return 62 + 2 * np.sin(t / 9)
    if t < 180:
        return 62 + 66 * (1 - np.exp(-(t - 90) / 18))
    return 70 + 58 * np.exp(-(t - 180) / 25)


def beat_times():
    """R-peak times with respiratory sinus arrhythmia and a little noise."""
    t, beats = 0.4, []
    while t < DURATION_S:
        beats.append(t)
        rr = 60 / hr_profile(t)
        rr *= 1 + 0.045 * np.sin(2 * np.pi * t / 4.5) * (1 if t < 90 else 0.3)
        rr += RNG.normal(0, 0.008)
        t += rr
    beats = np.array(beats)
    return beats


def in_dropout(t):
    return DROPOUT[0] <= t < DROPOUT[1]


class Log:
    def __init__(self, root):
        self.dir = root / f"SF_{START:%Y%m%d-%H%M%S}_2-sensors"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.streams = []

    def write(self, file, device, signal, columns, rows, unit=None, hz=None, settings=None):
        with open(self.dir / file, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(columns)
            w.writerows(rows)
        self.streams.append({
            "file": file, "deviceId": device, "signal": signal, "columns": columns,
            "unit": unit, "sampleRateHz": hz, "settings": settings or {}, "rowCount": len(rows),
        })


def framed(times, device, per_frame, values_fn):
    """Group samples into BLE frames; every row of a frame shares rx_unix_ms."""
    rows, frame = [], 0
    for i in range(0, len(times), per_frame):
        chunk = times[i:i + per_frame]
        if in_dropout(chunk[-1]) and device == VERITY:
            continue
        rx = round(unix_ms(chunk[-1]) + RNG.uniform(*BLE_DELAY_MS))
        for t in chunk:
            rows.append([sensor_ns(t, device), frame, rx, *values_fn(t)])
        frame += 1
    return rows


def ecg_wave(beats):
    def value(t):
        k = np.searchsorted(beats, t)
        v = 0.0
        for b in beats[max(k - 1, 0):k + 1]:
            d = t - b
            v += 1150 * np.exp(-(d / 0.012) ** 2)            # R
            v -= 180 * np.exp(-((d + 0.035) / 0.010) ** 2)   # Q
            v -= 260 * np.exp(-((d - 0.035) / 0.012) ** 2)   # S
            v += 260 * np.exp(-((d - 0.26) / 0.045) ** 2)    # T
            v += 90 * np.exp(-((d + 0.17) / 0.025) ** 2)     # P
        v += 60 * np.sin(2 * np.pi * t / 4.5)                # baseline wander
        v += RNG.normal(0, 18)
        return [int(round(v))]
    return value


def motion(t):
    """Activity 90-180 s: ~2.6 Hz cadence."""
    return 1.0 if 90 <= t < 180 else 0.0


def acc_values(placement):
    def value(t):
        m = motion(t)
        c = 2 * np.pi * 2.6 * t
        if placement == "chest":
            base = (30, -990, 80)
        else:
            base = (-640, 120, 740)
        x = base[0] + m * 250 * np.sin(c) + RNG.normal(0, 8)
        y = base[1] + m * 420 * np.sin(c + 0.6) + RNG.normal(0, 8)
        z = base[2] + m * 180 * np.sin(2 * c) + RNG.normal(0, 8)
        return [int(round(x)), int(round(y)), int(round(z))]
    return value


def ppg_values(beats):
    def value(t):
        k = np.searchsorted(beats, t) - 1
        phase = 0.0 if k < 0 else (t - beats[k] - 0.22)  # pulse arrives ~220 ms after R
        pulse = np.exp(-(phase / 0.09) ** 2) + 0.35 * np.exp(-((phase - 0.28) / 0.12) ** 2)
        resp = np.sin(2 * np.pi * t / 4.5)
        noise = motion(t) * 9000 * np.sin(2 * np.pi * 2.6 * t)
        chans = [
            int(260_000 - 14_000 * pulse + 2_500 * resp + noise + RNG.normal(0, 400)),
            int(248_000 - 12_500 * pulse + 2_300 * resp + noise + RNG.normal(0, 400)),
            int(255_000 - 13_000 * pulse + 2_400 * resp + noise + RNG.normal(0, 400)),
            int(4_100 + RNG.normal(0, 60)),
        ]
        return ["ppg3_ambient1", *chans]
    return value


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "sample_log"
    log = Log(out)
    beats = beat_times()
    rr_ms = np.diff(beats) * 1000

    # H10 HR + RR: one notification per second, the RR intervals completed in it.
    hr_rows, rr_rows = [], []
    for frame, sec in enumerate(range(1, DURATION_S)):
        rx = round(unix_ms(sec) + RNG.uniform(*BLE_DELAY_MS))
        done = [rr for b, rr in zip(beats[1:], rr_ms) if sec - 1 <= b < sec]
        hr = int(round(hr_profile(sec)))
        hr_rows.append([rx, frame, hr, 0, 0, int(bool(done)), 1, 1])
        rr_rows += [[rx, frame, int(round(rr))] for rr in done]
    log.write(f"{H10}_hr.csv", H10, "hr", ["rx_unix_ms", "frame", "hr_bpm", "corrected_hr_bpm",
              "ppg_quality", "rr_available", "contact", "contact_supported"], hr_rows, "bpm", 1)
    log.write(f"{H10}_rr.csv", H10, "rr", ["rx_unix_ms", "frame", "rr_ms"], rr_rows, "ms")

    ts = ["sensor_ts_ns", "frame", "rx_unix_ms"]
    ecg_t = np.arange(0, DURATION_S, 1 / 130)
    log.write(f"{H10}_ecg.csv", H10, "ecg", ts + ["ecg_uv"], framed(ecg_t, H10, 73, ecg_wave(beats)),
              "µV", 130, {"sampleRate": "130", "resolution": "14"})
    acc_t = np.arange(0, DURATION_S, 1 / 50)
    log.write(f"{H10}_acc.csv", H10, "acc", ts + ["x_mg", "y_mg", "z_mg"],
              framed(acc_t, H10, 36, acc_values("chest")), "mG", 50,
              {"sampleRate": "50", "resolution": "16", "range": "8"})

    ppg_t = np.arange(0, DURATION_S, 1 / 55)
    log.write(f"{VERITY}_ppg.csv", VERITY, "ppg", ts + ["frame_type", "ch0", "ch1", "ch2", "ch3"],
              framed(ppg_t, VERITY, 35, ppg_values(beats)), "ADC", 55,
              {"sampleRate": "55", "resolution": "22", "channels": "4", "frameType": "ppg3_ambient1"})
    vacc_t = np.arange(0, DURATION_S, 1 / 52)
    log.write(f"{VERITY}_acc.csv", VERITY, "acc", ts + ["x_mg", "y_mg", "z_mg"],
              framed(vacc_t, VERITY, 36, acc_values("arm")), "mG", 52,
              {"sampleRate": "52", "resolution": "16", "range": "8"})

    # PPI: starts ~25 s in, blocker set during activity (motion), one row per beat.
    ppi_rows, frame = [], 0
    for b, rr in zip(beats[1:], rr_ms):
        if b < 25 or in_dropout(b):
            continue
        blocker = int(90 <= b < 180 and RNG.random() < 0.55)
        ppi = rr + RNG.normal(0, 6) + (RNG.normal(0, 120) if blocker else 0)
        rx = round(unix_ms(b + 0.4) + RNG.uniform(*BLE_DELAY_MS))
        ppi_rows.append([sensor_ns(b, VERITY), frame, rx, int(round(ppi)), int(8 + 12 * blocker),
                         int(round(60000 / ppi)), blocker, 1, 1])
        frame += 1
    log.write(f"{VERITY}_ppi.csv", VERITY, "ppi", ts + ["ppi_ms", "error_estimate_ms", "hr_bpm",
              "blocker", "skin_contact", "skin_contact_supported"], ppi_rows, "ms")

    markers = [[round(unix_ms(t)), label] for t, label in
               [(5, "Rest"), (90, "Activity"), (180, "Recovery"), (152, "Artifact")]]
    markers.sort()
    log.write("markers.csv", None, "markers", ["unix_time_ms", "label"], markers)

    end = START + timedelta(seconds=DURATION_S)
    phone = {"batteryLevel": 0.81, "batteryState": "unplugged", "lowPowerMode": False,
             "thermalState": "nominal", "freeDiskBytes": 48_211_000_000, "locale": "fi_FI"}
    manifest = {
        "formatVersion": 1, "id": str(uuid.UUID(int=int(RNG.integers(2**63)))).upper(),
        "status": "complete", "source": "online",
        "startedAt": iso(START), "endedAt": iso(end), "timeZone": "Europe/Helsinki",
        "polarEpochUnixSeconds": POLAR_EPOCH_UNIX_S,
        "devices": [
            {"id": H10, "name": f"Polar H10 {H10}", "model": "H10", "firmwareVersion": "5.0.0",
             "hardwareVersion": "39044024.02", "batteryAtStart": 87, "batteryAtEnd": 86,
             "timeSyncedAt": iso(START - timedelta(seconds=20)), "clockOffsetMs": CLOCK_OFFSET_MS[H10] + 15,
             "information": {"Manufacturer Name": "Polar Electro Oy", "Model Number": "H10"}},
            {"id": VERITY, "name": f"Polar Sense {VERITY}", "model": "Polar Sense",
             "firmwareVersion": "2.1.0", "hardwareVersion": "00184440.03", "batteryAtStart": 64,
             "batteryAtEnd": 61, "timeSyncedAt": iso(START - timedelta(seconds=18)),
             "clockOffsetMs": CLOCK_OFFSET_MS[VERITY] + 15,
             "information": {"Manufacturer Name": "Polar Electro Oy", "Model Number": "Polar Sense"}},
        ],
        "host": {"appVersion": "1.0", "appBuild": "1", "polarSdkVersion": "8.5.0",
                 "deviceModel": "iPhone16,1", "systemVersion": "26.1"},
        "context": {"studyId": "DEMO", "subjectId": "S01", "activity": "Rest – stairs – recovery",
                    "subject": {"ageYears": 34, "heightCm": 180, "weightKg": 76, "sex": "male",
                                "fitzpatrickSkinType": 2},
                    "placements": {H10: "chest", VERITY: "upper_arm_left"},
                    "customFields": [{"key": "room_temp_c", "value": "21"}]},
        "environmentAtStart": {"capturedAt": iso(START), "phone": phone, "notes": ["location off"]},
        "environmentAtEnd": {"capturedAt": iso(end), "phone": {**phone, "batteryLevel": 0.79},
                             "notes": ["location off"]},
        "streams": log.streams,
        "events": [
            {"at": iso(START + timedelta(seconds=DROPOUT[0])), "kind": "disconnected", "deviceId": VERITY},
            {"at": iso(START + timedelta(seconds=DROPOUT[1])), "kind": "reconnected", "deviceId": VERITY},
        ],
        "notes": "Synthetic sample generated by make_sample_log.py",
        "tags": ["sample"],
    }
    with open(log.dir / "session.json", "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, ensure_ascii=False)
    print(log.dir)


if __name__ == "__main__":
    main()
