"""执行真实 Platform ARM 指令；只证明采集边界/整数记录，不推断 WCET 或模拟电路。"""
from pathlib import Path
import json
import struct
import tempfile

from verify_platform import Model, compile_image, run, ADC1_ISR, ADC2_ISR, BDTR, MOE

REQUEST = 0x52415731
SIZE = 2104
FIELDS = ('state count reason first_slot last_slot epoch max_copy_cycles reserved '
          'first_time last_time first_entry_cycles last_entry_cycles').split()


def record(model):
    raw = bytes(model.cpu.mem_read(model.symbols['g_g431_raw_adc_capture'], SIZE))
    return dict(zip(FIELDS, struct.unpack_from('<8I2Q2I', raw))), raw


def request(model):
    model.write('g_g431_raw_adc_request', REQUEST)


def main():
    cases = []
    with tempfile.TemporaryDirectory(prefix='g431-raw-adc-') as folder:
        root = Path(folder)
        (root / 'normal').mkdir()
        normal = compile_image(root / 'normal')
        assert 'g_g431_raw_adc_capture' not in normal[1]
        assert 'g_g431_raw_adc_request' not in normal[1]
        cases.append('普通构建不链接采集缓冲/请求/钩子')
        (root / 'capture').mkdir()
        image = compile_image(root / 'capture', definitions=['BLDC_G431_RAW_ADC_CAPTURE'])
        run(image)  # 同一生产对象仍通过已有输出/抢占/截止回归。

        m = Model(image)
        m.tick(0)
        assert record(m)[1] == bytes(SIZE)
        m.write('g_g431_raw_adc_request', REQUEST ^ 1)
        m.tick(0)
        assert record(m)[1] == bytes(SIZE)
        cases.append('默认静默，错误请求值无效')
        request(m)
        # 边界码与非周期序列，验证 B/C 顺序及整个 512 对数组。
        expected = [(i * 73 % 4096, 4095 - i * 37 % 4096) for i in range(512)]
        callback_instructions = []
        for index, (b, c) in enumerate(expected):
            m.put(0x50000180, b)
            m.put(0x50000080, c)
            m.next_sample()
            instructions = [0]
            m.on_instruction = lambda _m, _pc: instructions.__setitem__(0, instructions[0] + 1)
            m.tick(0, advance=0, count=None)
            m.on_instruction = None
            callback_instructions.append(instructions[0])
            state, raw = record(m)
            assert state['count'] == index + 1
            assert state['state'] == (2 if index == 511 else 1)
            assert not m.moe() and state['reason'] == 0
        assert list(struct.iter_unpack('<2H', raw[56:])) == expected
        assert state['last_slot'] - state['first_slot'] == 511
        assert state['last_time'] - state['first_time'] == 1022
        assert state['first_time'] == state['first_slot'] * 2
        assert state['last_entry_cycles'] - state['first_entry_cycles'] == 511 * 17000
        assert m.word('model_position_ticks') == m.word('model_bus_ticks') == 514
        assert m.word('g_g431_raw_adc_request') == REQUEST  # SWD 写后核验不能被消费时清零。
        cases.append('512 对整数码连续完整，B/C 顺序/首尾时间/辅助 tick 不变')
        frozen = raw
        request(m)
        for _ in range(3): m.tick(0)
        assert record(m)[1] == frozen
        # 冻结后走原有使能路径；诊断不拦截/修改业务，也不再写缓冲。
        m.powered()
        assert record(m)[1] == frozen
        cases.append('完成后再次请求不重采，后续控制行为不变且缓冲冻结')

        for start_powered in (False, True):
            m = Model(image)
            if start_powered: m.powered()
            else: m.put(BDTR, m.get(BDTR) | MOE)
            request(m)
            before = m.get(BDTR)
            m.tick(0 if not start_powered else 2)
            state, _ = record(m)
            assert (state['state'], state['reason'], state['count']) == (3, 1, 0)
            assert m.get(BDTR) == before
        cases.append('软件功率已开或仅 MOE 已开均拒绝；诊断不写功率寄存器')

        m = Model(image)
        request(m); m.tick(0)
        m.put(BDTR, m.get(BDTR) | MOE); m.tick(0)
        state, frozen = record(m)
        assert (state['state'], state['reason'], state['count']) == (4, 1, 1)
        request(m); m.tick(0); assert record(m)[1] == frozen
        cases.append('采集中出现功率开启则冻结失败，不自动重试')

        for before_first in (False, True):
            m = Model(image); request(m)
            if not before_first: m.tick(0)
            m.next_sample(); m.put(ADC1_ISR, 96); m.put(ADC2_ISR, 0)
            m.call('platform_model_tick')
            state, _ = record(m)
            assert (state['state'], state['reason'], state['count']) == (3 if before_first else 4, 2, 0 if before_first else 1)
        cases.append('不完整 ADC 对分别拒绝首拍或中止已有记录')

        m = Model(image); request(m); m.tick(0)
        m.call('realtime_motor_io_safe_off'); m.tick(0)
        state, _ = record(m)
        assert (state['state'], state['reason'], state['count']) == (4, 3, 1)
        cases.append('safe-off epoch 改变使当前采集失效')

        # 只篡改诊断元数据，模拟遗漏/损坏；不改变控制变量来制造测试通过。
        for offset, value, reason in [(16, 999, 4), (4, 512, 6)]:
            m = Model(image); request(m); m.tick(0)
            m.put(m.symbols['g_g431_raw_adc_capture'] + offset, value)
            m.tick(0)
            state, _ = record(m)
            assert (state['state'], state['reason']) == (4, reason)
        cases.append('非连续 slot 或损坏 count 拒绝写入数组')

        # 额外整数指令是执行路径度量，Unicorn 不模拟真实 Flash/RAM/总线延迟。
        def instruction_count(candidate, armed):
            model = Model(candidate)
            if armed: request(model)
            model.next_sample()
            count = [0]
            model.on_instruction = lambda _m, _pc: count.__setitem__(0, count[0] + 1)
            model.tick(0, advance=0, count=None)
            return count[0]
        baseline = instruction_count(normal, False)
        inactive = instruction_count(image, False)
        summary = dict(cases=cases, normal_tick_instructions=baseline,
                       diagnostic_idle_extra_instructions=inactive-baseline,
                       diagnostic_active_extra_instructions_min=min(callback_instructions)-baseline,
                       diagnostic_active_extra_instructions_max=max(callback_instructions)-baseline,
                       capture_bytes=SIZE, limits='ARM 指令数不是硬件 cycle 或 WCET；不访问设备。')
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f'Raw ADC capture ARM model: {len(cases)} cases PASS')


if __name__ == '__main__':
    main()
