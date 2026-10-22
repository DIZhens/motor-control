"""真实ARMCC、Kernel/Port及任务代码；替代异常切换边界，不声称模拟实物调度。"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_INVALID
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_BASEPRI, UC_ARM_REG_PRIMASK

TARGET = Path(__file__).resolve().parents[2]
ROOT = TARGET.parents[2]
TESTS = Path(__file__).resolve().parent
KERNEL = ROOT / 'firmware/third_party/FreeRTOS/Source'
APP = ROOT / 'firmware/app/system_runtime'
COMPILER = Path(os.environ.get('KEIL_ARMCC_BIN', r'D:\keil5\ARM\ARMCC\bin'))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--report', type=Path)
args = parser.parse_args()
groups = []


def passed(name):
    groups.append(name)
    print('PASS:', name)


def run(command, cwd):
    result = subprocess.run([str(a) for a in command], cwd=cwd, capture_output=True)
    output = (result.stdout + result.stderr).decode(errors='replace')
    assert result.returncode == 0 and 'warning' not in output.lower(), output


manifest = json.loads((KERNEL.parent / 'UPSTREAM.json').read_text())
for rel, expected in manifest['source_sha256'].items():
    assert hashlib.sha256((KERNEL / rel).read_bytes()).hexdigest() == expected, rel
passed('供应Kernel和RVDS/ARM_CM4F逐文件哈希保持原样')

project = ET.parse(TARGET / 'MDK-ARM/BLDC_control V1.uvprojx')
run_members = {x.text for x in project.findall('.//FileName')}
assert {'tasks.c', 'list.c', 'queue.c', 'port.c', 'system_runtime.c',
        'target_system_runtime.c', 'stm32g4xx_hal_timebase_tim.c'} <= run_members
assert not {'heap_1.c', 'heap_4.c', 'cmsis_os.c', 'cmsis_os2.c', 'freertos.c',
            'target_vofa.c', 'target_uart.c', 'vofa.c', 'motor_vofa.c'} & run_members
validation = ET.parse(TARGET / 'MDK-ARM/BLDC_G431_Validation.uvprojx')
for target in validation.findall('./Targets/Target'):
    assert not {'tasks.c', 'queue.c', 'list.c', 'port.c', 'system_runtime.c',
                'stm32g4xx_hal_timebase_tim.c'} & {x.text for x in target.findall('.//FileName')}
passed('Run独占原生静态RTOS；Validation不链接Kernel、Port或TIM7 HAL实现')

sources = [TESTS / 'system_runtime_model.c', APP / 'system_runtime.c',
           *[KERNEL / s for s in ['tasks.c', 'queue.c', 'list.c', 'portable/RVDS/ARM_CM4F/port.c']]]
with tempfile.TemporaryDirectory(prefix='g431-system-runtime-') as directory:
    temp = Path(directory)
    scatter = temp / 'model.sct'
    scatter.write_text('LR 0x08000000 0x40000 { ER 0x08000000 0x40000 { .ANY (+RO) } '
                       'RW 0x20000000 0x8000 { .ANY (+RW +ZI) } }')
    command = [COMPILER / 'armcc.exe', '--cpu=Cortex-M4.fp', '--c99', '-O2',
               '--split_sections', '-g']
    for include in [APP, TARGET / 'runtime', KERNEL / 'include',
                    KERNEL / 'portable/RVDS/ARM_CM4F', ROOT / 'firmware/app/motor']:
        command += ['-I', include]
    objects = []
    for source in sources:
        obj = temp / (source.stem + '.o')
        run(command + ['-c', source, '-o', obj], temp)
        objects.append(obj)
    axf = temp / 'model.axf'
    run([COMPILER / 'armlink.exe', '--cpu=Cortex-M4.fp', '--scatter', scatter,
         '--entry=model_start', '--keep=system_runtime.o(*)', '--keep=system_runtime_model.o(*)',
         '--keep=tasks.o(*)',
         '-o', axf, *objects], temp)
    with axf.open('rb') as stream:
        elf = ELFFile(stream)
        data = [(s['sh_addr'], s.data()) for s in elf.iter_sections()
                if s['sh_flags'] & 2 and s['sh_type'] == 'SHT_PROGBITS']
        symbols = {s.name: s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()}
    symbols['prvStartFirstTask'] = next(v for n, v in symbols.items() if n.endswith('__prvStartFirstTask'))

    class Model:
        def __init__(self):
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            for base, size in [(0x08000000, 0x40000), (0x20000000, 0x8000), (0xE000E000, 0x2000)]:
                self.cpu.mem_map(base, size)
            for address, contents in data:
                self.cpu.mem_write(address, contents)
            self.write(0xE000ED88, 0xF00000)
            self.write(0xE000ED0C, 0x300)
            # NVIC只实现高4位，Port启动会实际探测。
            self.cpu.hook_add(UC_HOOK_MEM_READ, self.priority_read, begin=0xE000E400, end=0xE000E400)
            self.mode = ''
            self.stop = ''
            self.events = []
            self.calls = {}
            self.delay_hal = True
            self.fail_create = ''
            self.return_scheduler = False
            self.cpu.hook_add(UC_HOOK_CODE, self.code)
            self.cpu.hook_add(UC_HOOK_MEM_INVALID, self.invalid)

        def invalid(self, cpu, access, address, size, value, user):
            pc = cpu.reg_read(UC_ARM_REG_PC)
            near = sorted(((v, n) for n, v in symbols.items() if v <= pc and v >= 0x08000000), reverse=True)[:3]
            raise AssertionError(('unmapped', hex(address), hex(pc), near, self.events))

        def read(self, address):
            if isinstance(address, str): address = symbols[address]
            return struct.unpack('<I', self.cpu.mem_read(address, 4))[0]

        def write(self, address, value):
            if isinstance(address, str): address = symbols[address]
            self.cpu.mem_write(address, struct.pack('<I', value))

        def priority_read(self, cpu, access, address, size, value, user):
            cpu.mem_write(address, bytes([cpu.mem_read(address, 1)[0] & 0xF0]))

        def skip(self, result=0):
            self.cpu.reg_write(UC_ARM_REG_R0, result)
            self.cpu.reg_write(UC_ARM_REG_PC, self.cpu.reg_read(UC_ARM_REG_LR))

        def code(self, cpu, address, size, user):
            def at(name): return address == (symbols.get(name, 0) & ~1)
            if at('system_runtime_target_fatal'):
                self.stop = 'fatal'
                self.events.append(('fatal', cpu.reg_read(UC_ARM_REG_R0)))
                cpu.emu_stop()
            elif self.fail_create and at(self.fail_create):
                self.skip(0)
            elif self.return_scheduler and at('xPortStartScheduler'):
                self.skip(0)
            elif at('prvStartFirstTask'):
                self.stop = 'port-first-task'
                cpu.emu_stop()
            elif self.mode == 'bootstrap' and at('vTaskDelay'):
                delay = cpu.reg_read(UC_ARM_REG_R0)
                self.events.append(('delay', delay))
                self.write('xTickCount', self.read('xTickCount') + delay)
                if self.delay_hal: self.write('model_hal_tick', self.read('model_hal_tick') + delay)
                self.skip()
            elif self.mode == 'bootstrap' and at('vTaskDelete'):
                self.stop = 'delete-bootstrap'
                cpu.emu_stop()
            elif self.mode == 'communication' and at('ulTaskNotifyTake'):
                count = self.calls.get('notify', 0)
                self.calls['notify'] = count + 1
                self.events.append(('notify-wait', cpu.reg_read(UC_ARM_REG_R1)))
                if count: self.stop = 'communication-block'; cpu.emu_stop()
                else: self.skip()
            elif self.mode == 'telemetry' and at('vTaskDelayUntil'):
                self.events.append(('telemetry-period', cpu.reg_read(UC_ARM_REG_R1)))
                self.stop = 'telemetry-block'
                cpu.emu_stop()

        def call(self, name, *values):
            self.stop = ''
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0803F001)
            self.cpu.reg_write(UC_ARM_REG_PRIMASK, 0)
            self.cpu.reg_write(UC_ARM_REG_BASEPRI, 0)
            for reg, value in zip([UC_ARM_REG_R0, UC_ARM_REG_R1], values): self.cpu.reg_write(reg, value)
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0803F000, count=500000)
            except UcError:
                pc = self.cpu.reg_read(UC_ARM_REG_PC)
                print('PC', hex(pc), 'near', sorted(((v, n) for n, v in symbols.items()
                    if 0x08000000 <= v <= pc), reverse=True)[:4], 'events', self.events)
                raise
            assert self.stop or self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, name
            return self.cpu.reg_read(UC_ARM_REG_R0)

        def current(self, handle):
            self.write('pxCurrentTCB', handle)

        def start(self):
            self.call('model_start')
            assert self.stop == 'port-first-task', self.events
            assert self.call('uxTaskGetNumberOfTasks') == 2  # Bootstrap和静态Idle。

        def bootstrap(self):
            self.mode = 'bootstrap'
            self.call('bootstrap_task', 0)

    m = Model(); m.start()
    assert m.read(0xE000E014) == 169999
    assert m.read(0xE000ED20) >> 16 == 0xF0F0
    assert m.read('model_pre_count') == 1 and m.read('model_boot_count') == 0
    passed('真实Port启动探测4位NVIC并设置1kHz SysTick；装配在调度器边界之后')
    m.bootstrap()
    assert m.stop == 'delete-bootstrap' and m.read('model_boot_count') == 1
    assert m.read('g_system_runtime_status') == 3
    assert m.call('uxTaskGetNumberOfTasks') == 4
    assert m.read('s_mailbox') and m.read('s_communication') and m.read('s_telemetry')
    passed('Bootstrap创建真实静态Mailbox及长期任务后装配Target，记录高水位再请求自删除')
    for rate in [50, 250, 500]:
        m.write('model_rate_hz', rate)
        m.write('model_sample_sequence', rate)
        m.mode = 'telemetry'; m.current(m.read('s_telemetry')); m.call('telemetry_task', 0)
        assert m.stop == 'telemetry-block' and m.events[-1] == ('telemetry-period', 1000 // rate)
    # 未消费的50/250被500覆盖，真实Queue只保留最新单槽值。
    m.mode = 'communication'; m.current(m.read('s_communication')); m.call('communication_task', 0)
    assert m.stop == 'communication-block' and m.events[-1] == ('notify-wait', 1)
    assert m.read(symbols['g_system_runtime_status'] + 7 * 4) == 500
    passed('真实Queue覆盖保留最新样本；Communication最长等待1ms，Telemetry周期20/4/2ms')
    for available_name in ['model_sample_available', 'model_config_available', 'model_enabled']:
        m = Model(); m.start(); m.bootstrap(); m.write(available_name, 0)
        m.mode = 'telemetry'; m.current(m.read('s_telemetry')); m.call('telemetry_task', 0)
        assert m.stop == 'telemetry-block'
        assert m.read(symbols['g_system_runtime_status'] + 5 * 4) == 0
    passed('无一致样本、无配置或遥测关闭时不伪造样本且仍阻塞')
    for variable, value in [('model_pre_ok', 0), ('model_boot_ok', 0), ('model_rate_hz', 123)]:
        m = Model(); m.write(variable, value)
        if variable == 'model_pre_ok': m.call('model_start')
        else:
            m.start(); m.bootstrap()
            if variable == 'model_rate_hz':
                m.mode = 'telemetry'; m.current(m.read('s_telemetry')); m.call('telemetry_task', 0)
        assert m.stop == 'fatal' and m.read('g_system_runtime_status') == 4
    passed('启动前检查、Target装配或内部遥测配置失败均进入Fail-closed')
    for name in ['xTaskCreateStatic', 'xQueueGenericCreateStatic']:
        m = Model()
        if name == 'xTaskCreateStatic':
            m.fail_create = name; m.call('model_start')
        else:
            m.start(); m.fail_create = name; m.bootstrap()
        assert m.stop == 'fatal' and m.read('model_boot_count') == 0
    m = Model(); m.start(); m.fail_create = 'xTaskCreateStatic'; m.bootstrap()
    assert m.stop == 'fatal' and m.read('model_boot_count') == 0
    passed('Bootstrap、长期任务或Mailbox创建失败时，不进入Target装配')
    m = Model(); m.return_scheduler = True; m.call('model_start')
    assert m.stop == 'fatal' and m.events[-1] == ('fatal', 2)
    passed('调度器意外返回立即锁内部故障，不回落裸机主循环')
    m = Model(); m.start(); m.delay_hal = False; m.bootstrap()
    assert m.stop == 'fatal' and m.read('model_boot_count') == 0
    passed('Kernel tick推进但HAL tick停滞时，不装配Motor或开放后台')
    for entry, reason in [('system_runtime_assert_failed', 2), ('vApplicationStackOverflowHook', 3)]:
        m = Model(); m.call(entry, 0, 0)
        assert m.events[-1] == ('fatal', reason)
    passed('Assert与栈溢出不等待ADC或协议，直达独立关断合同')

if args.report:
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(dict(status='PASS', groups=groups, hardware_access=False,
        exception_context_switch='substituted at task/Port boundaries; not a scheduler proof',
        source_sha256={str(p.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in [Path(__file__), *sources, TARGET/'runtime/FreeRTOSConfig.h']}),
        ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
