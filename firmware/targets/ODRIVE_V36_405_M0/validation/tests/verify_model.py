"""Execute actual ARM validation code with fake HCC/time; no hardware access."""
from pathlib import Path
import sys
import struct
import subprocess
import os
import tempfile
import shutil
import xml.etree.ElementTree as ET

tests = Path(__file__).resolve().parent
workspace = tempfile.TemporaryDirectory(prefix="f405-validation-model-")
temp = Path(workspace.name)
shutil.copyfile(tests / "model.c", temp / "model.c")
(temp / "model.sct").write_text(
    "LR_IROM1 0x08000000 0x00100000 {\n"
    " ER_IROM1 0x08000000 0x00100000 { .ANY (+RO) }\n"
    " RW_IRAM1 0x20000000 0x00020000 { .ANY (+RW +ZI) }\n"
    "}\n", encoding="utf-8")

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC
from elftools.elf.elffile import ELFFile

root = tests.parent.parent
bin_path = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
if not (bin_path / "armcc.exe").is_file():
    raise SystemExit("Set KEIL_ARMCC_BIN to the installed ARM Compiler 5 bin directory.")
project = ET.parse(root / 'MDK-ARM/ODRIVE_V36_405_M0.uvprojx')
settings = project.find('.//Cads/VariousControls')
cmd = [str(bin_path / 'armcc.exe'), '--cpu=Cortex-M4.fp', '--c99', '-O3',
       '--split_sections', '-g']
for p in settings.findtext('IncludePath').split(';'):
    cmd += ['-I', str((root / 'MDK-ARM' / p.replace('\\', '/')).resolve())]
cmd += ['-D' + d for d in settings.findtext('Define').split(',')]
for source in [temp / 'model.c', root / 'validation/target_validation.c']:
    result = subprocess.run(cmd + ['-c', str(source), '-o', str(temp / (source.stem + '.o'))],
                            capture_output=True, text=True)
    assert result.returncode == 0 and 'warning' not in result.stderr.lower(), result.stderr
result = subprocess.run([str(bin_path / 'armlink.exe'), '--cpu=Cortex-M4.fp',
                         '--scatter', str(temp / 'model.sct'), '--entry=model_init',
                         '--keep=model.o(*)', '-o', str(temp / 'model.axf'),
                         str(temp / 'model.o'), str(temp / 'target_validation.o')],
                        cwd=temp, capture_output=True, text=True)
assert result.returncode == 0, result.stdout + result.stderr
with (temp / 'model.axf').open('rb') as stream:
    elf = ELFFile(stream)
    segments = [(s['p_vaddr'], s.data()) for s in elf.iter_segments() if s['p_type'] == 'PT_LOAD']
    # ARM scatter ELF 的单一 PT_LOAD 使用 load address；直接调用函数前补齐
    # __main normally scatter-load 的 RAM initializer，BSS 保持新映射的全零。
    initialized_sections = [(s['sh_addr'], s.data()) for s in elf.iter_sections()
                            if (s['sh_flags'] & 2) and s['sh_type'] == 'SHT_PROGBITS']
    symbols = {s.name: s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()}

offset_names = ['sequence', 'started', 'state', 'result', 'command_sequence',
                'fast_tick_count', 'invalid_current_count', 'current_interval_min_us',
                'current_interval_max_us', 'stage_attempts', 'stage_successes',
                'enable_attempts', 'enable_successes', 'bus_count', 'mailbox_size',
                'current_statistics', 'statistics_request', 'statistics_count',
                'statistics_first_time', 'statistics_last_time', 'statistics_b', 'statistics_c']

class Model:
    def __init__(self, pre_submit=False, startup_failure=None):
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
        for base, size in [(0x08000000, 0x100000), (0x20000000, 0x20000),
                           (0x40010000, 0x1000), (0xE0001000, 0x1000),
                           (0xE000E000, 0x1000)]:
            self.cpu.mem_map(base, size)
        for address, data in segments:
            self.cpu.mem_write(address, data)
        for address, data in initialized_sections:
            self.cpu.mem_write(address, data)
        self.cpu.mem_write(0xE000ED88, struct.pack('<I', 0x00F00000))
        data = self.cpu.mem_read(symbols['model_offsets'], len(offset_names) * 4)
        self.offsets = dict(zip(offset_names, struct.unpack('<' + 'I' * len(offset_names), data)))
        self.sequence = 0
        if startup_failure:
            self.write(startup_failure, 0)
        if pre_submit:
            self.submit(4 if pre_submit == 'capture' else 3, key=0x4050A11E)
        self.call('model_init')

    def call(self, name):
        self.cpu.reg_write(UC_ARM_REG_SP, 0x2001FFF0)
        self.cpu.reg_write(UC_ARM_REG_LR, 0x080FF001)
        self.cpu.emu_start(symbols[name] | 1, 0x080FF000, count=1000000)
        assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x080FF000, (name, hex(self.cpu.reg_read(UC_ARM_REG_PC)))

    def word(self, name):
        return struct.unpack('<I', self.cpu.mem_read(symbols[name], 4))[0]

    def write(self, name, value, fmt='I'):
        self.cpu.mem_write(symbols[name], struct.pack('<' + fmt, value))

    def status(self, name):
        return struct.unpack('<I', self.cpu.mem_read(
            symbols['g_target_validation'] + self.offsets[name], 4))[0]

    def submit(self, command, key=0, duration=2000, duty=0.5):
        self.sequence += 1
        self.cpu.mem_write(symbols['g_target_validation_request'],
                           struct.pack('<IIIIf', self.sequence, command, key, duration, duty))

    def statistics(self):
        address = symbols['g_target_validation'] + self.offsets['current_statistics']
        result = {}
        for name, fmt in [('request', 'I'), ('count', 'I'),
                          ('first_time', 'Q'), ('last_time', 'Q'),
                          ('b', 'ffff'), ('c', 'ffff')]:
            values = struct.unpack('<' + fmt, self.cpu.mem_read(
                address + self.offsets['statistics_' + name], struct.calcsize('<' + fmt)))
            result[name] = values[0] if len(values) == 1 else values
        return result

    def tick(self, count=1):
        for _ in range(count):
            self.call('model_tick')

    def moe(self):
        return struct.unpack('<I', self.cpu.mem_read(0x40010044, 4))[0] & (1 << 15)

def powered():
    m = Model()
    m.tick(200)  # 10 ms，避免 unsigned reference-time underflow 干扰 stale 用例。
    m.submit(3, key=0x4050A11E)
    m.tick()
    assert not m.moe() and m.word('model_enable_calls') == 0
    assert m.word('model_stage_calls') == 1
    m.tick()
    assert m.moe() and m.status('enable_successes') == 1
    return m

cases = []
def passed(name):
    cases.append(name)

m = Model(pre_submit=True)
m.tick(200)
assert not m.moe() and m.word('model_enable_calls') == 0
assert m.status('started') == 1 and m.status('sequence') % 2 == 0
assert m.status('current_interval_min_us') == m.status('current_interval_max_us') == 50, {
    k: m.status(k) for k in offset_names if k != 'mailbox_size'}
passed('reset clears pending power request; observation stays disabled/coherent')

m.submit(2)
m.tick(4)
assert m.status('stage_successes') == 2 and m.word('model_enable_calls') == 0
assert m.status('state') == 0 and m.status('result') == 1 and not m.moe()
passed('stage-only crosses callbacks without enabling power')

for duty in [-1.0, 0.099, 0.901, 1.0, float('nan'), float('inf')]:
    m = Model(); m.submit(3, key=0x4050A11E, duty=duty); m.tick(3)
    assert m.status('result') == 4 and not m.moe() and m.word('model_enable_calls') == 0
    passed(f'reject duty {duty}')
for duration in [0, 999, 10001, 0xFFFFFFFF]:
    m = Model(); m.submit(3, key=0x4050A11E, duration=duration); m.tick(3)
    assert m.status('result') == 4 and not m.moe()
    passed(f'reject duration {duration}')
for key in [0, 1, 0x4050A11F]:
    m = Model(); m.submit(3, key=key); m.tick(3)
    assert m.status('result') == 3 and not m.moe()
    passed('reject absent/wrong power key')
for voltage in [0.0, 9.99, 14.01, float('nan'), float('inf')]:
    m = Model(); m.write('model_bus_voltage', voltage, 'f')
    m.submit(3, key=0x4050A11E); m.tick(3)
    assert m.status('result') == 7 and not m.moe()
    passed(f'reject bus voltage {voltage}')
for fault in ['model_current_valid', 'model_current_mask', 'model_bus_valid']:
    m = Model(); m.write(fault, 0); m.submit(3, key=0x4050A11E); m.tick(3)
    assert m.status('result') in [6, 7] and not m.moe()
    passed('reject incomplete/invalid observation ' + fault)
m = Model(); m.tick(200); m.write('model_bus_age_us', 5001)
m.submit(3, key=0x4050A11E); m.tick(3)
assert m.status('result') == 7 and not m.moe()
passed('reject old bus sample')

for duty in [0.1, 0.5, 0.9]:
    m = Model(); m.submit(3, key=0x4050A11E, duty=duty); m.tick(2)
    assert m.moe() and m.word('model_common_duty_failures') == 0
    passed(f'accept boundary/common duty {duty}')
m = powered()
m.write('model_time', 13000, 'Q'); m.call('model_update')
assert not m.moe() and m.word('g_target_validation_stop_reason') == 1
assert struct.unpack('<I', m.cpu.mem_read(0x40010000, 4))[0] & 1
m.tick(50)
assert m.status('state') == 0 and m.word('model_enable_calls') == 1
passed('deadline ends burst with ADC absent; timing continues; no repeat')

for fault, reason in [('model_current_valid', 6), ('model_bus_valid', 7)]:
    m = powered(); m.write(fault, 0); m.tick()
    assert not m.moe() and m.status('result') == reason
    m.write(fault, 1); m.tick(20)
    assert m.word('model_enable_calls') == 1 and not m.moe()
    passed('fault stops and recovery does not auto-enable ' + fault)
m = powered()
m.submit(1); m.tick()
assert not m.moe() and m.status('result') == 2
m.submit(3); m.tick(3)
assert m.status('result') == 3 and m.word('model_enable_calls') == 1
passed('operator stop; consumed key cannot authorize another sequence')
for fault, reason in [('model_stage_ok', 8), ('model_enable_ok', 9)]:
    m = Model(); m.write(fault, 0); m.submit(3, key=0x4050A11E); m.tick(3)
    assert not m.moe() and m.status('result') == reason
    passed('rejected HCC action stops ' + fault)
m = powered()
m.cpu.mem_write(0x40010044, struct.pack('<I', 0)); m.tick()
assert m.status('result') == 10 and m.word('model_enable_calls') == 1
passed('Platform safe-off cannot be undone within burst')
m = powered()
m.write('model_time', 13000, 'Q'); m.tick()
assert not m.moe() and m.status('result') == 1
passed('ADC callback also enforces burst deadline')
m = Model()
m.cpu.mem_write(0x40010044, struct.pack('<I', 1 << 15)); m.tick()
assert not m.moe() and m.status('result') == 10
passed('observation mode rejects unexpected MOE')
m = Model(); m.write('model_bus_valid', 0); m.submit(2); m.tick(4)
assert m.status('result') == 1 and m.word('model_enable_calls') == 0
passed('stage-only can run without a powered bus')
m = powered(); previous_stages = m.word('model_stage_calls')
m.submit(3, key=0x4050A11E, duty=0.1); m.tick()
assert m.status('result') == 5 and m.word('model_enable_calls') == 1
assert m.word('model_stage_calls') == previous_stages + 1
m.write('model_time', 13000, 'Q'); m.call('model_update'); m.tick(50)
assert not m.moe() and m.word('model_enable_calls') == 1
passed('busy request cannot replace/restart current burst')
m = Model(); m.tick(200); m.write('model_bus_age_us', 5000)
m.submit(3, key=0x4050A11E); m.tick(2)
assert m.moe()
passed('accept bus age boundary')
m = Model(); m.submit(2); m.tick(4); previous = m.word('model_stage_calls')
m.tick(100)
assert m.word('model_stage_calls') == previous and not m.moe()
passed('same mailbox sequence is not replayed')
for fault in ['model_facts_ok', 'model_bind_ok', 'model_arm_ok', 'model_drive_start_ok']:
    m = Model(startup_failure=fault)
    assert not m.moe() and m.status('started') == 0
    assert m.word('model_enable_calls') == 0
    passed('startup failure preserves disabled power ' + fault)

m = Model(pre_submit='capture'); m.tick(10)
assert m.statistics()['count'] == 0 and m.status('state') == 0
passed('reset clears pending passive capture request')

m = Model(); m.write('model_current_b_a', -1.5, 'f'); m.write('model_current_c_a', 0.75, 'f')
m.submit(4, duration=0, duty=float('nan')); m.tick(4096)
statistics = m.statistics()
assert statistics['count'] == 4096 and statistics['request'] == m.sequence
assert statistics['last_time'] - statistics['first_time'] == 4095 * 50
assert statistics['b'] == (-1.5, -1.5, -1.5, 0.0)
assert statistics['c'] == (0.75, 0.75, 0.75, 0.0)
assert m.status('result') == 1 and m.status('state') == 0 and not m.moe()
assert m.word('model_enable_calls') == m.word('model_stage_calls') == 0
m.tick(100)
assert m.statistics() == statistics
passed('capture exact contiguous window; independent offsets; frozen result; no power/staging')

m = Model(); m.submit(4)
for index in range(4096):
    sign = 1 if index % 2 else -1
    m.write('model_current_b_a', -1.5 + sign * 0.25, 'f')
    m.write('model_current_c_a', 0.75 + sign * 0.5, 'f')
    m.tick()
statistics = m.statistics()
assert abs(statistics['b'][0] + 1.5) < 1e-5 and abs(statistics['c'][0] - 0.75) < 1e-5
assert statistics['b'][1:3] == (-1.75, -1.25) and statistics['c'][1:3] == (0.25, 1.25)
assert abs(statistics['b'][3] - 0.25**2 * 4096 / 4095) < 1e-5
assert abs(statistics['c'][3] - 0.5**2 * 4096 / 4095) < 1e-5
passed('Welford statistics match known noise variance with independent nonzero offsets')

for name, value, fmt in [('model_current_valid', 0, 'I'), ('model_current_mask', 4, 'I'),
                         ('model_current_b_a', float('nan'), 'f'),
                         ('model_current_c_a', float('inf'), 'f'),
                         ('model_tick_step_us', 100, 'I')]:
    m = Model(); m.submit(4); m.tick(10); m.write(name, value, fmt); m.tick()
    assert m.status('result') == 6 and m.status('state') == 0
    assert m.statistics()['count'] == 10 and not m.moe()
    assert m.word('model_enable_calls') == m.word('model_stage_calls') == 0
    passed('capture fails closed on invalid input/cadence ' + name)

m = Model(); m.submit(4); m.tick(10); m.submit(1); m.tick(10)
assert m.status('result') == 2 and m.status('state') == 0 and m.statistics()['count'] == 10
assert not m.moe() and m.word('model_enable_calls') == 0
passed('operator stop cancels capture without automatic restart')

m = Model(); m.submit(4); m.tick(10); capture_sequence = m.sequence
for command in [2, 3, 4]:
    m.submit(command, key=0x4050A11E); m.tick()
    assert m.status('result') == 5 and m.status('state') == 3
assert m.statistics()['request'] == capture_sequence
assert m.word('model_enable_calls') == m.word('model_stage_calls') == 0 and not m.moe()
passed('capture cannot be replaced by stage, power burst or another capture')

m = powered(); m.submit(4); m.tick()
assert m.status('result') == 5 and m.status('state') == 2
assert m.statistics()['count'] == 0 and m.word('model_enable_calls') == 1
passed('capture request cannot interrupt a powered transaction')

m = Model(); m.submit(4); m.tick(10)
m.cpu.mem_write(0x40010044, struct.pack('<I', 1 << 15)); m.tick()
assert m.status('result') == 10 and m.statistics()['count'] == 10 and not m.moe()
passed('unexpected MOE cancels passive capture and requests safe-off')

m = Model(); m.submit(4); m.tick(10)
m.write('model_time', 220000, 'Q'); m.call('model_update')
assert m.word('g_target_validation_stop_reason') == 6 and not m.moe()
m.tick()
assert m.status('result') == 6 and m.status('state') == 0 and m.statistics()['count'] == 10
passed('independent update deadline terminates capture with ADC absent')

m = Model(); m.submit(4); m.tick(10); m.submit(1); m.tick()
m.write('model_current_b_a', -0.5, 'f'); m.submit(4); m.tick(4096)
assert m.statistics()['count'] == 4096 and m.statistics()['request'] == m.sequence
assert m.statistics()['b'] == (-0.5, -0.5, -0.5, 0.0) and m.status('result') == 1
passed('new capture starts a fresh window after cancelled partial statistics')

(temp / 'model-results.txt').write_text('\n'.join(cases), encoding='utf-8')
print(f'PASS: {len(cases)} ARM-executed validation behaviors with fake HCC/time.')
print('Does not emulate ADC/TIM/SPI electrical behavior or prove hardware L1.')
