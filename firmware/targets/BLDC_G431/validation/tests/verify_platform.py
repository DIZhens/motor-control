"""Execute the real G431 Platform ARM object; no ST-Link or board access.

The timer model implements CCR preloads, update transfers, UDIS, CNT/DIR and
status-flag clearing. Time advances only at explicit test injection points: this
is an ordering/deadline regression, not a cycle-accurate peripheral or WCET test.
"""
from pathlib import Path
import os
import re
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_SP, UC_ARM_REG_LR
from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_PRIMASK

ROOT = Path(__file__).resolve().parents[2]
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
TIM1 = 0x40012C00
CR1, SR, EGR, CNT, ARR = TIM1, TIM1 + 0x10, TIM1 + 0x14, TIM1 + 0x24, TIM1 + 0x2C
CCRS = (TIM1 + 0x34, TIM1 + 0x38, TIM1 + 0x3C)
BDTR = TIM1 + 0x44
DWT_CTRL, DWT_CYCCNT = 0xE0001000, 0xE0001004
ADC1_ISR = 0x50000000
ADC2_ISR = 0x50000100
CR2, DIER, RCR = TIM1 + 4, TIM1 + 0x0C, TIM1 + 0x30
GATE = 7 << 20
CEN, UDIS, DIR, MOE = 1, 2, 16, 1 << 15
BREAKS = (1 << 7) | (1 << 8)
STOP = 0x0801F000
NEUTRAL = (2125, 2125, 2125)
def physical_phase_channels():
    """从原始板卡网表和固定 LQFP48 引脚事实独立得到期望，不读取生产 C 赋值。"""
    netlist = Path(__file__).resolve().parents[5] / 'hardware/BLDC_G431/schematic/Netlist_Schematic1_2026-09-09.tel'
    text = netlist.read_text(encoding="utf-8-sig")
    # ST CubeMX STM32G431C(6-8-B)Tx.xml：30/31/32=PA8/9/10，27/28/29=PB13/14/15。
    high = {30: 1, 31: 2, 32: 3}
    low = {27: 1, 28: 2, 29: 3}
    result = {}
    for phase in "ABC":
        h = re.search(r"^INH"+phase+r" ; U4\.(\d+) U5\.", text, re.M)
        l = re.search(r"^INL"+phase+r" ; U4\.(\d+) U5\.", text, re.M)
        assert h and l, "网表缺少物理相输入"
        assert high[int(h[1])] == low[int(l[1])], "高低侧通道配对不符"
        result[phase] = high[int(h[1])]
    assert sorted(result.values()) == [1,2,3]
    return result


PHASE_CHANNELS = physical_phase_channels()
REQUEST = tuple(dict(zip(PHASE_CHANNELS.values(), (850,2125,3400)))[channel] for channel in [1,2,3])
STAGE, NULL_REQUEST, DUPLICATE, OFF, ENABLE = 2, 4, 8, 16, 32


def compile_image(directory, extra_sources=(), definitions=()):
    scatter = directory / "platform.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00020000 {\n"
        " ER_IROM1 0x08000000 0x00020000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")
    settings = ET.parse(ROOT / 'MDK-ARM/BLDC_G431_Validation.uvprojx').find(".//Cads/VariousControls")
    command = [str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
               "--split_sections", "-g", "-I", str(ROOT / 'platform'), "-I", str(ROOT / 'runtime')]
    for include in settings.findtext("IncludePath").split(";"):
        command += ["-I", str((ROOT / 'MDK-ARM' / include.replace("\\", "/")).resolve())]
    command += ["-D" + item for item in settings.findtext("Define").split(",")]
    command += ["-D" + item for item in definitions]
    if 'BLDC_G431_FINITE_RUN' in definitions:
        command += ['-I', str(ROOT / 'runtime')]
    objects = []
    # Compile the production translation unit unchanged; no stage/enable substitutes.
    for source in [ROOT / 'validation/tests/platform_model.c', ROOT / 'platform/realtime_motor_io.c', *extra_sources]:
        obj = directory / (source.stem + ".o")
        result = subprocess.run(command + ["-c", str(source), "-o", str(obj)],
                                capture_output=True, text=True)
        assert result.returncode == 0 and "warning" not in result.stderr.lower(), result.stderr
        objects.append(str(obj))
    result = subprocess.run([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp",
                             "--scatter", str(scatter), "--entry=platform_model_init",
                             "--keep=platform_model.o(*)", "-o", str(directory / "platform.axf"),
                             *objects], cwd=directory, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    with (directory / "platform.axf").open("rb") as stream:
        elf = ELFFile(stream)
        contents = [(s["p_vaddr"], s.data()) for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
        contents += [(s["sh_addr"], s.data()) for s in elf.iter_sections()
                     if (s["sh_flags"] & 2) and s["sh_type"] == "SHT_PROGBITS"]
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
    return contents, symbols


class Model:
    def __init__(self, image, hal_ok=True, cycle_counter=123456, auto_start=True):
        contents, self.symbols = image
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
        for base, size in [(0x08000000, 0x20000), (0x20000000, 0x8000),
                           (0x40012000, 0x1000), (0x40021000, 0x1000), (0x50000000, 0x2000),
                           (0xE0001000, 0x1000), (0xE000E000, 0x1000)]:
            self.cpu.mem_map(base, size)
        for address, data in contents:
            self.cpu.mem_write(address, data)
        self.put(0xE000ED88, 0x00F00000)
        self.put(DWT_CYCCNT, cycle_counter)
        self.shadow = [0, 0, 0]
        self.active = (0, 0, 0)
        self.transfers = []
        self.writes = []
        self.pending_fixes = {}
        self.pending_delay = 0
        self.pending_irq = False
        self.pending_update = False
        self.in_update = False
        self.running = 0
        self.repetition = 0
        self.triggers = []
        self.uevs = []
        self.deferred_advance = 0
        self.interrupted = False
        self.in_irq = False
        self.on_marker = None
        self.on_ccr = None
        self.on_read = None
        self.on_write = None
        self.on_instruction = None
        self.irq_calls = 0
        self.instruction_budget = 200000
        # Unicorn 2.1.4's global RAM-write hook misexecutes ARMCC memclr's IT tail.
        # Observe only modeled MMIO and the explicit marker, leaving bulk RAM alone.
        for start, end in [(TIM1, TIM1 + 0x7F), (ADC1_ISR, ADC1_ISR + 3), (ADC2_ISR, ADC2_ISR + 3),
                           (DWT_CTRL, DWT_CYCCNT + 3),
                           (self.symbols["model_marker"], self.symbols["model_marker"] + 3)]:
            self.cpu.hook_add(UC_HOOK_MEM_WRITE, self.memory_write, begin=start, end=end)
        # Time/fault injections read MMIO only; avoid instrumenting conditional RAM loads.
        for start, end in [(TIM1, TIM1 + 0x7F), (DWT_CTRL, DWT_CYCCNT + 3)]:
            self.cpu.hook_add(UC_HOOK_MEM_READ, self.memory_read, begin=start, end=end)
        self.cpu.hook_add(UC_HOOK_CODE, self.instruction)
        self.write("model_hal_ok", int(hal_ok))
        self.write("model_auto_start", int(auto_start))
        self.call("platform_model_init")

    def put(self, address, value):
        self.cpu.mem_write(address, struct.pack("<I", value & 0xFFFFFFFF))

    def get(self, address):
        return struct.unpack("<I", self.cpu.mem_read(address, 4))[0]

    def write(self, symbol, value, fmt="I"):
        self.cpu.mem_write(self.symbols[symbol], struct.pack("<" + fmt, value))

    def word(self, symbol):
        return self.get(self.symbols[symbol])

    def moe(self):
        return bool(self.get(BDTR) & MOE)

    def transfer(self, cause):
        suppressed = bool(self.get(CR1) & UDIS)
        if not suppressed:
            self.active = tuple(self.shadow)
            self.put(SR, self.get(SR) | 1)
            self.uevs.append((cause, self.get(DWT_CYCCNT)))
            if cause != "UG" and self.get(DIER) & 1:
                self.pending_update = True
        self.transfers.append((cause, suppressed, self.active, tuple(self.shadow)))

    def advance(self, cycles):
        while cycles:
            if not self.get(CR1) & CEN:
                if self.get(DWT_CTRL) & 1:
                    self.put(DWT_CYCCNT, self.get(DWT_CYCCNT) + cycles)
                break
            down = bool(self.get(CR1) & DIR)
            count, top = self.get(CNT), self.get(ARR)
            boundary = count if down else top - count
            distance = min(cycles, boundary)
            # PWM2 的 OC4REF 仅在上数跨过 compare 时产生上升沿。
            compare = self.get(TIM1 + 0x40)
            if not down and count < compare <= count + distance and self.get(CR2) & GATE == GATE:
                self.triggers.append((self.get(DWT_CYCCNT) + compare - count) & 0xFFFFFFFF)
                # 显式 callback 前模拟转换完成；不从 Unicorn 指令数推断真实转换延迟。
                self.put(ADC1_ISR, self.get(ADC1_ISR) | 96)
                self.put(ADC2_ISR, self.get(ADC2_ISR) | 96)
            if self.get(DWT_CTRL) & 1:
                self.put(DWT_CYCCNT, self.get(DWT_CYCCNT) + distance)
            self.put(CNT, count - distance if down else count + distance)
            cycles -= distance
            if distance < boundary:
                break
            self.put(CR1, self.get(CR1) ^ DIR)
            if self.repetition == 0:
                self.repetition = self.get(RCR)
                self.transfer("bottom" if down else "top")
            else:
                self.repetition -= 1
            if self.pending_update and not self.cpu.reg_read(UC_ARM_REG_PRIMASK):
                if self.running:
                    self.deferred_advance += cycles
                    break
                self.pending_update = False
                self.in_update = True
                self.call("platform_model_update_irq")
                self.in_update = False

    def position(self, count=4000, down=True):
        self.put(CNT, count)
        self.put(CR1, (self.get(CR1) | DIR) if down else (self.get(CR1) & ~DIR))

    def memory_write(self, cpu, access, address, size, value, user_data):
        if self.on_write:
            self.on_write(self, address, value)
        if address == SR:
            self.pending_fixes[address] = self.get(SR) & value
        elif address in (ADC1_ISR, ADC2_ISR):
            self.pending_fixes[address] = self.get(address) & ~value
        elif address == EGR and value & 1:
            self.repetition = self.get(RCR)
            self.transfer("UG")
        elif address in CCRS:
            self.shadow[CCRS.index(address)] = value
            self.writes.append((address, value, self.get(CR1), cpu.reg_read(UC_ARM_REG_PRIMASK)))
            if self.on_ccr:
                self.on_ccr(self, address, value)
        elif address == self.symbols["model_marker"] and self.on_marker:
            self.on_marker(self, value)

    def memory_read(self, cpu, access, address, size, value, user_data):
        if self.on_read:
            self.on_read(self, address)

    def instruction(self, cpu, address, size, user_data):
        for target, value in self.pending_fixes.items():
            self.put(target, value)
        self.pending_fixes.clear()
        if not self.in_update and not self.pending_update and self.deferred_advance:
            delay, self.deferred_advance = self.deferred_advance, 0
            self.advance(delay)
        if self.pending_delay:
            delay, self.pending_delay = self.pending_delay, 0
            self.advance(delay)
        if self.on_instruction and not self.in_irq:
            self.on_instruction(self, address)
        if (self.pending_update and not self.in_update or self.pending_irq and not self.in_irq) and not cpu.reg_read(UC_ARM_REG_PRIMASK):
            self.interrupted = True
            cpu.emu_stop()

    def call(self, name, stack=0x20007FF0):
        self.cpu.reg_write(UC_ARM_REG_SP, stack)
        self.cpu.reg_write(UC_ARM_REG_LR, STOP | 1)
        self.cpu.reg_write(UC_ARM_REG_PC, self.symbols[name] | 1)
        for _ in range(20):
            self.interrupted = False
            self.running += 1
            self.cpu.emu_start(self.cpu.reg_read(UC_ARM_REG_PC) | 1, STOP, count=self.instruction_budget)
            self.running -= 1
            if self.interrupted:
                context = self.cpu.context_save()
                if self.pending_update:
                    self.in_update, self.pending_update = True, False
                    self.call("platform_model_update_irq", stack=0x20007000)
                    self.in_update = False
                else:
                    self.in_irq, self.pending_irq = True, False
                    self.call("realtime_motor_io_safe_off", stack=0x20007000)
                    self.irq_calls += 1
                    self.in_irq = False
                self.cpu.context_restore(context)
            else:
                assert self.cpu.reg_read(UC_ARM_REG_PC) == STOP, name
                return
        raise AssertionError("interrupt injection did not settle")

    def next_sample(self):
        # 推进到真正开放的首峰，再模拟 CNT=4000 下数时的转换完成入口。
        old = len(self.triggers)
        for _ in range(300):
            self.advance(250)
            if len(self.triggers) > old:
                age = (self.get(DWT_CYCCNT) - self.triggers[-1]) & 0xFFFFFFFF
                if age < 260:
                    self.advance(260 - age)
                return
        raise AssertionError("no gated sample")

    def tick(self, actions=STAGE, count=4000, down=True, advance=17000):
        if advance:
            self.next_sample()
        elif count is not None:
            self.position(count, down)
        self.put(ADC1_ISR, (1 << 5) | (1 << 6))
        self.put(ADC2_ISR, (1 << 5) | (1 << 6))
        self.write("model_actions", actions)
        for name in ["model_stage_result", "model_second_stage_result", "model_enable_result",
                     "model_early_enable_result"]:
            self.write(name, 0xFFFFFFFF)
        self.call("platform_model_tick")

    def powered(self):
        self.tick(STAGE)
        assert self.word("model_stage_result") == 1 and not self.moe()
        self.tick(STAGE | ENABLE)
        assert self.word("model_enable_result") == 1 and self.moe()
        return self

    def timing_failure(self):
        names = ['sequence','reason','slot_sequence','callback_slot','observed_cycles','slot_start_cycles',
            'irq_entry_cycles','callback_start_cycles','consumer_entry_cycles','consumer_exit_cycles',
            'auxiliary_exit_cycles','reserved','tim_count','tim_status','tim_control','state_bits',
            'adc1_status','adc2_status','actuation_epoch','callback_epoch','observation_time']
        return dict(zip(names,struct.unpack('<20IQ',self.cpu.mem_read(self.symbols['g_g431_timing_failure'],88))))


def run(image):
    cases = []
    def passed(name): cases.append(name)
    for phase in "ABC":
        m = Model(image)
        values = [0.5,0.5,0.5]
        values["ABC".index(phase)] = 0.2
        m.cpu.mem_write(m.symbols["model_request"], struct.pack("<3f", *values))
        m.tick(STAGE)
        expected = list(NEUTRAL)
        expected[PHASE_CHANNELS[phase]-1] = 850
        assert m.word("model_stage_result") == 1 and tuple(m.shadow) == tuple(expected)
        m.tick(0)
        assert m.active == tuple(expected) and not m.moe()
        passed("原始网表确定物理相 "+phase+" 在 CH"+str(PHASE_CHANNELS[phase])+" 预装载和生效")
    m = Model(image)
    assert m.word("model_bind_result") == m.word("model_arm_result") == m.word("model_start_result") == 1
    assert m.word("model_ug_after_adc") == 0 and m.active == NEUTRAL and not m.moe()
    assert m.get(DWT_CYCCNT) == 123456 and m.get(CR2) & GATE == 0
    assert [x[0] for x in m.uevs] == ["UG"] and m.repetition == 1
    passed("UG/neutral/RCR phase precede external ADC arm; start has no UG")
    m.advance(4250); assert len(m.uevs) == 1 and not m.triggers
    m.advance(4250); assert m.uevs[-1][0] == "bottom" and m.get(CR2) & GATE == GATE
    m.advance(4500); m.tick(0, advance=0)
    assert m.word("model_observation_time") == 2
    before = len(m.triggers); m.advance(8500)
    assert len(m.triggers) == before
    passed("first effective bottom opens first peak only; second peak suppressed")
    m.tick(0); assert m.word("model_observation_time") == 4
    assert (m.triggers[-1] - m.triggers[-2]) & 0xFFFFFFFF == 17000
    passed("100 us current samples advance the 20 kHz ControlTime by two")

    m = Model(image); m.call("platform_model_stage_outside"); m.call("platform_model_enable_outside")
    assert m.word("model_stage_result") == m.word("model_enable_result") == 0
    passed("background stage and enable rejected")
    m = Model(image, hal_ok=False)
    assert m.word("model_arm_result") == m.word("model_start_result") == 0
    passed("arm failure cannot start")
    m = Model(image); m.call("platform_model_wrong_adc_tick"); assert m.word("model_callbacks") == 0
    passed("ADC2 cannot duplicate the authority callback")

    for raw_b, raw_c in [(0, 4095), (4095, 0), (2048, 2014)]:
        m = Model(image); m.put(0x50000180, raw_b); m.put(0x50000080, raw_c); m.tick(0)
        assert m.word("model_observation_valid") == 1 and m.word("model_observation_mask") == 6
        for phase, raw in [("b", raw_b), ("c", raw_c)]:
            actual = struct.unpack("<f", m.cpu.mem_read(m.symbols["model_observation_" + phase + "_a"], 4))[0]
            assert abs(actual - (raw * 3.3 / 4095.0 - 1.623) / .7049) < .000002
        passed("fixed ADC B/C mapping " + str((raw_b, raw_c)))
    m = Model(image); m.next_sample(); m.put(ADC1_ISR, 96); m.put(ADC2_ISR, 0)
    m.call("platform_model_tick"); assert m.word("model_observation_valid") == 0
    m.tick(0); assert m.word("model_observation_valid") == 0
    passed("incomplete ADC pair remains invalid")

    invalid = [
        (TIM1 + 0x28, 1), (ARR, 4249), (RCR, 0), (CR1, 0x80),
        (CR1, 0x120), (CR1, 0xA8), (CR1, 0xA2),
        (TIM1 + 0x18, 1 << 11), (TIM1 + 0x18, 1 << 3),
        (TIM1 + 0x1C, 1 << 11), (0x40021008, 4 << 11), (TIM1 + 0x40, 4230)]
    for register, value in invalid:
        m = Model(image, auto_start=False); m.put(register, value); m.call("platform_model_start")
        assert m.word("model_start_result") == 0 and not m.moe()
        passed(f"unsupported configuration {register:x}/{value:x}")

    m = Model(image); m.tick(STAGE | DUPLICATE)
    assert m.word("model_stage_result") == 1 and m.word("model_second_stage_result") == 0
    assert m.active == NEUTRAL and tuple(m.shadow) == REQUEST
    assert all(x[2] & UDIS and x[3] for x in m.writes[-3:])
    m.advance(4000); assert m.active == NEUTRAL
    m.advance(8500); assert m.active == REQUEST
    passed("three preloads remain unchanged at intermediate bottom and commit together at target UEV")
    m = Model(image); m.tick(STAGE | ENABLE)
    assert m.word("model_enable_result") == 0 and not m.moe()
    m.tick(STAGE); m.tick(STAGE | ENABLE); assert m.moe()
    passed("warmup must commit at a true UEV before fresh stage and enable")

    for delay, accepted in [(0, True), (4000, True), (8500, True), (10799, True), (10800, False), (12500, False), (17000, False), (25500, False)]:
        m = Model(image)
        m.on_marker = lambda model, marker, delay=delay: model.advance(delay) if marker == 1 else None
        m.tick(STAGE)
        assert m.word("model_stage_result") == int(accepted), (delay, m.word("model_stage_result"))
        if not accepted: assert not m.moe() and tuple(m.shadow) == NEUTRAL
        passed(f"slot absolute deadline and cross-carrier alias {delay}/{accepted}")

    for phase in range(3):
        for invalid_value in [-.1, .099, .901, 1.1, float("nan"), float("inf"), -float("inf")]:
            m = Model(image); req = [.2, .5, .8]; req[phase] = invalid_value
            m.cpu.mem_write(m.symbols["model_request"], struct.pack("<fff", *req)); m.tick(STAGE | ENABLE)
            assert m.word("model_stage_result") == m.word("model_enable_result") == 0 and not m.moe()
            passed(f"invalid duty {phase}/{invalid_value}")
    m = Model(image); m.tick(STAGE | NULL_REQUEST); assert m.word("model_stage_result") == 0
    passed("null duty rejected")
    for duty in [.1, .9]:
        m = Model(image); m.cpu.mem_write(m.symbols["model_request"], struct.pack("<fff", duty, duty, duty)); m.tick()
        assert m.word("model_stage_result") == 1
        passed(f"inclusive duty edge {duty}")

    m = Model(image).powered(); m.tick(0)
    assert not m.moe() and tuple(m.shadow) == NEUTRAL and m.get(CR1) & CEN
    passed("missing RUN submission fails off while timing continues")
    for marker in [3, 4]:
        m = Model(image).powered()
        assert not m.timing_failure()['sequence']
        stores=[]
        def diagnostic_store(cpu, access, address, size, value, unused):
            assert not m.moe(), 'diagnostic publication preceded safe-off'
            stores.append(address)
        address=m.symbols['g_g431_timing_failure']
        m.cpu.hook_add(UC_HOOK_MEM_WRITE,diagnostic_store,begin=address,end=address+87)
        m.on_marker = lambda model, point, marker=marker: model.advance(10800) if point == marker else None
        m.tick(STAGE)
        assert not m.moe() and tuple(m.shadow) == NEUTRAL
        failure=m.timing_failure()
        assert stores and failure['sequence']==2 and failure['reason']==(6 if marker==3 else 7),failure
        assert failure['slot_sequence']==failure['callback_slot'] and failure['reserved']==0
        assert (failure['observed_cycles']-failure['irq_entry_cycles'])%2**32>=10800
        assert failure['state_bits']&768==768
        m.on_marker = None
        m.tick(0); assert m.word("model_observation_valid") == 0
        m.tick(STAGE | ENABLE)
        assert m.word("model_stage_result") == m.word("model_enable_result") == 0 and not m.moe()
        m.call('realtime_motor_io_safe_off');assert m.timing_failure()==failure
        passed(f"consumer/HAL tail overrun at marker {marker} stays observable even with MOE already off")
        passed(f"first timing cause {failure['reason']} publishes after safe-off and survives Stop/next samples")
    m=Model(image,cycle_counter=0xffff3cb0).powered()
    m.on_marker=lambda model,point:model.advance(10800) if point==4 else None
    m.tick(STAGE);failure=m.timing_failure()
    assert failure['sequence']==2 and failure['reason']==7
    assert (failure['observed_cycles']-failure['irq_entry_cycles'])%2**32==10800,failure
    passed('first-failure timestamps retain unsigned elapsed across cycle-clock wrap')
    m=Model(image);m.put(SR,1);m.call('platform_model_update_irq')
    assert m.timing_failure()['reason']==1 and not m.moe()
    passed('premature UEV retains early-update first cause')
    m=Model(image).powered();m.next_sample();m.advance(1450)
    m.tick(STAGE,advance=0,count=None)
    assert m.timing_failure()['reason']==5 and not m.moe()
    passed('late injected admission is distinguished from consumer or HAL tail')
    m=Model(image).powered()
    m.cpu.mem_write(m.symbols['s_stage_pending_boundary'],bytes(1));m.advance(12500)
    assert m.timing_failure()['reason']==3 and not m.moe()
    passed('missing committed preload has delivery first cause')
    m = Model(image).powered(); m.tick(STAGE | OFF | ENABLE | 64)
    assert not m.moe() and m.word("model_enable_result") == m.word("model_second_stage_result") == 0
    m.tick(STAGE | ENABLE); assert not m.moe()
    m.tick(STAGE); m.tick(STAGE | ENABLE); assert m.moe()
    assert m.word("model_observation_valid") == 1
    passed("Stop invalidates pending/committed epoch without latching a timing defect")
    for flag in [1 << 7, 1 << 8, BREAKS]:
        m = Model(image).powered(); m.next_sample(); m.put(SR, m.get(SR) | flag); m.tick(STAGE | ENABLE, advance=0)
        assert not m.moe() and m.word("model_enable_result") == 0 and m.get(SR) & flag == flag
        passed(f"break remains latched {flag}")

    for at in CCRS:
        m = Model(image).powered()
        def cross(model, address, value, at=at):
            if address == at:
                model.on_ccr = None; model.pending_delay = 13000
        m.on_ccr = cross; m.tick(STAGE)
        assert m.word("model_stage_result") == 0 and not m.moe() and not m.get(CR1) & UDIS
        assert tuple(m.shadow) == NEUTRAL
        passed(f"UDIS transaction crossing target during CCR{CCRS.index(at)+1} fails safe")
    m = Model(image).powered()
    def release(model, address, value):
        if address == CR1 and model.get(CR1) & UDIS and not value & UDIS:
            model.on_write = None; model.pending_delay = 10800
    m.on_write = release; m.tick(STAGE)
    assert m.word("model_stage_result") == 0 and not m.moe()
    passed("deadline checked after UDIS release")

    # 迟到 UP 即使碰巧恢复相同 CNT/DIR，或已跨整 slot，也不能开门。
    for delay in [1700, 8500, 17000, 25500]:
        m = Model(image); m.cpu.reg_write(UC_ARM_REG_PRIMASK, 1)
        m.advance(8500 + delay); m.cpu.reg_write(UC_ARM_REG_PRIMASK, 0)
        m.call("platform_model_update_irq"); m.pending_update = False
        assert m.get(CR2) & GATE == 0 and not m.moe()
        m.next_sample(); m.tick(0, advance=0)
        expected = 2 * (delay // 17000 + 2)
        assert m.word("model_observation_time") == expected, (delay, m.word("model_observation_time"), expected)
        passed(f"late/aliased UP skips sample and preserves ControlTime gap {delay}")

    for point in ["before gate", "after gate write"]:
        m = Model(image)
        if point == "before gate":
            reads = [0]
            def stall(model, address):
                if address == DWT_CYCCNT and model.in_update:
                    reads[0] += 1
                    if reads[0] == 2:
                        model.on_read = None; model.advance(1700)
            m.on_read = stall
        else:
            def stall(model, address, value):
                if address == CR2 and value & GATE == GATE:
                    model.on_write = None; model.pending_delay = 1700
            m.on_write = stall
        m.advance(8500)
        assert m.get(CR2) & GATE == 0 and not m.moe() and not m.triggers
        passed("arming transaction rechecks deadline " + point)

    m = Model(image).powered(); m.next_sample(); m.put(ADC1_ISR, 96); m.call("platform_model_adc_enter")
    m.advance(12500)
    assert not m.moe() and m.get(CR2) & GATE == 0
    before = m.word("model_callbacks"); m.call("platform_model_adc_callback"); m.call("platform_model_adc_exit")
    assert m.word("model_callbacks") == before
    m.put(ADC1_ISR, 0); m.tick(0)
    assert not m.moe()
    passed("UP preempts old ADC busy; stale callback cannot publish as the new slot")

    for before_sample in [False, True]:
        m = Model(image).powered()
        if before_sample:
            # 当前 slot 完成后执行 UP，在首峰之前保持门开放。
            m.advance(12500)
            assert m.get(CR2) & GATE == GATE
        gate = m.get(CR2) & GATE
        m.put(ADC1_ISR, 1 << 2); m.call("platform_model_adc_enter"); m.call("platform_model_adc_exit")
        assert m.moe() and m.get(CR2) & GATE == gate
        m.tick(STAGE | ENABLE); assert m.moe()
        passed(f"regular-only ADC IRQ preserves power, pending and gate before_sample={before_sample}")
    m = Model(image).powered()
    m.put(ADC1_ISR, 4); m.call("platform_model_adc_enter")
    m.advance(12500)
    assert m.moe() and m.get(CR2) & GATE == GATE
    m.call("platform_model_regular_exit")
    m.tick(STAGE | ENABLE)
    assert m.word("model_observation_time") == 6 and m.word("model_observation_valid") == 1
    assert m.word("model_stage_result") == m.word("model_enable_result") == 1 and m.moe()
    passed("regular-only IRQ crossing UEV keeps gate and fresh sample continuity")

    for same_irq in [True, False]:
        m = Model(image).powered(); m.advance(12500); m.advance(4200)
        entered=m.get(DWT_CYCCNT)
        m.put(ADC1_ISR, 4); m.call("platform_model_adc_enter")
        m.advance(300)
        m.write("model_actions", STAGE | ENABLE)
        if same_irq:
            m.call("platform_model_adc_callback")
            assert m.word('s_irq_entry_cycles')==entered
            m.call("platform_model_adc_exit")
        else:
            m.call("platform_model_regular_exit")
            assert m.get(ADC1_ISR) & 96 == 96 and m.get(ADC2_ISR) & 96 == 96
            m.tick(STAGE | ENABLE, advance=0, count=None)
        assert m.word("model_observation_time") == 6 and m.word("model_observation_valid") == 1
        assert m.word("model_stage_result") == m.word("model_enable_result") == 1 and m.moe()
        passed("regular-to-injected arrival upgrades same HAL IRQ" if same_irq else
               "JEOS after regular HAL snapshot remains pending for the next IRQ")

    for flags in [96, 100]:
        m = Model(image).powered(); m.next_sample()
        m.put(ADC1_ISR, flags); m.call("platform_model_adc_enter")
        m.advance(12500)
        assert not m.moe() and m.get(CR2) & GATE == 0
        before = m.word("model_callbacks")
        m.call("platform_model_adc_callback"); m.call("platform_model_adc_exit")
        assert m.word("model_callbacks") == before
        passed(f"injected or mixed EOC/JEOS still owns busy through full HAL tail: {flags}")
    m = Model(image).powered(); m.next_sample()
    m.advance(12500)
    assert not m.moe() and m.get(CR2) & GATE == 0
    passed("unclaimed old JEOS at UEV still rejects new sample arming")

    for delay in [1450, 4000, 8500, 17000]:
        m = Model(image); m.next_sample(); m.advance(delay)
        m.tick(STAGE, advance=0, count=None)
        assert m.word("model_callbacks") == 0 and not m.moe() and m.get(CR2) & GATE == 0
        assert tuple(m.shadow) == NEUTRAL
        passed(f"delayed injected entry {delay} cannot relabel a second peak or stale JDR")
    m = Model(image); m.next_sample(); m.put(ADC1_ISR, 96); m.put(ADC2_ISR, 96)
    m.call("platform_model_adc_enter"); m.call("realtime_motor_io_safe_off")
    m.write("model_actions", STAGE | ENABLE); m.call("platform_model_adc_callback"); m.call("platform_model_adc_exit")
    assert m.word("model_callbacks") == 0 and not m.moe() and tuple(m.shadow) == NEUTRAL
    m.put(ADC1_ISR, 0); m.tick(STAGE); m.tick(STAGE | ENABLE); assert m.moe()
    passed("Stop between IRQ enter and callback invalidates original ADC epoch")

    for point in ["CCR group", "UDIS release", "MOE set"]:
        m = Model(image).powered()
        def break_write(model, address, value, point=point):
            match = point == "CCR group" and address == CCRS[1]
            match |= point == "UDIS release" and address == CR1 and model.get(CR1) & UDIS and not value & UDIS
            match |= point == "MOE set" and address == BDTR and value & MOE
            if match:
                model.put(SR, model.get(SR) | BREAKS); model.put(BDTR, model.get(BDTR) & ~MOE)
                if address == BDTR: model.pending_fixes[BDTR] = value & ~MOE
                model.on_write = None
        m.on_write = break_write; m.tick(STAGE | ENABLE)
        assert not m.moe() and m.word("model_enable_result") == 0 and tuple(m.shadow) == NEUTRAL
        passed("asynchronous hardware Break at " + point)
    for point in ["before stage", "between CCR writes", "before enable"]:
        m = Model(image).powered()
        if point == "between CCR writes":
            def interrupt(model, address, value):
                if address == CCRS[0]: model.pending_irq = True; model.on_ccr = None
            m.on_ccr = interrupt
        else:
            def interrupt(model, marker, point=point):
                if marker == (1 if point == "before stage" else 2): model.pending_irq = True; model.on_marker = None
            m.on_marker = interrupt
        m.tick(STAGE | ENABLE)
        assert m.irq_calls == 1 and not m.moe() and m.word("model_enable_result") == 0
        passed("asynchronous safe-off " + point)
    m = Model(image).powered(); m.put(SR, m.get(SR) | BREAKS); m.put(BDTR, m.get(BDTR) & ~MOE)
    m.advance(12500)
    assert m.get(SR) & 1 == 0 and m.get(SR) & BREAKS == BREAKS
    assert not m.moe() and m.get(CR2) & GATE == 0
    m.put(SR, m.get(SR) & ~BREAKS); m.tick(0)
    assert m.word("model_observation_time") == 8 and not m.moe()
    passed("simultaneous Break and UEV is owned once, fails off and preserves the skipped sample time")
    for register, bit in [(DWT_CTRL, 1), (0xE000EDFC, 1 << 24)]:
        m = Model(image); m.next_sample(); m.put(register, m.get(register) & ~bit); m.tick(STAGE, advance=0)
        assert m.word("model_stage_result") == 0 and not m.moe()
        passed(f"missing cycle-clock prerequisite {register:x}")
    m = Model(image); m.next_sample(); m.cpu.reg_write(UC_ARM_REG_PRIMASK, 1); m.tick(STAGE, advance=0)
    assert m.cpu.reg_read(UC_ARM_REG_PRIMASK) == 1
    m.call("realtime_motor_io_safe_off"); assert m.cpu.reg_read(UC_ARM_REG_PRIMASK) == 1
    passed("nested critical sections preserve PRIMASK")
    m = Model(image, cycle_counter=0xFFFFFF00); m.tick(STAGE); m.tick(STAGE | ENABLE)
    assert m.moe()
    passed("slot/target age remains bounded across DWT wrap")

    print(f"G431 actual Platform ARM regression: {len(cases)} cases PASS")
    print("Scope: real ARM source and explicit RCR/UEV/gate/UP preemption model; no WCET or electrical proof.")
    for name in cases: print("PASS " + name)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="g431-platform-model-") as temp:
        run(compile_image(Path(temp)))
