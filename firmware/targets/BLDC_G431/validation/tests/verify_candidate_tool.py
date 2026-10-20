"""Candidate identity/entry-point checks with synthetic files; no SWD or build."""
from argparse import Namespace
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import candidate_manifest as candidate
import logic_check as common
import probe_once as probe
import pwm_once as pwm


def elf_fixture(layout, payload=b"\x01\x02\x03\x04"):
    """Minimal real ELF32/ARM with load bytes and a symbol table for parser tests."""
    strings = bytearray(b"\0")
    symbols = bytearray(16)
    for name, (key, size) in candidate.SYMBOLS.items():
        name_offset = len(strings)
        strings.extend(name.encode("ascii") + b"\0")
        symbols.extend(struct.pack("<IIIBBH", name_offset, layout[key], size, 0x11, 0, 0xFFF1))
    names = b"\0.strtab\0.symtab\0.shstrtab\0"
    string_offset = 84 + len(payload)
    symbol_offset = string_offset + len(strings)
    name_offset = symbol_offset + len(symbols)
    section_offset = name_offset + len(names)
    header = struct.pack("<16sHHIIIIIHHHHHH", b"\x7fELF\x01\x01\x01" + b"\0" * 9,
        2, 40, 1, 0x08000001, 52, section_offset, 0x05000400, 52, 32, 1, 40, 4, 3)
    program = struct.pack("<8I", 1, 84, 0x08000000, 0x08000000, len(payload), len(payload), 5, 4)
    sections = b"\0" * 40
    sections += struct.pack("<10I", 1, 3, 0, 0, string_offset, len(strings), 0, 0, 1, 0)
    sections += struct.pack("<10I", 9, 2, 0, 0, symbol_offset, len(symbols), 1, 1, 4, 16)
    sections += struct.pack("<10I", 17, 3, 0, 0, name_offset, len(names), 0, 0, 1, 0)
    return header + program + payload + strings + symbols + names + sections


def hex_fixture(payload):
    """生成校验和正确的连续 Flash 镜像，供真实压缩尺寸夹具使用。"""
    rows = [":020000040800F2"]
    for offset in range(0, len(payload), 16):
        block = payload[offset:offset + 16]
        record = bytes([len(block), offset >> 8, offset & 255, 0]) + block
        rows.append(":" + (record + bytes([-sum(record) & 255])).hex().upper())
    return "\n".join([*rows, ":00000001FF", ""])


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="g431-candidate-test-")
        self.root = Path(self.temp.name)
        self.target = self.root / "firmware/targets/BLDC_G431"
        self.build = self.target / "MDK-ARM/build"
        self.build.mkdir(parents=True)
        (self.target / "Core").mkdir()
        (self.target / "validation/tools").mkdir(parents=True)
        (self.target / "MDK-ARM/BLDC_control V1.uvprojx").write_text(
            '<Project><FilePath>../Core/main.c</FilePath></Project>', encoding="utf-8")
        (self.target / "Core/main.c").write_text("void main(void) {}", encoding="utf-8")
        (self.target / "Core/main.h").write_text("/* fixture */", encoding="utf-8")
        (self.target / "validation/tools/entry.py").write_text("# fixture", encoding="utf-8")
        (self.build / candidate.ARTIFACTS[0]).write_text(
            ":020000040800F2\n:0400000001020304F2\n:00000001FF\n", encoding="ascii")
        self.layout = {"legacy": 0x20000000, "request": 0x20000050,
                       "snapshot": 0x20000064, "capture": 0x20000120,
                       "probe": 0x20000170, "stop": 0x20000200, "tick": 0x20000204}
        (self.build / candidate.ARTIFACTS[1]).write_bytes(elf_fixture(self.layout))
        mapping = "Image Entry point : 0x08000001\nLoad Region LR_IROM1 (Base: 0x08000000, Size: 0x00000004, Max: 0x20000)\n"
        mapping += "".join("%s 0x%08x Data %d fixture.o(.data)\n" % (name, self.layout[key], size)
                           for name, (key, size) in candidate.SYMBOLS.items())
        (self.build / candidate.ARTIFACTS[2]).write_text(mapping, encoding="utf-8")
        self.executable = self.root / "fake-tool.exe"
        self.executable.write_bytes(b"never executed")
        self.path = self.root / "candidate.json"
        self.manifest = {"schema_version": candidate.SCHEMA, "target": "BLDC_G431",
            "status": candidate.STATUS, "build_directory": self.build.relative_to(self.root).as_posix(),
            "files": {name: candidate.digest(self.build / name) for name in candidate.ARTIFACTS},
            "source_files": {name: candidate.digest(path) for name, path in candidate.source_paths(self.target).items()},
            "layout": self.layout}
        self.write()
        self.globals = patch.multiple(common, TARGET=self.target, CANDIDATE=None,
            BUILD=self.build, EXPECTED=common.DEFAULT_EXPECTED, CLI=self.executable, GDB=self.executable)
        self.globals.start()

    def tearDown(self):
        self.globals.stop()
        self.temp.cleanup()

    def write(self, manifest=None):
        self.path.write_text(json.dumps(manifest or self.manifest), encoding="utf-8")

    def configure(self):
        common.configure_candidate(Namespace(candidate_manifest=self.path,
                                             cli=self.executable, gdb=self.executable))

    def test_valid_candidate_checks_live_flash(self):
        self.configure()
        with patch.object(common, "offline_layout", return_value=self.layout), \
                patch.object(common.Session, "upload", return_value={"flash": b"\x01\x02\x03\x04"}) as upload:
            session = common.Session(self.root / "session")
        upload.assert_called_once_with("identity", [("flash", 0x08000000, 4)])
        self.assertFalse(session.write_attempted)
        self.assertEqual(common.EXPECTED, self.manifest["files"][candidate.ARTIFACTS[0]])
        identity = json.loads((session.out / "candidate-identity.json").read_text())
        self.assertFalse(identity["hardware_qualified"])

    def test_live_flash_mismatch_still_rejected(self):
        self.configure()
        with patch.object(common, "offline_layout", return_value=self.layout), \
                patch.object(common.Session, "upload", return_value={"flash": b"\0" * 4}) as upload:
            with self.assertRaisesRegex(RuntimeError, "Live Flash differs"):
                common.Session(self.root / "session")
        upload.assert_called_once()

    def test_artifact_hash_missing_and_source_failure_before_swd(self):
        for name in candidate.ARTIFACTS:
            original = (self.build / name).read_bytes()
            for missing in [False, True]:
                with self.subTest(name=name, missing=missing), patch.object(common.Session, "upload") as upload:
                    if missing:
                        (self.build / name).unlink()
                    else:
                        (self.build / name).write_bytes(b"changed")
                    with self.assertRaises(RuntimeError):
                        self.configure()
                    upload.assert_not_called()
                (self.build / name).write_bytes(original)
        source = self.target / "Core/main.c"
        source.write_text("changed", encoding="utf-8")
        with patch.object(common.Session, "upload") as upload, self.assertRaisesRegex(RuntimeError, "source/tool changed"):
            self.configure()
        upload.assert_not_called()

    def test_omitted_required_source_or_tool_rejected(self):
        for name in self.manifest["source_files"]:
            bad = deepcopy(self.manifest)
            del bad["source_files"][name]
            self.write(bad)
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "incomplete"):
                self.configure()

    def test_changed_layout_rejected_before_swd(self):
        self.configure()
        with patch.object(common, "offline_layout", return_value={**self.layout, "request": 0x20000054}), \
                patch.object(common.Session, "upload") as upload:
            with self.assertRaisesRegex(RuntimeError, "layout differs"):
                common.Session(self.root / "session")
            upload.assert_not_called()

    def test_modified_after_configuration_rejected(self):
        self.configure()
        (self.target / "validation/tools/entry.py").write_text("changed", encoding="utf-8")
        with patch.object(common.Session, "upload") as upload, self.assertRaises(RuntimeError):
            common.Session(self.root / "session")
        upload.assert_not_called()

    def test_cross_image_predecessor_rejected_before_session(self):
        previous = self.root / "previous.json"
        previous.write_text(json.dumps({"hex_sha256": common.DEFAULT_EXPECTED}), encoding="utf-8")
        for tool, session_owner, extras in [(probe, common, []), (pwm, pwm, ["--duty-percent", "70"])]:
            argv = ["tool", "--candidate-manifest", str(self.path), "--cli", str(self.executable),
                    "--gdb", str(self.executable), "--previous-summary", str(previous),
                    "--output", str(self.root / "output"), "--execute-once", *extras]
            with self.subTest(tool=tool.__name__), patch.object(sys, "argv", argv), \
                    patch.object(session_owner, "Session") as session:
                with self.assertRaisesRegex(RuntimeError, "another image"):
                    tool.main()
                session.assert_not_called()

    def test_new_source_freeze_does_not_read_or_rewrite_history(self):
        self.configure()
        common.verify_source_freeze(self.root)
        verified = json.loads((self.root / "source-verification.json").read_text())
        self.assertEqual(verified["verified"], self.manifest["source_files"])
        self.assertFalse((self.target / "validation/local-evidence").exists())

    def test_missing_manifest_and_tool_paths_fail_closed(self):
        with self.assertRaises(RuntimeError):
            common.configure_candidate(Namespace(candidate_manifest=self.root / "absent.json", cli=None, gdb=None))
        with self.assertRaisesRegex(RuntimeError, "executable missing"):
            common.configure_candidate(Namespace(candidate_manifest=self.path, cli=self.root / "absent.exe", gdb=self.executable))

    def test_default_selection_is_unchanged(self):
        self.configure()
        common.configure_candidate(Namespace(candidate_manifest=None, cli=None, gdb=None))
        self.assertEqual((common.BUILD, common.EXPECTED, common.CLI, common.GDB),
                         (common.DEFAULT_BUILD, common.DEFAULT_EXPECTED, common.DEFAULT_CLI, common.DEFAULT_GDB))
        self.assertIsNone(common.CANDIDATE)

    def test_malformed_hex_rejected_even_if_hash_matches(self):
        for raw in [":00000001FF\n", ":020000040800F2\n:0400000001020304F3\n:00000001FF\n",
                    ":020000040900F1\n:0400000001020304F2\n:00000001FF\n"]:
            (self.build / candidate.ARTIFACTS[0]).write_text(raw, encoding="ascii")
            self.manifest["files"][candidate.ARTIFACTS[0]] = candidate.digest(self.build / candidate.ARTIFACTS[0])
            self.write()
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                self.configure()

    def test_mixed_axf_hex_rejected_even_if_hashes_match(self):
        axf = self.build / candidate.ARTIFACTS[1]
        axf.write_bytes(elf_fixture(self.layout, b"\x01\x02\x03\x05"))
        self.manifest["files"][candidate.ARTIFACTS[1]] = candidate.digest(axf)
        self.write()
        with patch.object(common.Session, "upload") as upload, self.assertRaisesRegex(RuntimeError, "mixed build"):
            self.configure()
        upload.assert_not_called()

    def test_mixed_map_rejected_even_if_hash_matches(self):
        mapping = self.build / candidate.ARTIFACTS[2]
        original = mapping.read_text(encoding="utf-8")
        for old, new in [("0x20000050", "0x20000054"), ("Size: 0x00000004", "Size: 0x00000005"),
                         ("Image Entry point : 0x08000001", "Image Entry point : 0x08000003")]:
            mapping.write_text(original.replace(old, new), encoding="utf-8")
            self.manifest["files"][candidate.ARTIFACTS[2]] = candidate.digest(mapping)
            self.write()
            with self.subTest(change=old), patch.object(common.Session, "upload") as upload, self.assertRaises(RuntimeError):
                self.configure()
            upload.assert_not_called()

    def test_armcc_real_compressed_size_metadata_passes(self):
        # 与本次 ARMCC 真实输出同尺寸：压缩前 0xa1f4，实际加载 0xa0a0。
        payload = bytes(range(256)) * (0xA0A0 // 256) + bytes(range(0xA0A0 % 256))
        self.assertEqual(len(payload), 41120)
        (self.build / candidate.ARTIFACTS[0]).write_text(hex_fixture(payload), encoding="ascii")
        (self.build / candidate.ARTIFACTS[1]).write_bytes(elf_fixture(self.layout, payload))
        mapping = self.build / candidate.ARTIFACTS[2]
        original = mapping.read_text(encoding="utf-8")
        mapping.write_text(original.replace("Size: 0x00000004, Max: 0x20000",
            "Size: 0x0000a1f4, Max: 0x00020000, ABSOLUTE, COMPRESSED[0x0000a0a0]"), encoding="utf-8")
        self.manifest["files"] = {name: candidate.digest(self.build / name) for name in candidate.ARTIFACTS}
        self.write()
        with patch.object(common.Session, "upload") as upload:
            self.configure()
        upload.assert_not_called()

    def test_compressed_map_tampering_rejected_before_swd(self):
        mapping = self.build / candidate.ARTIFACTS[2]
        original = mapping.read_text(encoding="utf-8")
        valid = "Load Region LR_IROM1 (Base: 0x08000000, Size: 0x00000008, Max: 0x20000, ABSOLUTE, COMPRESSED[0x00000004])"
        prefix = "Load Region LR_IROM1 (Base: 0x08000000, Size: 0x00000004, Max: 0x20000)"
        valid_mapping = original.replace(prefix, valid)
        mapping.write_text(valid_mapping, encoding="utf-8")
        candidate.verify_artifacts(self.build, self.layout)
        mutations = [
            valid.replace("COMPRESSED[0x00000004]", "COMPRESSED[0x00000003]"),
            valid.replace("COMPRESSED[0x00000004]", "COMPRESSED[0x00000005]"),
            valid.replace("COMPRESSED[0x00000004]", "COMPRESSED[0x00000000]"),
            valid.replace("Base: 0x08000000", "Base: 0x08000004"),
            valid.replace("Base: 0x08000000", "Base: 0x07000000"),
            valid.replace("Size: 0x00000008", "Size: 0x00000003"),
            valid.replace("Size: 0x00000008", "Size: 0x00020001"),
            valid.replace("Max: 0x20000", "Max: 0x00000007"),
            valid.replace("Max: 0x20000", "Max: 0x20001"),
            valid.replace("Max: 0x20000", "Max: 0x0"),
            valid.replace(", Max: 0x20000", ""),
            valid.replace("COMPRESSED[0x00000004]", "COMPRESSED[0x00000004], COMPRESSED[0x00000004]"),
            valid.replace("COMPRESSED[0x00000004]", "COMPRESSED[0x00000004], COMPRESSED[0x00000003]"),
            valid.replace("COMPRESSED[0x00000004]", "COMPRESSED[garbage]"),
            valid.replace("ABSOLUTE", "ABSOLUTE, ABSOLUTE"),
            valid + "\n" + valid,
            valid + "\nLoad Region LR_OTHER (malformed)",
        ]
        for header in mutations:
            mapping.write_text(original.replace(prefix, header), encoding="utf-8")
            self.manifest["files"][candidate.ARTIFACTS[2]] = candidate.digest(mapping)
            self.write()
            with self.subTest(header=header), patch.object(common.Session, "upload") as upload:
                with self.assertRaises(RuntimeError):
                    self.configure()
                upload.assert_not_called()

    def test_compressed_flag_never_bypasses_axf_hex_byte_comparison(self):
        mapping = self.build / candidate.ARTIFACTS[2]
        mapping.write_text(mapping.read_text(encoding="utf-8").replace(
            "Size: 0x00000004, Max: 0x20000", "Size: 0x00000008, Max: 0x20000, ABSOLUTE, COMPRESSED[0x00000004]"),
            encoding="utf-8")
        (self.build / candidate.ARTIFACTS[1]).write_bytes(elf_fixture(self.layout, b"\x01\x02\x03\x05"))
        with self.assertRaisesRegex(RuntimeError, "mixed build"):
            candidate.verify_artifacts(self.build, self.layout)

    def test_ram_execution_uses_physical_rom_load_address(self):
        axf = self.build / candidate.ARTIFACTS[1]
        payload = bytearray(axf.read_bytes())
        struct.pack_into("<I", payload, 52 + 8, 0x20000000)
        axf.write_bytes(payload)
        self.manifest["files"][candidate.ARTIFACTS[1]] = candidate.digest(axf)
        self.write()
        self.configure()

    def test_prepare_mixed_build_does_not_publish_manifest(self):
        output = self.root / "mixed.json"
        (self.build / candidate.ARTIFACTS[1]).write_bytes(elf_fixture(self.layout, b"\x01\x02\x03\x05"))
        argv = ["candidate_manifest", "--build-dir", str(self.build), "--output", str(output),
                "--gdb", str(self.executable)]
        with patch.object(sys, "argv", argv), patch.object(common, "offline_layout", return_value=self.layout), \
                patch.object(common.Session, "upload") as upload:
            with self.assertRaisesRegex(RuntimeError, "mixed build"):
                candidate.main()
            self.assertFalse(output.exists())
            upload.assert_not_called()

    def test_prepare_offline_and_refuse_overwrite(self):
        output = self.root / "new.json"
        argv = ["candidate_manifest", "--build-dir", str(self.build), "--output", str(output),
                "--gdb", str(self.executable)]
        with patch.object(sys, "argv", argv), patch.object(common, "offline_layout", return_value=self.layout), \
                patch.object(common.Session, "upload") as upload, redirect_stdout(io.StringIO()):
            candidate.main()
            prepared, _ = candidate.load(output, self.target)
            self.assertEqual(prepared["status"], candidate.STATUS)
            with self.assertRaisesRegex(RuntimeError, "overwrite"):
                candidate.main()
            upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
