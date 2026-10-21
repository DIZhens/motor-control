"""Synthetic current-capture checks; no ST-Link access."""
from copy import deepcopy
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import current_check as current


LAYOUT = dict(zip(current.FIELDS, [0, 8, 12, 16, 20, 24, 32, 40, 48, 52, 56, 60, 64, 68, 72, 76]))


def capture(changes=None):
    values = dict(zip(current.FIELDS, [4, 7, 1, 4096, 16900, 17100, 100, 8290,
                                      -0.012, -0.02, 0, 1e-5, 0, -0.01, 0.01, 1e-5]))
    values.update(changes or {})
    data = bytearray(80)
    for key, value in values.items():
        struct.pack_into("<" + current.FIELDS[key], data, LAYOUT[key], value)
    return bytes(data)


class Tests(unittest.TestCase):
    def test_statistics(self):
        stats = current.decode_capture(capture(), LAYOUT, 7)
        self.assertAlmostEqual(stats["phase_b.stddev_a"], 0.00316227766)
        self.assertAlmostEqual(stats["phase_c.peak_to_peak_a"], 0.02)

    def test_bad_statistics(self):
        for field, value in [("sequence", 3), ("statistics.request_sequence", 6),
                             ("statistics.result", 6), ("statistics.sample_count", 126),
                             ("statistics.last_sample_time", 8291),
                             ("statistics.interval_min_cycles", 12749),
                             ("statistics.interval_max_cycles", 21251),
                             ("statistics.phase_b.mean_a", float("nan")),
                             ("statistics.phase_c.variance_a2", -1),
                             ("statistics.phase_c.min_a", 0.02)]:
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                current.decode_capture(capture({field: value}), LAYOUT, 7)

    def test_write_restriction(self):
        session = object.__new__(current.Session)
        session.layout = {"request": 0x20000000}
        with patch.object(current.pwm.Session, "execute") as execute:
            for command in [1, 4]:
                session.execute("test", ["-w32", "0x20000004", str(command), "0", "2000", "0", "-w32", "0x20000000", "7"])
            self.assertEqual(execute.call_count, 2)
            for command, key in [(3, 0), (5, 0), (8, 0), (9, 0), (4, 0x4310A11E)]:
                with self.subTest(command=command, key=key), self.assertRaises(RuntimeError):
                    session.execute("test", ["-w32", "0x20000004", str(command), str(key), "2000", "0", "-w32", "0x20000000", "7"])
            self.assertEqual(execute.call_count, 2)

    def test_transactions(self):
        before = {k: 0 for k in current.UNCHANGED}
        before["guard_irq_count"] = 0
        good = {**before, "guard_irq_count": 4095, "command_sequence": 7,
                "active_request_sequence": 7, "result": 1, "stop_reason": 1, "mailbox_command": 4}
        for mutation in [None, {"enable_attempts": 1}, {"stage_successes": 1},
                         {"guard_irq_count": 0}, {"result": 6}, {"active_request_sequence": 6}]:
            with self.subTest(mutation=mutation), TemporaryDirectory() as tmp:
                after = {**good, **(mutation or {})}
                session = unittest.mock.Mock(out=Path(tmp), layout={"capture": 0x20000968})
                session.submit_logic.return_value = 7
                session.upload.return_value = {"capture": capture()}
                pairs = [(before, before, {}), (after, after, {})]
                with patch.object(current, "healthy_pair", side_effect=pairs), patch.object(current.time, "sleep") as wait:
                    if mutation:
                        with self.assertRaises(RuntimeError):
                            current.window(session, LAYOUT, 1)
                    else:
                        current.window(session, LAYOUT, 1)
                session.submit_logic.assert_called_once_with(4, "window-1-submit-capture")
                wait.assert_called_once_with(0.5)

    def test_preflight_failure_no_write(self):
        session = unittest.mock.Mock()
        with patch.object(current, "healthy_pair", side_effect=RuntimeError("unhealthy")):
            with self.assertRaises(RuntimeError):
                current.window(session, LAYOUT, 1)
        session.submit_logic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
