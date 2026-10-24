import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { outputPath, pageUrl, storyboardErrors } from './record.mjs';

const SCRIPT = fileURLToPath(new URL('./record.mjs', import.meta.url));

test('a valid storyboard has no errors', () => {
  const board = {
    viewport: { width: 1280, height: 800 },
    scenes: [
      { page: 'page.html', caption: 'Settings page', steps: [{ wait: 500 }, { scroll: 600 }, { hover: '.btn' }], hold: 1200 },
      { page: 'https://example.com', steps: [{ click: 'a' }, { type: ['input', 'hello'] }] },
    ],
  };
  assert.deepEqual(storyboardErrors(board), []);
});

test('storyboard errors name the scene and step', () => {
  assert.deepEqual(storyboardErrors([]), ['the storyboard must be a JSON object']);
  assert.deepEqual(storyboardErrors({}), ['scenes must be a non-empty list']);
  const errors = storyboardErrors({
    viewport: { width: 100, height: 800 },
    scenes: [{ caption: 3, hold: -1, steps: [{ jump: 1 }, { wait: 20000 }, { type: ['input'] }] }],
  });
  assert.deepEqual(errors, [
    'viewport needs integer width and height from 320 to 3840x2160',
    'scene 1: page must be a file path or a URL',
    'scene 1: caption must be text of at most 140 characters',
    'scene 1: hold must be 0 to 10000 ms',
    'scene 1 step 1: one of wait, scroll, hover, click, type',
    'scene 1 step 2: wait is 0 to 10000 ms',
    'scene 1 step 3: type takes [selector, text]',
  ]);
});

test('pages and the output resolve from the storyboard folder', () => {
  const base = resolve('/work/docs/pbi/ui/settings');
  assert.equal(pageUrl('https://example.com/a', base), 'https://example.com/a');
  assert.ok(pageUrl('mock.html', base).endsWith('/work/docs/pbi/ui/settings/mock.html'));
  assert.equal(outputPath({}, {}, base), resolve(base, 'walkthrough.mp4'));
  assert.equal(outputPath({ out: 'a.mp4' }, { out: 'b.webm' }, base), resolve(base, 'b.webm'));
  assert.throws(() => outputPath({ out: 'a.gif' }, {}, base), /must end in .mp4 or .webm/);
  assert.throws(() => outputPath({}, { out: 'clip' }, base), /must end in .mp4 or .webm/);
});

test('the CLI checks the storyboard and pages without launching a browser', () => {
  const dir = mkdtempSync(join(tmpdir(), 'ui-video-test-'));
  writeFileSync(join(dir, 'page.html'), '<h1>hi</h1>');
  writeFileSync(join(dir, 'ok.json'), JSON.stringify({ scenes: [{ page: 'page.html' }] }));
  writeFileSync(join(dir, 'missing.json'), JSON.stringify({ scenes: [{ page: 'nope.html' }] }));
  const ok = spawnSync(process.execPath, [SCRIPT, join(dir, 'ok.json'), '--dry-run'], { encoding: 'utf8' });
  assert.equal(ok.status, 0, ok.stderr);
  assert.match(ok.stdout, /^ok 1 scenes -> /);
  const missing = spawnSync(process.execPath, [SCRIPT, join(dir, 'missing.json'), '--dry-run'], { encoding: 'utf8' });
  assert.equal(missing.status, 2);
  assert.match(missing.stdout, /error page not found: nope.html/);
  const usage = spawnSync(process.execPath, [SCRIPT], { encoding: 'utf8' });
  assert.equal(usage.status, 2);
  // --out needs a value, and never takes the next option as one
  for (const extra of [['--out'], ['--out', '--dry-run'], ['--out', '']]) {
    const bad = spawnSync(process.execPath, [SCRIPT, join(dir, 'ok.json'), ...extra], { encoding: 'utf8' });
    assert.equal(bad.status, 2, extra.join(' '));
  }
});
