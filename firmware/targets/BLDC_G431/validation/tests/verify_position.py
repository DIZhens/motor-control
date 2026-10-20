"""执行真实 SSI ARM backend，验证有限时钟、故障隔离和 publication 事务。"""
from pathlib import Path
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_SP, UC_ARM_REG_LR
from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_PRIMASK
from verify_platform import ROOT, COMPILER

SPI = 0x40013000
CR1, CR2, SR, DR = SPI, SPI + 4, SPI + 8, SPI + 12
RSTR, GPIO_BSRR = 0x40021040, 0x48000018
SPE, TXE, RXNE, BSY, OVR = 64, 2, 1, 128, 64
IRQ_BITS = 32 | 64 | 128
STOP = 0x0801F000


def compile_image(directory):
    scatter = directory / "position.sct"
    scatter.write_text("LR_IROM1 0x08000000 0x20000 { ER_IROM1 0x08000000 0x20000 { .ANY (+RO) } RW_IRAM1 0x20000000 0x8000 { .ANY (+RW +ZI) } }", encoding="utf-8")
    settings = ET.parse(ROOT / "MDK-ARM/BLDC_control V1.uvprojx").find(".//Cads/VariousControls")
    command = [str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3", "--split_sections", "-g", "-I", str(ROOT / "platform")]
    for include in settings.findtext("IncludePath").split(";"):
        command += ["-I", str((ROOT / "MDK-ARM" / include.replace(chr(92), "/")).resolve())]
    command += ["-D" + item for item in settings.findtext("Define").split(",")]
    objects = []
    for source in [ROOT / "validation/tests/position_model.c", ROOT / "platform/position_observation.c"]:
        obj = directory / (source.stem + ".o")
        result = subprocess.run(command + ["-c", str(source), "-o", str(obj)], capture_output=True, text=True)
        assert result.returncode == 0 and "warning" not in result.stderr.lower(), result.stdout + result.stderr
        objects.append(str(obj))
    result = subprocess.run([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter), "--entry=position_model_init", "--keep=position_model.o(*)", "-o", str(directory / "position.axf"), *objects], cwd=directory, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    with (directory / "position.axf").open("rb") as stream:
        elf = ELFFile(stream)
        contents = [(s["p_vaddr"], s.data()) for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
        contents += [(s["sh_addr"], s.data()) for s in elf.iter_sections() if s["sh_flags"] & 2 and s["sh_type"] == "SHT_PROGBITS"]
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
    return contents, symbols


def crc6(data):
    value = 0
    for bit in range(17, -1, -1):
        feedback = (data >> bit & 1) ^ (value >> 5 & 1)
        value = value << 1 & 63
        if feedback: value ^= 3
    return value


def packet(angle=10156, status=0, bad_crc=False):
    frame = angle << 10 | status << 6
    frame |= crc6(frame >> 6) ^ int(bad_crc)
    return list(struct.pack(">I", 0x80000000 | frame << 7))


class Model:
    def __init__(self, image, auto_arm=True):
        contents, self.symbols = image
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
        for address, size in [(0x08000000, 0x20000), (0x20000000, 0x8000), (0x40013000, 0x1000), (0x40021000, 0x1000), (0x48000000, 0x1000), (0xE000E000, 0x1000)]:
            self.cpu.mem_map(address, size)
        for address, data in contents: self.cpu.mem_write(address, data)
        self.put(0xE000ED88, 0x00F00000)
        self.fifo = []
        self.tx = []
        self.response = packet()
        self.auto_response = False
        self.busy = False
        self.resets = 0
        self.spi_reads = 0
        self.pending = {}
        self.pending_tick = False
        self.interrupted = False
        self.in_tick = False
        self.on_instruction = None
        self.trace = []
        self.instructions = 0
        self.critical_max = 0
        self.critical_count = 0
        for start, end in [(SPI, SPI + 0x20), (RSTR, RSTR + 3), (GPIO_BSRR, GPIO_BSRR + 3)]:
            self.cpu.hook_add(UC_HOOK_MEM_WRITE, self.write_hook, begin=start, end=end)
        self.cpu.hook_add(UC_HOOK_MEM_READ, self.read_hook, begin=SPI, end=SPI + 0x20)
        self.cpu.hook_add(UC_HOOK_CODE, self.instruction)
        self.write("model_auto_arm", int(auto_arm))
        self.call("position_model_init")
        self.status()

    def put(self, address, value): self.cpu.mem_write(address, struct.pack("<I", value & 0xFFFFFFFF))
    def get(self, address): return struct.unpack("<I", self.cpu.mem_read(address, 4))[0]
    def word(self, symbol): return self.get(self.symbols[symbol])
    def write(self, symbol, value, fmt="I"): self.cpu.mem_write(self.symbols[symbol], struct.pack("<" + fmt, value))
    def status(self, errors=0): self.put(SR, TXE | (RXNE if self.fifo else 0) | (BSY if self.busy else 0) | errors)

    def write_hook(self, cpu, access, address, size, value, _):
        self.trace.append((address, value, cpu.reg_read(UC_ARM_REG_PRIMASK)))
        if address == DR:
            assert size == 1, "8-bit access must not pack an extra clocked frame"
            assert self.get(CR1) & SPE and not self.word("model_cs_high")
            self.tx.append(value)
            assert len(self.tx) <= 4, "fifth TX byte extends SSI clocks"
            if self.auto_response:
                self.fifo.append(self.response[len(self.tx) - 1])
                self.status()
        elif address == RSTR and value & 1 << 12:
            self.resets += 1
            for offset in range(0, 0x20, 4): self.put(SPI + offset, 0)
            self.fifo.clear(); self.busy = False
            self.status()

    def read_hook(self, cpu, access, address, size, value, _):
        if address == DR:
            assert size == 1 and self.fifo
            self.put(DR, self.fifo.pop(0)); self.spi_reads += 1; self.status()

    def instruction(self, cpu, address, size, _):
        self.instructions += 1
        if cpu.reg_read(UC_ARM_REG_PRIMASK):
            self.critical_count += 1
            self.critical_max = max(self.critical_max, self.critical_count)
        else: self.critical_count = 0
        if self.on_instruction and not self.in_tick: self.on_instruction(self, address)
        if self.pending_tick and not self.in_tick and not cpu.reg_read(UC_ARM_REG_PRIMASK):
            self.interrupted = True; cpu.emu_stop()

    def call(self, name, stack=0x20007FF0):
        self.cpu.reg_write(UC_ARM_REG_SP, stack); self.cpu.reg_write(UC_ARM_REG_LR, STOP | 1)
        self.cpu.reg_write(UC_ARM_REG_PC, self.symbols[name] | 1)
        for _ in range(8):
            self.interrupted = False
            self.cpu.emu_start(self.cpu.reg_read(UC_ARM_REG_PC) | 1, STOP, count=100000)
            if not self.interrupted:
                assert self.cpu.reg_read(UC_ARM_REG_PC) == STOP, name
                return
            context = self.cpu.context_save(); self.pending_tick = False; self.in_tick = True
            self.call("position_model_tick", stack=0x20007000)
            self.in_tick = False; self.cpu.context_restore(context)
        raise AssertionError("IRQ injection did not settle")

    def tick(self, count=1):
        for _ in range(count): self.call("position_model_tick")

    def begin(self):
        self.tick(10)
        assert not self.word("model_cs_high") and self.get(CR1) & SPE
        assert not self.tx
        return self

    def receive(self, data=None, busy=False):
        self.fifo += list(self.response if data is None else data)
        assert len(self.fifo) <= 4
        self.busy = busy; self.status()
        self.call("position_model_irq")

    def latest(self):
        self.call("position_model_latest")
        if not self.word("model_latest_result"): return None
        assert self.word("model_observation_size") == 24
        address = self.symbols["model_observation"]
        raw = bytes(self.cpu.mem_read(address, 24))
        return {"position": struct.unpack_from("<f", raw)[0], "time": struct.unpack_from("<Q", raw, 8)[0], "referenced": bool(raw[16]), "valid": bool(raw[17])}


def run(image):
    cases = []
    def passed(name): cases.append(name)
    m = Model(image)
    assert m.word("model_arm_result") and m.word("model_cs_high") and not m.get(CR1) & SPE
    assert not m.get(CR1) & 1024
    assert m.latest() is None
    m.tick(9); assert not m.get(CR1) & SPE and not m.tx
    m.tick(); assert m.get(CR1) & SPE and not m.word("model_cs_high") and not m.tx
    passed("1 kHz divider starts no clock until low-priority SPI IRQ writes DR")
    m.call("position_model_irq"); assert len(m.tx) == 4 and m.spi_reads == 0
    assert not m.get(CR2) & 128
    m.receive(); obs = m.latest()
    assert obs == {"position": 10156 / 16384, "time": 20, "referenced": True, "valid": True}, obs
    assert m.word("model_cs_high") and not m.get(CR1) & SPE and not m.get(CR2) & IRQ_BITS
    for _ in range(5): m.call("position_model_irq")
    assert len(m.tx) == 4 and m.latest() == obs
    passed("delayed ADC service leads to exactly 32 finite clocks and one coherent publication")

    for angle in [0, 1, 8192, 16383]:
        m = Model(image).begin(); m.response = packet(angle); m.auto_response = True
        m.call("position_model_irq"); m.call("position_model_irq")
        obs = m.latest(); assert obs["valid"] and obs["position"] == angle / 16384
        assert len(m.tx) == m.spi_reads == 4
        passed(f"interleaved TX/RX valid angle {angle}")
    for status, crc_bad, valid in [(0, True, False), (1, False, False), (2, False, False), (8, False, False), (4, False, True)]:
        m = Model(image).begin(); m.call("position_model_irq"); m.receive(packet(status=status, bad_crc=crc_bad))
        obs = m.latest(); assert obs["valid"] == valid and obs["referenced"] == valid
        m.tx.clear(); m.tick(10); assert m.get(CR1) & SPE
        m.call("position_model_irq"); m.receive(packet())
        assert m.latest()["valid"] and m.resets == 1
        passed(f"complete CRC/status-invalid frame does not latch transport error {status}/{crc_bad}")

    m = Model(image).begin(); m.call("position_model_irq"); m.receive(busy=True)
    assert m.latest() is None and not m.get(CR2) & IRQ_BITS and not m.word("model_cs_high")
    m.busy = False; m.status(); m.tick()
    assert m.latest()["valid"] and m.latest()["time"] == 20 and m.word("model_cs_high")
    passed("last RX with BSY set defers completion to next fast tick without polling")

    for mode in ["busy", "no-irq", "partial"]:
        m = Model(image).begin()
        if mode != "no-irq": m.call("position_model_irq")
        if mode == "busy": m.receive(busy=True)
        if mode == "partial": m.receive(packet()[:2])
        m.tick(); assert m.latest() is None
        before = m.instructions; m.tick()
        assert m.instructions - before < 2000
        obs = m.latest(); assert obs and not obs["valid"] and obs["time"] == 20
        assert m.word("model_cs_high") and not m.get(CR1) & SPE and not m.get(CR2) & IRQ_BITS
        assert m.resets == 2
        tx = len(m.tx); m.tick(40); m.call("position_model_irq")
        assert len(m.tx) == tx and m.latest() == obs and not m.get(CR1) & SPE
        passed("200 us transport timeout resets and isolates without retry: " + mode)
    for error in [OVR, 32, 16, 256]:
        m = Model(image).begin(); m.call("position_model_irq"); m.status(error)
        m.call("position_model_irq")
        assert not m.latest()["valid"] and m.resets == 2 and not m.get(CR1) & SPE
        m.tx.clear(); m.tick(20); m.call("position_model_irq"); assert not m.tx
        passed(f"hardware error {error} resets and remains isolated")

    m = Model(image).begin(); m.call("position_model_irq"); m.receive(packet()[:2])
    assert m.latest() is None
    m.receive(packet()[2:]); assert m.latest()["valid"] and m.spi_reads == 4
    passed("partial frame cannot publish before four bytes")

    m = Model(image).begin(); m.call("position_model_irq")
    m.fifo = packet(); m.status()
    fired = [False]
    def interrupt(model, address):
        if address == (model.symbols["publish_completion"] & ~1) and not fired[0]:
            fired[0] = True; model.on_instruction = None
            model.write("model_time", 22, "Q"); model.pending_tick = True
    m.on_instruction = interrupt; m.call("position_model_irq")
    assert fired[0] and not m.latest()["valid"] and m.latest()["time"] == 20
    assert m.resets == 2 and m.word("model_cs_high")
    m.tick(20); assert not m.get(CR1) & SPE
    passed("ADC timeout preempts frame parsing; old SPI snapshot cannot overwrite invalid publication")

    m = Model(image).begin(); m.call("position_model_irq"); m.fifo = packet(); m.status()
    snapshots = []
    def view(model, address):
        if address == (model.symbols["publish_completion"] & ~1):
            model.on_instruction = None
            snapshots.append(model.get(model.symbols["s_transfer_active"]))
    m.on_instruction = view; m.call("position_model_irq")
    assert snapshots and snapshots[0] & 1 and not m.get(m.symbols["s_transfer_active"]) & 1
    passed("active remains set until complete publication commits")

    m = Model(image).begin(); m.call("position_model_irq"); m.receive()
    m.tx.clear(); m.tick(9)
    assert m.word("model_cs_high") and not m.get(CR1) & SPE
    m.tick(); assert not m.word("model_cs_high") and m.get(CR1) & SPE
    m.call("position_model_irq"); m.receive(packet(bad_crc=True))
    assert m.latest()["referenced"] and not m.latest()["valid"] and m.latest()["time"] == 40
    passed("1 kHz next transfer preserves prior reference while publishing an invalid complete frame")

    m = Model(image).begin(); m.put(SR, 0)
    before = m.instructions; m.call("position_model_irq")
    assert not m.tx and m.spi_reads == 0 and m.instructions - before < 1000
    m.status(); m.call("position_model_irq"); m.receive()
    assert m.latest()["valid"] and len(m.tx) == 4
    passed("TXE unavailable returns immediately and later IRQ advances finite transfer")

    m = Model(image); m.write("model_time", (1 << 32) - 20, "Q"); m.begin()
    m.call("position_model_irq"); m.receive()
    assert m.latest()["time"] == 1 << 32 and m.latest()["valid"]
    passed("coherent transfer timestamp preserves 64-bit ControlTime carry")

    m = Model(image, auto_arm=False)
    assert m.get(CR1) & 1024
    init_size = m.word("model_spi_init_size")
    original_init = bytes(m.cpu.mem_read(m.symbols["hspi1"] + 4, init_size))
    m.call("position_model_arm")
    assert m.word("model_arm_result") and not m.get(CR1) & (SPE | 1024)
    assert bytes(m.cpu.mem_read(m.symbols["hspi1"] + 4, init_size)) == original_init
    assert all(address != GPIO_BSRR or value in (1 << 15, 1 << 31) for address, value, _ in m.trace)
    passed("static RXONLY pin configuration becomes a finite internal transaction without changing Init or routing MOSI")
    for register, bits, set_bits in [(CR1, 64, True), (CR1, 32768, True),
                                      (CR1, 8192, True), (CR1, 128, True),
                                      (CR1, 1, True), (CR1, 2, False),
                                      (CR1, 4, False), (CR1, 256, False),
                                      (CR1, 512, False), (CR1, 1024, False),
                                      (CR1, 32, True), (CR2, 4096, False),
                                      (CR2, 256, False), (CR2, 16, True)]:
        m = Model(image, auto_arm=False)
        m.put(register, m.get(register) | bits if set_bits else m.get(register) & ~bits)
        before = len(m.tx); m.call("position_model_arm")
        assert not m.word("model_arm_result") and len(m.tx) == before and m.resets == 0
        passed(f"unsupported static SPI configuration rejected {register:x}/{bits}/{set_bits}")
    m = Model(image); m.cpu.reg_write(UC_ARM_REG_PRIMASK, 1); m.tick(10); m.call("position_model_irq")
    assert m.cpu.reg_read(UC_ARM_REG_PRIMASK) == 1
    passed("nested critical sections preserve caller PRIMASK")

    print(f"G431 actual SSI ARM MMIO regression: {len(cases)} cases PASS")
    print("Scope: fixed byte clocks, FIFO/register access and interrupt/publication ordering; no real timing or electrical proof.")
    for name in cases: print("PASS " + name)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="g431-position-model-") as directory:
        run(compile_image(Path(directory)))
