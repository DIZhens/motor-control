"""Core Protocol串口CLI；默认仅检查，不自动启动或恢复电机。"""
import argparse
from contextlib import nullcontext
import json
from . import codec
from .client import Client, JsonlLogger, json_value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--baud', type=int, default=460800, choices=[460800])
    parser.add_argument('--log', help='新建JSONL文件，已存在时拒绝覆盖')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('inspect')
    read = sub.add_parser('read'); read.add_argument('object', type=lambda text: int(text, 0))
    write = sub.add_parser('write-pi'); write.add_argument('kp', type=float); write.add_argument('ki', type=float)
    config = sub.add_parser('telemetry'); config.add_argument('enabled', type=int, choices=[0, 1]); config.add_argument('rate', type=int, choices=[50, 250, 500])
    for name in ['start', 'speed']:
        command = sub.add_parser(name); command.add_argument('rpm', type=float)
    sub.add_parser('clear'); sub.add_parser('stop')
    args = parser.parse_args()
    import serial
    with (JsonlLogger(args.log) if args.log else nullcontext(None)) as logger:
        with serial.Serial(args.port, args.baud, timeout=0.005, write_timeout=0.100) as stream:
            client = Client(stream, logger=logger)
            if args.action == 'stop':
                result = client.stop()
            else:
                opened = client.connect()
                if opened['result_code'] != codec.Result.OK:
                    raise SystemExit(json.dumps(opened))
                if args.action == 'inspect':
                    result = dict(session=client.session, device=client.read_object(codec.DEVICE_INFO),
                                  recovery=client.last_recovery, communication=client.read_object(codec.COMMUNICATION_STATUS))
                elif args.action == 'read': result = client.read_object(args.object)
                elif args.action == 'write-pi': result = client.write_pi(args.kp, args.ki)
                elif args.action == 'telemetry': result = client.configure_telemetry(args.enabled, args.rate)
                else: result = client.execute(dict(start=1, speed=2, clear=4)[args.action], getattr(args, 'rpm', None))
            print(json.dumps(json_value(result), ensure_ascii=False, indent=2, allow_nan=False))

if __name__ == '__main__': main()
