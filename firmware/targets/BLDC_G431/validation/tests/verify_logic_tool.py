"""Host safety checks only; no CLI, SWD or hardware access."""
import importlib.util
from pathlib import Path
import sys

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("logic_check", Path(__file__).resolve().parents[1] / 'tools/logic_check.py')
logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logic)
count = 0


def rejected(function):
    global count
    try:
        function()
    except RuntimeError:
        count += 1
    else:
        raise AssertionError("Unsafe input accepted")


session = logic.Session.__new__(logic.Session)
session.layout = {"request": 0x2000089C}
session.last_sequence = 0
calls = []
session.execute = lambda tag, arguments: calls.append((tag, arguments))
for command in [1, 2, 3, 4, 5, 6, 7]:
    previous = session.last_sequence
    sequence = session.submit_logic(command, "mock")
    arguments = calls[-1][1]
    assert sequence == previous + 1
    assert arguments == ["-w32", "0x200008a0", hex(command), "0x0", "0x7d0", "0x3f000000",
                         "-w32", "0x2000089c", hex(sequence)]
    count += 1
for command in [0, 8, 9, 0x4310A11E]:
    rejected(lambda: session.submit_logic(command, "must-not-send"))
assert len(calls) == 7
session.last_sequence = 0xFFFFFFFF
assert session.submit_logic(1, "wrap") == 1
count += 1
session.last_sequence = None
rejected(lambda: session.submit_logic(2, "no-read"))

data = bytearray([0xA5] * 128)
layout = {}
for index, field in enumerate(logic.SNAP_FIELDS):
    layout["s_" + field] = index * 4
    layout["z_" + field] = 1 if field == "current.measured_phases" else 4
offset = layout["s_current.measured_phases"]
data[offset] = 6
assert logic.decode_snapshot(data, layout)["current.measured_phases"] == 6
count += 1
bad_layout = dict(layout, z_started=2)
rejected(lambda: logic.decode_snapshot(data, bad_layout))

healthy = dict(status=1, started=1, state=0, halted=False, core_lockup=False, drive_running=True, adc_irq=True,
    moe=False, high_inputs=0, low_inputs=0, guard_running=False, guard_irq=False,
    enable_attempts=0, enable_successes=0, invalid_current_count=0, legacy_invalid=0, moe_violations=0,
    trip_armed=True, legacy_trip_valid=True, trip_valid=1, legacy_trip_active=False, legacy_trip_causes=0,
    bie=True, break_flags=0, comp_mask=0, bdtr=0x03001400, power_key=0, mailbox_sequence=0,
    command_sequence=0, probe_outcome=0, probe_held=0, dac_codes=[2065, 345, 345], bus_valid=True, bus_v=2.65)
healthy.update({"current.acquisition_valid": True, "current.measured_phases": 6,
                "trip.condition_active": False, "trip.causes": 0})
logic.check_logic_idle(healthy)
count += 1
for changed in [dict(moe=True), dict(enable_attempts=1), dict(bus_v=12.0), dict(bus_v=float("nan")),
                dict(probe_held=1), dict(adc_irq=False), dict(power_key=0x4310A11E),
                dict(command_sequence=1), dict(core_lockup=True), {"trip.causes": 4}, {"current.measured_phases": 0x08006906}]:
    rejected(lambda: logic.check_logic_idle(dict(healthy, **changed)))

print(f"PASS: {count} host logic-tool safety cases; no hardware access.")
