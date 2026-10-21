"""纯标准库 Core Protocol V1。只处理线格式，不执行设备命令或管理会话。"""

from dataclasses import dataclass, field
from enum import IntEnum
import math
import struct


HEADER_SIZE = 16
PAYLOAD_MAX = 110
DECODED_MAX = 128
ENCODED_MAX = 129
WIRE_MAX = 130
PARTIAL_TIMEOUT_MS = 20
NAN_BITS = 0x7FC00000
NAN = struct.unpack('<f', struct.pack('<I', NAN_BITS))[0]


class Status(IntEnum):
    OK = 0
    BAD_ARGUMENT = 1
    CAPACITY = 2
    COBS_ERROR = 3
    LENGTH_ERROR = 4
    CRC_ERROR = 5
    FRAME_VERSION = 6
    FRAME_FLAGS = 7
    FRAME_KIND = 8


class Result(IntEnum):
    OK = 0
    BAD_REQUEST = 0x0101
    UNKNOWN_MESSAGE = 0x0102
    BAD_PAYLOAD = 0x0103
    UNSUPPORTED_FLAGS = 0x0104
    UNSUPPORTED_VERSION = 0x0105
    UNKNOWN_COMMAND = 0x0106
    SESSION_REQUIRED = 0x0201
    SESSION_INVALID = 0x0202
    SEQUENCE_CONFLICT = 0x0203
    IN_PROGRESS = 0x0204
    SEQUENCE_INVALID = 0x0205
    SESSION_BUSY = 0x0206
    UNKNOWN_OBJECT = 0x0301
    READ_ONLY = 0x0302
    BAD_VALUE = 0x0303
    OBJECT_WRONG_STATE = 0x0304
    LENGTH_MISMATCH = 0x0305
    BUSY = 0x0401
    CANCELLED_BY_STOP = 0x0402
    PRECONDITION_FAILED = 0x0403
    SYSTEM_FAIL_CLOSED = 0x0501
    INTERNAL_ERROR = 0x0502


class CodecError(ValueError):
    def __init__(self, code: Status | Result):
        super().__init__(code.name)
        self.code = code


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    if len(data) > DECODED_MAX:
        raise CodecError(Status.LENGTH_ERROR)
    # V1最大128字节，不存在254字节的长块。
    blocks = data.split(b'\x00')
    return b''.join(bytes([len(block) + 1]) + block for block in blocks)


def cobs_decode(data: bytes) -> bytes:
    if not data or len(data) > ENCODED_MAX:
        raise CodecError(Status.COBS_ERROR)
    output = bytearray()
    index = 0
    while index < len(data):
        code = data[index]
        end = index + code
        if code == 0 or end > len(data) or 0 in data[index + 1:end]:
            raise CodecError(Status.COBS_ERROR)
        output.extend(data[index + 1:end])
        index = end
        if code != 255 and index < len(data):
            output.append(0)
    if len(output) > DECODED_MAX:
        raise CodecError(Status.LENGTH_ERROR)
    return bytes(output)


@dataclass(frozen=True)
class Frame:
    kind: int
    message_id: int
    session_id: int
    sequence: int
    payload: bytes = b''
    version: int = 1
    flags: int = 0
    header_size: int = HEADER_SIZE


def header_status(frame: Frame) -> Status:
    if frame.version != 1:
        return Status.FRAME_VERSION
    if frame.flags:
        return Status.FRAME_FLAGS
    if frame.kind not in (1, 2, 3, 4):
        return Status.FRAME_KIND
    return Status.OK


def encode_frame(frame: Frame) -> bytes:
    if len(frame.payload) > PAYLOAD_MAX or frame.header_size != HEADER_SIZE:
        raise CodecError(Status.LENGTH_ERROR)
    status = header_status(frame)
    if status != Status.OK:
        raise CodecError(status)
    raw = struct.pack('<4BHHII', frame.version, frame.kind, frame.flags, frame.header_size,
                      len(frame.payload), frame.message_id, frame.session_id, frame.sequence) + frame.payload
    return cobs_encode(raw + struct.pack('<H', crc16(raw))) + b'\x00'


def decode_frame(candidate: bytes) -> tuple[Status, Frame]:
    """输入不含delimiter；只有完整性可信才返回Frame。调用方仅将OK交给业务处理。"""
    raw = cobs_decode(candidate)
    if len(raw) < HEADER_SIZE + 2:
        raise CodecError(Status.LENGTH_ERROR)
    version, kind, flags, size, length, message, session, sequence = struct.unpack_from('<4BHHII', raw)
    if length > PAYLOAD_MAX or len(raw) != HEADER_SIZE + length + 2:
        raise CodecError(Status.LENGTH_ERROR)
    if crc16(raw[:-2]) != struct.unpack_from('<H', raw, len(raw) - 2)[0]:
        raise CodecError(Status.CRC_ERROR)
    if size != HEADER_SIZE:
        raise CodecError(Status.LENGTH_ERROR)
    frame = Frame(kind, message, session, sequence, raw[HEADER_SIZE:-2], version, flags, size)
    return header_status(frame), frame


@dataclass
class Parser:
    candidate: bytearray = field(default_factory=bytearray)
    last_byte_ms: int = 0
    discarding: bool = False
    stats: dict[str, int] = field(default_factory=lambda: dict.fromkeys(
        ('cobs_errors', 'crc_errors', 'length_errors', 'version_errors', 'flags_errors',
         'message_errors', 'timeouts', 'discontinuities'), 0))

    def _count(self, name: str) -> None:
        self.stats[name] = (self.stats[name] + 1) & 0xFFFFFFFF

    def poll(self, now_ms: int) -> None:
        if self.candidate and ((now_ms - self.last_byte_ms) & 0xFFFFFFFF) >= PARTIAL_TIMEOUT_MS:
            self.candidate.clear()
            self.discarding = True
            self._count('timeouts')

    def discontinuity(self) -> None:
        self.candidate.clear()
        self.discarding = True
        self._count('discontinuities')

    def feed(self, data: bytes, now_ms: int) -> list[tuple[Status, Frame]]:
        self.poll(now_ms)
        frames = []
        counters = {Status.COBS_ERROR: 'cobs_errors', Status.LENGTH_ERROR: 'length_errors',
                    Status.CRC_ERROR: 'crc_errors', Status.FRAME_VERSION: 'version_errors',
                    Status.FRAME_FLAGS: 'flags_errors', Status.FRAME_KIND: 'message_errors'}
        for byte in data:
            if byte == 0:
                if self.candidate and not self.discarding:
                    try:
                        status, frame = decode_frame(bytes(self.candidate))
                        frames.append((status, frame))
                    except CodecError as error:
                        status = error.code
                    if status != Status.OK:
                        self._count(counters[status])
                self.candidate.clear()
                self.discarding = False
            elif not self.discarding:
                if len(self.candidate) == ENCODED_MAX:
                    self.candidate.clear()
                    self.discarding = True
                    self._count('length_errors')
                else:
                    self.candidate.append(byte)
                    self.last_byte_ms = now_ms & 0xFFFFFFFF
        return frames


# 固定线上布局，不是运行时对象数据库。read/write的长度分别检查。
DEVICE_INFO, MOTOR_SNAPSHOT, COMMUNICATION_STATUS = 1, 2, 3
SPEED_PI_DEFAULT, SPEED_PI_STAGED, SPEED_PI_ACTIVE, TELEMETRY_CONFIG = 0x101, 0x102, 0x103, 0x201
READ_SIZES = {1: 24, 2: 48, 3: 104, 0x101: 8, 0x102: 12, 0x103: 16, 0x201: 8}
WRITE_SIZES = {0x102: 8, 0x201: 4}
DEVICE_FIELDS = ('protocol_version', 'device_contract_version', 'target_id', 'control_profile_id',
                 'capability_flags', 'control_time_ticks_per_second', 'firmware_build_id')
SNAPSHOT_FIELDS = ('control_time_ticks', 'control_time_ticks_per_second', 'motor_state', 'fault_flags',
                   'motor_command_result', 'valid_flags', 'target_velocity_rpm', 'velocity_rpm',
                   'id_a', 'iq_a', 'bus_voltage_v')
STATUS_FIELDS = tuple('session_id rx_bytes tx_bytes event_drop_count rx_discontinuities rx_dma_overruns '
                     'uart_pe uart_fe uart_ne uart_ore cobs_errors crc_errors length_errors version_errors '
                     'message_errors parser_timeouts response_fifo_high_water completion_fifo_high_water '
                     'event_fifo_high_water telemetry_overwrites communication_stack_min_words '
                     'telemetry_stack_min_words internal_fault_reason internal_fault_count tx_busy_count tx_errors'.split())
SAMPLE_FIELDS = ('sample_sequence', 'control_time_ticks', 'position_observation_time',
                 'target_velocity_rpm', 'velocity_rpm', 'mechanical_position_turns', 'iq_reference_a',
                 'iq_feedback_a', 'id_feedback_a', 'bus_voltage_v', 'motor_state', 'fault_flags',
                 'valid_flags', 'limit_flags', 'active_profile_generation', 'telemetry_config_generation')
SAMPLE_FLOAT_BITS = (4, 8, 2, 16, 32, 64, 128)


def f32(value: float) -> float:
    try:
        return struct.unpack('<f', struct.pack('<f', value))[0]
    except (OverflowError, struct.error) as error:
        raise CodecError(Result.BAD_VALUE) from error


def _pack(fmt: str, *values) -> bytes:
    try:
        return struct.pack('<' + fmt, *values)
    except (OverflowError, struct.error) as error:
        raise CodecError(Result.BAD_VALUE) from error


def _observations(values: list[float], bits: tuple[int, ...], valid: int) -> tuple[list[float], int]:
    output = []
    for value, bit in zip(values, bits):
        value = f32(value)
        if not valid & bit or not math.isfinite(value):
            value = NAN
            valid &= ~bit
        output.append(value)
    return output, valid


def _check_observations(data: bytes, offset: int, bits: tuple[int, ...], valid: int) -> None:
    for i, bit in enumerate(bits):
        value = struct.unpack_from('<f', data, offset + 4 * i)[0]
        binary = struct.unpack_from('<I', data, offset + 4 * i)[0]
        canonical = math.isfinite(value) if valid & bit else binary == NAN_BITS
        if not canonical:
            raise CodecError(Result.BAD_VALUE)


def _object_size(object_id: int, write: bool) -> int:
    if object_id not in READ_SIZES:
        raise CodecError(Result.UNKNOWN_OBJECT)
    if write and object_id not in WRITE_SIZES:
        raise CodecError(Result.READ_ONLY)
    return (WRITE_SIZES if write else READ_SIZES)[object_id]


def decode_object(object_id: int, data: bytes, *, write: bool = False) -> dict:
    if len(data) != _object_size(object_id, write):
        raise CodecError(Result.LENGTH_MISMATCH)
    if object_id == DEVICE_INFO:
        value = dict(zip(DEVICE_FIELDS, struct.unpack('<4H2IQ', data)))
        if value['capability_flags'] & ~0x3F:
            raise CodecError(Result.BAD_VALUE)
        return value
    if object_id == MOTOR_SNAPSHOT:
        value = dict(zip(SNAPSHOT_FIELDS, struct.unpack('<Q5I5f', data)))
        valid = value['valid_flags']
        if valid & ~0x1FF:
            raise CodecError(Result.BAD_VALUE)
        _check_observations(data, 28, (2, 4, 8, 16, 32), valid)
        return value
    if object_id == COMMUNICATION_STATUS:
        return dict(zip(STATUS_FIELDS, struct.unpack('<26I', data)))
    if object_id in (SPEED_PI_DEFAULT, SPEED_PI_STAGED, SPEED_PI_ACTIVE):
        kp, ki = struct.unpack_from('<2f', data)
        if not (math.isfinite(kp) and math.isfinite(ki) and f32(.0005) <= kp <= f32(.005)
                and 0 <= ki <= f32(.02)):
            raise CodecError(Result.BAD_VALUE)
        value = dict(kp=kp, ki=ki)
        if not write and object_id != SPEED_PI_DEFAULT:
            value['generation'] = struct.unpack_from('<I', data, 8)[0]
        if object_id == SPEED_PI_ACTIVE:
            if data[12] > 1 or data[13:] != b'\x00' * 3:
                raise CodecError(Result.BAD_PAYLOAD)
            value['currently_in_use'] = bool(data[12])
        return value
    enabled, reserved, rate = struct.unpack_from('<BBH', data)
    if reserved:
        raise CodecError(Result.BAD_PAYLOAD)
    if enabled not in (0, 1) or rate not in (50, 250, 500):
        raise CodecError(Result.BAD_VALUE)
    value = dict(enabled=enabled, rate_hz=rate)
    if not write:
        value['generation'] = struct.unpack_from('<I', data, 4)[0]
    return value


def encode_object(object_id: int, value: dict, *, write: bool = False) -> bytes:
    _object_size(object_id, write)
    if object_id == DEVICE_INFO:
        data = _pack('4H2IQ', *(value[name] for name in DEVICE_FIELDS))
    elif object_id == MOTOR_SNAPSHOT:
        valid = value['valid_flags'] & 0x1FF
        floats, valid = _observations([value[name] for name in SNAPSHOT_FIELDS[6:]], (2, 4, 8, 16, 32), valid)
        data = _pack('Q5I5f', *(value[name] for name in SNAPSHOT_FIELDS[:5]), valid, *floats)
    elif object_id == COMMUNICATION_STATUS:
        data = _pack('26I', *(value[name] for name in STATUS_FIELDS))
    elif object_id in (SPEED_PI_DEFAULT, SPEED_PI_STAGED, SPEED_PI_ACTIVE):
        data = _pack('2f', value['kp'], value['ki'])
        if not write and object_id != SPEED_PI_DEFAULT:
            data += _pack('I', value['generation'])
        if object_id == SPEED_PI_ACTIVE:
            data += _pack('B3x', value['currently_in_use'])
    else:
        data = _pack('BxH', value['enabled'], value['rate_hz'])
        if not write:
            data += _pack('I', value['generation'])
    decode_object(object_id, data, write=write)
    return data


def decode_request(frame: Frame) -> dict:
    """验证固定格式和值域；不判会话/状态、不执行，不把Stop前缀当成完整Stop。"""
    if frame.version != 1:
        raise CodecError(Result.UNSUPPORTED_VERSION)
    if frame.flags:
        raise CodecError(Result.UNSUPPORTED_FLAGS)
    if frame.kind != 1 or frame.header_size != HEADER_SIZE or len(frame.payload) > PAYLOAD_MAX:
        raise CodecError(Result.BAD_REQUEST)
    data, message = frame.payload, frame.message_id
    if message == 1:
        if len(data) != 4:
            raise CodecError(Result.BAD_PAYLOAD)
        proposed, = struct.unpack('<I', data)
        if not proposed:
            raise CodecError(Result.BAD_VALUE)
        return dict(message_id=message, proposed_session_id=proposed)
    if message not in (2, 3, 4):
        raise CodecError(Result.UNKNOWN_MESSAGE)
    if len(data) < 2:
        raise CodecError(Result.BAD_PAYLOAD)
    subject, = struct.unpack_from('<H', data)
    value = dict(message_id=message, subject_id=subject)
    if message == 2:
        if len(data) != 2:
            raise CodecError(Result.BAD_PAYLOAD)
        _object_size(subject, False)
    elif message == 3:
        value['value'] = decode_object(subject, data[2:], write=True)
    elif subject in (1, 2):
        if len(data) != 6:
            raise CodecError(Result.BAD_PAYLOAD)
        target, = struct.unpack_from('<f', data, 2)
        if not math.isfinite(target) or not -80 <= target <= 80:
            raise CodecError(Result.BAD_VALUE)
        value['target_velocity_rpm'] = target
    elif subject in (3, 4):
        if len(data) != 2:
            raise CodecError(Result.BAD_PAYLOAD)
    else:
        raise CodecError(Result.UNKNOWN_COMMAND)
    return value


def encode_request(value: dict) -> bytes:
    message = value['message_id']
    if message == 1:
        data = _pack('I', value['proposed_session_id'])
    else:
        subject = value['subject_id']
        data = _pack('H', subject)
        if message == 3:
            data += encode_object(subject, value['value'], write=True)
        elif message == 4 and subject in (1, 2):
            data += _pack('f', value['target_velocity_rpm'])
    decode_request(Frame(1, message, 0, 1, data))
    return data


def decode_response(message: int, data: bytes) -> dict:
    if len(data) < 4:
        raise CodecError(Result.BAD_PAYLOAD)
    result, subject = struct.unpack_from('<2H', data)
    value = dict(result_code=result, subject_id=subject)
    if message == 1 and subject:
        raise CodecError(Result.BAD_PAYLOAD)
    if message == 4:
        if len(data) != 8 or data[6:] != b'\x00\x00':
            raise CodecError(Result.BAD_PAYLOAD)
        motor, = struct.unpack_from('<H', data, 4)
        if (result != 0 and motor != 0xFFFF) or (result == 0 and motor > 6):
            raise CodecError(Result.BAD_VALUE)
        value['motor_result'] = motor
    elif result != 0:
        if len(data) != 4:
            raise CodecError(Result.BAD_PAYLOAD)
    elif message == 1:
        if len(data) != 8:
            raise CodecError(Result.BAD_PAYLOAD)
        accepted, = struct.unpack_from('<I', data, 4)
        if not accepted:
            raise CodecError(Result.BAD_VALUE)
        value['accepted_session_id'] = accepted
    elif message in (2, 3):
        value['value'] = decode_object(subject, data[4:])
    else:
        raise CodecError(Result.UNKNOWN_MESSAGE)
    return value


def encode_response(message: int, value: dict) -> bytes:
    result, subject = value['result_code'], value['subject_id']
    data = _pack('2H', result, subject)
    if message == 4:
        data += _pack('H2x', value['motor_result'])
    elif result == 0:
        if message == 1:
            data += _pack('I', value['accepted_session_id'])
        elif message in (2, 3):
            data += encode_object(subject, value['value'])
        else:
            raise CodecError(Result.UNKNOWN_MESSAGE)
    decode_response(message, data)
    return data


def encode_sample(value: dict) -> bytes:
    valid = value['valid_flags'] & 0x3FF
    floats, valid = _observations([value[name] for name in SAMPLE_FIELDS[3:10]], SAMPLE_FLOAT_BITS, valid)
    data = _pack('I2Q7f3IHH2I', value['sample_sequence'],
                 value['control_time_ticks'] if valid & 1 else 0,
                 value['position_observation_time'] if valid & 2 else 0, *floats,
                 value['motor_state'] if valid & 256 else 0, value['fault_flags'] if valid & 256 else 0,
                 valid, value['limit_flags'] & 15, 0,
                 value['active_profile_generation'] if valid & 512 else 0,
                 value['telemetry_config_generation'] if valid & 512 else 0)
    return data


def decode_sample(data: bytes) -> dict:
    if len(data) != 72:
        raise CodecError(Result.BAD_PAYLOAD)
    fields = list(struct.unpack('<I2Q7f3IHH2I', data))
    reserved = fields.pop(14)
    value = dict(zip(SAMPLE_FIELDS, fields))
    valid = value['valid_flags']
    if valid & ~0x3FF or value['limit_flags'] & ~15 or reserved:
        raise CodecError(Result.BAD_PAYLOAD)
    _check_observations(data, 20, SAMPLE_FLOAT_BITS, valid)
    for bit, names in ((1, ('control_time_ticks',)), (2, ('position_observation_time',)),
                       (256, ('motor_state', 'fault_flags')),
                       (512, ('active_profile_generation', 'telemetry_config_generation'))):
        if not valid & bit and any(value[name] for name in names):
            raise CodecError(Result.BAD_VALUE)
    return value


def encode_event(message: int, value: dict) -> bytes:
    if message not in (0x2001, 0x2002):
        raise CodecError(Result.UNKNOWN_MESSAGE)
    return _pack('2IQ', value['first'], value['second'], value['control_time_ticks'])


def decode_event(message: int, data: bytes) -> dict:
    if message not in (0x2001, 0x2002):
        raise CodecError(Result.UNKNOWN_MESSAGE)
    if len(data) != 16:
        raise CodecError(Result.BAD_PAYLOAD)
    return dict(zip(('first', 'second', 'control_time_ticks'), struct.unpack('<2IQ', data)))
