"""スクリプトが子プロセスを起こすとき、Windows でコンソールのウインドウを出さないこと (creationflags に NO_WINDOW)。

Claude Code から流すと、子 (git・jevlint の .cmd・node など) のたびにウインドウがちらつくため。
"""
import ast
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CALLS = {'run', 'call', 'Popen', 'check_output', 'check_call'}


def subprocess_calls(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in CALLS
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'subprocess'):
            yield node


class NoWindowTest(unittest.TestCase):
    def test_every_subprocess_call_hides_the_console_window(self):
        scripts = [p for p in sorted(HERE.glob('*.py')) + sorted(HERE.glob('jev/*.py'))
                   if not p.name.startswith('test_')]
        missing = []
        for path in scripts:
            for node in subprocess_calls(path):
                keywords = {k.arg for k in node.keywords}
                # **flags で渡す呼び出し (jev_mcp.py) は flags の中に NO_WINDOW を入れている
                if 'creationflags' not in keywords and None not in keywords:
                    missing.append(f'{path.name}:{node.lineno}')
        self.assertEqual(missing, [])

    def test_no_window_is_zero_outside_windows(self):
        import lint_run
        self.assertEqual(lint_run.NO_WINDOW, getattr(subprocess, 'CREATE_NO_WINDOW', 0))


if __name__ == '__main__':
    unittest.main()
