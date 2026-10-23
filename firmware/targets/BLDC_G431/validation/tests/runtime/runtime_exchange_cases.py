"""真实Run边界的ticket、参数、SPSC、发布与内部故障模型；无硬件及调度时长证明。"""
import math
import struct

from unicorn import UC_HOOK_MEM_WRITE
from unicorn.arm_const import UC_ARM_REG_PRIMASK


def verify(Model, symbols, passed):
    def read(m, symbol, fmt, offset=0):
        return struct.unpack('<' + fmt, m.cpu.mem_read(symbols[symbol] + offset, struct.calcsize('<' + fmt)))

    def result(m):
        return read(m, 'exchange_result', '16I')

    def tick(m, n=1):
        m.phase = 'sample'
        for _ in range(n):
            m.call('model_sample')
        m.phase = ''

    def submit(m, kind=0, command=3, target=0., kp=.001, ki=.004, rate=250, enabled=1, unit=False):
        for name, val in [('kind', kind), ('command', command), ('rate', rate), ('enabled', enabled)]:
            m.write('exchange_' + name, val)
        for name, val in [('target', target), ('kp', kp), ('ki', ki)]:
            m.write('exchange_' + name, val, 'f')
        m.call('exchange_unit_submit' if unit else 'exchange_submit')
        return result(m)[:3]

    def pop(m, unit=False):
        m.call('exchange_unit_pop' if unit else 'exchange_pop')
        r = result(m)
        return r[4:12] if r[3] else None

    def profiles(m):
        m.call('exchange_read')
        assert result(m)[12] == 1
        return read(m, 'exchange_profile_values', '6f'), read(m, 'exchange_profile_meta', '6I')

    def sample(m):
        m.call('exchange_read')
        if not result(m)[13]:
            return None
        layout = read(m, 'exchange_layout', '18I')
        raw = bytes(m.cpu.mem_read(symbols['exchange_sample'], layout[7]))
        def item(index, fmt):
            return struct.unpack_from('<' + fmt, raw, layout[index])[0]
        return dict(sequence=item(8, 'I'), time=item(9, 'Q'), position_time=item(10, 'Q'),
                    floats=struct.unpack_from('<7f', raw, layout[11]), valid=item(12, 'I'),
                    active_generation=item(13, 'I'), telemetry_generation=item(14, 'I'),
                    limits=item(15, 'H'))

    def fault(m):
        m.call('exchange_read')
        offset = read(m, 'exchange_layout', '18I')[17]
        return read(m, 'exchange_fault', 'B')[0], read(m, 'exchange_fault', 'I', offset)[0]

    def ready():
        m = Model()
        m.write('model_bus_voltage', 12., 'f')
        m.write('model_position_available', 1); m.write('model_position_use_fixed_time', 0)
        m.write('model_current_b', -.012, 'f'); m.write('model_current_c', -.001, 'f')
        tick(m, 4250)
        assert m.motor()[:2] == (1, 0) and m.current()['state'] == 3
        return m

    def accepted_start(m):
        r, ticket, cancelled = submit(m, command=0, target=60.)
        assert (r, cancelled) == (0, 0) and ticket != 0
        tick(m)
        c = pop(m)
        assert c[:4] == (ticket, 0, 0, 1)
        return ticket

    m = Model()
    gains, meta = profiles(m)
    assert gains == (gains[0], gains[1]) * 3 and math.isclose(gains[0], .001, rel_tol=1e-6)
    assert math.isclose(gains[1], .004, rel_tol=1e-6) and meta == (1, 0, 0, 1, 250, 1)
    assert pop(m) is None and sample(m) is None
    m.call('exchange_nulls'); assert result(m)[:2] == (2, 1)
    m = Model(start=False)
    assert submit(m)[0] == 4
    passed('默认Profile与空FIFO，无采样不伪造publication；空指针/未装配请求拒绝')

    m = ready()
    before = profiles(m)
    r, ticket, cancel = submit(m, kind=1, kp=.002, ki=.008)
    assert (r, ticket, cancel) == (0, 1, 0) and profiles(m) == before
    m.write('exchange_kp', .004, 'f'); m.write('exchange_ki', .016, 'f'); m.call('exchange_change_source')
    tick(m)
    c = pop(m); gains, meta = profiles(m)
    assert c[:3] == (ticket, 1, 0) and c[4:6] == (0, 2)
    assert math.isclose(gains[2], .002, rel_tol=1e-6) and math.isclose(gains[3], .008, rel_tol=1e-6)
    assert gains[4:] == before[0][4:] and meta[:3] == (2, 0, 0)
    assert pop(m) is None
    old = bytes(m.cpu.mem_read(symbols['exchange_parameters'], read(m, 'exchange_layout', '18I')[0]))
    version = m.word('s_publication_sequence'); m.write('s_publication_sequence', version | 1)
    m.call('exchange_read')
    assert result(m)[12] == 0 and bytes(m.cpu.mem_read(symbols['exchange_parameters'], len(old))) == old
    m.write('s_publication_sequence', version)
    passed('参数请求完整复制，后台不能改Staged；ADC整组应用并返回准确generation与一次完成')

    m = ready()
    r, first, _ = submit(m, command=0, target=60.)
    assert r == 0 and submit(m, command=1, target=40.) == (1, 0xA5A5A5A5, 0xA5A5A5A5)
    r, stop, cancelled = submit(m, command=2)
    assert r == 0 and stop != first and cancelled == first
    assert submit(m, command=2)[0] == 1 and submit(m, kind=1)[0] == 1
    tick(m, 3)
    assert pop(m)[:4] == (stop, 0, 0, 1) and pop(m) is None
    assert m.motor()[0] == 1 and m.motor()[3] == 1 and m.word('model_enable_count') == 0
    passed('Stop返回待消费旧ticket，不覆盖已排队Stop；取消不执行旧Start或重复产生Completion')

    m = ready(); first = accepted_start(m)
    assert profiles(m)[1][:3] == (1, 1, 1) and m.motor()[0] == 1
    r, stop, cancel = submit(m, command=2)
    assert r == 0 and cancel == 0 and stop != first
    tick(m)
    assert pop(m)[:4] == (stop, 0, 0, 1) and pop(m) is None
    assert profiles(m)[1][:3] == (1, 1, 0) and m.word('model_enable_count') == 0
    passed('已消费Start的ACCEPTED不改为取消；Stop撤销两拍准备并保留Active修订')

    m = ready()
    for target, expected in [(81., 3), (math.nan, 3), (math.inf, 3)]:
        _, ticket, _ = submit(m, command=0, target=target)
        tick(m)
        assert pop(m)[:4] == (ticket, 0, 0, expected)
        assert profiles(m)[1][:3] == (1, 0, 0)
    _, ticket, _ = submit(m, command=1, target=40.); tick(m)
    assert pop(m)[:4] == (ticket, 0, 0, 4)
    assert submit(m, kind=99)[0] == 2 and submit(m, command=99)[0] == 2
    assert fault(m) == (0, 0)
    passed('逐ticket忠实返回Motor BAD_TARGET/WRONG_STATE；拒绝Start不改Active，外部错误不Fail-closed')

    m = ready()
    for kp, ki, expected in [(.0005, 0., 0), (.005, .02, 0), (.00049, 0., 1), (.0051, .004, 1),
                             (.001, -.001, 1), (.001, .0201, 1), (math.nan, .004, 1), (.001, math.inf, 1)]:
        old = profiles(m)
        _, ticket, _ = submit(m, kind=1, kp=kp, ki=ki); tick(m); c = pop(m)
        assert c[0] == ticket and c[4] == expected
        if expected:
            assert profiles(m) == old
    assert fault(m) == (0, 0)
    passed('速度PI边界/NaN/Inf均由ADC判定，非法写不增加修订、不部分修改或触发内部故障')

    m = ready()
    submit(m, kind=1, kp=.002, ki=.008); tick(m); pop(m)
    accepted_start(m)
    before = profiles(m)
    submit(m, kind=1, kp=.003, ki=.012); tick(m); assert pop(m)[4] == 2
    assert profiles(m) == before and m.motor()[0] == 1
    tick(m, 2); assert m.motor()[0] == 2
    assert submit(m, kind=1, kp=.004, ki=.016)[0] == 0
    tick(m); assert pop(m)[4] == 2 and profiles(m) == before
    submit(m, command=0, target=40.); tick(m)
    assert pop(m)[3] == 4 and profiles(m) == before
    offset = read(m, 'exchange_layout', '18I')[16]
    actual = struct.unpack('<2f', m.cpu.mem_read(symbols['s_control_runtime'] + offset, 8))
    assert actual == before[0][4:] and actual != before[0][:2]
    submit(m, command=2); tick(m); pop(m)
    assert profiles(m)[0] == before[0] and profiles(m)[1][:3] == (2, 2, 0)
    passed('Start锁存Staged且真正安装PI实例；启动准备/RUN写拒绝、重复Start拒绝、Stop保留Active')

    m = ready()
    for kp, ki in [(.002, .008), (.003, .012)]:
        submit(m, kind=1, kp=kp, ki=ki); tick(m)
    assert profiles(m)[1][0] == 3
    assert pop(m)[5] == 2 and pop(m)[5] == 3 and pop(m) is None
    before = profiles(m)
    _, pi_ticket, _ = submit(m, kind=1, kp=.004, ki=.016)
    _, stop_ticket, cancelled = submit(m, command=2)
    assert cancelled == pi_ticket
    tick(m); assert pop(m)[0] == stop_ticket and pop(m) is None and profiles(m) == before
    passed('Completion保留每次写的权威修订，后续写不污染旧结果；Stop取消排队参数写')

    m = ready(); layout = read(m, 'exchange_layout', '18I')
    m.cpu.mem_write(symbols['s_parameters'] + layout[1], struct.pack('<I', 0xFFFFFFFF))
    submit(m, kind=1, kp=.002, ki=.008); tick(m); assert pop(m)[5] == 0
    accepted_start(m)
    assert profiles(m)[1][:3] == (0, 0, 1)
    passed('Staged generation自然回绕，Active generation=0且in_use=true仍明确表示已锁存')

    m = ready(); before = m.motor()[3]
    for rate in [50, 250, 500]:
        submit(m, kind=2, rate=rate); tick(m); c = pop(m)
        assert c[4] == 0 and c[6] == rate
    assert profiles(m)[1][3:] == (1, 500, 4)
    assert submit(m, command=0, target=60.)[0] == 5 and m.motor()[3] == before
    submit(m, kind=2, rate=251); tick(m); assert pop(m)[4] == 1
    submit(m, kind=2, rate=250); tick(m); pop(m); accepted_start(m); tick(m, 2)
    submit(m, kind=2, rate=50); tick(m); assert pop(m)[4] == 2
    passed('遥测配置同样实时整组写，500Hz禁止Start进入Motor，RUN禁止修改')

    m = ready(); a = sample(m)
    tick(m, 100); b = sample(m)
    assert b['sequence'] - a['sequence'] == 10 and b['time'] - a['time'] == 200
    assert b['valid'] & 0x3EF == 0x3EF and not b['valid'] & 16
    assert b['active_generation'] == 0 and b['telemetry_generation'] == 1
    assert b['position_time'] <= b['time'] and b['floats'][6] == 12.
    m.write('s_commissioning_count', 0xFFFFFFFF); m.write('s_commissioning_version', 0xFFFFFFFE)
    tick(m, 10)
    assert sample(m)['sequence'] == 0 and m.word('s_commissioning_version') == 0
    submit(m, kind=1, kp=.005, ki=.004); tick(m); pop(m)
    accepted_start(m); tick(m, 20); c = sample(m)
    assert c['valid'] == 1023 and c['floats'][0] == 60. and abs(c['floats'][3]) <= .100001
    assert c['limits'] & 1 and c['limits'] & ~15 == 0 and c['active_generation'] == 2
    old = c; sample(m); sample(m); assert sample(m) == old
    # 一致性竞争失败保留输出；心跳过期不得把旧publication当成健康数据。
    old_bytes = bytes(m.cpu.mem_read(symbols['exchange_sample'], layout[7]))
    version = m.word('s_commissioning_version'); m.write('s_commissioning_version', version | 1)
    assert sample(m) is None and bytes(m.cpu.mem_read(symbols['exchange_sample'], layout[7])) == old_bytes
    m.write('s_commissioning_version', version)
    m.memory_write(0xE0001004, m.memory_word(0xE0001004) + 126 * 170)
    assert sample(m)['valid'] == 0 and sample(m)['limits'] == 0
    assert not m.fast_uart_accesses
    passed('1kHz固定publication取真实控制状态，读者不推进序号；奇偶竞争/心跳过期/有效位正确且无ISR串口')

    m = ready(); accepted_start(m); tick(m, 2)
    first = profiles(m)
    m.write('exchange_reason', 3); m.call('exchange_fail')
    assert m.memory_word(0x40012C44) & 0x8000 == 0 and m.motor()[0] == 2
    assert fault(m) == (3, 1) and result(m)[15] == 0
    assert submit(m, command=0, target=60.)[0] == 4 and submit(m, kind=1)[0] == 4
    assert profiles(m)[1][2] == 0
    m.write('exchange_reason', 7); m.call('exchange_fail'); assert fault(m) == (3, 2)
    tick(m)
    assert m.motor()[0] == 3 and m.motor()[1] & 128 and profiles(m)[0] == first[0]
    _, ticket, _ = submit(m, command=3); tick(m); assert pop(m)[:4] == (ticket, 0, 0, 2)
    _, ticket, _ = submit(m, command=2); tick(m); assert pop(m)[:4] == (ticket, 0, 0, 1)
    assert m.motor()[0] == 3 and profiles(m)[1][2] == 0
    passed('无ADC等待立即safe-off/首因锁存/计数/Session资格撤销；下一边界FAULT，Clear不恢复')

    m = ready()
    tickets = []
    for _ in range(5):
        _, ticket, _ = submit(m, command=3); tickets.append(ticket); tick(m)
    assert m.memory_word(0x40012C44) & 0x8000 == 0 and fault(m) == (4, 1)
    assert [pop(m)[0] for _ in range(4)] == tickets[:4] and pop(m) is None
    tick(m); assert m.motor()[0] == 3 and m.motor()[1] & 128
    passed('Completion深度4，第5个失败不覆盖旧结果，真实Target入口立即safe-off并锁内部故障')

    m = ready(); accepted_start(m); tick(m, 2)
    _, ticket, _ = submit(m, command=1, target=40.)
    # 仅推进独立守卫的真实时长边界，ADC尚未消费Update。
    started = m.memory_word(symbols['g_target_runtime_run_status'] + 12)
    now = (started + 255000000) & 0xFFFFFFFF
    m.memory_write(0xE0001004, now)
    m.write('s_last_sample_cycles', now); m.write('s_last_guard_cycles', (now - 8500) & 0xFFFFFFFF)
    m.memory_write(0x40001010, 1); m.call('model_guard')
    assert pop(m) is None
    tick(m)
    c = pop(m)
    assert c[0] == ticket and c[2] == 1 and pop(m) is None
    assert m.motor()[0:2] == (1, 0) and m.motor()[4] == 0 and profiles(m)[1][2] == 0
    passed('TIM6安全Stop只交接，ADC唯一生产取消Completion；旧Update不执行且不丢ticket')

    m = Model(); layout = read(m, 'exchange_layout', '18I'); m.call('exchange_unit_init')
    unit_base = symbols['exchange_unit']
    for off in layout[2:4]:
        m.cpu.mem_write(unit_base + off, struct.pack('<I', 0xFFFFFFFE))
    # 观察真实ARM写入顺序，slot必须先于head发布；DMB机器码来自正式Target。
    barrier = bytes(m.cpu.mem_read(symbols['runtime_exchange_barrier'] & ~1, 16))
    assert bytes.fromhex('bff35f8f') in barrier
    writes = []
    handle = m.cpu.hook_add(UC_HOOK_MEM_WRITE, lambda uc, access, address, size, value, ctx:
                           writes.append(address), begin=unit_base, end=unit_base + 512)
    for ticket in range(1, 5):
        m.write('exchange_reason', ticket); m.write('exchange_command', 1); m.call('exchange_unit_complete')
        assert result(m)[3] == 1
    m.cpu.hook_del(handle)
    slot_write = next(i for i, address in enumerate(writes)
                      if unit_base + layout[5] <= address < unit_base + layout[5] + 4 * layout[6])
    assert slot_write < writes.index(unit_base + layout[2])
    before = bytes(m.cpu.mem_read(unit_base, layout[5] + 4 * layout[6]))
    m.write('exchange_reason', 5); m.call('exchange_unit_complete'); assert result(m)[3] == 0
    assert bytes(m.cpu.mem_read(unit_base, len(before))) == before
    assert [pop(m, unit=True)[0] for _ in range(4)] == [1, 2, 3, 4] and pop(m, unit=True) is None
    m.cpu.mem_write(symbols['s_exchange'] + layout[4], struct.pack('<I', 0xFFFFFFFF))
    assert submit(m)[0] == 3 and fault(m)[0] == 6
    passed('SPSC跨u32 head/tail回绕、slot-before-head与真实DMB、满队列不改数据；ticket用尽不重用')

    m = Model(); layout = read(m, 'exchange_layout', '18I')
    m.cpu.mem_write(symbols['s_exchange'] + layout[2], struct.pack('<I', 5))
    assert pop(m) is None and fault(m) == (6, 1)
    passed('完成队列身份/计数不变量损坏直接Fail-closed，不作为普通空队列或外部错误处理')

    m = Model(start=False); m.write('exchange_reason', 1); m.call('exchange_fail'); m.call('model_start')
    assert fault(m) == (1, 1) and m.motor()[0:2] == (3, 128) and m.word('model_start_count') == 0
    m = Model(); m.cpu.reg_write(UC_ARM_REG_PRIMASK, 1)
    assert submit(m)[0] == 0 and m.cpu.reg_read(UC_ARM_REG_PRIMASK) == 1
    m.write('exchange_reason', 2); m.call('exchange_fail')
    assert m.cpu.reg_read(UC_ARM_REG_PRIMASK) == 1
    passed('启动前内部锁存不被装配清除；提交和Fail-closed保持调用者原PRIMASK')
