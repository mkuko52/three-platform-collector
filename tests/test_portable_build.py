"""Packaging checks with fake build output; no browser, network or real PyInstaller."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_reverse_portable as builder


class PortableBuildTest(unittest.TestCase):
    def test_speed_entries_private_data_exclusion_and_atomic_publication(self):
        make_archive = shutil.make_archive
        for fail_publish in (True, False):
            with self.subTest(fail_publish=fail_publish), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                out, package = root/'dist/rpc/reverse_collector', root/'双机分片'
                fixtures = {
                    'start_all.py': '# current launcher fixture',
                    'collectors/douyin.py': '# current douyin fixture',
                    'collectors/kuaishou.py': '# current kuaishou fixture',
                    'collectors/xiaohongshu.py': '# current xhs fixture',
                    'collectors/__pycache__/stale.pyc': 'do not package bytecode',
                    'archive/previous_workspace/双减舆情需求.md': 'scope fixture',
                    '双机分片/异机分片使用说明.md': 'current manual fixture',
                    'runtime/douyin/browser/private.txt': 'fixture only, do not package',
                    'output/csv/private.csv': 'fixture only, do not package',
                }
                for name, text in fixtures.items():
                    path = root/name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding='utf-8')
                archive = package/'reverse_collector_windows_x64_rpc.zip'
                archive.write_bytes(b'previous release must survive publication failure')

                def build(command, **kwargs):
                    if command[0] == 'fake-pyinstaller':
                        self.assertEqual(command[-1], str(root/'start_all.py'))
                        out.mkdir(parents=True)
                        (out/'reverse_collector.exe').write_bytes(b'fake executable')
                    else:
                        self.assertEqual(command[0], str(out/'reverse_collector.exe'))
                        self.assertIn('--check', command)
                    return SimpleNamespace(returncode=0, stdout='采集范围核验', stderr='')

                def publish(base, *args, **kwargs):
                    if fail_publish:
                        Path(base+'.zip').write_bytes(b'incomplete archive')
                        raise OSError('fixture: interrupted archive creation')
                    return make_archive(base, *args, **kwargs)

                with patch.object(builder, 'ROOT', root), patch.object(builder, 'OUT', out), \
                     patch.object(builder, 'PACKAGE', package), \
                     patch.object(builder, 'os', SimpleNamespace(name='nt', environ=os.environ)), \
                     patch.object(builder.platform, 'machine', return_value='AMD64'), \
                     patch.object(builder.shutil, 'which', return_value='fake-pyinstaller'), \
                     patch.object(builder.subprocess, 'run', side_effect=build), \
                     patch.object(builder.shutil, 'make_archive', side_effect=publish), \
                     patch('start_all.rate_overrides', return_value={'douyin':68, 'kuaishou':60}):
                    if fail_publish:
                        with self.assertRaises(OSError):
                            builder.main()
                        self.assertEqual(archive.read_bytes(), b'previous release must survive publication failure')
                        continue
                    builder.main()
                self.assertFalse((package/'reverse_collector_windows_x64_rpc.next.zip').exists())
                with zipfile.ZipFile(archive) as z:
                    names = z.namelist()
                    self.assertFalse(any('__pycache__' in name or '/private.' in name or '/output/' in name for name in names))
                    rates = json.loads(z.read('reverse_collector/runtime/collector_rate_overrides.json'))
                    self.assertEqual(rates, {'douyin':68, 'kuaishou':90, 'xiaohongshu':90})
                    for flag in ('fast', 'high-speed', 'ultra-speed', 'ultimate-speed'):
                        name = f'reverse_collector/采集速度版本/start_reverse_{flag.replace("-", "_")}.bat'
                        text = z.read(name).decode('ascii')
                        self.assertIn(f'call "%~dp0..\\start_reverse.bat" --{flag} %*', text)
                        self.assertIn('exit /b %ERRORLEVEL%', text)
                    launch = z.read('reverse_collector/start_reverse.bat').decode('ascii')
                    self.assertIn('--fail-fast --retries 0 --shard reverse %*', launch)


if __name__ == '__main__':
    unittest.main()
