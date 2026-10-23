"""正式 UART 遥测向本机 VOFA 单向转发；不接受网络命令，不启动电机。"""
import argparse
import json
import math
import socket
import struct
import time

from communication.host.protocol import codec
from communication.host.protocol.client import Client, JsonlLogger, json_value


# 顺序与旧固件直连 VOFA 十通道不同；整数精确值以原始协议日志为准。
CHANNELS = ('target_velocity_rpm', 'velocity_rpm', 'iq_reference_a', 'iq_feedback_a',
            'id_feedback_a', 'bus_voltage_v', 'motor_state', 'fault_flags',
            'valid_flags', 'limit_flags')
VIEWS = {'speed': (0, 1), 'current': (2, 3, 4), 'all': tuple(range(10))}
TAIL = bytes.fromhex('0000807f')


def justfloat(sample, view='all'):
    """无效测量保持 NaN；状态/故障也服从其有效位，不用零冒充有效值。"""
    values = [float(sample[name]) for name in CHANNELS]
    for index, bit in enumerate((4, 8, 16, 32, 64, 128, 256, 256)):
        if not sample['valid_flags'] & bit:
            values[index] = math.nan
    selected = [values[index] for index in VIEWS[view]]
    return struct.pack('<' + str(len(selected)) + 'f', *selected) + TAIL


class TelemetryForwarder:
    """复用 Client 的逐帧记录入口，先保存原始事实，再最佳努力单向显示。

    只转发已被 Client 验证为当前 Session 的解码遥测。网络失败不阻断原始记录或
    控制客户端；无重发队列、无网络接收，VOFA 无法通过此对象控制 MCU。
    """
    def __init__(self, logger, port=1347, view='speed'):
        if not 1 <= port <= 65535:
            raise ValueError('UDP port须为1..65535')
        if view not in VIEWS:
            raise ValueError('未知VOFA视图')
        self.logger = logger
        self.view = view
        self.address = ('127.0.0.1', port)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        self.forwarded = self.display_errors = 0

    def __call__(self, record):
        self.logger(record)
        frame, sample = record.get('frame', {}), record.get('value')
        if (record.get('direction') != 'rx' or record.get('status') != int(codec.Status.OK)
                or frame.get('kind') != 3 or frame.get('message_id') != 0x1001 or sample is None):
            return
        try:
            data = justfloat(sample, self.view)
            if self.socket.sendto(data, self.address) == len(data):
                self.forwarded += 1
            else:
                self.display_errors += 1
        except OSError:
            self.display_errors += 1

    def close(self):
        self.socket.close()


def checked(response):
    if response['result_code'] != codec.Result.OK:
        raise RuntimeError(response)
    return response


def observe(client, seconds):
    """有限时长观察，不写 PI、不发 Start/Update/Stop/Clear、不自动重连。"""
    checked(client.connect())
    initial = client.last_recovery['motor_snapshot']['value']
    if (initial['valid_flags'] & 0xC0) != 0xC0 or initial['motor_state'] != 1 or initial['fault_flags']:
        raise RuntimeError('观察入口只允许有效、无故障的IDLE')
    device = checked(client.read_object(codec.DEVICE_INFO))['value']
    checked(client.configure_telemetry(False, 250))
    checked(client.connect())
    configured = checked(client.configure_telemetry(True, 250))['value']
    client._record(direction='metadata', device=device, channels=CHANNELS, rate_hz=250,
                   telemetry_config=configured)
    parser_baseline = dict(client.parser.stats)
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        client.poll()
    checked(client.configure_telemetry(False, 250))
    until = time.monotonic() + .1
    while time.monotonic() < until:
        client.poll()
    final = checked(client.read_object(codec.MOTOR_SNAPSHOT))['value']
    communication = checked(client.read_object(codec.COMMUNICATION_STATUS))['value']
    return dict(device=device, initial=initial, final=final, communication=communication,
                telemetry_domains=list(client.telemetry_domains.values()),
                parser=client.parser.stats, parser_baseline=parser_baseline,
                parser_during_observation={name: count - parser_baseline[name]
                                           for name, count in client.parser.stats.items()},
                async_errors=client.async_errors,
                motion_commands=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True, help='独占的MCU串口，例如COM5')
    parser.add_argument('--udp-port', type=int, default=1347, help='VOFA本机接收端口')
    parser.add_argument('--view', choices=VIEWS, default='speed', help='仅影响VOFA显示，原始日志仍保存完整遥测')
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--log', required=True, help='新建原始协议JSONL')
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 600 or not 1 <= args.udp_port <= 65535:
        parser.error('seconds须为(0,600]，UDP port须为1..65535')
    import serial
    with JsonlLogger(args.log) as logger:
        forwarder = TelemetryForwarder(logger, args.udp_port, args.view)
        try:
            with serial.Serial(args.port, 460800, timeout=.005, write_timeout=.1) as stream:
                result = observe(Client(stream, logger=forwarder), args.seconds)
            result.update(forwarded=forwarder.forwarded, display_errors=forwarder.display_errors,
                          view=args.view, display_channels=[CHANNELS[index] for index in VIEWS[args.view]])
            logger(dict(direction='summary', **result))
            print(json.dumps(json_value(result), ensure_ascii=False, indent=2, allow_nan=False))
        finally:
            forwarder.close()


if __name__ == '__main__':
    main()
