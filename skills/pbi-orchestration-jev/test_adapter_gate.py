"""adapter_gate.py の判定・表・警告・終了コードを、CLI を subprocess で動かして確かめる。

PBI の境目 (diff 299/300/301・PBI 2/3 件・deep・sensitive・route.json が無い・--numstat なし・
バイナリの行・.rework-1 の付いたファイル・PBI 0 件) と、numstat の文字コード (UTF-8 の BOM・
UTF-16)・読めない行・空のファイル、agents.json の設定 (off・スキル名・警告)、
終了コード 2 を、一時ディレクトリに作った入力で見る。home は必ず一時ディレクトリにして、
実際のユーザーの agents.json を読まないようにする。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'adapter_gate.py')

HEADER = 'phase\tsetting\tdecision\treason'
DESIGN_REASON = 'decided by the orchestrator (appendix/adapters.md)'
OFF_REASON = 'turned off in agents.json'


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def write_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


class AdapterGateTest(unittest.TestCase):
    def setUp(self):
        # tempfile の 0o700 は権限を絞った環境で読めなくなるので、既定の権限で作る。
        self.dir = os.path.join(tempfile.gettempdir(), f'pbi-gate-{uuid.uuid4().hex}')
        os.mkdir(self.dir)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.base = os.path.join(self.dir, 'pbi')
        self.home = os.path.join(self.dir, 'home')
        self.numstat = os.path.join(self.dir, 'numstat.txt')
        os.makedirs(self.base)
        os.makedirs(self.home)

    def reset(self):
        """PBI と home の中身を空にして、境目ごとの入力をまっさらな状態から作る。"""
        shutil.rmtree(self.base, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)
        os.makedirs(self.base)
        os.makedirs(self.home)

    def add_pbi(self, number, review='normal', sensitive=False, route=True, facts=True, name=None):
        """PBI-<number>.md と route.json・facts.json を置く。route・facts は False なら置かない。"""
        stem = name or f'PBI-{number:03d}'
        write_text(os.path.join(self.base, f'{stem}.md'), f'# {stem}: ダミー\n')
        if route:
            write_json(os.path.join(self.base, f'{stem}.route.json'), {'review': review})
        if facts:
            write_json(os.path.join(self.base, f'{stem}.facts.json'), {'sensitive': sensitive})
        return stem

    def write_numstat(self, added=0, deleted=0, extra=()):
        """追加・削除の 1 行と、extra の行 (バイナリなど) を numstat に書く。"""
        lines = [f'{added}\t{deleted}\tfile.py'] + list(extra)
        write_text(self.numstat, ''.join(f'{line}\n' for line in lines))

    def write_numstat_bytes(self, data):
        """numstat をバイト列のまま書く (BOM や文字コードを変えるテスト用)。"""
        with open(self.numstat, 'wb') as f:
            f.write(data)

    def write_adapters(self, adapters):
        write_json(os.path.join(self.home, 'agents.json'), {'adapters': adapters})

    def gate(self, *args, numstat=True, home=None, pbi_dir=None):
        """CLI を起動して完成したプロセスを返す。--numstat は numstat=False のときだけ省く。"""
        command = [sys.executable, SCRIPT, pbi_dir or self.base]
        if numstat:
            command += ['--numstat', self.numstat]
        command += ['--home', home or self.home]
        command += list(args)
        return subprocess.run(command, capture_output=True, text=True, encoding='utf-8')

    def table(self, result):
        """標準出力の表を phase → [setting, decision, reason] にする。見出しと warn の行は確かめる。"""
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], HEADER)
        rows = {}
        for line in lines[1:]:
            cells = line.split('\t')
            rows[cells[0]] = cells[1:]
        return rows

    def assertRow(self, rows, phase, setting, decision, reason):
        self.assertEqual(rows[phase], [setting, decision, reason])

    def test_diff_boundaries(self):
        """299 と 300 は skip、301 は run。"""
        self.add_pbi(1)
        cases = [(299, 'skip', 'diff 299 lines, 1 PBIs, no deep or sensitive'),
                 (300, 'skip', 'diff 300 lines, 1 PBIs, no deep or sensitive'),
                 (301, 'run', 'diff 301 lines > 300')]
        for lines, decision, reason in cases:
            with self.subTest(lines=lines):
                self.write_numstat(added=lines)
                rows = self.table(self.gate())
                self.assertRow(rows, 'review', 'auto', decision, reason)
                self.assertRow(rows, 'security', 'auto', 'skip', 'no sensitive or deep PBI')

    def test_pbi_count_boundaries(self):
        """2 件は skip、3 件は run。"""
        for count, decision, reason in [(2, 'skip', 'diff 0 lines, 2 PBIs, no deep or sensitive'),
                                        (3, 'run', '3 PBIs > 2')]:
            with self.subTest(count=count):
                self.reset()
                for number in range(1, count + 1):
                    self.add_pbi(number)
                self.write_numstat()
                rows = self.table(self.gate())
                self.assertRow(rows, 'review', 'auto', decision, reason)

    def test_deep_pbi_runs_review_and_security(self):
        self.add_pbi(1)
        self.add_pbi(2, review='deep')
        self.write_numstat()
        rows = self.table(self.gate())
        self.assertRow(rows, 'review', 'auto', 'run', 'PBI-002 is deep')
        self.assertRow(rows, 'security', 'auto', 'run', 'PBI-002 is deep')

    def test_sensitive_pbi_runs_review_and_security(self):
        self.add_pbi(1, sensitive=True)
        self.write_numstat()
        rows = self.table(self.gate())
        self.assertRow(rows, 'review', 'auto', 'run', 'PBI-001 is sensitive')
        self.assertRow(rows, 'security', 'auto', 'run', 'PBI-001 is sensitive')

    def test_security_reason_lists_sensitive_before_deep(self):
        self.add_pbi(1, review='deep')
        self.add_pbi(2, sensitive=True)
        self.write_numstat()
        rows = self.table(self.gate())
        self.assertRow(rows, 'security', 'auto', 'run', 'PBI-002 is sensitive; PBI-001 is deep')

    def test_missing_route_json_counts_as_deep(self):
        self.add_pbi(1, route=False)
        self.write_numstat()
        rows = self.table(self.gate())
        self.assertRow(rows, 'review', 'auto', 'run', 'PBI-001 is deep (no route.json)')
        self.assertRow(rows, 'security', 'auto', 'run', 'PBI-001 is deep (no route.json)')

    def test_unreadable_facts_json_is_not_sensitive(self):
        self.add_pbi(1, facts=False)
        self.write_numstat()
        rows = self.table(self.gate())
        self.assertRow(rows, 'review', 'auto', 'skip', 'diff 0 lines, 1 PBIs, no deep or sensitive')
        self.assertRow(rows, 'security', 'auto', 'skip', 'no sensitive or deep PBI')

    def test_missing_numstat_runs_review(self):
        self.add_pbi(1)
        rows = self.table(self.gate(numstat=False))
        self.assertRow(rows, 'review', 'auto', 'run', 'diff size unknown (no --numstat)')
        self.assertRow(rows, 'security', 'auto', 'skip', 'no sensitive or deep PBI')

    def test_binary_lines_count_as_zero(self):
        self.add_pbi(1)
        binary = '-\t-\timage.png'
        cases = [([binary], 'diff 0 lines, 1 PBIs, no deep or sensitive'),
                 ([binary, '2\t0\tcode.py'], 'diff 2 lines, 1 PBIs, no deep or sensitive')]
        for extra, reason in cases:
            with self.subTest(extra=extra):
                self.write_numstat(extra=extra)
                rows = self.table(self.gate())
                self.assertRow(rows, 'review', 'auto', 'skip', reason)

    def test_utf8_bom_numstat_is_counted(self):
        """UTF-8 の BOM が付いた numstat も、そのままの行数で数える。"""
        self.add_pbi(1)
        self.write_numstat_bytes(b'\xef\xbb\xbf290\t20\ta.py\n5\t0\tb.py\n')
        rows = self.table(self.gate())
        self.assertRow(rows, 'review', 'auto', 'run', 'diff 315 lines > 300')

    def test_utf16_numstat_is_counted(self):
        """UTF-16 (BOM 付き、LE) の numstat も数える。"""
        self.add_pbi(1)
        text = '290\t20\ta.py\n5\t0\tb.py\n'
        self.write_numstat_bytes(b'\xff\xfe' + text.encode('utf-16-le'))
        rows = self.table(self.gate())
        self.assertRow(rows, 'review', 'auto', 'run', 'diff 315 lines > 300')

    def test_unreadable_numstat_line_makes_size_unknown(self):
        """読めない行が 1 つでもあれば、行数は不明として run に倒す。"""
        self.add_pbi(1)
        self.write_numstat_bytes(b'fatal: bad revision\n')
        rows = self.table(self.gate())
        self.assertRow(rows, 'review', 'auto', 'run',
                       'diff size unknown (unreadable numstat line 1)')

    def test_empty_numstat_is_zero_lines(self):
        """空のファイル (または空の行だけ) は変更が無いので 0 行。"""
        self.add_pbi(1)
        for data in (b'', b'\n\n'):
            with self.subTest(data=data):
                self.write_numstat_bytes(data)
                rows = self.table(self.gate())
                self.assertRow(rows, 'review', 'auto', 'skip',
                               'diff 0 lines, 1 PBIs, no deep or sensitive')

    def test_rework_and_brief_files_are_not_pbis(self):
        self.add_pbi(1)
        self.add_pbi(2, review='deep', name='PBI-002.rework-1')
        self.add_pbi(2, sensitive=True, name='PBI-002.brief')
        self.write_numstat()
        rows = self.table(self.gate())
        self.assertRow(rows, 'review', 'auto', 'skip', 'diff 0 lines, 1 PBIs, no deep or sensitive')
        self.assertRow(rows, 'security', 'auto', 'skip', 'no sensitive or deep PBI')

    def test_no_pbi_is_skip(self):
        self.write_numstat()
        rows = self.table(self.gate())
        self.assertRow(rows, 'design', 'auto', 'manual', DESIGN_REASON)
        self.assertRow(rows, 'review', 'auto', 'skip', 'diff 0 lines, 0 PBIs, no deep or sensitive')
        self.assertRow(rows, 'security', 'auto', 'skip', 'no sensitive or deep PBI')
        self.assertEqual(len(rows), 3)

    def test_off_setting(self):
        self.add_pbi(1)
        self.write_numstat(added=999)
        self.write_adapters({'design': 'off', 'review': 'off', 'security': 'off'})
        rows = self.table(self.gate())
        for phase in ('design', 'review', 'security'):
            self.assertRow(rows, phase, 'off', 'off', OFF_REASON)

    def test_skill_name_setting_keeps_decision(self):
        self.add_pbi(1)
        self.write_numstat()
        self.write_adapters({'design': 'code-review',
                             'review': 'mattpocock-skills:code-review',
                             'security': 'security-review'})
        rows = self.table(self.gate())
        self.assertRow(rows, 'design', 'code-review', 'manual', DESIGN_REASON)
        self.assertRow(rows, 'review', 'mattpocock-skills:code-review', 'skip',
                       'diff 0 lines, 1 PBIs, no deep or sensitive')
        self.assertRow(rows, 'security', 'security-review', 'skip', 'no sensitive or deep PBI')

    def test_warnings_come_after_the_table(self):
        self.add_pbi(1)
        self.write_numstat()
        self.write_adapters({'review': 1, 'zzz': 'off'})
        result = self.gate()
        rows = self.table(result)
        self.assertRow(rows, 'review', 'auto', 'skip', 'diff 0 lines, 1 PBIs, no deep or sensitive')
        self.assertEqual(result.stdout.splitlines()[4:],
                         ['warn adapters.review: invalid value, using auto',
                          'warn adapters.zzz: unknown phase, ignored'])

    def test_missing_pbi_dir_exits_2(self):
        result = self.gate(pbi_dir=os.path.join(self.dir, 'nowhere'))
        self.assertEqual(result.returncode, 2)

    def test_unreadable_numstat_exits_2(self):
        self.add_pbi(1)
        result = self.gate(pbi_dir=self.base)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
