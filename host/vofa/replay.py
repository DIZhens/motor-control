"""将已保存的真实遥测有限回放到 VOFA；不打开串口，不连接或启动电机。"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import time

from host.protocol import codec
from host.protocol.client import json_value
from host.vofa.bridge import TelemetryForwarder, VIEWS


def load_samples(path):
    samples = []
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            record = json.loads(line)
            frame = record.get('frame', {})
            if (record.get('direction') != 'rx' or record.get('status') != 0
                    or frame.get('kind') != 3 or frame.get('message_id') != 0x1001
                    or record.get('value') is None):
                continue
            # 从原始 payload 重解码，不信任手改后的派生值；原文件摘要由调用者保存。
            sample = codec.decode_sample(bytes.fromhex(frame['payload']))
            if json_value(sample) != record['value']:
                raise ValueError('原始遥测与日志解码值不一致')
            if not sample['valid_flags'] & 1:
                continue
            record['value'] = sample
            samples.append(record)
    if not samples:
        raise ValueError('没有具有有效控制时间的已验证遥测')
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('--view', choices=VIEWS, default='speed')
    parser.add_argument('--udp-port', type=int, default=1347)
    parser.add_argument('--ticks-per-second', type=float, default=20000)
    parser.add_argument('--repeat', type=int, choices=range(1, 11), default=1)
    args = parser.parse_args()
    if not math.isfinite(args.ticks_per_second) or args.ticks_per_second <= 0:
        parser.error('控制时基须为有限正数')
    samples = load_samples(args.log)
    sink = TelemetryForwarder(lambda _: None, args.udp_port, args.view)
    try:
        for _ in range(args.repeat):
            previous = None
            for record in samples:
                sample = record['value']
                domain = (record['frame']['session_id'], sample['telemetry_config_generation'])
                if previous and domain == previous[0]:
                    interval = (sample['control_time_ticks'] - previous[1]) / args.ticks_per_second
                    if not 0 < interval <= 5:
                        raise ValueError('控制时间不递增或存在超过5秒缺口，请先分段')
                    deadline += interval
                    time.sleep(max(0., deadline - time.monotonic()))
                else:
                    deadline = time.monotonic()
                sink(record)
                previous = domain, sample['control_time_ticks']
        print(json.dumps(dict(mode='OFFLINE_REPLAY_NO_HARDWARE', source=str(args.log.resolve()),
              source_sha256=hashlib.sha256(args.log.read_bytes()).hexdigest(), view=args.view,
              samples_per_repeat=len(samples), repeats=args.repeat, forwarded=sink.forwarded,
              display_errors=sink.display_errors), ensure_ascii=False, indent=2))
    finally:
        sink.close()


if __name__ == '__main__':
    main()
