#!/usr/bin/env python3
"""gpu-furnace heatd — thermostat-controlled GPU heat plant.

Reads room temperature (POSTed by an ESP32 or polled from a sensor),
runs hysteresis against a setpoint, and starts/stops loop workloads to
match heat demand. Holds everything off while an inference lock is set.

Python standard library only with JSON configuration. HTTP API on :8787.
"""

import argparse
import hmac
import http.server
import json
import math
import os
from pathlib import Path
import secrets
import signal
import subprocess
import tempfile
import threading
import time
import urllib.parse

try:
    import yaml
except ImportError:  # YAML is optional; JSON uses only the standard library
    yaml = None

CONFIG_PATH = os.environ.get(
    "FURNACE_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "workloads.example.json"),
)
PORT = int(os.environ.get("FURNACE_PORT", "8787"))
SENSOR_STALE_S = 120  # ignore sensor readings older than this
KILL_TIMEOUT_S = 30  # SIGTERM -> wait -> SIGKILL
CONTROL_TOKEN = ""  # initialized at startup, never embedded in source
ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "setpoint_f": 70.0,
    "deadband_f": 1.5,
    "poll_interval_s": 20,
    "max_watts": 600.0,
    # Built-in sensor: motherboard SYSTIN thermistor (needs nct6775 module).
    # room_f = systin_f - offset_f. Readings are only trusted while the
    # rig is idle (total GPU watts < idle_watts_max); during a burn the
    # case sensor reads case air, not room air.
    "sensor_mode": "external",  # auto | systin | external | off
    "systin_calibrated": False,
    "offset_f": 0.0,  # case bias at idle; refined by auto-cal
    "idle_watts_max": 120.0,
    "auto_calibrate": False,  # trust the pre-dawn minimum once nightly
    "night_min_target_f": 68.0,  # expected room at coldest hour
    "gpu_edge_bias_f": 4.0,  # idle GPU edge sits ~4F above intake air
    "max_gpu_temp_c": 85.0,  # hard thermal guard: bank everything above
    "reheat_temp_c": 75.0,  # ...and don't relight until it cools below
    "heat_mode": "bench",  # which fuel full heat burns: fold|bench|forge
}


def find_systin():
    """Locate the SYSTIN input on an nct67xx hwmon chip. Returns path or None."""
    import glob

    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            with open(os.path.join(h, "name")) as f:
                if not f.read().strip().startswith("nct"):
                    continue
            for lab_path in glob.glob(os.path.join(h, "temp*_label")):
                with open(lab_path) as f:
                    if f.read().strip().upper() == "SYSTIN":
                        return lab_path.replace("_label", "_input")
        except OSError:
            continue
    return None


def log(msg):
    print(f"[heatd {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- config


def load_config(path):
    """JSON needs only Python; existing YAML installs can opt into PyYAML."""
    with open(path) as f:
        if str(path).lower().endswith((".yaml", ".yml")):
            if yaml is None:
                raise ValueError(
                    "YAML requires PyYAML; use the JSON example for zero dependencies"
                )
            try:
                cfg = yaml.safe_load(f)
            except yaml.YAMLError:
                raise ValueError("invalid YAML configuration") from None
        else:
            cfg = json.load(f)
    validate_config(cfg)
    return cfg


def finite(value, low, high, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return number


def validate_config(cfg):
    if not isinstance(cfg, dict) or not isinstance(cfg.get("settings", {}), dict):
        raise ValueError("config must contain a settings object and workloads list")
    settings = {**DEFAULTS, **cfg.get("settings", {})}
    for name, low, high in [
        ("setpoint_f", 45, 95),
        ("deadband_f", 0.1, 20),
        ("poll_interval_s", 1, 300),
        ("max_watts", 1, 100000),
        ("offset_f", -60, 60),
        ("idle_watts_max", 0, 100000),
        ("max_gpu_temp_c", 30, 120),
        ("reheat_temp_c", 0, 119),
        ("night_min_target_f", 30, 100),
        ("gpu_edge_bias_f", -30, 60),
    ]:
        settings[name] = finite(settings[name], low, high, name)
    if settings["reheat_temp_c"] >= settings["max_gpu_temp_c"]:
        raise ValueError("reheat_temp_c must be below max_gpu_temp_c")
    if settings["sensor_mode"] not in ("auto", "systin", "external", "off"):
        raise ValueError("sensor_mode must be auto, systin, external, or off")
    if settings["heat_mode"] not in ("fold", "bench", "forge"):
        raise ValueError("heat_mode must be fold, bench, or forge")
    for field in ("auto_calibrate", "systin_calibrated"):
        if not isinstance(settings[field], bool):
            raise ValueError(f"{field} must be true or false")
    specs = cfg.get("workloads")
    if not isinstance(specs, list):
        raise ValueError("workloads must be a list")
    names = set()
    for spec in specs:
        if (
            not isinstance(spec, dict)
            or not isinstance(spec.get("name"), str)
            or not spec["name"].strip()
        ):
            raise ValueError("each workload needs a name")
        if spec["name"] in names:
            raise ValueError("workload names must be unique")
        names.add(spec["name"])
        if bool(spec.get("unit")) == bool(spec.get("cmd")):
            raise ValueError(f"{spec['name']}: specify exactly one of cmd or unit")
        for field in ("cmd", "unit", "cleanup"):
            if field in spec and (
                not isinstance(spec[field], str) or not spec[field].strip()
            ):
                raise ValueError(f"{spec['name']}: {field} must be a nonempty string")
        if spec.get("unit", "").startswith("-"):
            raise ValueError("unit cannot start with a dash")
        for field in ("enabled", "unit_user", "sudo"):
            if field in spec and not isinstance(spec[field], bool):
                raise ValueError(f"{field} must be true or false")
        if spec.get("unit_user") and spec.get("sudo"):
            raise ValueError("unit_user and sudo cannot both be true")
        if spec.get("fuel", "any") not in ("any", "fold", "bench", "forge"):
            raise ValueError("fuel must be any, fold, bench, or forge")
        finite(spec.get("watts", 150), 1, 100000, "workload watts")
    cfg["settings"] = settings


def load_control_token(path):
    """Create an owner-only token on first use. Never print its contents."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    token = path.read_text().strip()
    if len(token) < 24 or any(c.isspace() for c in token):
        raise ValueError(
            "control token must have at least 24 characters and no whitespace"
        )
    if path.stat().st_mode & 0o077:
        raise ValueError(
            "control token file must be owner-only: chmod 600 <token-file>"
        )
    return token


# ---------------------------------------------------------------- power


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _amd_hwmon():
    import glob

    paths = set()
    for item in glob.glob("/sys/class/drm/card[0-9]*/device/hwmon/*"):
        path = Path(item).resolve()
        try:
            if (path / "name").read_text().strip() == "amdgpu":
                paths.add(path)
        except OSError:
            pass
    return sorted(paths)


def _nvidia_metrics():
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=power.draw,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return [
            tuple(float(v.strip()) for v in line.split(",")[:2])
            for line in result.stdout.splitlines()
            if line.strip()
        ]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def measure_gpu_temps_c():
    """Edge/core readings for the optional, explicitly calibrated estimate."""
    temps = [row[1] for row in _nvidia_metrics()]
    for path in _amd_hwmon():
        value = _read_int(path / "temp1_input")
        if value is not None:
            temps.append(value / 1000.0)
    return temps or None


def measure_watts():
    """Sum NVIDIA + AMD board telemetry; guard on the hottest known sensor.

    Return unavailable if any discovered GPU lacks power/temperature data.
    These are board readings, not a wall-power measurement.
    """
    watts, temps = [], []
    nvidia = _nvidia_metrics()
    if not nvidia:
        for vendor in Path("/sys/class/drm").glob("card[0-9]*/device/vendor"):
            try:
                if vendor.read_text().strip().lower() == "0x10de":
                    return None, None
            except OSError:
                pass
    for w, t in nvidia:
        watts.append(w)
        temps.append(t)
    for path in _amd_hwmon():
        value = _read_int(path / "power1_average")
        if value is None:
            value = _read_int(path / "power1_input")
        card_temps = [_read_int(p) for p in path.glob("temp*_input")]
        card_temps = [v / 1000.0 for v in card_temps if v is not None]
        if value is None or not card_temps:
            return None, None
        watts.append(value / 1e6)
        temps.extend(card_temps)
    if (
        not watts
        or not temps
        or not all(math.isfinite(v) for v in watts + temps)
        or any(w < 0 for w in watts)
        or any(not -20 < t < 180 for t in temps)
    ):
        return None, None
    return sum(watts), max(temps)


# ---------------------------------------------------------------- furnace


class Workload:
    def __init__(self, spec, index):
        self.name = spec["name"]
        self.index = index
        self.watts = float(spec.get("watts", 150))
        self.fuel = spec.get("fuel", "any")  # fold | bench | forge | any
        self.unit = spec.get("unit")  # systemd unit, preferred
        self.unit_user = bool(spec.get("unit_user", False))  # --user scope
        self.sudo = bool(spec.get("sudo", False))  # escalate: sudo -n systemctl
        self.cmd = spec.get("cmd")  # or inline command
        self.cleanup = spec.get("cleanup")  # shell run after stop (stray procs)
        self.enabled = bool(spec.get("enabled", True))
        self.proc = None
        self.restart_timer = 0.0  # backoff after crash

    def _systemctl(self, verb):
        """systemctl for this unit: system, --user, or via sudo (NOPASSWD)."""
        base = ["systemctl"]
        if self.unit_user:
            base.append("--user")
        elif self.sudo:
            base = ["sudo", "-n"] + base
        return subprocess.run(
            base + [verb, self.unit], capture_output=True, timeout=KILL_TIMEOUT_S
        )

    @property
    def running(self):
        if self.unit:
            base = (
                ["sudo", "-n", "systemctl"]
                if self.sudo
                else ["systemctl", "--user"] if self.unit_user else ["systemctl"]
            )
            return (
                subprocess.run(
                    base + ["--quiet", "is-active", self.unit],
                    capture_output=True,
                    timeout=10,
                ).returncode
                == 0
            )
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.running:
            return True
        if self.proc is not None:
            self.stop()
        try:
            if self.unit:
                r = self._systemctl("start")
                if r.returncode != 0:
                    raise RuntimeError("systemd refused to start workload")
            elif self.cmd:
                self.proc = subprocess.Popen(
                    self.cmd,
                    shell=True,
                    start_new_session=True,
                    cwd=ROOT,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            log(f"stoked: {self.name}")
            return True
        except Exception as e:
            log(f"start failed {self.name}: {e}")
            return False

    def stop(self):
        # The shell can exit before its children. Retain its process-group ID
        # and run cleanup even when the parent is already gone.
        if self.proc is None and (not self.unit or not self.running):
            return
        try:
            if self.unit:
                if self.running:
                    result = self._systemctl("stop")
                    if result.returncode:
                        raise RuntimeError("systemd refused to stop workload")
            elif self.proc is not None:
                group = self.proc.pid
                deadline = time.monotonic() + KILL_TIMEOUT_S
                try:
                    os.killpg(group, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.proc.wait(timeout=KILL_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    pass
                # Give workers their grace period even if the shell exits first.
                while time.monotonic() < deadline:
                    try:
                        os.killpg(group, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait(timeout=5)
            if self.cleanup:
                subprocess.run(
                    self.cleanup,
                    shell=True,
                    cwd=ROOT,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=True,
                )
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            log(f"stop failed {self.name}: {type(e).__name__}")
            raise
        else:
            self.proc = None


class Furnace:
    def __init__(self, cfg):
        validate_config(cfg)
        self.demo = False
        self.settings = {**DEFAULTS, **cfg.get("settings", {})}
        self.workloads = [
            Workload(w, i)
            for i, w in enumerate(cfg["workloads"])
            if w.get("enabled", True)
        ]
        self.lock = threading.RLock()
        self.reserved_until = 0.0
        self.reserve_reason = ""
        self.sensor = {"temp_f": None, "ts": 0.0, "src": None}
        self.mode = "off"
        self.watts = None
        self.gpu_temp = None
        self.systin_path = find_systin()
        self.systin_f = None  # raw case-air °F
        self.gpu_temps_c = None  # per-GPU edge/hottest °C
        self.state_dir = os.environ.get(
            "FURNACE_STATE",
            os.path.join(
                os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
                "gpu-furnace",
            ),
        )
        os.makedirs(self.state_dir, exist_ok=True, mode=0o700)
        self.state_file = os.path.join(self.state_dir, "furnace_state.json")
        self.history_file = os.path.join(self.state_dir, "readings.jsonl")
        self._load_state()
        if self.systin_path:
            log(f"systin sensor: {self.systin_path} (offset {self.offset_f}F)")
        else:
            log("no systin sensor found; relying on external POSTs")

    # -- persisted state: offset, setpoint, manual override --
    def _load_state(self):
        self.offset_f = float(self.settings["offset_f"])
        self.calibrated = bool(self.settings.get("systin_calibrated", False))
        self.night_min_raw = None  # raw systin °F, coldest since last cal
        self.night_min_ts = 0.0
        self.cal_history = []
        self.setpoint_f = float(self.settings["setpoint_f"])
        self.manual_mode = None  # None | "full" | "off"
        self.manual_until = 0.0
        self.thermal_hold = False  # GPUs hit max temp, fire banked
        try:
            with open(self.state_file) as f:
                st = json.load(f)
            if not isinstance(st, dict):
                raise ValueError("state must be an object")
            self.offset_f = float(st.get("offset_f", self.offset_f))
            self.calibrated = bool(st.get("calibrated", False))
            self.night_min_raw = st.get("night_min_raw")
            self.night_min_ts = float(st.get("night_min_ts", 0.0))
            self.cal_history = st.get("cal_history", [])[-10:]
            self.setpoint_f = float(st.get("setpoint_f", self.setpoint_f))
            # Never restore unattended full heat after a restart.
            self.manual_mode = "off" if st.get("manual_mode") == "off" else None
            self.manual_until = float(st.get("manual_until", 0.0))
            if st.get("heat_mode") in ("fold", "bench", "forge"):
                self.settings["heat_mode"] = st["heat_mode"]
            self.offset_f = finite(self.offset_f, -60, 60, "saved offset")
            self.setpoint_f = finite(self.setpoint_f, 45, 95, "saved setpoint")
        except (OSError, ValueError, TypeError):
            self.offset_f = float(self.settings["offset_f"])
            self.setpoint_f = float(self.settings["setpoint_f"])
            self.calibrated = bool(self.settings.get("systin_calibrated", False))

    def _save_state(self):
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(
                    {
                        "offset_f": self.offset_f,
                        "calibrated": self.calibrated,
                        "night_min_raw": self.night_min_raw,
                        "night_min_ts": self.night_min_ts,
                        "cal_history": self.cal_history,
                        "setpoint_f": self.setpoint_f,
                        "manual_mode": self.manual_mode,
                        "manual_until": self.manual_until,
                        "heat_mode": self.settings.get("heat_mode"),
                    },
                    f,
                )
            os.replace(tmp, self.state_file)
        except OSError as e:
            log(f"state save failed: {e}")

    # -- systin sampling (case air; only meaningful when rig is idle) --
    def read_systin(self):
        if not self.systin_path:
            return None
        try:
            with open(self.systin_path) as f:
                mc = float(f.read().strip())
            t = mc / 1000.0 * 9 / 5 + 32
            return t if -40 < t < 212 else None
        except (OSError, ValueError):
            return None

    def update_sensor(self, now):
        """Refresh the room-temp estimate. External sensors win while fresh;
        otherwise fall back to idle-gated systin minus offset."""
        mode = self.settings["sensor_mode"]
        if mode == "off":
            self.sensor = {"temp_f": None, "ts": 0.0, "src": None}
            return
        # fresh external reading wins in auto/external modes
        ext_fresh = (
            self.sensor["src"] not in (None, "systin", "systin-cal")
            and now - self.sensor["ts"] < SENSOR_STALE_S
        )
        if (ext_fresh and mode == "auto") or mode == "external":
            return
        if mode not in ("auto", "systin") or not self.calibrated:
            return
        self.systin_f = self.read_systin()
        if self.systin_f is None:
            return
        # idle gate: during a burn this sensor reads case air, not room
        watts = self.watts
        if watts is None or watts > self.settings["idle_watts_max"]:
            return
        room = self.systin_f - self.offset_f
        self.sensor = {
            "temp_f": round(room, 1),
            "ts": now,
            "src": "systin-cal" if self.calibrated else "systin",
        }
        self._track_night_min(now)

    # -- overnight-min self-calibration --
    # Pre-dawn (2-6am) the room sits near its nightly low; the coldest idle
    # raw systin reading of the night, minus the expected room low, is the
    # true case offset. Applied once per morning, clamped to sane steps.
    def _track_night_min(self, now):
        if not self.settings["auto_calibrate"] or self.systin_f is None:
            return
        hour = time.localtime(now).tm_hour
        if 2 <= hour < 6:
            if self.night_min_raw is None or self.systin_f < self.night_min_raw:
                self.night_min_raw = self.systin_f
                self.night_min_ts = now
        elif (
            hour >= 6
            and self.night_min_raw is not None
            and now - self.night_min_ts > 3600
        ):
            # one hour of soak past the nightly low, then apply
            self._apply_night_min(now)

    def _apply_night_min(self, now):
        raw_min = float(self.night_min_raw)
        new_offset = raw_min - float(self.settings["night_min_target_f"])
        # clamp movement so one weird night can't wreck the model
        lo, hi = self.offset_f - 8, self.offset_f + 8
        new_offset = max(5.0, min(60.0, max(lo, min(hi, new_offset))))
        self.cal_history.append(
            {
                "ts": now,
                "raw_min_f": round(raw_min, 1),
                "old_offset": round(self.offset_f, 1),
                "new_offset": round(new_offset, 1),
            }
        )
        self.cal_history = self.cal_history[-10:]
        self.offset_f = new_offset
        self.calibrated = True
        self.night_min_raw = None
        self.night_min_ts = 0.0
        self._save_state()
        log(
            f"nightly auto-cal: offset -> {new_offset:.1f}F "
            f"(raw min {raw_min:.1f}F)"
        )

    def set_offset(self, offset_f):
        self.offset_f = float(offset_f)
        self.calibrated = True
        self._save_state()
        log(f"manual offset set: {self.offset_f:.1f}F")

    def set_setpoint(self, temp_f):
        self.setpoint_f = round(float(temp_f), 1)
        self._save_state()
        log(f"setpoint -> {self.setpoint_f}F")

    def set_manual(self, mode):
        self.manual_mode = None if mode == "auto" else mode
        self._save_state()
        log(f"manual mode: {mode}")

    # -- calibrate offset from GPU edge sensors (they read intake air) --
    # At idle, the coolest GPU edge is a second, independent room estimate:
    # edge ≈ room + gpu_edge_bias (config, default 4F). If it disagrees
    # with the systin-derived estimate, snap the systin offset so the two
    # agree. Refuses while burning (> idle_watts_max), same as the sensor.
    def calibrate_from_gpus(self):
        if self.watts is None:
            return False, "GPU power telemetry unavailable"
        if self.watts > self.settings["idle_watts_max"]:
            return False, f"rig not idle ({self.watts:.0f}W) — wait for GPUs to cool"
        self.systin_f = self.read_systin()
        self.gpu_temps_c = measure_gpu_temps_c()
        if not self.systin_f or not self.gpu_temps_c:
            return False, "systin or GPU sensors unavailable"
        room_f = (
            min(self.gpu_temps_c) * 9 / 5
            + 32
            - float(self.settings.get("gpu_edge_bias_f", 4.0))
        )
        if not (30 < room_f < 110):
            return False, f"implausible GPU estimate ({room_f:.1f}F)"
        old = self.offset_f
        new = self.systin_f - room_f
        new = max(5.0, min(60.0, new))
        self.offset_f = round(new, 1)
        self.calibrated = True
        self.cal_history.append(
            {
                "ts": time.time(),
                "old_offset": round(old, 1),
                "new_offset": self.offset_f,
                "edge_min_f": round(min(self.gpu_temps_c) * 9 / 5 + 32, 1),
                "systin_f": round(self.systin_f, 1),
            }
        )
        self.cal_history = self.cal_history[-10:]
        self._save_state()
        log(
            f"GPU-edge cal: offset {old:.1f} -> {self.offset_f}F "
            f"(edge min {room_f + 4:.1f}F raw, room est {room_f:.1f}F)"
        )
        return True, {
            "offset_f": self.offset_f,
            "old_offset_f": round(old, 1),
            "room_est_f": round(room_f, 1),
            "edge_min_f": round(min(self.gpu_temps_c) * 9 / 5 + 32, 1),
        }

    # -- history log for the thermometer page --
    def log_reading(self, now):
        rec = {
            "ts": int(now),
            "mode": self.mode,
            "room_f": (
                self.sensor["temp_f"]
                if now - self.sensor["ts"] <= SENSOR_STALE_S
                else None
            ),
            "watts": self.watts,
            "systin_f": round(self.systin_f, 1) if self.systin_f else None,
            "src": self.sensor["src"],
        }
        try:
            with open(self.history_file, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def history(self, hours=24):
        """Downsampled readings for charting: ~one point per 5 minutes."""
        cutoff = time.time() - hours * 3600
        out, last_bucket = [], -1
        try:
            with open(self.history_file) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if (
                        not isinstance(r, dict)
                        or not isinstance(r.get("ts"), (int, float))
                        or not math.isfinite(r["ts"])
                        or not cutoff <= r["ts"] <= time.time()
                    ):
                        continue
                    if any(
                        r.get(key) is not None
                        and (
                            not isinstance(r[key], (int, float))
                            or not math.isfinite(r[key])
                        )
                        for key in ("room_f", "watts")
                    ):
                        continue
                    bucket = r["ts"] // 300
                    if bucket != last_bucket:
                        out.append(r)
                        last_bucket = bucket
        except OSError:
            pass
        return out

    # -- fuel selection: full-heat burns the workloads tagged for the
    # currently selected heat mode (fold | bench | forge) --
    def ladder(self):
        want = str(self.settings.get("heat_mode", "any"))
        return [w for w in self.workloads if w.fuel in (want, "any")]

    def set_heat_mode(self, mode):
        if mode not in ("fold", "bench", "forge"):
            return False
        if not any(w.fuel in (mode, "any") for w in self.workloads):
            return False
        self.settings["heat_mode"] = mode
        self._save_state()
        log(f"heat mode: {mode}")
        return True

    # -- demand ladder: how many workloads to run for the current error --
    def demand_count(self):
        # manual overrides (from the hearth page) win over the thermostat
        if self.manual_mode == "full":
            self.mode = "full"
            return len(self.ladder())
        if self.manual_mode == "off":
            self.mode = "manual-off"
            return 0
        t = self.sensor["temp_f"]
        if t is None or time.time() - self.sensor["ts"] > SENSOR_STALE_S:
            return self.default_demand()
        sp = self.setpoint_f
        db = self.settings["deadband_f"]
        deficit = sp - t
        if deficit <= -db:  # warmer than setpoint+deadband: bank all
            self.mode = "idle"
            return 0
        if deficit < db:  # inside deadband: hold current fire
            self.mode = "hold"
            return sum(1 for w in self.ladder() if w.running)
        # cold: ladder up; full burn beyond 2x deadband deficit
        fuel = self.ladder()
        n = min(len(fuel), int(deficit // db) + 1)
        self.mode = "stoking" if deficit >= db else "hold"
        return n

    def default_demand(self):
        # No trustworthy reading: fail COLD. The furnace must never burn
        # GPUs it can't justify — inference availability beats a warm room,
        # and a blind controller has no idea whether the room is warm yet.
        self.mode = "blind"
        return 0

    def tick(self):
        watts, gpu_temp = measure_watts()
        gpu_temps = measure_gpu_temps_c()
        now = time.time()
        with self.lock:
            self.watts, self.gpu_temp = watts, gpu_temp
            self.gpu_temps_c = gpu_temps
            self.update_sensor(now)
            # inference lock wins over everything
            if self.reserved_until > now:
                for w in self.workloads:
                    w.stop()
                self.mode = "reserved"
                self.log_reading(now)
                return
            if self.reserved_until:
                self.reserved_until = 0.0
                log("inference lock expired, furnace back online")

            want = self.demand_count()
            if self.watts is None or self.gpu_temp is None:
                want = 0
                self.mode = "telemetry-missing"
            fuel = self.ladder()

            # hard thermal guard: junction overheating banks the fire
            hot = self.gpu_temp is not None and self.gpu_temp >= float(
                self.settings["max_gpu_temp_c"]
            )
            cool = self.gpu_temp is not None and self.gpu_temp < float(
                self.settings["reheat_temp_c"]
            )
            if hot or (self.thermal_hold and not cool):
                if not self.thermal_hold:
                    log(f"THERMAL: GPU {self.gpu_temp:.0f}C >= limit — banking all")
                self.thermal_hold = True
                want = 0
                self.mode = "thermal-hold"
            elif self.thermal_hold and cool:
                self.thermal_hold = False
                log(f"GPUs cooled to {self.gpu_temp:.0f}C — furnace back")

            # watts cap: trim the ladder from the top
            planned = sum(w.watts for w in fuel[:want])
            while want > 0 and planned > self.settings["max_watts"]:
                planned -= fuel[want - 1].watts
                want -= 1

            # stop everything above the fuel ladder (mode changed),
            # then stop from the top down
            for w in [x for x in self.workloads if x not in fuel]:
                w.stop()
            for w in sorted(fuel, key=lambda w: -w.index):
                if fuel.index(w) >= want:
                    w.stop()
            # start from the bottom up, with crash backoff
            for w in fuel[:want]:
                if not w.running:
                    if w.proc is not None and w.proc.poll() is not None:
                        w.stop()
                        w.restart_timer = now + 60
                        continue
                    if now < w.restart_timer:
                        continue
                    if not w.start():
                        w.restart_timer = now + 60  # back off crashes/missing
        self.log_reading(now)

    # -- API operations --
    def reserve(self, seconds, reason):
        with self.lock:
            self.reserved_until = time.time() + seconds
            self.reserve_reason = reason
            for w in self.workloads:
                w.stop()
            self.mode = "reserved"
        log(f"inference lock for {seconds}s ({reason})")

    def release(self):
        with self.lock:
            self.reserved_until = 0.0
            self.reserve_reason = ""
        log("inference lock released")

    def status(self):
        return {
            "demo": self.demo,
            "auth_required": bool(CONTROL_TOKEN),
            "max_watts": self.settings["max_watts"],
            "planned_watts": sum(w.watts for w in self.workloads if w.running),
            "sensor_fresh": self.sensor["temp_f"] is not None
            and time.time() - self.sensor["ts"] <= SENSOR_STALE_S,
            "heat_modes": sorted(
                {
                    m
                    for w in self.workloads
                    for m in ("fold", "bench", "forge")
                    if w.fuel in (m, "any")
                }
            ),
            "mode": self.mode,
            "manual_mode": self.manual_mode,
            "heat_mode": self.settings.get("heat_mode"),
            "sensor": self.sensor,
            "setpoint_f": self.setpoint_f,
            "deadband_f": self.settings["deadband_f"],
            "watts": self.watts,
            "gpu_temp_c": self.gpu_temp,
            "gpu_temps_c": (
                [round(t, 1) for t in self.gpu_temps_c] if self.gpu_temps_c else None
            ),
            "systin_f": round(self.systin_f, 1) if self.systin_f else None,
            "offset_f": round(self.offset_f, 1),
            "calibrated": self.calibrated,
            "cal_history": self.cal_history[-3:],
            "reserved": self.reserved_until > time.time(),
            "reserve_reason": self.reserve_reason or None,
            "workloads": [
                {"name": w.name, "watts": w.watts, "fuel": w.fuel, "running": w.running}
                for w in self.workloads
            ],
        }


# ---------------------------------------------------------------- http

FURNACE = None
WAKE = threading.Event()


class Handler(http.server.BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _respond(self, code, body, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code, obj):
        self._respond(code, json.dumps(obj, allow_nan=False).encode())

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/":
            self._respond(
                200,
                (ROOT / "heatd/hearth.html").read_bytes(),
                "text/html; charset=utf-8",
            )
            return
        with FURNACE.lock:
            if parsed.path == "/status":
                self._send(200, FURNACE.status())
            elif parsed.path == "/history":
                try:
                    hours = finite(query.get("hours", [24])[0], 0.01, 168, "hours")
                except (TypeError, ValueError):
                    self._send(400, {"error": "hours must be between 0.01 and 168"})
                    return
                self._send(200, {"hours": hours, "readings": FURNACE.history(hours)})
            elif parsed.path == "/demand":
                fuel = FURNACE.ladder()
                self._send(
                    200,
                    {
                        "demand": sum(w.running for w in fuel) / max(1, len(fuel)),
                        "mode": FURNACE.mode,
                    },
                )
            else:
                self._send(404, {"error": "unknown path"})

    def _body_params(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("chunked request bodies are not supported")
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= 16384:
            raise ValueError("request body must be at most 16 KiB")
        body = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if not body:
            return {}
        if ctype == "application/json":
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data
        if ctype == "application/x-www-form-urlencoded":
            return {k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()}
        raise ValueError("use application/json or application/x-www-form-urlencoded")

    def do_POST(self):
        # Credentials travel in a header only: query strings leak into logs,
        # browser history, and referrers. Real controllers always have a token.
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + CONTROL_TOKEN
        if CONTROL_TOKEN and not hmac.compare_digest(
            supplied.encode(), expected.encode()
        ):
            self.close_connection = True
            self._send(401, {"error": "control token required"})
            return
        try:
            params = self._body_params()
            with FURNACE.lock:
                result = self._mutate(urllib.parse.urlparse(self.path).path, params)
            self._send(200, result)
            WAKE.set()
        except (KeyError, TypeError, ValueError, IndexError, OverflowError):
            self.close_connection = True
            self._send(400, {"error": "invalid or missing request parameters"})
        except RuntimeError as e:
            self._send(409, {"error": str(e)})
        except (OSError, subprocess.SubprocessError):
            self._send(
                503, {"error": "workload operation failed; check the service log"}
            )

    def _mutate(self, path, params):
        if path == "/temperature":
            temp = finite(params["temp_f"], -59, 159, "temp_f")
            src = str(params.get("src", "external"))[:80]
            if src in ("systin", "systin-cal", ""):
                raise ValueError("reserved sensor name")
            FURNACE.sensor = {"temp_f": temp, "ts": time.time(), "src": src}
        elif path == "/reserve":
            seconds = finite(params.get("seconds", 7200), 1, 86400, "seconds")
            FURNACE.reserve(seconds, str(params.get("reason", ""))[:200])
        elif path == "/release":
            FURNACE.release()
        elif path == "/offset":
            FURNACE.set_offset(finite(params["offset_f"], -60, 60, "offset_f"))
        elif path == "/setpoint":
            value = (
                FURNACE.setpoint_f + finite(params["delta_f"], -50, 50, "delta_f")
                if "delta_f" in params
                else params["setpoint_f"]
            )
            FURNACE.set_setpoint(finite(value, 45, 95, "setpoint_f"))
        elif path == "/mode":
            mode = params["mode"]
            if mode not in ("full", "off", "auto"):
                raise ValueError("unknown mode")
            FURNACE.set_manual(mode)
            if mode == "off":
                for workload in FURNACE.workloads:
                    workload.stop()
                FURNACE.mode = "manual-off"
        elif path == "/heatmode":
            if not FURNACE.set_heat_mode(params["mode"]):
                raise RuntimeError(
                    "No enabled workloads for that fuel. Configure one first."
                )
        elif path == "/calibrate":
            ok, info = FURNACE.calibrate_from_gpus()
            if not ok:
                raise RuntimeError(info)
            return {"ok": True, "info": info}
        else:
            raise ValueError("unknown endpoint")
        return {"ok": True}

    def log_message(self, fmt, *args):
        pass  # never log URLs, Authorization headers, or request bodies


class DemoWorkload(Workload):
    def __init__(self, spec, index):
        super().__init__(spec, index)
        self.active = False

    @property
    def running(self):
        return self.active

    def start(self):
        self.active = True
        return True

    def stop(self):
        self.active = False


class DemoFurnace(Furnace):
    """Interactive preview: no hardware reads, commands, or persisted settings."""

    def __init__(self, cfg):
        super().__init__({**cfg, "workloads": []})
        self.demo = True
        self.settings["setpoint_f"] = self.setpoint_f = 72.0
        self.workloads = [
            DemoWorkload(
                {
                    "name": f"GPU {i + 1} · matrix workload",
                    "cmd": "demo",
                    "fuel": "bench",
                    "watts": 150,
                },
                i,
            )
            for i in range(4)
        ]
        self.started = time.time()

    def tick(self):
        with self.lock:
            now = time.time()
            self.sensor = {
                "temp_f": 69.3 + math.sin((now - self.started) / 120) * 0.4,
                "ts": now,
                "src": "demo sensor",
            }
            if self.reserved_until > now:
                want, self.mode = 0, "reserved"
            else:
                want = self.demand_count()
            for i, w in enumerate(self.workloads):
                w.active = i < want
            self.watts = sum(w.watts for w in self.workloads if w.running)
            self.gpu_temp = 61.0 if want else 34.0
            self.gpu_temps_c = [self.gpu_temp - i * 1.3 for i in range(4)]

    def history(self, hours=24):
        now = time.time()
        return [
            {
                "ts": int(now - (288 - i) * hours * 3600 / 288),
                "room_f": 68.8 + math.sin(i / 32) * 1.5 + i / 288,
                "watts": 300 + 150 * math.sin(i / 22),
                "mode": "stoking",
            }
            for i in range(289)
        ]


def main():
    global FURNACE, CONTROL_TOKEN
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--host", default=os.environ.get("FURNACE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument(
        "--check", action="store_true", help="validate config without starting jobs"
    )
    parser.add_argument(
        "--demo", action="store_true", help="interactive simulated 4 × 150 W rig"
    )
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        if args.check:
            log(f"config valid: {len(cfg['workloads'])} workload definitions")
            return
        if args.demo:
            if args.host not in ("127.0.0.1", "::1", "localhost"):
                parser.error("demo mode must bind to loopback")
            demo_state = tempfile.TemporaryDirectory(prefix="gpu-furnace-demo-")
            os.environ["FURNACE_STATE"] = demo_state.name
            FURNACE = DemoFurnace(cfg)
        else:
            token_path = os.environ.get(
                "FURNACE_TOKEN_FILE",
                str(
                    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
                    / "gpu-furnace/control.token"
                ),
            )
            CONTROL_TOKEN = load_control_token(token_path)
            log(f"control token file: {token_path} (contents never logged)")
            FURNACE = Furnace(cfg)
    except (OSError, ValueError, TypeError) as e:
        parser.error(str(e))

    stop = threading.Event()

    def request_stop(signum, frame):
        stop.set()
        WAKE.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(
        f"{'DEMO · ' if args.demo else ''}hearth at http://{args.host}:{server.server_port}"
    )
    try:
        while not stop.is_set():
            WAKE.clear()
            try:
                FURNACE.tick()
            except Exception as e:
                log(f"controller error: {type(e).__name__}; stopping workloads")
                for workload in FURNACE.workloads:
                    try:
                        workload.stop()
                    except Exception:
                        log(f"stop failed: {workload.name}; check workload externally")
            WAKE.wait(FURNACE.settings["poll_interval_s"])
    finally:
        log("shutting down, stopping workloads")
        server.shutdown()
        server.server_close()
        for workload in FURNACE.workloads:
            try:
                workload.stop()
            except Exception:
                log(f"stop failed: {workload.name}; check workload externally")
        if args.demo:
            demo_state.cleanup()


if __name__ == "__main__":
    main()
