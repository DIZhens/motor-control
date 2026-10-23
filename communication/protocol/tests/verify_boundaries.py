"""核对生产协议头依赖与Firmware绑定方向；不访问硬件。"""
from pathlib import Path
import argparse
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / 'communication/protocol'
STANDARD = {'stdbool.h', 'stdint.h', 'stddef.h', 'string.h', 'math.h', 'float.h', 'limits.h'}


def verify():
    paths = sorted(p for folder in ('device', 'transport', 'uart', 'can')
                   for p in (PROTOCOL / folder).glob('*') if p.suffix in ('.c', '.h'))
    includes = [PROTOCOL / 'device', PROTOCOL / 'transport', PROTOCOL / 'uart', PROTOCOL / 'can']
    edges = {}
    for path in paths:
        text = re.sub(r'/\*.*?\*/|//[^\n]*', '', path.read_text(encoding='utf-8-sig'), flags=re.S)
        dependencies = []
        for name in re.findall(r'^\s*#\s*include\s*[<"]([^>"]+)', text, re.M):
            if name in STANDARD:
                continue
            found = next((p / name for p in [path.parent, *includes] if (p / name).is_file()), None)
            assert found is not None and found.resolve().is_relative_to(PROTOCOL), (path, name)
            dependencies.append(found.relative_to(ROOT).as_posix())
        assert not re.search(r'\b(?:RuntimeRequest|RuntimeCommandCompletion|TaskHandle_t|Motor|ControlRuntime)\b', text), path
        edges[path.relative_to(ROOT).as_posix()] = dependencies
    binding = ROOT / 'firmware/targets/BLDC_G431/communication/motor_node_binding.c'
    text = binding.read_text(encoding='utf-8')
    assert not re.search(r'\b(?:core_frame_|core_cobs_|core_parser_|CAN_ID|FDCAN|HAL_|xTask|xQueue)', text)
    assert re.findall(r'^#include "([^"]+)"', text, re.M) == [
        'protocol_device_port.h', 'motor_node_binding_target.h']
    runtime = ROOT / 'firmware/targets/BLDC_G431/runtime'
    producer = (runtime / 'system_runtime.c').read_text(encoding='utf-8')
    producer_code = re.sub(r'/\*.*?\*/|//[^\n]*', '', producer, flags=re.S)
    assert not re.search(r'\b(?:Session|session_id|active_epoch|epoch_counter|UART|CAN)\b', producer_code)
    assert 'system_runtime_target_telemetry_epoch' not in producer
    task = producer[producer.index('static void telemetry_task('):producer.index('static void bootstrap_task(')]
    assert 'target_communication' not in task and 'server' not in task
    port = (PROTOCOL / 'device/protocol_device_port.h').read_text(encoding='utf-8')
    port_code = re.sub(r'/\*.*?\*/|//[^\n]*', '', port, flags=re.S)
    assert not re.search(r'\b(?:session_id|sequence|Session|TaskHandle_t|QueueHandle_t)\b', port_code)
    for operation in ('begin', 'end', 'peek', 'retire'):
        assert f'protocol_device_telemetry_{operation}' in port
        assert f'motor_node_binding_target_telemetry_{operation}' in text
    target_binding = ROOT / 'firmware/targets/BLDC_G431/communication/target_motor_node_binding.c'
    target_code = target_binding.read_text(encoding='utf-8')
    for operation in ('begin', 'end', 'peek', 'retire'):
        assert f'system_runtime_telemetry_{operation}' in target_code
    for directory in (runtime, target_binding.parent, PROTOCOL / 'uart', PROTOCOL / 'can'):
        for path in directory.glob('*'):
            if path.suffix not in ('.c', '.h'):
                continue
            code = path.read_text(encoding='utf-8-sig')
            assert not re.search(r'SpeedCommissioningSample|target_runtime_read_commissioning|'
                                 r'target_runtime_session_permitted|\w+_telemetry_epoch|'
                                 r'\b(?:active_epoch|epoch_counter|epoch_first)\b', code), path
    return dict(status='PASS', hardware_access=False, production_files=len(paths), dependencies=edges,
                binding='no UART framing/CAN identifier/HAL/RTOS; generic subscription through retained Target seam',
                telemetry='SystemRuntime owns subscription; producer never queries Communication; Server owns wire identity',
                source_sha256={p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in [*paths, binding, target_binding, runtime / 'system_runtime.c']})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    report = verify()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('PASS protocol dependency direction:', report['production_files'], 'production files')
