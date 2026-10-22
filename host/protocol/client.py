"""单所有者同步客户端；重发同一身份，不在重连后恢复旧运动请求。"""
from dataclasses import asdict
import json
import math
import secrets
import time
from . import codec


class RequestTimeout(TimeoutError):
    """结果未知；设备可能已执行，客户端会撤销本地会话资格。"""


class ConnectionLost(ConnectionError):
    pass


def json_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


class JsonlLogger:
    """每条线上帧保存原始字段和解码值；保留会话/序号，不把重连计作丢帧。"""
    def __init__(self, path):
        self.file = open(path, 'x', encoding='utf-8', buffering=1)

    def __call__(self, record):
        self.file.write(json.dumps(json_value(record), ensure_ascii=False, allow_nan=False) + '\n')

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Client:
    def __init__(self, stream, *, timeout=0.100, retries=2, clock=time.monotonic, logger=None):
        if not math.isfinite(timeout) or timeout <= 0 or not isinstance(retries, int) or not 0 <= retries <= 2:
            raise ValueError('timeout须为正，重发次数须为0..2')
        # pyserial默认read/write可无限等待；本实例作为唯一所有者收紧I/O等待。
        if hasattr(stream, 'timeout') and (stream.timeout is None or stream.timeout > min(timeout, 0.005)):
            stream.timeout = min(timeout, 0.005)
        if hasattr(stream, 'write_timeout') and (stream.write_timeout is None or stream.write_timeout > timeout):
            stream.write_timeout = timeout
        self.stream, self.timeout, self.retries = stream, timeout, retries
        self.clock, self.logger = clock, logger
        self.parser = codec.Parser()
        self.session = 0
        self.sequence = 1
        # 无会话Stop以随机初值关联应答，减少不同客户端从1开始时误收迟到应答的机会。
        self.stop_sequence = secrets.randbelow(0xFFFFFFFE) + 1
        self._busy = False
        self.received_frames = self.ignored_frames = self.retransmissions = 0
        self.last_recovery = None
        self.telemetry_domains = {}
        self.latest_telemetry = self.latest_event = None
        self.event_sequence = 0
        self.event_gaps = self.async_errors = 0

    def _asynchronous(self, status, frame):
        if status != codec.Status.OK or frame.kind not in (3, 4) or not self.session or frame.session_id != self.session:
            return None
        try:
            if not 1 <= frame.sequence <= 0xFFFFFFFE:
                raise ValueError('异步序号非法')
            if frame.kind == 3:
                if frame.message_id != 0x1001:
                    raise ValueError('未知遥测')
                value = codec.decode_sample(frame.payload)
                domain = (frame.session_id, value['telemetry_config_generation'])
                stats = self.telemetry_domains.setdefault(domain, dict(received=0, gaps=0, order_errors=0,
                                                                      first=frame.sequence, last=0))
                if stats['last']:
                    if frame.sequence <= stats['last']:
                        stats['order_errors'] += 1
                    else:
                        stats['gaps'] += frame.sequence - stats['last'] - 1
                stats['received'] += 1
                stats['last'] = frame.sequence
                self.latest_telemetry = (frame, value)
            else:
                value = codec.decode_event(frame.message_id, frame.payload)
                if self.event_sequence and frame.sequence <= self.event_sequence:
                    raise ValueError('事件序号倒退')
                if self.event_sequence:
                    self.event_gaps += frame.sequence - self.event_sequence - 1
                self.event_sequence = frame.sequence
                self.latest_event = (frame, value)
            return value
        except (codec.CodecError, ValueError):
            self.async_errors += 1
            return None

    def poll(self):
        """单所有者空闲收帧；不发请求，不自动重连，记录器同步保存异步帧。"""
        if self._busy:
            raise RuntimeError('不能在请求处理中重入poll')
        try:
            frames = self._read_frames()
        except OSError as error:
            self.session = 0
            raise ConnectionLost(str(error)) from error
        for status, frame in frames:
            self.received_frames += 1
            value = self._asynchronous(status, frame)
            if value is None:
                self.ignored_frames += 1
            self._record(direction='rx', frame=asdict(frame), status=int(status), correlated=False, value=value)

    def _read_frames(self):
        # 先排驱动中已到达的字节；不能把Python调度/日志停顿当成线上空闲。
        count = max(1, min(512, self.stream.in_waiting)) if hasattr(self.stream, 'in_waiting') else 512
        data = self.stream.read(count)
        now = int(self.clock() * 1000) & 0xFFFFFFFF
        if not data:
            self.parser.poll(now)
            return []
        if self.parser.candidate:
            self.parser.last_byte_ms = now
        return self.parser.feed(data, now)

    def _record(self, **fields):
        if self.logger:
            self.logger(dict(host_monotonic_s=self.clock(), **fields))

    def _exchange(self, frame):
        if self._busy:
            raise RuntimeError('客户端仅支持同一所有者的单个请求')
        wire = codec.encode_frame(frame)
        subject = int.from_bytes(frame.payload[:2], 'little') if frame.message_id in (2, 3, 4) and len(frame.payload) >= 2 else 0
        self._busy = True
        try:
            for attempt in range(self.retries + 1):
                if attempt:
                    self.retransmissions += 1
                self._record(direction='tx', frame=asdict(frame), wire=wire, attempt=attempt)
                if self.stream.write(wire) != len(wire):
                    raise ConnectionLost('请求未完整写入，停止自动重试')
                deadline = self.clock() + self.timeout
                while self.clock() < deadline:
                    frames = self._read_frames()
                    matched = None
                    for status, incoming in frames:
                        self.received_frames += 1
                        correlated = status == codec.Status.OK and incoming.kind == 2 and (
                            incoming.message_id, incoming.session_id, incoming.sequence) == (
                                frame.message_id, frame.session_id, frame.sequence)
                        value = None
                        if correlated:
                            try:
                                value = codec.decode_response(incoming.message_id, incoming.payload)
                                correlated = value['subject_id'] == subject
                            except codec.CodecError:
                                correlated = False
                        if not correlated:
                            value = self._asynchronous(status, incoming)
                        self._record(direction='rx', frame=asdict(incoming), status=int(status),
                                     correlated=correlated, value=value)
                        if correlated:
                            if value['result_code'] != codec.Result.IN_PROGRESS:
                                matched = value
                        elif value is None:
                            self.ignored_frames += 1
                    if matched is not None:
                        if matched['result_code'] in (codec.Result.SESSION_INVALID, codec.Result.SYSTEM_FAIL_CLOSED):
                            self.session = 0
                        return matched
            self.session = 0
            raise RequestTimeout(f'请求{frame.session_id:08X}/{frame.sequence}在原身份重发后仍无终态；结果未知')
        except (OSError, ConnectionLost) as error:
            self.session = 0
            if isinstance(error, RequestTimeout):
                raise
            raise ConnectionLost(str(error)) from error
        finally:
            self._busy = False

    def _request(self, message, subject, **fields):
        if not self.session:
            raise ConnectionLost('须先显式建立新会话，不自动重连或重放请求')
        if self.sequence > 0xFFFFFFFE:
            self.session = 0
            raise ConnectionLost('序号已用尽，须显式建立不同的新会话')
        seq = self.sequence
        self.sequence += 1
        payload = codec.encode_request(dict(message_id=message, subject_id=subject, **fields))
        return self._exchange(codec.Frame(1, message, self.session, seq, payload))

    def connect(self, proposed=None):
        """每次显式连接取新ID，读回设备状态/PI；不恢复模式、目标或旧写请求。"""
        proposed = proposed if proposed is not None else secrets.randbelow(0xFFFFFFFF) + 1
        if not 1 <= proposed <= 0xFFFFFFFF:
            raise ValueError('会话ID须为非零u32')
        payload = codec.encode_request(dict(message_id=1, proposed_session_id=proposed))
        response = self._exchange(codec.Frame(1, 1, 0, 1, payload))
        if response['result_code'] != codec.Result.OK:
            return response
        if response['accepted_session_id'] != proposed:
            self.session = 0
            raise ConnectionLost('设备应答的会话身份不匹配')
        # 同ID OPEN在设备幂等；本实例同ID时也不重置请求序号。
        if proposed != self.session:
            self.sequence = 1
            self.event_sequence = 0
        self.session = proposed
        self.last_recovery = {name: self.read_object(obj) for name, obj in (
            ('motor_snapshot', codec.MOTOR_SNAPSHOT), ('speed_pi_staged', codec.SPEED_PI_STAGED),
            ('speed_pi_active', codec.SPEED_PI_ACTIVE))}
        return response

    def read_object(self, object_id):
        return self._request(2, object_id)

    def write_pi(self, kp, ki):
        return self._request(3, codec.SPEED_PI_STAGED, value=dict(kp=kp, ki=ki))

    def configure_telemetry(self, enabled, rate_hz):
        return self._request(3, codec.TELEMETRY_CONFIG, value=dict(enabled=int(enabled), rate_hz=rate_hz))

    def execute(self, command, target=None):
        fields = dict(target_velocity_rpm=target) if command in (1, 2) else {}
        return self._request(4, command, **fields)

    def stop(self):
        """完整合法的无会话Stop；不建会话、不清故障，响应仅为最佳努力。"""
        if self.stop_sequence > 0xFFFFFFFE:
            raise ConnectionLost('本实例Stop序号用尽')
        seq = self.stop_sequence
        self.stop_sequence += 1
        return self._exchange(codec.Frame(1, 4, 0, seq, codec.encode_request(dict(message_id=4, subject_id=3))))
