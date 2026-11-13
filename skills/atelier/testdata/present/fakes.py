"""偽の node と偽の am を一時ディレクトリに置く小さな助け (present・adapter_gate・tools_check・conductor の試験が使う)。

  install_node(dir, version=None) -> dir   PATH に載せる node を dir に置く。version を渡すと node の既定の版にする
                                           (環境変数 FAKE_NODE_VERSION が優先)。
  install_am(skills_dir, name) -> path     <skills>/<name>/SKILL.md と scripts/am.mjs を置き、am.mjs のパスを返す。
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_NODE = os.path.join(HERE, 'fake_node.py')
FAKE_AM = os.path.join(HERE, 'am.mjs')
AM_NAME = 'answer-me-with-any'


def install_node(directory, version=None):
    """偽の node を directory に置く。Windows は node.cmd、ほかは実行権のある node。今の python で fake_node.py を動かす。

    version を渡すと、その版を node の中に固定する (環境変数 FAKE_NODE_VERSION が優先)。
    """
    os.makedirs(directory, exist_ok=True)
    fake = os.path.join(directory, 'fake_node.py')
    shutil.copyfile(FAKE_NODE, fake)
    if version:
        with open(fake, 'r', encoding='utf-8') as stream:
            text = stream.read()
        text = text.replace("os.environ.get('FAKE_NODE_VERSION') or 'v22.1.0'",
                            f"os.environ.get('FAKE_NODE_VERSION') or {version!r}")
        with open(fake, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
    if os.name == 'nt':
        launcher = os.path.join(directory, 'node.cmd')
        # cmd.exe はバッチファイルを OEM のコードページで読む。ASCII に限ると、日本語のユーザー名の一時ディレクトリなどで
        # 書けない。fake_node.py は %~dp0 (このファイルのフォルダ) で指し、埋め込むパスを Python だけにする
        text = f'@echo off\r\n"{sys.executable}" "%~dp0fake_node.py" %*\r\nexit /b %ERRORLEVEL%\r\n'
        try:
            data = text.encode('oem')
        except UnicodeEncodeError as error:
            raise RuntimeError(f'the Python path cannot be written in the OEM code page: {sys.executable}') from error
        with open(launcher, 'wb') as stream:
            stream.write(data)
    else:
        launcher = os.path.join(directory, 'node')
        with open(launcher, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
        os.chmod(launcher, 0o755)
    return directory


def install_am(skills_dir, name=AM_NAME):
    """<skills_dir>/<name>/SKILL.md と scripts/am.mjs (偽の am) を置き、am.mjs のパスを返す。"""
    skill = os.path.join(skills_dir, name)
    os.makedirs(os.path.join(skill, 'scripts'), exist_ok=True)
    with open(os.path.join(skill, 'SKILL.md'), 'wb') as stream:
        stream.write(f'---\nname: {name}\n---\n'.encode('utf-8'))
    target = os.path.join(skill, 'scripts', 'am.mjs')
    shutil.copyfile(FAKE_AM, target)
    return target
