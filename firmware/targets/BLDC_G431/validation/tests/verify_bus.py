"""执行实际母线采集ARM源码；HAL起动/DR及寄存器事件替身，不连接实物。"""
from pathlib import Path
import os
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_PRIMASK

ROOT=Path(__file__).resolve().parents[2]
COMPILER=Path(os.environ.get('KEIL_ARMCC_BIN',r'D:\keil5\ARM\ARMCC\bin'))
ADC=0x50000000
cases=[]

with tempfile.TemporaryDirectory(prefix='g431-bus-model-') as directory:
    temp=Path(directory);scatter=temp/'bus.sct'
    scatter.write_text('LR_IROM1 0x08000000 0x20000 { ER_IROM1 0x08000000 0x20000 { .ANY (+RO) } RW_IRAM1 0x20000000 0x8000 { .ANY (+RW +ZI) } }',encoding='utf-8')
    settings=ET.parse(ROOT/'MDK-ARM/BLDC_control V1.uvprojx').find('.//Cads/VariousControls')
    command=[str(COMPILER/'armcc.exe'),'--cpu=Cortex-M4.fp','--c99','-O3','--split_sections','-g','-I',str(ROOT/'platform')]
    for include in settings.findtext('IncludePath').split(';'):
        command+=['-I',str((ROOT/'MDK-ARM'/include.replace('\\','/')).resolve())]
    command+=['-D'+item for item in settings.findtext('Define').split(',')]
    objects=[]
    for source in [ROOT/'validation/tests/bus_model.c',ROOT/'platform/bus_observation.c']:
        obj=temp/(source.stem+'.o');p=subprocess.run(command+['-c',str(source),'-o',str(obj)],capture_output=True,text=True)
        assert p.returncode==0 and 'warning' not in p.stderr.lower(),p.stderr
        objects.append(str(obj))
    p=subprocess.run([str(COMPILER/'armlink.exe'),'--cpu=Cortex-M4.fp','--scatter',str(scatter),
        '--entry=bus_model_start','--keep=bus_model.o(*)','--keep=bus_observation.o(*)','-o',str(temp/'bus.axf'),*objects],capture_output=True,text=True)
    assert p.returncode==0,p.stdout+p.stderr
    with (temp/'bus.axf').open('rb') as stream:
        elf=ELFFile(stream)
        sections=[(s['sh_addr'],s.data()) for s in elf.iter_sections() if s['sh_flags']&2 and s['sh_type']=='SHT_PROGBITS']
        symbols={s.name:s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()}

    class Model:
        def __init__(self):
            self.cpu=Uc(UC_ARCH_ARM,UC_MODE_THUMB|UC_MODE_MCLASS);self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            for address,size in [(0x08000000,0x20000),(0x20000000,0x8000),(ADC,0x1000),(0x1fff7000,0x1000),(0xe000e000,0x1000)]:self.cpu.mem_map(address,size)
            for address,data in sections:self.cpu.mem_write(address,data)
            self.put(0xe000ed88,0xf00000);self.cpu.mem_write(0x1fff75aa,struct.pack('<H',1500))

        def put(self,address,value):self.cpu.mem_write(address,struct.pack('<I',value))
        def write(self,name,value):self.put(symbols[name],value)
        def word(self,name):return struct.unpack('<I',self.cpu.mem_read(symbols[name],4))[0]
        def call(self,name):
            self.cpu.reg_write(UC_ARM_REG_SP,0x20007ff0);self.cpu.reg_write(UC_ARM_REG_LR,0x0801f001)
            self.cpu.emu_start(symbols[name]|1,0x0801f000,count=100000)
            assert self.cpu.reg_read(UC_ARM_REG_PC)==0x0801f000,name
        def start(self):self.call('bus_observation_arm');self.call('bus_model_start')
        def rank(self,raw,eos=False):
            self.write('model_raw',raw);self.put(ADC,8 if eos else 4);self.call('bus_model_rank')
        def read(self):
            self.call('bus_model_read')
            return struct.unpack('<f4xQ?7x',self.cpu.mem_read(symbols['model_bus'],24)),struct.unpack('<Q10I',self.cpu.mem_read(symbols['model_diagnostic'],48))

    m=Model();m.call('bus_model_read');assert m.word('model_available')==0
    m.start();assert m.word('model_start_calls')==1
    m.call('bus_model_start');assert m.word('model_start_calls')==1
    cases.append('unpublished state is unavailable; active sequence is not started twice')

    m.rank(1400);m.rank(1500);m.rank(900,True);bus,diag=m.read()
    expected=1400*3.0/4095*(52.1/5.1)
    assert abs(bus[0]-expected)<1e-5 and bus[1:]==(20,1),(bus,diag)
    assert diag[:7]==(20,0,0,3,1400,1500,0),diag
    m.cpu.reg_write(UC_ARM_REG_PRIMASK,1);m.read();assert m.cpu.reg_read(UC_ARM_REG_PRIMASK)==1
    cases.append('valid three-rank conversion has coherent raw data/time and preserves PRIMASK')

    for status in [1,2,3]:
        m=Model();m.write('model_start_status',status);m.start();bus,diag=m.read()
        assert bus[2]==0 and diag[1:4]==(1,status,0),diag
        cases.append('start failure status retained: '+str(status))
    for raws in [[1000],[1000,1500],[1000,1500,900,900]]:
        m=Model();m.start()
        for i,raw in enumerate(raws):m.rank(raw,i==len(raws)-1)
        bus,diag=m.read();assert bus[2]==0 and diag[1]==2 and diag[3]==len(raws),diag
        cases.append('EOS rank mismatch retained: '+str(len(raws)))
    m=Model();m.start();m.rank(1400);m.rank(0);m.rank(900,True);bus,diag=m.read()
    assert bus[2]==0 and diag[1]==3 and diag[5]==0
    cases.append('zero VREFINT has explicit conversion failure cause')
    m=Model();m.start();m.rank(1400);m.put(ADC,16);m.call('bus_model_error');bus,diag=m.read()
    assert bus[2]==0 and diag[1]==4 and diag[3]==1 and diag[6]==2 and diag[8]==16,diag
    cases.append('ADC overrun error/ISR and partial rank retained')
    m.call('bus_model_start');m.rank(1300);m.rank(1500);m.rank(900,True);bus,diag=m.read()
    assert bus[2]==1 and diag[1]==0 and diag[4]==1300 and diag[6]==2
    cases.append('new publication replaces diagnostic with matching sequence, stale HAL error is not a new failure')

    m=Model();m.start();m.rank(1453);m.write('model_eos_after_read',1);m.rank(1498)
    bus,diag=m.read()
    assert bus[2]==0 and diag[1:7]==(2,0,2,1453,1498,0) and diag[8]&12==12,diag
    assert m.word('model_raw')==900
    cases.append('EOS arriving after second DR read reproduces rank-2 rejection with good data and no overrun')

print('PASS:',len(cases),'ARM-executed bus acquisition cases')
for case in cases:print('PASS',case)
print('Explicit register/HAL events only; no ADC IRQ timing or electrical accuracy proof.')
