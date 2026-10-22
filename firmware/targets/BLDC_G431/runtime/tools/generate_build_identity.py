"""由正式Run工程源、配置及保守头文件依赖闭包生成稳定64位身份；不含时间戳。"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import struct
import xml.etree.ElementTree as ET

TARGET = Path(__file__).resolve().parents[2]
ROOT = TARGET.parents[2]
PROJECT = TARGET / 'MDK-ARM/BLDC_control V1.uvprojx'
HEADER = TARGET / 'runtime/firmware_build_identity.h'


def identity():
    project = ET.parse(PROJECT)
    targets = project.findall('./Targets/Target')
    assert len(targets) == 1 and targets[0].findtext('TargetName') == 'BLDC_G431_Run'
    target = targets[0]
    paths = {PROJECT, TARGET / 'BLDC_control V1.ioc', Path(__file__).resolve()}
    pending = [(PROJECT.parent / text.text.replace('\\', '/')).resolve()
               for text in target.findall('./Groups/Group/Files/File/FilePath')]
    includes = [PROJECT.parent / text.replace('\\', '/') for text in
                target.findtext('./TargetOption/TargetArmAds/Cads/VariousControls/IncludePath').split(';') if text]
    while pending:
        path = pending.pop()
        if path == HEADER or path in paths:
            continue
        assert path.is_file() and path.is_relative_to(ROOT), path
        paths.add(path)
        text = path.read_text(encoding='utf-8-sig', errors='replace')
        for name in re.findall(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', text, re.M):
            for folder in [path.parent, *includes]:
                dependency = (folder / name).resolve()
                if dependency.is_file():
                    if dependency.is_relative_to(ROOT): pending.append(dependency)
                    break
    digest = hashlib.sha256()
    sources = {}
    for path in sorted(paths, key=lambda path: path.relative_to(ROOT).as_posix()):
        name = path.relative_to(ROOT).as_posix().encode('utf-8')
        content = path.read_bytes().removeprefix(b'\xef\xbb\xbf').replace(b'\r\n', b'\n')
        digest.update(struct.pack('<I', len(name)) + name + struct.pack('<I', len(content)) + content)
        sources[name.decode()] = hashlib.sha256(content).hexdigest()
    full = digest.hexdigest()
    return dict(algorithm='SHA256 sorted repo-relative UTF8 path and BOM-free LF content; each length-prefixed u32 LE',
                dependency_scope='Run project members/config plus conservative quoted/angle header closure, including conditional branches; compiler system headers excluded',
                compiler='ARM Compiler 5.06 update5 build528', source_sha256=sources,
                full_sha256=full, firmware_build_id='0x' + full[:16])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='仅核对，过期则失败')
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    record = identity()
    content = ('/* 自动生成：修改正式源码/配置后运行runtime/tools/generate_build_identity.py。 */\n'
               '#ifndef G431_FIRMWARE_BUILD_IDENTITY_H\n#define G431_FIRMWARE_BUILD_IDENTITY_H\n'
               '#define G431_FIRMWARE_BUILD_ID ' + record['firmware_build_id'] + 'ULL\n#endif\n')
    if args.check:
        assert HEADER.read_text(encoding='utf-8') == content, '构建身份过期，须重新生成并Rebuild'
    elif not HEADER.exists() or HEADER.read_text(encoding='utf-8') != content:
        HEADER.write_text(content, encoding='utf-8', newline='\n')
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(record['firmware_build_id'], record['full_sha256'], len(record['source_sha256']), 'source/config files')

if __name__ == '__main__': main()
