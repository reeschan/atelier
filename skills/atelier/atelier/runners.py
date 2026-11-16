"""Build role launches and collect external CLI output."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess


class Command(list):
    """An argv list carrying the execution context for run_external."""


def _registered_command(runner, agents_path=None):
    path = Path(agents_path).expanduser() if agents_path is not None else Path.home() / '.config' / 'atelier' / 'agents.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        return None
    entry = (data.get('runners') or {}).get(runner) if isinstance(data, dict) else None
    cli = entry.get('path') if isinstance(entry, dict) else None
    return [cli] if isinstance(cli, str) and cli.strip() else None


def launch_spec(role, step, prompt_path, result_path, root, agents_path=None):
    runner = step['runner']
    prompt, result, root = map(lambda p: str(Path(p).resolve()), (prompt_path, result_path, root))
    model, effort = step.get('model', ''), step.get('effort')
    if runner == 'agent':
        return {'runner': runner, 'role': role, 'model': model, 'effort': effort,
                'prompt': prompt, 'result': result}
    if runner not in ('codex', 'opencode', 'agy', 'dsh', 'claude'):
        raise ValueError('unknown runner: ' + runner)
    configured = os.environ.get('ATELIER_' + runner.upper() + '_CMD')
    command = json.loads(configured) if configured else (_registered_command(runner, agents_path) or [runner])
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError('runner command must be a nonempty JSON string array')
    task = f'Read {prompt} and follow its instructions. Write the result to {result}.'
    base = Path(result)
    config = str(base) + '.' + runner + '.config.json'
    last = str(base) + '.last.md'
    if runner == 'codex':
        args = ['-a', 'never', 'exec', '--json', '-s', 'workspace-write',
                '-c', 'sandbox_workspace_write.network_access=false',
                '-c', 'sandbox_workspace_write.writable_roots=[]']
        if os.name == 'nt':
            args += ['-c', 'windows.sandbox="elevated"', '-c', 'features.prefer_mxc=true']
        if model:
            args += ['-m', model]
        if effort:
            args += ['-c', 'model_reasoning_effort=' + json.dumps(effort)]
        args += ['-o', last, '-']
    elif runner == 'agy':
        args = ['-p', task, '--model', model, '--mode', 'accept-edits', '--settings', config, '--output-format', 'json']
    elif runner == 'opencode':
        args = ['run', '-m', model + ('#' + effort if effort else ''), '--format', 'json', task]
    elif runner == 'dsh':
        args = ['--profile', 'headless', '--patch', str(base) + '.patch.yml', '--json', task]
    else:
        args = ['-p', '--output-format', 'json', '--model', model, '--tools', 'Read,Edit,Write,Glob,Grep', '--permission-mode', 'acceptEdits', '--permission-prompts', 'none', '--strict-mcp-config', '--restricted']
        if effort:
            args += ['--effort', effort]
    spec = Command(command + args)
    spec.runner, spec.root, spec.prompt, spec.result = runner, root, task, result
    spec.config, spec.last, spec.model, spec.effort = config, last, model, effort
    return spec


def _config(runner):
    verbs = 'commit|push|reset|rebase|checkout|switch|restore|stash|clean|merge|cherry-pick|tag|worktree'
    if runner == 'agy':
        return {'permissions': {'allow': ['command(*)', 'read_file(*)'], 'deny': [
            f'command(regex:(.*[^A-Za-z0-9_-])?git +({verbs})( .*)?)',
            'command(regex:.*rm +-[A-Za-z]*r[A-Za-z]*f.*)',
            'command(regex:.*rm +-[A-Za-z]*f[A-Za-z]*r.*)',
            'command(regex:.*Remove-Item +.*-Recurse.*)',
            'command(regex:.*(npm|pnpm|yarn|pip|pip3) +(install|add).*)']}}
    denied = {f'git {verb}*': 'deny' for verb in verbs.split('|')}
    denied.update({x: 'deny' for x in ('rm -r*', 'rm -rf*', 'Remove-Item *-Recurse*', 'npm install*', 'npm i *', 'pnpm add*', 'yarn add*', 'pip install*')})
    return {'permission': {'edit': 'allow', 'bash': {'*': 'allow', **denied}, 'external_directory': 'deny'}}


def _events(text):
    try:
        obj = json.loads(text)
        return [obj] if isinstance(obj, dict) else []
    except ValueError:
        events = []
        for line in text.splitlines():
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    events.append(obj)
            except ValueError:
                pass
        return events


def _usage(events, runner):
    tokens, cost = 'unknown', 'unknown'
    for event in events:
        usage = event.get('usage') or (event.get('part') or {}).get('tokens') or event.get('tokens')
        if runner == 'claude' and isinstance(event.get('modelUsage'), dict):
            entries = list(event['modelUsage'].values())
            if entries and all(isinstance(e, dict) and 'inputTokens' in e and 'outputTokens' in e for e in entries):
                usage = {'input': sum(e['inputTokens'] for e in entries), 'output': sum(e['outputTokens'] for e in entries)}
        if isinstance(usage, dict):
            inp = usage.get('input_tokens', usage.get('inputTokens', usage.get('input')))
            out = usage.get('output_tokens', usage.get('outputTokens', usage.get('output')))
            total = usage.get('total_tokens', usage.get('totalTokens', usage.get('total')))
            if isinstance(inp, int) and isinstance(out, int):
                tokens = {'input': inp, 'output': out, 'total': total if isinstance(total, int) else inp + out}
        value = event.get('total_cost_usd', event.get('cost', (event.get('part') or {}).get('cost')))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            cost = value
    return tokens, cost


def run_external(spec, timeout):
    if not isinstance(spec, Command):
        raise TypeError('run_external requires the Command returned by launch_spec')
    result = Path(spec.result)
    result.parent.mkdir(parents=True, exist_ok=True)
    stdout, stderr = Path(str(result) + '.stdout'), Path(str(result) + '.stderr')
    env = dict(os.environ)
    if spec.runner in ('agy', 'opencode'):
        Path(spec.config).write_text(json.dumps(_config(spec.runner)), encoding='utf-8')
        if spec.runner == 'opencode':
            env['OPENCODE_CONFIG'] = spec.config
    if spec.runner == 'dsh':
        provider, sep, model = spec.model.partition('/')
        if not sep or not re.fullmatch(r'[A-Za-z0-9._-]+', provider) or not re.fullmatch(r'[A-Za-z0-9._-]+', model):
            raise ValueError('dsh model must be provider/model')
        patch = [{'id': 'agent-default-model', 'config': {'provider': provider, 'model': model}}]
        if spec.effort:
            patch[0]['config']['reasoningEffort'] = spec.effort
        Path(str(result) + '.patch.yml').write_text(json.dumps(patch), encoding='utf-8')
        env['DSH_PERMISSION_MODE'] = 'workspace-write'
    if spec.runner == 'claude':
        for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_BEDROCK_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK'):
            env.pop(key, None)
    reason, code = None, None
    last_before = None
    if spec.runner == 'codex' and Path(spec.last).exists():
        stat = Path(spec.last).stat()
        last_before = (stat.st_mtime_ns, stat.st_size)
    try:
        with stdout.open('wb') as out, stderr.open('wb') as err:
            proc = subprocess.run(spec, cwd=spec.root, input=spec.prompt.encode('utf-8') if spec.runner in ('codex', 'claude') else None,
                                  stdin=None if spec.runner in ('codex', 'claude') else subprocess.DEVNULL,
                                  stdout=out, stderr=err, env=env, timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            code = proc.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = str(exc)
    text = stdout.read_text(encoding='utf-8', errors='replace') if stdout.exists() else ''
    errors = stderr.read_text(encoding='utf-8', errors='replace') if stderr.exists() else ''
    events = _events(text)
    tokens, cost = _usage(events, spec.runner)
    if not reason and code != 0:
        reason = f'exit code {code}'
    if not reason and re.search(r'\b(quota|rate.?limit|unauthorized|authentication|invalid.api.key|permission denied|permission rejected|permission rejection)\b', text + '\n' + errors, re.I):
        reason = 'runner reported quota, authentication, or permission failure'
    if not reason and any(e.get('denied_actions') or e.get('type') in ('error', 'turn.failed') or e.get('is_error') for e in events):
        reason = 'runner reported an error or denied action'
    if not reason:
        if spec.runner == 'codex':
            path = Path(spec.last)
            stat = path.stat() if path.is_file() else None
            present = bool(stat and (stat.st_mtime_ns, stat.st_size) != last_before and path.read_text(encoding='utf-8').strip())
        elif spec.runner == 'dsh':
            present = any(e.get('type') == 'final' and (e.get('text') or e.get('message')) for e in events)
        elif spec.runner == 'opencode':
            present = any(e.get('type') == 'text' and (e.get('part') or {}).get('text') for e in events)
        else:
            present = any(e.get('result') or e.get('response') or e.get('text') for e in events)
        if not present:
            reason = 'missing last message'
    return {'exit_code': code, 'stdout_path': str(stdout), 'stderr_path': str(stderr), 'tokens': tokens, 'cost': cost, 'unusable_reason': reason}
