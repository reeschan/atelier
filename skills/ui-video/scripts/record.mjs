#!/usr/bin/env node
// Record a short walkthrough video of UI pages from a storyboard (SKILL.md).
//
//   node record.mjs <storyboard.json> [--out FILE] [--dry-run]
//
// The storyboard lists scenes. Each scene opens a page (a local HTML file or a URL), shows an
// optional caption, runs a few steps (wait, scroll, hover, click, type) and holds. Playwright
// records the browser as WebM; when ffmpeg is on PATH the result is converted to MP4 (H.264).
//
// Exit codes: 0 = written, 2 = bad storyboard or arguments, 3 = Playwright not installed,
// 4 = recording failed.
import { execFileSync, spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, extname, isAbsolute, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

export const LIMITS = { scenes: 20, steps: 30, holdMs: 10000, waitMs: 10000, captionChars: 140 };
const STEP_KINDS = ['wait', 'scroll', 'hover', 'click', 'type'];

// Returns the list of problems in a storyboard (empty when it is valid).
export function storyboardErrors(board) {
  const errors = [];
  if (!board || typeof board !== 'object' || Array.isArray(board)) return ['the storyboard must be a JSON object'];
  const vp = board.viewport ?? { width: 1280, height: 800 };
  if (!Number.isInteger(vp.width) || !Number.isInteger(vp.height) || vp.width < 320 || vp.height < 320 ||
      vp.width > 3840 || vp.height > 2160) {
    errors.push('viewport needs integer width and height from 320 to 3840x2160');
  }
  if (board.out !== undefined && (typeof board.out !== 'string' || !board.out)) errors.push('out must be a file path');
  if (!Array.isArray(board.scenes) || board.scenes.length === 0) return errors.concat('scenes must be a non-empty list');
  if (board.scenes.length > LIMITS.scenes) errors.push(`at most ${LIMITS.scenes} scenes`);
  board.scenes.forEach((scene, i) => {
    const at = `scene ${i + 1}`;
    if (!scene || typeof scene !== 'object') { errors.push(`${at}: must be an object`); return; }
    if (typeof scene.page !== 'string' || !scene.page) errors.push(`${at}: page must be a file path or a URL`);
    if (scene.caption !== undefined && (typeof scene.caption !== 'string' || scene.caption.length > LIMITS.captionChars)) {
      errors.push(`${at}: caption must be text of at most ${LIMITS.captionChars} characters`);
    }
    if (scene.hold !== undefined && !(Number.isInteger(scene.hold) && scene.hold >= 0 && scene.hold <= LIMITS.holdMs)) {
      errors.push(`${at}: hold must be 0 to ${LIMITS.holdMs} ms`);
    }
    const steps = scene.steps ?? [];
    if (!Array.isArray(steps) || steps.length > LIMITS.steps) { errors.push(`${at}: steps must be a list of at most ${LIMITS.steps}`); return; }
    steps.forEach((step, j) => {
      const kind = step && typeof step === 'object' ? Object.keys(step) : [];
      const where = `${at} step ${j + 1}`;
      if (kind.length !== 1 || !STEP_KINDS.includes(kind[0])) { errors.push(`${where}: one of ${STEP_KINDS.join(', ')}`); return; }
      const value = step[kind[0]];
      if (kind[0] === 'wait' && !(Number.isInteger(value) && value >= 0 && value <= LIMITS.waitMs)) errors.push(`${where}: wait is 0 to ${LIMITS.waitMs} ms`);
      if (kind[0] === 'scroll' && !Number.isInteger(value)) errors.push(`${where}: scroll is a pixel count`);
      if ((kind[0] === 'hover' || kind[0] === 'click') && (typeof value !== 'string' || !value)) errors.push(`${where}: ${kind[0]} takes a CSS selector`);
      if (kind[0] === 'type' && !(Array.isArray(value) && value.length === 2 && value.every((v) => typeof v === 'string') && value[0])) {
        errors.push(`${where}: type takes [selector, text]`);
      }
    });
  });
  return errors;
}

// A scene's page as a URL: http(s) and file URLs as they are, anything else as a path from the storyboard's folder.
export function pageUrl(page, baseDir) {
  if (/^(https?|file):\/\//i.test(page)) return page;
  return pathToFileURL(isAbsolute(page) ? page : resolve(baseDir, page)).href;
}

// The output file: --out, then the storyboard's out, then walkthrough.mp4. Only .mp4 and .webm are written.
export function outputPath(board, args, baseDir) {
  const out = args.out ?? board.out ?? 'walkthrough.mp4';
  if (!['.mp4', '.webm'].includes(extname(out).toLowerCase())) throw new Error(`${out}: the output must end in .mp4 or .webm`);
  return isAbsolute(out) ? out : resolve(baseDir, out);
}

function parseArgs(argv) {
  const args = { file: null, out: undefined, dryRun: false };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--out') {
      const value = argv[++i];
      if (!value || value.startsWith('--')) return null;
      args.out = value;
    }
    else if (argv[i] === '--dry-run') args.dryRun = true;
    else if (!args.file) args.file = argv[i];
    else return null;
  }
  return args.file ? args : null;
}

// Playwright from the current project, then from the global npm folder.
async function loadPlaywright() {
  try { return await import('playwright'); } catch { /* try the global install */ }
  try {
    const root = execFileSync(process.platform === 'win32' ? 'npm.cmd' : 'npm', ['root', '-g'], { encoding: 'utf8', shell: process.platform === 'win32' }).trim();
    return createRequire(join(root, 'noop.js'))('playwright');
  } catch { return null; }
}

function hasFfmpeg() {
  return spawnSync('ffmpeg', ['-version'], { stdio: 'ignore' }).status === 0;
}

async function showCaption(page, text) {
  await page.evaluate((caption) => {
    let bar = document.getElementById('__ui_video_caption');
    if (!caption) { if (bar) bar.remove(); return; }
    if (!bar) {
      bar = document.createElement('div');
      bar.id = '__ui_video_caption';
      bar.style.cssText = 'position:fixed;left:50%;bottom:32px;transform:translateX(-50%);max-width:80%;' +
        'padding:12px 20px;border-radius:12px;background:rgba(17,17,17,.82);color:#fff;font:600 20px/1.4 system-ui,sans-serif;' +
        'z-index:2147483647;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,.25);pointer-events:none';
      document.body.appendChild(bar);
    }
    bar.textContent = caption;
  }, text ?? '');
}

async function runStep(page, step) {
  const [kind] = Object.keys(step);
  const value = step[kind];
  if (kind === 'wait') await page.waitForTimeout(value);
  if (kind === 'scroll') {
    await page.evaluate((dy) => window.scrollBy({ top: dy, behavior: 'smooth' }), value);
    await page.waitForTimeout(700);
  }
  if (kind === 'hover') await page.hover(value, { timeout: 5000 });
  if (kind === 'click') await page.click(value, { timeout: 5000 });
  if (kind === 'type') await page.type(value[0], value[1], { delay: 60, timeout: 5000 });
  if (kind !== 'wait') await page.waitForTimeout(400);
}

async function main(argv) {
  const args = parseArgs(argv);
  if (!args) { console.error('usage: node record.mjs <storyboard.json> [--out FILE] [--dry-run]'); return 2; }
  let board;
  try { board = JSON.parse(readFileSync(args.file, 'utf8')); } catch (e) { console.error(`${args.file}: ${e.message}`); return 2; }
  const errors = storyboardErrors(board);
  if (errors.length) { errors.forEach((e) => console.log(`error ${e}`)); return 2; }
  const baseDir = dirname(resolve(args.file));
  let out;
  try { out = outputPath(board, args, baseDir); } catch (e) { console.log(`error ${e.message}`); return 2; }
  const missing = board.scenes.map((s) => s.page).filter((p) => !/^(https?|file):\/\//i.test(p))
    .filter((p) => !existsSync(isAbsolute(p) ? p : resolve(baseDir, p)));
  if (missing.length) { missing.forEach((p) => console.log(`error page not found: ${p}`)); return 2; }
  const ffmpeg = hasFfmpeg();
  const finalOut = extname(out).toLowerCase() === '.mp4' && !ffmpeg ? out.replace(/\.mp4$/i, '.webm') : out;
  if (args.dryRun) { console.log(`ok ${board.scenes.length} scenes -> ${finalOut}${ffmpeg ? '' : ' (no ffmpeg: WebM)'}`); return 0; }

  const playwright = await loadPlaywright();
  if (!playwright) {
    console.error('Playwright is not installed. Run: npm install -g playwright && npx playwright install chromium');
    return 3;
  }
  const viewport = board.viewport ?? { width: 1280, height: 800 };
  const work = mkdtempSync(join(tmpdir(), 'ui-video-'));
  let browser;
  try {
    browser = await playwright.chromium.launch();
    const context = await browser.newContext({ viewport, recordVideo: { dir: work, size: viewport } });
    const page = await context.newPage();
    for (const scene of board.scenes) {
      await page.goto(pageUrl(scene.page, baseDir), { waitUntil: 'load', timeout: 30000 });
      await page.waitForTimeout(300);
      await showCaption(page, scene.caption);
      for (const step of scene.steps ?? []) {
        await runStep(page, step);
        // a click can open another page, which drops the caption; put it back
        await showCaption(page, scene.caption);
      }
      await page.waitForTimeout(scene.hold ?? 1500);
    }
    await context.close();
    const webm = readdirSync(work).find((f) => f.endsWith('.webm'));
    if (!webm) throw new Error('no video was recorded');
    mkdirSync(dirname(finalOut), { recursive: true });
    if (extname(finalOut).toLowerCase() === '.mp4') {
      const r = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-i', join(work, webm), '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart', finalOut], { stdio: 'inherit' });
      if (r.status !== 0) throw new Error('ffmpeg failed');
    } else {
      copyFileSync(join(work, webm), finalOut);
    }
    console.log(`written ${finalOut}`);
    return 0;
  } catch (e) {
    console.error(`recording failed: ${e.message}`);
    return 4;
  } finally {
    if (browser) await browser.close().catch(() => {});
    rmSync(work, { recursive: true, force: true });
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  process.exitCode = await main(process.argv.slice(2));
}
