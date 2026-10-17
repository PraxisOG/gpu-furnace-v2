import http.client
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("heatd", ROOT / "heatd/heatd.py")
heatd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(heatd)


def config(count=4):
    return {
        "settings": {"max_watts": 600},
        "workloads": [
            {"name": f"GPU {i}", "cmd": "unused", "fuel": "bench", "watts": 150}
            for i in range(count)
        ],
    }


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"FURNACE_STATE": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.systin = patch.object(heatd, "find_systin", return_value=None)
        self.systin.start()
        self.addCleanup(self.systin.stop)
        self.furnace = heatd.Furnace(config())
        self.furnace.workloads = [
            heatd.DemoWorkload(
                {"name": f"GPU {i}", "cmd": "unused", "fuel": "bench", "watts": 150}, i
            )
            for i in range(4)
        ]

    def tick(self, watts=60, temp=55):
        with patch.object(
            heatd, "measure_watts", return_value=(watts, temp)
        ), patch.object(
            heatd, "measure_gpu_temps_c", return_value=[temp] if temp else None
        ):
            self.furnace.tick()

    def room(self, temp, age=0):
        self.furnace.sensor = {"temp_f": temp, "ts": time.time() - age, "src": "test"}

    def active(self):
        return sum(w.running for w in self.furnace.workloads)

    def test_600_w_ladder_and_warm_stop(self):
        self.room(60)
        self.tick()
        self.assertEqual(self.active(), 4)
        self.assertEqual(self.furnace.status()["planned_watts"], 600)
        self.room(75)
        self.tick()
        self.assertEqual(self.active(), 0)

    def test_warm_room_reaps_exited_shells_with_remaining_workers(self):
        from unittest.mock import Mock

        workload = heatd.Workload({"name": "orphaned workers", "cmd": "unused"}, 0)
        workload.proc = Mock(pid=12345)
        workload.proc.poll.return_value = 0
        self.furnace.workloads = [workload]
        self.room(75)
        with patch.object(heatd.os, "killpg") as signals, patch.object(
            heatd, "KILL_TIMEOUT_S", 0
        ):
            self.tick()
        self.assertIsNone(workload.proc)
        self.assertEqual(signals.call_args_list[0].args, (12345, signal.SIGTERM))

    def test_hysteresis_and_budget(self):
        self.room(60)
        self.furnace.settings["max_watts"] = 450
        self.tick()
        self.assertEqual(self.active(), 3)
        self.room(70)
        self.tick()
        self.assertEqual(self.active(), 3)

    def test_missing_and_stale_room_sensor_stop_automatic(self):
        for age in (0, 121):
            self.room(None if age == 0 else 60, age)
            self.tick()
            self.assertEqual(self.active(), 0)
            self.assertEqual(self.furnace.mode, "blind")

    def test_full_bypasses_room_only(self):
        self.furnace.set_manual("full")
        self.tick()
        self.assertEqual(self.active(), 4)
        self.tick(watts=None, temp=None)
        self.assertEqual(self.active(), 0)
        self.assertEqual(self.furnace.mode, "telemetry-missing")

    def test_thermal_hold_hysteresis(self):
        self.furnace.set_manual("full")
        self.tick(temp=86)
        self.assertTrue(self.furnace.thermal_hold)
        self.assertEqual(self.active(), 0)
        self.tick(temp=80)
        self.assertEqual(self.active(), 0)
        self.tick(temp=None)
        self.assertEqual(self.active(), 0)
        self.tick(temp=74)
        self.assertFalse(self.furnace.thermal_hold)
        self.assertEqual(self.active(), 4)

    def test_reservation_precedes_full_and_expires(self):
        self.furnace.set_manual("full")
        self.tick()
        self.furnace.reserve(60, "test")
        self.assertEqual(self.active(), 0)
        self.tick()
        self.assertEqual(self.furnace.mode, "reserved")
        self.furnace.reserved_until = time.time() - 1
        self.tick()
        self.assertEqual(self.active(), 4)

    def test_unavailable_fuel_never_falls_back(self):
        self.assertFalse(self.furnace.set_heat_mode("fold"))
        self.furnace.settings["heat_mode"] = "fold"
        self.assertEqual(self.furnace.ladder(), [])
        self.furnace.set_manual("full")
        self.tick()
        self.assertEqual(self.active(), 0)

    def test_restart_does_not_restore_full_but_preserves_off(self):
        self.furnace.set_manual("full")
        self.assertIsNone(heatd.Furnace(config()).manual_mode)
        self.furnace.set_manual("off")
        self.assertEqual(heatd.Furnace(config()).manual_mode, "off")

    def test_calibrated_systin_is_not_mistaken_for_external(self):
        f = self.furnace
        f.settings["sensor_mode"] = "systin"
        f.calibrated = True
        f.offset_f, f.watts = 10, 50
        f.sensor = {"temp_f": 10, "ts": time.time(), "src": "systin-cal"}
        with patch.object(f, "read_systin", return_value=80):
            f.update_sensor(time.time())
        self.assertEqual(f.sensor["temp_f"], 70)

    def test_systin_refuses_unknown_power_and_uncalibrated_sensor(self):
        f = self.furnace
        f.settings["sensor_mode"] = "auto"
        with patch.object(f, "read_systin", return_value=80):
            f.watts = 20
            f.update_sensor(time.time())
            self.assertIsNone(f.sensor["temp_f"])
            f.calibrated, f.watts = True, None
            f.update_sensor(time.time())
            self.assertIsNone(f.sensor["temp_f"])
        self.assertFalse(f.calibrate_from_gpus()[0])

    def test_malformed_state_recovers(self):
        Path(self.furnace.state_file).write_text("[]")
        self.assertEqual(heatd.Furnace(config()).setpoint_f, 70)


class ConfigurationTests(unittest.TestCase):
    def test_example_has_four_disabled_150_w_jobs(self):
        cfg = heatd.load_config(ROOT / "heatd/workloads.example.json")
        self.assertEqual(sum(w["watts"] for w in cfg["workloads"]), 600)
        self.assertTrue(all(not w["enabled"] for w in cfg["workloads"]))

    def test_rejects_invalid_settings(self):
        for settings in (
            {"deadband_f": 0},
            {"max_watts": float("nan")},
            {"max_gpu_temp_c": 75, "reheat_temp_c": 85},
            {"sensor_mode": "guess"},
            {"auto_calibrate": "false"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                cfg = config()
                cfg["settings"].update(settings)
                heatd.validate_config(cfg)

    def test_rejects_ambiguous_and_malformed_workloads(self):
        for update in (
            {"unit": "test.service"},
            {"watts": float("inf")},
            {"enabled": "false"},
            {"cmd": ["not", "a", "string"]},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                cfg = config(1)
                cfg["workloads"][0].update(update)
                heatd.validate_config(cfg)

    def test_token_generated_privately_reused_and_invalid_files_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.token"
            token = heatd.load_control_token(path)
            self.assertGreaterEqual(len(token), 32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(heatd.load_control_token(path), token)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                heatd.load_control_token(path)
            path.chmod(0o600)
            path.write_text("")
            with self.assertRaises(ValueError):
                heatd.load_control_token(path)

    def test_setup_is_idempotent_and_does_not_log_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "XDG_CONFIG_HOME": tmp}
            result = subprocess.run(
                ["bash", "scripts/setup.sh"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            folder = Path(tmp) / "gpu-furnace"
            token = (folder / "control.token").read_text().strip()
            self.assertNotIn(token, result.stdout + result.stderr)
            (folder / "workloads.json").write_text('{"custom": true}')
            subprocess.run(
                ["bash", "scripts/setup.sh"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                check=True,
            )
            self.assertEqual((folder / "control.token").read_text().strip(), token)
            self.assertEqual(
                json.loads((folder / "workloads.json").read_text()), {"custom": True}
            )

    def test_amd_hotspot_and_mixed_vendor_power(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for name, value in [
                ("power1_average", 150000000),
                ("temp1_input", 60000),
                ("temp2_input", 95000),
            ]:
                (path / name).write_text(str(value))
            with patch.object(heatd, "_amd_hwmon", return_value=[path]), patch.object(
                heatd, "_nvidia_metrics", return_value=[(150, 70)]
            ):
                self.assertEqual(heatd.measure_watts(), (300, 95))
                self.assertEqual(heatd.measure_gpu_temps_c(), [70, 60])
            (path / "power1_average").unlink()
            with patch.object(heatd, "_amd_hwmon", return_value=[path]), patch.object(
                heatd, "_nvidia_metrics", return_value=[]
            ):
                self.assertEqual(heatd.measure_watts(), (None, None))

    def test_command_cleanup_signals_group_after_shell_exit(self):
        # A shell may finish while GPU workers remain in its process group.
        # Verify that an exited parent does not skip either group signal.
        workload = heatd.Workload({"name": "child cleanup", "cmd": "unused"}, 0)
        from unittest.mock import Mock

        proc = Mock(pid=12345)
        proc.poll.return_value = 0
        workload.proc = proc
        with patch.object(heatd.os, "killpg") as kill_group, patch.object(
            heatd, "KILL_TIMEOUT_S", 0
        ):
            workload.stop()
        self.assertEqual(kill_group.call_args_list[0].args, (12345, signal.SIGTERM))
        self.assertEqual(kill_group.call_args_list[1].args, (12345, signal.SIGKILL))
        self.assertIsNone(workload.proc)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {"FURNACE_STATE": cls.tmp.name})
        cls.env.start()
        cls.token = secrets.token_urlsafe(32)
        heatd.CONTROL_TOKEN = cls.token
        heatd.FURNACE = heatd.Furnace(config(0))
        cls.server = heatd.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), heatd.Handler
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.env.stop()
        cls.tmp.cleanup()
        heatd.CONTROL_TOKEN = ""

    def request(self, method, path, body=None, authorized=False, raw=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer " + self.token
        try:
            conn.request(
                method,
                path,
                raw if raw is not None else json.dumps(body or {}),
                headers,
            )
            response = conn.getresponse()
            data = response.read().decode()
            self.assertNotIn(self.token, data)
            return response.status, data
        finally:
            conn.close()

    def test_read_endpoints_work_without_token(self):
        for path in ("/", "/status", "/history", "/demand"):
            self.assertEqual(self.request("GET", path)[0], 200)

    def test_every_mutating_endpoint_requires_header_auth(self):
        for path in (
            "temperature",
            "reserve",
            "release",
            "offset",
            "setpoint",
            "mode",
            "heatmode",
            "calibrate",
        ):
            self.assertEqual(self.request("POST", "/" + path)[0], 401)
        self.assertEqual(
            self.request("POST", "/mode?token=" + self.token, {"mode": "off"})[0], 401
        )

    def test_authenticated_controls_and_parameter_validation(self):
        self.assertEqual(
            self.request("POST", "/setpoint", {"setpoint_f": 73}, True)[0], 200
        )
        self.assertEqual(heatd.FURNACE.setpoint_f, 73)
        for path, body in [
            ("/reserve", {"seconds": -1}),
            ("/reserve", {"seconds": "NaN"}),
            ("/temperature", {"temp_f": float("nan")}),
            ("/setpoint", {"setpoint_f": 999}),
            ("/mode", {"mode": "wrong"}),
            ("/temperature", {"temp_f": True}),
        ]:
            self.assertEqual(self.request("POST", path, body, True)[0], 400)
        self.assertEqual(self.request("POST", "/mode", {"mode": "off"}, True)[0], 200)
        self.assertEqual(
            self.request("POST", "/heatmode", {"mode": "fold"}, True)[0], 409
        )

    def test_malformed_and_oversize_bodies_are_rejected(self):
        for raw in ("{broken", "[]", "x" * 16385):
            self.assertEqual(
                self.request("POST", "/setpoint", authorized=True, raw=raw)[0], 400
            )
        for hours in ("nan", "inf", "-1", "999"):
            self.assertEqual(self.request("GET", "/history?hours=" + hours)[0], 400)


if __name__ == "__main__":
    unittest.main()
