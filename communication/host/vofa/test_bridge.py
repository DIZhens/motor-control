"""本地 UDP 和正式 Client 解码入口验证；不连接设备、不执行运动。"""
import math
import socket
import struct
import unittest
from unittest.mock import Mock

from communication.host.protocol import codec
from communication.host.protocol.client import Client
from communication.host.vofa.bridge import CHANNELS, TelemetryForwarder, justfloat


def sample():
    return dict(zip(codec.SAMPLE_FIELDS, (
        3, 2000, 1998, 40., 39.5, .125, .06, .058, -.002, 12.1,
        2, 0, 1023, 0, 1, 1)))


class Stream:
    timeout, write_timeout = .005, .1
    def __init__(self, data):
        self.data = data
    @property
    def in_waiting(self):
        return len(self.data)
    def read(self, count):
        data, self.data = self.data[:count], self.data[count:]
        return data


class BridgeTest(unittest.TestCase):
    def test_wire_and_invalid(self):
        data = sample()
        wire = justfloat(data)
        self.assertEqual(len(wire), 44)
        self.assertEqual(wire[-4:], bytes.fromhex('0000807f'))
        for actual, field in zip(struct.unpack('<10f', wire[:-4]), CHANNELS):
            self.assertAlmostEqual(actual, data[field], places=5)
        data['valid_flags'] = 0
        values = struct.unpack('<10f', justfloat(data)[:-4])
        self.assertTrue(all(math.isnan(value) for value in values[:8]))
        self.assertEqual(values[8:], (0., 0.))
        data = sample()
        self.assertEqual(justfloat(data, 'speed'), struct.pack('<2f', 40., 39.5) + bytes.fromhex('0000807f'))
        self.assertEqual(len(justfloat(data, 'current')), 16)

    def test_real_client_filter_and_udp(self):
        valid = codec.Frame(3, 0x1001, 0x1234, 1, codec.encode_sample(sample()))
        stale = codec.Frame(3, 0x1001, 0x9999, 2, valid.payload)
        corrupt = bytearray(codec.encode_frame(valid))
        corrupt[10] ^= 1
        stream = Stream(codec.encode_frame(valid) + codec.encode_frame(stale) + bytes(corrupt))
        records = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(('127.0.0.1', 0))
            receiver.settimeout(.2)
            sink = TelemetryForwarder(records.append, receiver.getsockname()[1], 'all')
            try:
                client = Client(stream, logger=sink)
                client.session = 0x1234
                while stream.in_waiting:
                    client.poll()
                self.assertEqual(receiver.recv(512), justfloat(sample()))
                with self.assertRaises(socket.timeout):
                    receiver.recv(512)
                self.assertEqual(sink.forwarded, 1)
                self.assertEqual(sink.display_errors, 0)
                self.assertEqual(len([r for r in records if r.get('value')]), 1)
            finally:
                sink.close()

    def test_display_failure_keeps_raw_record(self):
        records = []
        sink = TelemetryForwarder(records.append)
        sink.close()
        sink.socket = Mock()
        sink.socket.sendto.side_effect = OSError('display unavailable')
        record = dict(direction='rx', status=0, frame=dict(kind=3, message_id=0x1001), value=sample())
        sink(record)
        self.assertEqual(records, [record])
        self.assertEqual((sink.forwarded, sink.display_errors), (0, 1))
        # 记录失败必须向所有者报告；不能以显示成功掩盖证据写入失败。
        sink.logger = Mock(side_effect=OSError('disk unavailable'))
        with self.assertRaises(OSError):
            sink(record)
        sink.socket.sendto.assert_called_once()


if __name__ == '__main__':
    unittest.main()
