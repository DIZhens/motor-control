"""真实ARM CAN Codec/Server及共享Firmware绑定/Runtime；帧传输与实时推进为替身。"""
from pathlib import Path
import argparse
import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_R0, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from communication.host.protocol import can_codec as py
from communication.host.protocol.profile import Result

ARM = Path(os.environ.get('KEIL_ARMCC_BIN', r'D:\keil5\ARM\ARMCC\bin'))
VECTORS = ROOT / 'communication/spec/test_vectors/can_v1.json'
GROUPS = []


def passed(name):
    GROUPS.append(name)
    print('PASS:', name, flush=True)


def fbits(value):
    return struct.unpack('<I', struct.pack('<f', value))[0]


class Model:
    data = []
    symbols = {}

    def __init__(self):
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
        for base, size in ((0x08000000, 0x40000), (0x20000000, 0x8000), (0xE000E000, 0x1000)):
            self.cpu.mem_map(base, size)
        for address, data in self.data:
            self.cpu.mem_write(address, data)
        self.put(0xE000ED88, 0xF00000)
        self.call('can_model_init')

    def put(self, name, value):
        self.cpu.mem_write(self.symbols[name] if isinstance(name, str) else name, struct.pack('<I', value & 0xFFFFFFFF))

    def get(self, name):
        return struct.unpack('<I', self.cpu.mem_read(self.symbols[name], 4))[0]

    def call(self, name, arg=0):
        self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
        self.cpu.reg_write(UC_ARM_REG_LR, 0x0803F001)
        self.cpu.reg_write(UC_ARM_REG_R0, arg)
        self.cpu.emu_start(self.symbols[name] | 1, 0x0803F000, count=1000000)
        assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, name
        return self.cpu.reg_read(UC_ARM_REG_R0)

    def frame_in(self, frame):
        raw = struct.pack('<HBBB8s', frame.id, frame.dlc, frame.extended, frame.remote, frame.data) + b'\x00'
        self.cpu.mem_write(self.symbols['can_model_input'], raw)

    def frame_out(self, symbol):
        raw = bytes(self.cpu.mem_read(self.symbols[symbol], 13))
        id, dlc, extended, remote, data = struct.unpack('<HBBB8s', raw)
        return py.Frame(id, data, bool(extended), bool(remote), dlc)

    def args(self, *values):
        self.cpu.mem_write(self.symbols['can_model_args'], struct.pack('<8I', *[v & 0xFFFFFFFF for v in values], *([0] * (8-len(values)))))

    def feed(self, frame):
        self.frame_in(frame)
        self.call('can_model_feed')

    def poll(self, now=None):
        if now is not None:
            self.put('model_now', now)
        self.call('can_model_poll')

    def take(self):
        self.call('can_model_flush')
        if not self.get('can_model_tx_pending'):
            return None
        frame = self.frame_out('can_model_tx')
        self.put('can_model_tx_pending', 0)
        self.put('can_model_tx_busy', 0)
        return py.decode_message(frame)

    def response(self, frame, rt=False):
        self.feed(frame)
        if rt:
            self.call('can_model_step')
        self.poll()
        result = self.take()
        assert result is not None and result['kind'] == 'response', result
        return result

    def open(self, tag=0x1234):
        result = self.response(py.request(1, tag, 0, 0))
        assert result['session'] == tag and result['result'] == 0 and result['motor'] == 0xFFFF
        return self


def codec_tests():
    m = Model()
    modes = dict(request=0, stop=1, response=2, state=3, motion=4, fault=5)
    vectors = json.loads(VECTORS.read_text(encoding='utf-8'))['frames']
    for v in vectors:
        args = list(v['args'])
        python = getattr(py, v['kind'])(v['node'], *args)
        assert python.id == v['id'] and python.data.hex() == v['data'], v['name']
        if v['kind'] == 'motion':
            args[1:3] = [fbits(x) for x in args[1:3]]
        m.args(v['node'], *args)
        assert m.call('can_model_codec', modes[v['kind']]) == 0, v['name']
        actual = m.frame_out('can_model_output')
        assert actual == python and actual.data.hex() == v['data'], (v['name'], actual)
        py.decode_message(actual)
        if v['kind'] in ('request', 'stop'):
            m.args(v['node']); m.frame_in(actual)
            assert m.call('can_model_codec', 6) == 0
            value = py.decode_request(v['node'], actual)
            raw = struct.unpack('<5I', m.cpu.mem_read(m.symbols['can_model_decoded'], 20))
            assert raw == (value['session'], value['sequence'], value['service'], value['target_x100'] & 0xFFFFFFFF, value['sessionless_stop'])
    passed(f'{len(vectors)}份人工固定CAN黄金帧，真实ARM C/Python逐字节编码与解码一致')

    used = {0}
    for node in range(1, 128):
        for base in (0, 0x80, 0x100, 0x180, 0x200, 0x280):
            id = base + node
            assert id not in used and id <= 0x7FF
            used.add(id)
        m.args(node, 1, False); assert m.call('can_model_codec', 1) == 0
        assert m.frame_out('can_model_output').id == node
        m.args(node); m.frame_in(py.stop(node, 1))
        assert m.call('can_model_codec', 6) == 0
    assert len(used) == 763
    for node in (0, 128, 255):
        m.args(node, 1, 0); assert m.call('can_model_codec', 1) == Result.BAD_REQUEST
    for id in (0x80, 0x100, 0x180, 0x200, 0x280):
        try:
            py.decode_message(py.Frame(id, bytes(8)))
        except py.CanCodecError:
            pass
        else:
            raise AssertionError(('reserved base+0', id))
        m.args(1); m.frame_in(py.Frame(id, bytes(8)))
        assert m.call('can_model_codec', 6) == Result.BAD_REQUEST
    passed('全127节点/6服务段加广播共763个ID唯一；Node0/越界与所有base+0普通节点帧拒绝')

    good = py.request(1, 1, 1, 1, 4000)
    invalid = [py.Frame(good.id, good.data, extended=True), py.Frame(good.id, good.data, remote=True),
               *[py.Frame(good.id, good.data, dlc=n) for n in (*range(8), 9, 15)],
               py.Frame(0x81, good.data), py.Frame(0x101+1, good.data)]
    for frame in invalid:
        m.args(1); m.frame_in(frame)
        assert m.call('can_model_codec', 6) == Result.BAD_REQUEST
    malformed = []
    for flags in (1, 128, 255):
        data = bytearray(good.data); data[5] = flags
        malformed.append((py.Frame(good.id, data), Result.UNSUPPORTED_FLAGS))
    for service in (5, 255):
        data = bytearray(good.data); data[4] = service
        malformed.append((py.Frame(good.id, data), Result.UNKNOWN_COMMAND))
    for target in (-32768, -8001, 8001, 32767):
        malformed.append((py.Frame(good.id, struct.pack('<HHBBh', 1, 1, 1, 0, target)), Result.BAD_VALUE))
    for index in range(2, 8):
        data = bytearray(8); data[index] = 1
        malformed.append((py.Frame(1, data), Result.BAD_PAYLOAD))
    malformed += [(py.Frame(0x101, struct.pack('<HHBBh', 0, 0, 0, 0, 0)), Result.BAD_REQUEST),
                  (py.Frame(0x101, struct.pack('<HHBBh', 1, 1, 0, 0, 0)), Result.BAD_REQUEST),
                  (py.Frame(0x101, struct.pack('<HHBBh', 1, 1, 3, 0, 1)), Result.BAD_PAYLOAD)]
    for frame, result in malformed:
        m.args(1); m.frame_in(frame)
        assert m.call('can_model_codec', 6) == result
        try:
            py.decode_request(1, frame)
        except py.CanCodecError as error:
            assert error.code == result
        else:
            raise AssertionError(frame)
    passed('非法ID/DLC/扩展/RTR/flags/service/reserved/target及OPEN组合均拒绝，不猜测Stop')

    for rpm in (0.0, 0.005, -0.005, 0.0049, -0.0049, 39.995, -39.985, 327.67, -327.68, 327.675, -327.685,
                1e30, -1e30, float('nan'), float('inf'), -float('inf')):
        m.args(fbits(rpm)); result = m.call('can_model_codec', 7)
        try:
            expected = py.rpm_x100(rpm)
        except py.CanCodecError:
            assert result == Result.BAD_VALUE and m.get('can_model_round_value') == 0x1234
        else:
            assert result == 0 and m.get('can_model_round_value') == expected & 0xFFFFFFFF, (rpm, expected)
    for value in range(-32768, 32768, 257):
        rpm = value/100
        m.args(fbits(rpm)); assert m.call('can_model_codec', 7) == 0
        assert m.get('can_model_round_value') == value & 0xFFFFFFFF
    passed('binary32乘100/半值远离0、i16边界/非有限与失败不改输出；跨范围定点往返一致')


def server_tests():
    start = lambda seq=1, tag=0x1234, target=6000: py.request(1, tag, seq, 1, target)
    clear = lambda seq=1, tag=0x1234: py.request(1, tag, seq, 4)
    m = Model().open()
    good = py.stop(1, 10)
    invalid = [py.Frame(0x41, good.data), py.Frame(0x80, good.data), py.Frame(0x100, good.data),
               py.Frame(0x81, good.data), py.Frame(0x102, good.data),
               py.Frame(1, good.data, extended=True), py.Frame(1, good.data, remote=True),
               py.Frame(1, good.data, dlc=7), py.Frame(1, good.data, dlc=9)]
    for index in range(2, 8):
        data = bytearray(good.data); data[index] = 1
        invalid.append(py.Frame(1, data))
    for frame in invalid:
        m.feed(frame); m.poll()
    assert m.get('model_submit_count') == 0 and m.get('model_failure') == 0
    assert m.call('can_model_session') == 0x1234 and m.call('can_model_highest') == 0
    assert py.decode_message(py.stop(1, 0, True))['node'] is None
    passed('Node1拒绝原0x041地址（现Node65）/其他节点/base+0/非法元数据或reserved；不提交、故障或改变会话水位')
    m = Model().open()
    assert m.get('model_submit_count') == 0 and m.call('can_model_session') == 0x1234
    assert m.response(start(tag=0))['result'] == Result.SESSION_REQUIRED
    assert m.response(start(tag=9))['result'] == Result.SESSION_INVALID
    passed('OPEN只改会话；错误/缺失Session不形成设备请求')
    m = Model().open(); frame = start()
    m.feed(frame); m.feed(frame)
    assert m.take()['result'] == Result.IN_PROGRESS and m.get('model_submit_count') == 1
    assert m.response(start(target=4000))['result'] == Result.SEQUENCE_CONFLICT
    m.call('can_model_step'); m.poll(); original = m.take()
    assert original['result'] == 0 and original['motor'] == 2
    assert m.response(frame) == original and m.get('model_submit_count') == 1
    assert m.response(start(target=4000))['result'] == Result.SEQUENCE_CONFLICT
    passed('在途重复一次提交、内容冲突、终态重放保持Motor NOT_READY结果；不重执行')
    m = Model().open()
    m.feed(start())
    assert m.response(py.request(1, 22, 0, 0))['result'] == Result.SESSION_BUSY
    assert m.call('can_model_session') == 0x1234
    m.call('can_model_step'); m.poll(); m.take()
    m.open(); assert m.call('can_model_highest') == 1
    assert m.response(start(seq=0))['result'] == Result.SEQUENCE_INVALID
    assert m.response(start(seq=65535))['result'] == Result.SEQUENCE_INVALID
    for seq in range(2, 8):
        assert m.response(clear(seq), True)['motor'] == 1
    assert m.response(start())['result'] == Result.SEQUENCE_INVALID
    m.open(99); assert m.call('can_model_highest') == 0
    passed('会话忙/同tag幂等/新tag复位水位；序号保留值和4项缓存淘汰后退休拒绝')
    m = Model().open()
    invalid = py.Frame(0x101, struct.pack('<HHBBh', 0x1234, 1, 1, 0, 8001))
    old = m.response(invalid)
    assert old['result'] == Result.BAD_VALUE and m.response(invalid) == old
    assert m.response(start())['result'] == Result.SEQUENCE_CONFLICT and m.get('model_submit_count') == 0
    m.put('model_submit_override', 5)
    assert m.response(start(seq=2))['result'] == Result.PRECONDITION_FAILED
    passed('新拒绝同样退休/缓存；交接前置拒绝忠实返回且不误报Motor执行')
    m = Model().open(); m.feed(start())
    stop = py.stop(1, 10)
    m.feed(stop); m.call('can_model_step'); m.poll()
    replies = [m.take(), m.take()]
    assert replies[0]['result'] == Result.CANCELLED_BY_STOP
    assert replies[1]['session'] == 0 and replies[1]['motor'] == 1
    assert m.get('model_submit_count') == 2
    assert m.response(stop, True) == replies[1] and m.get('model_submit_count') == 3
    assert m.call('can_model_session') == 0x1234 and m.call('can_model_highest') == 1
    passed('优先Stop取消准确未消费ticket；终态后相同Stop再次提交完成且Session/水位不变')
    for broadcast in (False, True):
        m = Model().open(); frame = py.stop(1, 0xFFFF, broadcast)
        m.feed(frame); m.feed(frame)
        assert m.take()['result'] == Result.IN_PROGRESS and m.get('model_submit_count') == 1
        m.call('can_model_step'); m.feed(frame)
        assert m.take()['result'] == Result.IN_PROGRESS and m.get('model_submit_count') == 1
        m.poll(); assert m.take()['motor'] == 1
        assert m.response(frame, True)['motor'] == 1 and m.get('model_submit_count') == 2
    m = Model(); assert m.response(py.stop(1, 0), True)['motor'] == 1
    assert m.response(py.stop(1, 0), True)['motor'] == 1 and m.call('can_model_session') == 0
    passed('指定/广播Stop均仅在途去重；完成未退休仍视为在途；无会话及0/FFFF序号可重复关断')
    m = Model().open(); frame = py.request(1, 0x1234, 1, 3)
    m.feed(frame); m.feed(frame); assert m.take()['result'] == Result.IN_PROGRESS
    m.call('can_model_step'); m.poll(); original = m.take()
    assert m.response(frame) == original and m.get('model_submit_count') == 1
    m = Model().open(); m.feed(py.stop(1, 1))
    assert m.response(py.stop(1, 2))['result'] == Result.BUSY
    assert m.response(py.stop(1, 1, True))['result'] == Result.SEQUENCE_CONFLICT
    m.call('can_model_step'); m.poll(); m.take()
    assert m.response(py.stop(1, 2), True)['motor'] == 1
    passed('session-bound Stop继续缓存去重；其他在途Stop BUSY/冲突且不覆盖原Stop或永久缓存BUSY')
    m = Model().open(); m.feed(py.stop(1, 1)); m.open(99)
    m.call('can_model_step'); m.poll(); assert m.take()['session'] == 0
    assert m.call('can_model_session') == 99 and m.get('model_submit_count') == 1
    passed('换会话保留旧Stop的ticket与响应身份，不将其完成写入新会话缓存')
    m = Model().open(); m.feed(start()); m.poll(20)
    assert m.get('model_failure') == 8 and m.call('can_model_session') == 0
    m = Model().open(); m.put('model_now', 0xFFFFFFF0); m.feed(start()); m.poll(3)
    assert m.get('model_failure') == 0
    m.poll(4); assert m.get('model_failure') == 8
    passed('20ms完成截止及u32时间回绕；迟到完成不恢复会话或执行运动')
    m = Model().open(); m.call('can_model_unknown'); m.poll()
    assert m.get('model_failure') == 6
    m = Model().open()
    for seq in range(1, 6):
        m.feed(clear(seq)); m.call('can_model_step'); m.poll()
    assert m.get('model_failure') == 5
    for mode in range(5):
        m = Model().open(); m.feed(start()); m.call('can_model_corrupt', mode); m.poll()
        assert m.get('model_failure') == (7 if mode == 4 else 6)
    passed('未知Completion、响应FIFO溢出/静态状态损坏/RX lost触发Fail-closed，不覆盖或猜测')
    m = Model().open(); m.put('can_model_tx_busy', 1); m.feed(clear()); m.call('can_model_step'); m.poll()
    assert m.take() is None
    m.put('can_model_tx_busy', 0); assert m.take()['motor'] == 1
    m = Model().open(); m.call('can_model_observe'); m.call('can_model_event_state', 3); m.call('can_model_observe')
    m.call('can_model_produce', 1); m.poll(100)
    m.feed(clear()); m.call('can_model_step'); m.poll()
    assert [m.take()['kind'] for _ in range(4)] == ['response', 'fault', 'state', 'motion']
    passed('TX BUSY保留完整响应；Response > Fault > Heartbeat > Motion发送，最新Mailbox接受后才退休')
    m = Model().open(); m.call('can_model_observe'); m.call('can_model_link_flags', 12); m.poll(100)
    assert m.take()['flags'] == 15 and m.get('model_failure') == 0
    m.call('can_model_produce', 1); m.call('can_model_produce', 2)
    assert m.take()['sequence'] == 8
    m = Model().open(); m.call('can_model_observe')
    for state in (3, 1, 3, 1, 3, 1, 3, 1, 3):
        m.call('can_model_event_state', state); m.call('can_model_observe')
    assert m.get('model_failure') == 0 and m.call('can_model_event_drops') == 1
    passed('Warning/Passive只发布状态；Mailbox覆盖取最新实时序号，Fault事件有界且不派生Motor故障')
    m = Model(); m.call('can_model_produce', 1)
    assert m.get('model_telemetry_pending') == 0
    m.open(); subscription = m.get('model_subscription')
    assert subscription != 0 and m.get('model_begin_calls') == 1
    m.call('can_model_produce', 16383); m.open()
    assert m.get('model_subscription') == subscription and m.get('model_begin_calls') == 1
    assert m.take()['sequence'] == 65532
    m.call('can_model_produce', 16384); assert m.take()['sequence'] == 0
    m.call('can_model_produce', 16385); m.open(99)
    assert m.get('model_telemetry_pending') == 0 and m.get('model_begin_calls') == 2
    m.call('can_model_produce', 1); m.call('can_model_old_subscription')
    assert m.take() is None and m.get('model_telemetry_pending') == 0
    m.call('can_model_produce', 2); assert m.take()['sequence'] == 8
    passed('CAN新Session清旧槽、拒绝迟到旧订阅；幂等OPEN保留待发值，Motion实时序号低16位自然回绕')

    m = Model().open(); m.call('can_model_produce', 1)
    m.call('can_model_link_flags', 16); m.poll()
    assert m.get('model_subscription') == 0 and m.get('model_telemetry_pending') == 0
    m.call('can_model_step'); m.poll(); assert m.call('can_model_recover') == 1
    assert m.call('can_model_session') == 0 and m.take() is None
    m.call('can_model_produce', 2); assert m.take() is None
    m.open(99); m.call('can_model_produce', 3)
    assert m.take()['kind'] == 'motion'
    m.put('model_permitted', 0); m.poll()
    assert m.get('model_subscription') == 0 and m.call('can_model_session') == 0
    m = Model().open(); m.call('can_model_bad_subscription'); m.poll()
    assert m.get('model_failure') == 6 and m.get('model_subscription') == 0
    m = Model(); m.put('model_subscription_counter', 0xFFFFFFFF)
    m.feed(py.request(1, 99, 0, 0)); m.poll()
    assert m.get('model_failure') == 6 and m.call('can_model_session') == 0
    passed('CAN Bus-off/内部故障结束订阅；Recovery无Session不重放，新OPEN恢复Motion，关联损坏/耗尽Fail-closed')
    m = Model().open(); fault_before_end = []
    address = m.symbols['motor_node_binding_target_telemetry_end'] & ~1
    m.cpu.hook_add(UC_HOOK_CODE, lambda cpu, pc, size, user:
                   fault_before_end.append(m.get('model_permitted')) if pc == address else None)
    m.cpu.ctl_flush_tb()
    m.call('can_model_corrupt', 0); m.poll()
    assert fault_before_end == [0] and m.get('model_failure') == 6
    passed('CAN内部不变量失败先到原Fail-closed关断端口，之后才结束RTOS订阅')


    m = Model().open(); m.feed(start()); m.call('can_model_link_flags', 16); m.poll()
    assert m.call('can_model_session') == 0 and m.get('model_submit_count') == 2
    m.feed(start(seq=2)); assert m.get('model_submit_count') == 2
    m.call('can_model_step'); m.poll(); assert m.get('model_failure') == 0
    assert m.call('can_model_recover') == 1 and m.call('can_model_session') == 0
    assert m.get('model_submit_count') == 2 and m.response(start(seq=2))['result'] == Result.SESSION_INVALID
    m = Model().open(); m.feed(start()); m.call('can_model_link_flags', 16); m.poll()
    assert m.call('can_model_recover') == 0 and m.get('model_failure') == 6
    passed('Bus-off模型撤会话、优先Stop取消旧Start、禁新请求；明确退休后恢复不重放，提前恢复Fail-closed')
    for mode in (0, 1):
        m = Model().open(); m.call('can_model_link_flags', 16); m.poll()
        m.call('can_model_bad_stop_completion', mode); m.poll()
        assert m.get('model_failure') == 6 and m.call('can_model_recover') == 0
    passed('Bus-off后的Stop拒绝/前置失败不作为恢复资格，无法证明关断业务完成时Fail-closed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    sources = [ROOT / 'communication/protocol/can/can_codec.c', ROOT / 'communication/protocol/can/can_server.c', Path(__file__).resolve().parent / 'can_model.c', ROOT / 'communication/protocol/tests/uart/core_server_model.c', ROOT / 'communication/protocol/uart/core_protocol.c', ROOT / 'communication/protocol/uart/core_protocol_values.c', ROOT / 'communication/protocol/uart/core_server.c', ROOT / 'firmware/targets/BLDC_G431/communication/motor_node_binding.c', ROOT / 'firmware/targets/BLDC_G431/runtime/runtime_exchange.c', ROOT / 'firmware/targets/BLDC_G431/runtime/runtime_parameters.c', ROOT / 'firmware/motor/motor.c']
    with tempfile.TemporaryDirectory(prefix='can-v1-model-') as directory:
        temp = Path(directory)
        scatter = temp/'model.sct'
        scatter.write_text('LR 0x08000000 0x40000 { ER 0x08000000 0x40000 { .ANY (+RO) } RW 0x20000000 0x8000 { .ANY (+RW +ZI) } }')
        objects = []
        for source in sources:
            obj = temp/(source.stem+'.o'); objects.append(obj)
            command = [ARM/'armcc.exe', '--cpu=Cortex-M4.fp', '--c99', '-O3', '-Otime', '--split_sections', '-g']
            includes = [ROOT / 'communication/protocol/can', ROOT / 'communication/protocol/device', ROOT / 'communication/protocol/transport', ROOT / 'communication/protocol/uart']
            if not source.is_relative_to(ROOT / 'communication/protocol/can') and not source.is_relative_to(ROOT / 'communication/protocol/uart'):
                includes += [ROOT / 'firmware/motor', ROOT / 'firmware/targets/BLDC_G431/communication', ROOT / 'firmware/targets/BLDC_G431/runtime']
            for include in includes:
                command += ['-I', include]
            result = subprocess.run([*map(str, command), '-c', str(source), '-o', str(obj)], capture_output=True)
            assert result.returncode == 0 and b'warning' not in result.stdout.lower()+result.stderr.lower(), result.stdout+result.stderr
        axf = temp/'model.axf'
        result = subprocess.run([*map(str, [ARM/'armlink.exe', '--cpu=Cortex-M4.fp', '--scatter', scatter,
            '--entry=can_model_init', '--keep=can_model.o(*)', '-o', axf, *objects])], capture_output=True)
        assert result.returncode == 0, result.stdout+result.stderr
        with axf.open('rb') as stream:
            elf = ELFFile(stream)
            Model.data = [(s['sh_addr'], s.data()) for s in elf.iter_sections() if s['sh_flags'] & 2 and s['sh_type'] == 'SHT_PROGBITS']
            Model.symbols = {s.name:s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()}
        codec_tests(); server_tests()
        server_size = Model().call('can_model_sizeof_server')
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        paths = [*sources, Path(__file__).resolve().parent / 'verify_can.py', VECTORS, ROOT / 'communication/host/protocol/can_codec.py', ROOT / 'communication/host/protocol/profile.py',
                 *[p for folder in ('device', 'transport', 'can') for p in (ROOT / 'communication/protocol'/folder).glob('*.h')]]
        report = dict(status='PASS', groups=GROUPS, hardware_access=False,
            execution='ARMCC Cortex-M4F + Unicorn; real CAN/Binding/Runtime/Motor, shared portable device fixture; frame TX and realtime stepping substituted',
            golden_frames=len(json.loads(VECTORS.read_text(encoding='utf-8'))['frames']),
            golden_sha256=hashlib.sha256(VECTORS.read_bytes()).hexdigest(),
            can_server_model_size_bytes=server_size,
            source_sha256={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
            limitations='不证明FDCAN/HAL/IRQ/Task、内部回环、外部收发器/ACK/仲裁/Bus-off或CAN电机运动')
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('PASS CAN model:', len(GROUPS), 'groups')


if __name__ == '__main__':
    main()
