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
    return dict(status='PASS', hardware_access=False, production_files=len(paths), dependencies=edges,
                binding='no UART framing/CAN identifier/HAL/RTOS; fixed Device Port -> Runtime mapping',
                source_sha256={p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in [*paths, binding]})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    report = verify()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('PASS protocol dependency direction:', report['production_files'], 'production files')
