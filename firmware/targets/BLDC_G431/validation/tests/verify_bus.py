"""执行实际母线DMA采集ARM源码；HAL起动及DMA事件替身，不连接实物。"""
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
DMA=0x40020008
cases=[]

ioc=dict(line.split('=',1) for line in (ROOT/'BLDC_control V1.ioc').read_text(encoding='utf-8').splitlines() if '=' in line)
for key,value in {'Instance':'DMA1_Channel1','Mode':'DMA_NORMAL','MemInc':'DMA_MINC_ENABLE',
    'PeriphDataAlignment':'DMA_PDATAALIGN_HALFWORD','MemDataAlignment':'DMA_MDATAALIGN_HALFWORD'}.items():
    assert ioc['Dma.ADC1.0.'+key]==value,key
assert ioc['ADC1.NbrOfConversion']=='3' and ioc['ADC1.EOCSelection']=='ADC_EOC_SEQ_CONV'
assert ioc['NVIC.DMA1_Channel1_IRQn']==ioc['NVIC.ADC1_2_IRQn'] and ioc['NVIC.PriorityGroup']=='NVIC_PRIORITYGROUP_4'
for target in ET.parse(ROOT/'MDK-ARM/BLDC_G431_Validation.uvprojx').findall('.//Target'):
    assert len([f for f in target.findall('.//File') if f.findtext('FileName')=='dma.c'])==1

with tempfile.TemporaryDirectory(prefix='g431-bus-model-') as directory:
    temp=Path(directory);scatter=temp/'bus.sct'
    scatter.write_text('LR_IROM1 0x08000000 0x20000 { ER_IROM1 0x08000000 0x20000 { .ANY (+RO) } RW_IRAM1 0x20000000 0x8000 { .ANY (+RW +ZI) } }',encoding='utf-8')
    settings=ET.parse(ROOT/'MDK-ARM/BLDC_G431_Validation.uvprojx').find('.//Cads/VariousControls')
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
            for address,size in [(0x08000000,0x20000),(0x20000000,0x8000),(ADC,0x1000),(0x40020000,0x1000),(0x1fff7000,0x1000),(0xe000e000,0x1000)]:self.cpu.mem_map(address,size)
            for address,data in sections:self.cpu.mem_write(address,data)
            self.put(0xe000ed88,0xf00000);self.cpu.mem_write(0x1fff75aa,struct.pack('<H',1500))
            for irq in [11,18]:self.cpu.mem_write(0xe000e400+irq,b'\x10')

        def put(self,address,value):self.cpu.mem_write(address,struct.pack('<I',value))
        def write(self,name,value):self.put(symbols[name],value)
        def word(self,name):return struct.unpack('<I',self.cpu.mem_read(symbols[name],4))[0]
        def call(self,name):
            self.cpu.reg_write(UC_ARM_REG_SP,0x20007ff0);self.cpu.reg_write(UC_ARM_REG_LR,0x0801f001)
            self.cpu.emu_start(symbols[name]|1,0x0801f000,count=100000)
            assert self.cpu.reg_read(UC_ARM_REG_PC)==0x0801f000,name
        def start(self):self.call('bus_model_arm');self.call('bus_model_start')
        def transfer(self,raw):self.write('model_raw',raw);self.call('bus_model_transfer')
        def read(self):
            self.call('bus_model_read')
            return struct.unpack('<f4xQ?7x',self.cpu.mem_read(symbols['model_bus'],24)),struct.unpack('<Q10I',self.cpu.mem_read(symbols['model_diagnostic'],48))

    m=Model();m.call('bus_model_read');assert m.word('model_available')==0
    m.start();assert m.word('model_start_calls')==1 and m.word('model_arm_result')==1
    assert m.word('model_length')==3 and m.word('model_buffer_address')%4==0
    assert not struct.unpack('<I',m.cpu.mem_read(DMA,4))[0]&4
    m.call('bus_model_start');assert m.word('model_start_calls')==1
    cases.append('aligned length-three DMA starts once and HT interrupt is disabled')

    m.transfer(1400);m.read();assert not m.word('model_available')
    m.transfer(1500);m.read();assert not m.word('model_available')
    m.transfer(900);bus,diag=m.read()
    expected=1400*3.0/4095*(52.1/5.1)
    assert abs(bus[0]-expected)<1e-5 and bus[1:]==(20,1),(bus,diag)
    assert diag[:7]==(20,0,0,3,1400,1500,0),diag
    m.cpu.reg_write(UC_ARM_REG_PRIMASK,1);m.read();assert m.cpu.reg_read(UC_ARM_REG_PRIMASK)==1
    assert m.word('model_getvalue_calls')==0
    cases.append('only full TC publishes coherent three-halfword data, without reading DR; PRIMASK preserved')

    for status in [1,2,3]:
        m=Model();m.write('model_start_status',status);m.start();bus,diag=m.read()
        assert bus[2]==0 and diag[1:4]==(1,status,0),diag
        m.call('bus_model_start');assert m.word('model_start_calls')==1
        cases.append('start failure status retained: '+str(status))

    for status in [0,4,8,12]:
        m=Model();m.start();m.transfer(1453);m.transfer(1498);m.put(ADC,status)
        m.read();assert not m.word('model_available')
        m.transfer(900);bus,diag=m.read()
        assert bus[2] and diag[1]==0 and diag[3:6]==(3,1453,1498) and m.word('model_getvalue_calls')==0,diag
        cases.append('second-rank EOC/EOS interleaving cannot publish early or invalidate DMA frame: '+str(status))

    for count in [1,2,4]:
        m=Model();m.start();m.put(DMA+4,count);m.call('bus_model_complete');bus,diag=m.read()
        assert not bus[2] and diag[1]==5 and diag[3]==(3-count if count<=3 else 0),diag
        m.call('bus_model_start');assert m.word('model_start_calls')==1
        cases.append('incomplete or corrupt DMA count fails closed without retry: '+str(count))

    m=Model();m.start();m.transfer(1400);m.transfer(0);m.transfer(900);bus,diag=m.read()
    assert bus[2]==0 and diag[1]==3 and diag[5]==0
    cases.append('zero VREFINT has explicit conversion failure cause')
    m=Model();m.start();m.transfer(1400);m.put(ADC,16);m.call('bus_model_error');bus,diag=m.read()
    assert bus[2]==0 and diag[1]==4 and diag[3]==1 and diag[6]==2 and diag[8]==16,diag
    m.call('bus_model_start');assert m.word('model_start_calls')==1
    cases.append('ADC overrun error/ISR and partial count retained, no acquisition retry')

    m=Model();m.start();m.transfer(1400);m.write('model_error_code',4);m.call('bus_model_error');bus,diag=m.read()
    assert not bus[2] and diag[1]==6 and diag[6]==4
    cases.append('DMA transfer error is distinguished from ADC error')
    for index in range(3):
        m=Model();m.start()
        for i,raw in enumerate([1400,1500,900]):m.transfer(65535 if i==index else raw)
        bus,diag=m.read();assert not bus[2] and diag[1]==5,diag
        cases.append('out-of-range DMA halfword rejected: '+str(index))
    m=Model();m.start();m.transfer(1400);m.transfer(1500);m.transfer(900)
    m.call('bus_model_start');assert m.word('model_start_calls')==2
    m.transfer(1300);m.transfer(1500);m.transfer(900);bus,diag=m.read()
    assert bus[2] and diag[4]==1300 and m.word('model_getvalue_calls')==0
    cases.append('next normal-mode acquisition reuses buffer only after completed publication')
    for bad in range(1,7):
        m=Model();m.write('model_bad_config',bad);m.start()
        assert not m.word('model_arm_result') and not m.word('model_start_calls')
        cases.append('incompatible DMA configuration rejected before start: '+str(bad))
    m=Model();m.cpu.mem_write(0xe000e400+11,b'\x20');m.start()
    assert not m.word('model_arm_result') and not m.word('model_start_calls')
    cases.append('mismatched ADC/DMA writer priority rejected')

print('PASS:',len(cases),'ARM-executed bus acquisition cases')
for case in cases:print('PASS',case)
print('Explicit register/HAL events only; no ADC IRQ timing or electrical accuracy proof.')
