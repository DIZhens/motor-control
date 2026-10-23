"""回放只消费可追溯真实 payload，不接受修改后的派生遥测。"""
import json
from pathlib import Path
import tempfile
import unittest

from host.protocol import codec
from host.protocol.client import json_value
from host.vofa.replay import load_samples
from host.vofa.test_bridge import sample


class ReplayTest(unittest.TestCase):
    def test_raw_payload_roundtrip(self):
        value = codec.decode_sample(codec.encode_sample(sample()))
        record = dict(direction='rx', status=0, frame=dict(kind=3, message_id=0x1001,
                      payload=codec.encode_sample(value).hex()), value=json_value(value))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.jsonl'
            path.write_text(json.dumps(record) + '\n', encoding='utf-8')
            self.assertEqual(load_samples(path)[0]['value'], value)

    def test_changed_or_untrusted_value_rejected(self):
        value = sample()
        record = dict(direction='rx', status=0, frame=dict(kind=3, message_id=0x1001,
                      payload=codec.encode_sample(value).hex()), value=dict(value, velocity_rpm=999))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.jsonl'
            path.write_text(json.dumps(record) + '\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                load_samples(path)
            record['status'] = 1
            path.write_text(json.dumps(record) + '\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                load_samples(path)


if __name__ == '__main__':
    unittest.main()
