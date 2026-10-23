"""编译并执行真实 ARM C 编解码器，与固定黄金字节及独立 Python 实现对照；不访问硬件。"""

import argparse
import binascii
import hashlib
import json
import math
import os
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB, __version__ as unicorn_version
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP, UC_CPU_ARM_CORTEX_M4


TESTS = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[4]
COMM = REPO / 'communication/protocol/uart'
sys.path.insert(0, str(REPO))
from communication.host.protocol import codec as py

COMPILER = Path(os.environ.get('KEIL_ARMCC_BIN', r'D:\keil5\ARM\ARMCC\bin'))
VECTOR_PATH = REPO / 'communication/spec/test_vectors/core_protocol_v1.json'
VECTORS = json.loads(VECTOR_PATH.read_text(encoding='utf-8'))
GROUPS = []
ASSERTIONS = 0


def check(condition, detail=''):
    global ASSERTIONS
    ASSERTIONS += 1
    assert condition, detail


def passed(name):
    GROUPS.append(name)
    print(f'PASS {name}', flush=True)


def fbits(value):
    if value == 'nan':
        return 0x7FC00000
    return struct.unpack('<I', struct.pack('<f', value))[0]


def fields(value):
    floats = {'kp', 'ki', *py.SNAPSHOT_FIELDS[6:], *py.SAMPLE_FIELDS[3:10], 'target_velocity_rpm'}
    return [fbits(v) if k in floats else int(v) for k, v in value.items()]


def object_value(value):
    return {k: float('nan') if v == 'nan' else v for k, v in value.items()}


def same_values(actual, expected):
    check(list(actual) == list(expected), (actual, expected))
    check(fields(actual) == fields(expected), (actual, expected))


def rejected(function, expected):
    try:
        function()
    except py.CodecError as error:
        check(error.code == expected, (error.code, expected))
    else:
        raise AssertionError(f'not rejected: {expected}')


def checked_run(arguments, directory):
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    check(result.returncode == 0 and 'warning' not in (result.stdout + result.stderr).lower(),
          result.stdout + result.stderr)


def raw_wire(raw, refresh=True):
    if refresh:
        raw = raw[:-2] + struct.pack('<H', binascii.crc_hqx(raw[:-2], 0xFFFF))
    return py.cobs_encode(raw) + b'\x00'


def run_tests(temporary):
    scatter = temporary / 'model.sct'
    scatter.write_text('LR_IROM1 0x08000000 0x00040000 {\n'
                       ' ER_IROM1 0x08000000 0x00040000 { .ANY (+RO) }\n'
                       ' RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n')
    sources = [COMM / 'core_protocol.c', COMM / 'core_protocol_values.c', TESTS / 'core_protocol_model.c']
    objects = []
    for source in sources:
        obj = temporary / (source.stem + '.o')
        checked_run([str(COMPILER / 'armcc.exe'), '--cpu=Cortex-M4.fp', '--c99', '-O3',
                     '--split_sections', '-g', '-I', str(COMM), '-I', str(REPO / 'communication/protocol/device'), '-c', str(source), '-o', str(obj)], temporary)
        objects.append(str(obj))
    executable = temporary / 'model.axf'
    checked_run([str(COMPILER / 'armlink.exe'), '--cpu=Cortex-M4.fp', '--scatter', str(scatter),
                 '--entry=model_init', '--keep=core_protocol_model.o(*)', '-o', str(executable), *objects], temporary)
    with executable.open('rb') as stream:
        elf = ELFFile(stream)
        segments = [(s['p_vaddr'], s.data()) for s in elf.iter_segments() if s['p_type'] == 'PT_LOAD']
        initializers = [(s['sh_addr'], s.data()) for s in elf.iter_sections()
                        if s['sh_flags'] & 2 and s['sh_type'] == 'SHT_PROGBITS']
        symbols = {s.name: s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()}
        undefined = [s.name for s in elf.get_section_by_name('.symtab').iter_symbols()
                     if s['st_shndx'] == 'SHN_UNDEF' and s.name]
        check(not undefined, undefined)

    class Model:
        def __init__(self):
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            self.cpu.mem_map(0x08000000, 0x40000)
            self.cpu.mem_map(0x20000000, 0x8000)
            self.cpu.mem_map(0xE000E000, 0x1000)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack('<I', 0x00F00000))
            self.call('model_init')

        def call(self, name):
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0803F001)
            self.cpu.emu_start(symbols[name] | 1, 0x0803F000, count=2000000)
            check(self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, name)

        def put(self, name, values, fmt='I'):
            if not isinstance(values, (tuple, list)):
                values = [values]
            self.cpu.mem_write(symbols[name], struct.pack('<' + fmt * len(values), *values))

        def get(self, name, count=1, fmt='I'):
            result = list(struct.unpack('<' + fmt * count,
                                       self.cpu.mem_read(symbols[name], struct.calcsize(fmt) * count)))
            return result[0] if count == 1 else result

        def run(self, op, data=b'', *, capacity=130, id=0, write=False, meta=None, values=None, now=0):
            check(len(data) <= 1024)
            self.cpu.mem_write(symbols['model_input'], bytes(data) or b'\x00')
            for name, value in [('length', len(data)), ('capacity', capacity), ('id', id),
                                ('write', int(write)), ('now', now & 0xFFFFFFFF)]:
                self.put('model_' + name, value)
            if values is not None:
                self.put('model_fields', values, 'Q')
            self.put('model_meta', list(meta or [0] * 7) + [op])
            self.call('model_run')
            check(self.get('model_decode_changed_on_reject') == 0, 'decoder altered rejected output')
            n = self.get('model_size')
            output = bytes(self.cpu.mem_read(symbols['model_output'], 144))
            check(output[:4] == b'\xa5' * 4 and output[134:] == b'\xa5' * 10, 'output canary')
            if n == 0x1234:
                check(output == b'\xa5' * 144, 'rejection must not alter output')
                return self.get('model_status'), b''
            check(n <= capacity, (n, capacity))
            check(output[4 + n:] == b'\xa5' * (140 - n), 'write beyond actual length')
            return self.get('model_status'), output[4:4 + n]

        def feed(self, data, now=0):
            self.run(20, data, now=now)

        def messages(self):
            n = self.get('model_count')
            check(n <= 128)
            if n == 0:
                return []
            statuses = self.get('model_statuses', n)
            seq = self.get('model_sequences', n)
            return [(statuses, seq)] if n == 1 else list(zip(statuses, seq))

    model = Model()
    check(model.run(0, b'123456789')[0] == 0x29B1)
    check(py.crc16(b'123456789') == 0x29B1)
    check(py.crc16(b'') == 0xFFFF)
    for vector in VECTORS['cobs']:
        raw, encoded = bytes.fromhex(vector['raw_hex']), bytes.fromhex(vector['encoded_hex'])
        check(py.cobs_encode(raw) == encoded)
        check(py.cobs_decode(encoded) == raw)
        check(model.run(1, raw) == (0, encoded))
        check(model.run(2, encoded) == (0, raw))
    passed('CRC published check and independent fixed COBS vectors')

    def payload_encode(vector):
        value, message = vector['value'], vector['message_id']
        typ = vector['type']
        if typ == 'request':
            meta = [value.get('subject_id', 0), value.get('proposed_session_id', 0),
                    fbits(value.get('target_velocity_rpm', 0)), 0, 0, 0, 0]
            vals = fields(value['value']) if 'value' in value else None
            actual = model.run(13, id=message, meta=meta, values=vals)
            check(model.run(13, id=message, meta=meta, values=vals,
                            capacity=len(bytes.fromhex(vector['payload_hex'])) - 1)[0] == py.Result.LENGTH_MISMATCH)
            expected = py.encode_request(value)
        elif typ == 'response':
            meta = [value['result_code'], value['subject_id'], value.get('motor_result', 0xFFFF),
                    value.get('accepted_session_id', 0), 0, 0, 0]
            vals = fields(value['value']) if 'value' in value else None
            actual = model.run(15, id=message, meta=meta, values=vals)
            check(model.run(15, id=message, meta=meta, values=vals,
                            capacity=len(bytes.fromhex(vector['payload_hex'])) - 1)[0] == py.Result.LENGTH_MISMATCH)
            expected = py.encode_response(message, value)
        elif typ == 'sample':
            actual = model.run(17, values=fields(value))
            check(model.run(17, values=fields(value), capacity=71)[0] == py.Result.LENGTH_MISMATCH)
            expected = py.encode_sample(object_value(value))
        else:
            actual = model.run(19, id=message, values=fields(value))
            check(model.run(19, id=message, values=fields(value), capacity=15)[0] == py.Result.LENGTH_MISMATCH)
            expected = py.encode_event(message, value)
        check(actual == (0, bytes.fromhex(vector['payload_hex'])), vector['name'])
        check(expected == actual[1], vector['name'])

    def payload_decode(vector):
        payload, value, message = bytes.fromhex(vector['payload_hex']), vector['value'], vector['message_id']
        typ = vector['type']
        if typ == 'request':
            check(model.run(12, payload, id=message)[0] == 0)
            actual = py.decode_request(py.Frame(1, message, vector['session_id'], vector['sequence'], payload))
            meta = model.get('model_meta', 7)
            check(meta[0] == value.get('subject_id', 0) and meta[1] == value.get('proposed_session_id', 0))
            check(meta[2] == fbits(value.get('target_velocity_rpm', 0)))
        elif typ == 'response':
            check(model.run(14, payload, id=message)[0] == 0)
            actual = py.decode_response(message, payload)
            meta = model.get('model_meta', 7)
            check(meta[:4] == [value['result_code'], value['subject_id'], value.get('motor_result', 0xFFFF),
                               value.get('accepted_session_id', 0)])
        elif typ == 'sample':
            check(model.run(16, payload)[0] == 0)
            actual = py.decode_sample(payload)
            check(model.get('model_fields', len(value), 'Q') == fields(value))
        else:
            check(model.run(18, payload, id=message)[0] == 0)
            actual = py.decode_event(message, payload)
            check(model.get('model_fields', len(value), 'Q') == fields(value))
        if 'value' in value:
            same_values(actual.pop('value'), value['value'])
            check(model.get('model_fields', len(value['value']), 'Q') == fields(value['value']))
            check(actual == {k: v for k, v in value.items() if k != 'value'})
        else:
            same_values(actual, value)

    for vector in VECTORS['frames']:
        wire, raw = bytes.fromhex(vector['wire_hex']), bytes.fromhex(vector['decoded_hex'])
        payload = bytes.fromhex(vector['payload_hex'])
        check(py.cobs_decode(wire[:-1]) == raw)
        check(binascii.crc_hqx(raw[:-2], 0xFFFF) == int.from_bytes(raw[-2:], 'little'))
        frame = py.Frame(vector['kind'], vector['message_id'], vector['session_id'], vector['sequence'], payload)
        check(py.encode_frame(frame) == wire, vector['name'])
        check(py.decode_frame(wire[:-1]) == (py.Status.OK, frame))
        check(model.run(3, wire[:-1]) == (0, wire))
        meta = [1, vector['kind'], 0, 16, vector['message_id'], vector['session_id'], vector['sequence']]
        check(model.get('model_meta', 7) == meta)
        check(model.run(4, payload, meta=meta) == (0, wire))
        payload_encode(vector)
        payload_decode(vector)
    passed('30 fixed golden frames: independent C/Python encode and typed decode, all layouts')

    rng = random.Random(0x431C0B5)
    for n in range(129):
        for raw in (bytes(n), b'\xFF' * n, bytes(rng.randrange(256) for _ in range(n))):
            encoded = py.cobs_encode(raw)
            check(model.run(1, raw) == (0, encoded))
            check(model.run(2, encoded) == (0, raw))
            check(py.cobs_decode(encoded) == raw)
            check(model.run(1, raw, capacity=len(encoded) - 1)[0] == py.Status.CAPACITY)
            if raw:
                check(model.run(2, encoded, capacity=len(raw) - 1)[0] == py.Status.CAPACITY)
    for candidate in (b'', b'\x00', b'\x02', b'\x03\x11', b'\x02\x00', b'\xff' + b'\x01' * 128):
        check(model.run(2, candidate)[0] == py.Status.COBS_ERROR)
        rejected(lambda: py.cobs_decode(candidate), py.Status.COBS_ERROR)
    passed('COBS every size 0..128, zeros/nonzeros/random, malformed blocks and capacity canaries')

    for n in range(111):
        payload = bytes(rng.randrange(256) for _ in range(n))
        frame = py.Frame(1 + n % 4, 0xABCD, 0x01020304, 0x7FFFFFFF, payload)
        wire = py.encode_frame(frame)
        meta = [1, frame.kind, 0, 16, frame.message_id, frame.session_id, frame.sequence]
        check(len(wire) == 20 + n)
        check(model.run(4, payload, meta=meta, capacity=len(wire)) == (0, wire))
        check(model.run(4, payload, meta=meta, capacity=len(wire) - 1)[0] == py.Status.CAPACITY)
        check(model.run(3, wire[:-1]) == (0, wire))
    check(len(py.encode_frame(py.Frame(2, 2, 1, 1, bytes(110)))) == 130)
    rejected(lambda: py.encode_frame(py.Frame(1, 1, 1, 1, bytes(111))), py.Status.LENGTH_ERROR)
    check(model.run(4, bytes(111), meta=[1, 1, 0, 16, 1, 1, 1])[0] == py.Status.LENGTH_ERROR)
    passed('all payload lengths 0..110 and exact 130-byte wire limit; no trailing writes')

    # CRC独立重算只用于构造“完整性可信但语义非法”的输入；bit-flip保持原CRC。
    base = bytes.fromhex(VECTORS['frames'][0]['decoded_hex'])
    good = bytes.fromhex(VECTORS['frames'][0]['wire_hex'])
    failures = []
    for offset, byte, status in [(0, 2, py.Status.FRAME_VERSION), (1, 5, py.Status.FRAME_KIND),
                                  (2, 1, py.Status.FRAME_FLAGS), (3, 15, py.Status.LENGTH_ERROR),
                                  (4, 111, py.Status.LENGTH_ERROR)]:
        raw = bytearray(base); raw[offset] = byte
        failures.append((raw_wire(raw), status))
    failures += [(raw_wire(base + b'x'), py.Status.LENGTH_ERROR),
                 (b'\x02\x00', py.Status.COBS_ERROR),
                 (py.cobs_encode(base[:16]) + b'\x00', py.Status.LENGTH_ERROR)]
    for bit in range(len(base) * 8):
        raw = bytearray(base); raw[bit // 8] ^= 1 << (bit % 8)
        wire = raw_wire(raw, refresh=False)
        status = py.Status.LENGTH_ERROR if bit // 8 in (4, 5) else py.Status.CRC_ERROR
        failures.append((wire, status))
    for wire, status in failures:
        check(model.run(3, wire[:-1])[0] == status)
        if status.value >= 6:
            check(py.decode_frame(wire[:-1])[0] == status)
        else:
            rejected(lambda: py.decode_frame(wire[:-1]), status)
        parser = py.Parser(); model.run(23)
        results = parser.feed(wire + good, 0); model.feed(wire + good)
        check(results[-1][1].sequence == 1 and results[-1][0] == py.Status.OK)
        expected = [(int(s), f.sequence) for s, f in results]
        check(model.messages() == expected)
        check(model.get('model_stats', 8) == list(parser.stats.values()))
    passed('every golden OpenSession bit flip, bad lengths/header/flags/version/kind, good-frame recovery')

    for vector in VECTORS['frames']:
        wire = bytes.fromhex(vector['wire_hex'])
        for split in range(len(wire) + 1):
            parser = py.Parser(); model.run(23)
            results = parser.feed(wire[:split], 10); model.feed(wire[:split], 10)
            results += parser.feed(wire[split:], 11); model.feed(wire[split:], 11)
            check(len(results) == 1 and model.messages() == [(0, vector['sequence'])])
    joined = b'\x00\x00' + b''.join(bytes.fromhex(v['wire_hex']) for v in VECTORS['frames']) + b'\x00'
    for step in (1, 7, 31, 127, 511):
        parser = py.Parser(); model.run(23); results = []
        for start in range(0, len(joined), step):
            chunk = joined[start:start + step]
            results += parser.feed(chunk, 1); model.feed(chunk, 1)
        check(model.messages() == [(0, v['sequence']) for v in VECTORS['frames']])
        check([f.sequence for _, f in results] == [v['sequence'] for v in VECTORS['frames']])
    for _ in range(20):
        parser = py.Parser(); model.run(23); results = []; start = 0
        while start < len(joined):
            chunk = joined[start:start + rng.randint(1, 170)]
            results += parser.feed(chunk, 5); model.feed(chunk, 5)
            start += len(chunk)
        check(model.messages() == [(0, v['sequence']) for v in VECTORS['frames']])
        check(len(results) == len(VECTORS['frames']))
    passed('every split point of 30 frames; bytewise and continuous multi-frame chunks with empty delimiters')

    for beginning in (0, 0xFFFFFFF5):
        for elapsed in (19, 20, 21):
            parser = py.Parser(); model.run(23)
            parser.feed(good[:5], beginning); model.feed(good[:5], beginning)
            now = (beginning + elapsed) & 0xFFFFFFFF
            parser.poll(now); model.run(21, now=now)
            result = parser.feed(good[5:] + good, now); model.feed(good[5:] + good, now)
            check(len(result) == (2 if elapsed == 19 else 1))
            check(model.messages() == [(0, 1)] * len(result))
            check(model.get('model_stats', 8) == list(parser.stats.values()))
    parser = py.Parser(); model.run(23)
    parser.feed(good[:5], 0); model.feed(good[:5])
    parser.discontinuity(); model.run(22)
    result = parser.feed(good + good, 1); model.feed(good + good, 1)
    check(len(result) == 1 and model.messages() == [(0, 1)])
    for count in (130, 512, 900):
        parser = py.Parser(); model.run(23)
        result = parser.feed(b'\x01' * count + good, 0); model.feed(b'\x01' * count + good)
        check(result == [] and model.messages() == [])
        result = parser.feed(good, 1); model.feed(good, 1)
        check(len(result) == 1 and model.messages() == [(0, 1)])
        check(parser.stats['length_errors'] == 1 and model.get('model_stats', 8) == list(parser.stats.values()))
    passed('19/20/21ms inactivity boundary, u32 wrap, discontinuity and overflow discard until delimiter')

    # 固定对象精确长度、只读边界及保留值，不经过业务层。
    for vector in VECTORS['frames']:
        if vector['type'] != 'response' or 'value' not in vector['value']:
            continue
        id = vector['value']['subject_id']; expected = vector['value']['value']
        data = bytes.fromhex(vector['payload_hex'])[4:]
        check(model.run(10, data, id=id)[0] == 0)
        check(model.get('model_fields', len(expected), 'Q') == fields(expected))
        check(model.run(11, id=id, values=fields(expected)) == (0, data))
        check(model.run(11, id=id, values=fields(expected), capacity=len(data) - 1)[0] == py.Result.LENGTH_MISMATCH)
        for bad in (data[:-1], data + b'\x00'):
            check(model.run(10, bad, id=id)[0] == py.Result.LENGTH_MISMATCH)
            rejected(lambda: py.decode_object(id, bad), py.Result.LENGTH_MISMATCH)
        if id not in py.WRITE_SIZES:
            check(model.run(10, data, id=id, write=True)[0] == py.Result.READ_ONLY)
            rejected(lambda: py.decode_object(id, data, write=True), py.Result.READ_ONLY)
    check(model.run(10, id=0xFFFF)[0] == py.Result.UNKNOWN_OBJECT)
    rejected(lambda: py.decode_object(0xFFFF, b''), py.Result.UNKNOWN_OBJECT)
    passed('all seven object layouts, little-endian 64-bit fields, exact read/write lengths and read-only objects')

    for id, data, expected in [(0x103, struct.pack('<2fI4B', .001, .004, 1, 2, 0, 0, 0), py.Result.BAD_PAYLOAD),
                                (0x103, struct.pack('<2fI4B', .001, .004, 1, 1, 0, 1, 0), py.Result.BAD_PAYLOAD),
                                (0x201, bytes.fromhex('0101fa0001000000'), py.Result.BAD_PAYLOAD),
                                (0x201, bytes.fromhex('0200fa0001000000'), py.Result.BAD_VALUE),
                                (0x201, bytes.fromhex('0100fb0001000000'), py.Result.BAD_VALUE)]:
        check(model.run(10, data, id=id)[0] == expected)
        rejected(lambda: py.decode_object(id, data), expected)
    for kp, ki in ((.0005, 0), (.005, .02), (.001, .004), (.00049, 0), (.00501, 0), (.001, -.01),
                   (.001, .0201), (math.nan, 0), (.001, math.inf), (-math.inf, 0)):
        data = struct.pack('<2f', kp, ki)
        expected = 0 if math.isfinite(kp) and math.isfinite(ki) and .0005 <= kp <= .005 and 0 <= ki <= .02 else py.Result.BAD_VALUE
        check(model.run(10, data, id=0x102, write=True)[0] == expected)
        if expected:
            rejected(lambda: py.decode_object(0x102, data, write=True), expected)
        else:
            py.decode_object(0x102, data, write=True)
    passed('PI binary32 endpoints/nonfinite/ranges and telemetry/active reserved bytes')

    request_failures = [(1, bytes(4), py.Result.BAD_VALUE), (1, bytes(3), py.Result.BAD_PAYLOAD),
                        (2, b'\x02', py.Result.BAD_PAYLOAD), (2, b'\xff\xff', py.Result.UNKNOWN_OBJECT),
                        (3, bytes.fromhex('02010000'), py.Result.LENGTH_MISMATCH),
                        (3, b'\x01\x00', py.Result.READ_ONLY), (0x9999, b'', py.Result.UNKNOWN_MESSAGE),
                        (4, b'\x99\x00', py.Result.UNKNOWN_COMMAND),
                        (4, b'\x03\x00\x01', py.Result.BAD_PAYLOAD), (4, b'\x04', py.Result.BAD_PAYLOAD)]
    for target in (math.nan, math.inf, -math.inf, -80.01, 80.01):
        request_failures.append((4, struct.pack('<Hf', 1, target), py.Result.BAD_VALUE))
    for id, data, expected in request_failures:
        check(model.run(12, data, id=id)[0] == expected)
        rejected(lambda: py.decode_request(py.Frame(1, id, 0, 1, data)), expected)
    for id in (1, 2, 3, 4):
        data = bytes.fromhex('01020000') if id != 4 else bytes.fromhex('01020000ffff0000')
        check(model.run(14, data, id=id)[0] == 0)
        for bad in (data[:-1], data + b'\x00'):
            check(model.run(14, bad, id=id)[0] == py.Result.BAD_PAYLOAD)
            rejected(lambda: py.decode_response(id, bad), py.Result.BAD_PAYLOAD)
    check(model.run(14, bytes.fromhex('01020100'), id=1)[0] == py.Result.BAD_PAYLOAD)
    rejected(lambda: py.decode_response(1, bytes.fromhex('01020100')), py.Result.BAD_PAYLOAD)
    for data in (bytes.fromhex('00000100ffff0000'), bytes.fromhex('0102010000000000'),
                 bytes.fromhex('0000010007000000'), bytes.fromhex('0000010000000100')):
        expected = py.Result.BAD_PAYLOAD if data[-2:] != b'\x00\x00' else py.Result.BAD_VALUE
        check(model.run(14, data, id=4)[0] == expected)
        rejected(lambda: py.decode_response(4, data), expected)
    passed('request shells, finite command values, malformed Stop, unknown IDs and terminal response result layers')

    full = next(v for v in VECTORS['frames'] if v['name'] == 'speed_sample_full')
    for bit in range(10):
        value = dict(full['value']); value['valid_flags'] &= ~(1 << bit)
        expected = py.encode_sample(value)
        check(model.run(17, values=fields(value)) == (0, expected))
        check(model.run(16, expected)[0] == 0)
        py.decode_sample(expected)
    for field in py.SAMPLE_FIELDS[3:10]:
        for invalid in (math.nan, math.inf, -math.inf):
            value = dict(full['value']); value[field] = invalid
            expected = py.encode_sample(value)
            check(model.run(17, values=fields(value)) == (0, expected))
            check(model.run(16, expected)[0] == 0)
    for offset in (56, 60, 62):
        bad = bytearray(bytes.fromhex(full['payload_hex']))
        bad[offset + (1 if offset == 56 else 0)] |= 0x80
        check(model.run(16, bad)[0] == py.Result.BAD_PAYLOAD)
        rejected(lambda: py.decode_sample(bad), py.Result.BAD_PAYLOAD)
    invalid = bytes.fromhex(next(v for v in VECTORS['frames'] if v['name'] == 'speed_sample_invalid')['payload_hex'])
    for offset in (4, 12, 20, 48, 52, 64, 68):
        bad = bytearray(invalid); bad[offset] ^= 1
        check(model.run(16, bad)[0] == py.Result.BAD_VALUE)
        rejected(lambda: py.decode_sample(bad), py.Result.BAD_VALUE)
    for n in (0, 71, 73):
        check(model.run(16, bytes(n))[0] == py.Result.BAD_PAYLOAD)
        rejected(lambda: py.decode_sample(bytes(n)), py.Result.BAD_PAYLOAD)
    for message in (0x2001, 0x2002):
        for n in (0, 15, 17):
            check(model.run(18, bytes(n), id=message)[0] == py.Result.BAD_PAYLOAD)
            rejected(lambda: py.decode_event(message, bytes(n)), py.Result.BAD_PAYLOAD)
    model.call('model_null_checks'); check(model.get('model_status') == 0)
    passed('all sample validity bits, canonical NaN/nonfinite, invalid integer groups, event lengths and null arguments')

    # 源码依赖检查与链接结果补充证明模型未悄悄使用设备或内核替身。
    for source in [COMM / 'core_protocol.h', COMM / 'core_protocol.c',
                   COMM / 'core_protocol_values.h', COMM / 'core_protocol_values.c',
                   REPO / 'communication/protocol/device/motor_node_profile.h', REPO / 'communication/protocol/device/protocol_result.h']:
        includes = [line for line in source.read_text(encoding='utf-8').splitlines() if line.startswith('#include')]
        check(not any(token in line.lower() for token in ('hal', 'stm32', 'freertos', 'motor.h') for line in includes))
    passed('actual ARMCC Cortex-M4F compile/link/execute without warnings, HAL, RTOS or device dependencies')
    return sources


def main():
    arguments = argparse.ArgumentParser()
    arguments.add_argument('--report', type=Path, help='可选：保存本次离线验证摘要')
    args = arguments.parse_args()
    with tempfile.TemporaryDirectory(prefix='core-protocol-') as directory:
        sources = run_tests(Path(directory))
    compiler_info = subprocess.run([str(COMPILER / 'armcc.exe'), '--vsn'],
                                   capture_output=True, text=True, check=True)
    compiler_version = next(line.removeprefix('Component: ').strip()
                            for line in (compiler_info.stdout + compiler_info.stderr).splitlines()
                            if line.startswith('Component: '))
    report = dict(checkpoint='P1', status='PASS', groups=GROUPS, assertions=ASSERTIONS,
                  golden_frames=len(VECTORS['frames']), golden_sha256=hashlib.sha256(VECTOR_PATH.read_bytes()).hexdigest(),
                  compiler=compiler_version, cpu='Cortex-M4.fp', execution='Unicorn ' + unicorn_version,
                  source_sha256={str(p.relative_to(REPO)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sources + [COMM / 'core_protocol.h', COMM / 'core_protocol_values.h',
                                                    REPO / 'communication/host/protocol/codec.py', TESTS / 'verify_core_protocol.py',
                                                    REPO / 'communication/protocol/device/motor_node_profile.h',
                                                    REPO / 'communication/protocol/device/protocol_result.h']},
                  hardware_access=False, linked_to_run=False,
                  limitations='离线库模型，不证明串口/DMA/RTOS、会话事务或实物控制；现有Run未改动。')
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(status='PASS', groups=len(GROUPS), assertions=ASSERTIONS, golden_frames=len(VECTORS['frames']))))


if __name__ == '__main__':
    main()
