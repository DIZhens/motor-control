"""Classical CAN V1纯标准库映射；不依赖UART framing、驱动或设备状态机。"""
from dataclasses import dataclass
import math
import struct
from .profile import Result


STOP_BASE = 0x000
FAULT_BASE = 0x080
REQUEST_BASE = 0x100
RESPONSE_BASE = 0x180
STATE_BASE = 0x200
MOTION_BASE = 0x280


class CanCodecError(ValueError):
    def __init__(self, code: Result):
        super().__init__(code.name)
        self.code = code


@dataclass(frozen=True)
class Frame:
    id: int
    data: bytes
    extended: bool = False
    remote: bool = False
    dlc: int = 8


def _node(node: int) -> None:
    if not 1 <= node <= 127:
        raise CanCodecError(Result.BAD_REQUEST)


def _complete(frame: Frame) -> None:
    if frame.extended or frame.remote or frame.dlc != 8 or len(frame.data) != 8 or not 0 <= frame.id <= 0x7FF:
        raise CanCodecError(Result.BAD_REQUEST)


def decode_request(node: int, frame: Frame) -> dict:
    _node(node)
    _complete(frame)
    if frame.id not in (0, node, REQUEST_BASE + node):
        raise CanCodecError(Result.BAD_REQUEST)
    if frame.id != REQUEST_BASE + node:
        sequence, = struct.unpack_from('<H', frame.data)
        if any(frame.data[2:]):
            raise CanCodecError(Result.BAD_PAYLOAD)
        return dict(session=0, sequence=sequence, service=3, target_x100=0, sessionless_stop=True)
    session, sequence, service, flags, target = struct.unpack('<HHBBh', frame.data)
    if flags:
        raise CanCodecError(Result.UNSUPPORTED_FLAGS)
    if service > 4:
        raise CanCodecError(Result.UNKNOWN_COMMAND)
    if service == 0 and not (session and sequence == 0 and target == 0):
        raise CanCodecError(Result.BAD_REQUEST)
    if service in (1, 2) and not -8000 <= target <= 8000:
        raise CanCodecError(Result.BAD_VALUE)
    if service in (3, 4) and target != 0:
        raise CanCodecError(Result.BAD_PAYLOAD)
    return dict(session=session, sequence=sequence, service=service, target_x100=target, sessionless_stop=False)


def request(node: int, session: int, sequence: int, service: int, target_x100: int = 0) -> Frame:
    _node(node)
    if not (0 <= session <= 0xFFFF and 0 <= sequence <= 0xFFFF and 0 <= service <= 255
            and -32768 <= target_x100 <= 32767):
        raise CanCodecError(Result.BAD_VALUE)
    frame = Frame(REQUEST_BASE + node, struct.pack('<HHBBh', session, sequence, service, 0, target_x100))
    decode_request(node, frame)
    return frame


def stop(node: int, sequence: int, broadcast: bool = False) -> Frame:
    _node(node)
    if not 0 <= sequence <= 0xFFFF:
        raise CanCodecError(Result.BAD_VALUE)
    return Frame(0 if broadcast else node, struct.pack('<HHI', sequence, 0, 0))


def rpm_x100(rpm: float) -> int:
    """按C的binary32乘法和半值远离0取整；不可表示时拒绝，不钳位。"""
    def f32(value):
        try:
            return struct.unpack('<f', struct.pack('<f', value))[0]
        except (OverflowError, struct.error):
            raise CanCodecError(Result.BAD_VALUE) from None
    scaled = f32(f32(rpm) * 100.0)
    if not -32768.5 < scaled < 32767.5:
        raise CanCodecError(Result.BAD_VALUE)
    return math.trunc(f32(scaled + (0.5 if scaled >= 0 else -0.5)))


def response(node: int, session: int, sequence: int, result: int, motor: int = 0xFFFF) -> Frame:
    _node(node)
    return Frame(RESPONSE_BASE + node, struct.pack('<4H', session, sequence, result, motor))


def state(node: int, counter: int, motor_state: int, flags: int, faults: int) -> Frame:
    _node(node)
    return Frame(STATE_BASE + node, struct.pack('<HBBI', counter, motor_state, flags, faults))


def motion(node: int, sequence: int, target: float, velocity: float, valid: int, limits: int) -> Frame:
    _node(node)
    values = [0, 0]
    flags = (limits & 15) << 8
    for index, (value, bit) in enumerate(((target, 4), (velocity, 8))):
        if valid & bit:
            try:
                values[index] = rpm_x100(value)
                flags |= 1 << index
            except CanCodecError:
                pass
    return Frame(MOTION_BASE + node, struct.pack('<HhhH', sequence, *values, flags))


def fault(node: int, newly: int, all_faults: int) -> Frame:
    _node(node)
    return Frame(FAULT_BASE + node, struct.pack('<2I', newly, all_faults))


def decode_message(frame: Frame) -> dict:
    _complete(frame)
    if frame.id == 0:
        return dict(kind='stop', node=None, broadcast=True, **decode_request(1, frame))
    if 1 <= frame.id <= 127:
        return dict(kind='stop', node=frame.id, broadcast=False, **decode_request(frame.id, frame))
    if FAULT_BASE + 1 <= frame.id <= FAULT_BASE + 127:
        newly, all_faults = struct.unpack('<2I', frame.data)
        return dict(kind='fault', node=frame.id-FAULT_BASE, newly=newly, all_faults=all_faults)
    if REQUEST_BASE + 1 <= frame.id <= REQUEST_BASE + 127:
        node = frame.id-REQUEST_BASE
        return dict(kind='request', node=node, **decode_request(node, frame))
    if RESPONSE_BASE + 1 <= frame.id <= RESPONSE_BASE + 127:
        session, sequence, result, motor = struct.unpack('<4H', frame.data)
        if result not in Result._value2member_map_ or motor not in (*range(7), 0xFFFF):
            raise CanCodecError(Result.BAD_PAYLOAD)
        return dict(kind='response', node=frame.id-RESPONSE_BASE, session=session,
                    sequence=sequence, result=result, motor=motor)
    if STATE_BASE + 1 <= frame.id <= STATE_BASE + 127:
        counter, motor_state, flags, faults = struct.unpack('<HBBI', frame.data)
        if flags & ~0x3F or motor_state > 3:
            raise CanCodecError(Result.BAD_PAYLOAD)
        return dict(kind='state', node=frame.id-STATE_BASE, counter=counter,
                    motor_state=motor_state, flags=flags, faults=faults)
    if MOTION_BASE + 1 <= frame.id <= MOTION_BASE + 127:
        sequence, target, velocity, flags = struct.unpack('<HhhH', frame.data)
        if flags & ~0x0F03 or (not flags & 1 and target) or (not flags & 2 and velocity):
            raise CanCodecError(Result.BAD_PAYLOAD)
        return dict(kind='motion', node=frame.id-MOTION_BASE, sequence=sequence,
                    target_x100=target, velocity_x100=velocity, flags=flags)
    raise CanCodecError(Result.BAD_REQUEST)
