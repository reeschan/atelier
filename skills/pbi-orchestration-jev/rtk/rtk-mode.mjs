#!/usr/bin/env node
// rtk モードをセッション単位で切り替える。rtk-gate.mjs がこのフラグを見て、Bash / PowerShell のコマンドを rtk に書き換える。
//   node rtk-mode.mjs on      # このセッションで有効にする
//   node rtk-mode.mjs off     # 無効にする
//   node rtk-mode.mjs status  # 状態と rtk の有無を表示
// セッションは環境変数 CLAUDE_CODE_SESSION_ID (--session <id> で上書き可)。on のたびに 3 日より古いフラグを消す。

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const FLAG_DIR = path.join(os.homedir(), ".claude", "state", "rtk");
const MAX_AGE_MS = 3 * 24 * 60 * 60 * 1000;

const args = process.argv.slice(2);
const action = args[0];
const si = args.indexOf("--session");
const sid = si >= 0 ? args[si + 1] : process.env.CLAUDE_CODE_SESSION_ID;

if (!["on", "off", "status"].includes(action)) {
  console.error("使い方: node rtk-mode.mjs on|off|status [--session <id>]");
  process.exit(2);
}
if (!sid || !/^[\w-]+$/.test(sid)) {
  console.error("セッション ID が分からない (CLAUDE_CODE_SESSION_ID が無い)。--session <id> で渡す");
  process.exit(1);
}

const flag = path.join(FLAG_DIR, `${sid}.on`);

function rtkVersion() {
  const r = spawnSync("rtk", ["--version"], { encoding: "utf8", windowsHide: true });
  return r.error || r.status !== 0 ? null : r.stdout.trim();
}

function prune() {
  if (!fs.existsSync(FLAG_DIR)) return;
  for (const name of fs.readdirSync(FLAG_DIR)) {
    const p = path.join(FLAG_DIR, name);
    try { if (Date.now() - fs.statSync(p).mtimeMs > MAX_AGE_MS) fs.unlinkSync(p); } catch {}
  }
}

const version = rtkVersion();
if (action === "on") {
  prune();
  fs.mkdirSync(FLAG_DIR, { recursive: true });
  fs.writeFileSync(flag, JSON.stringify({ on_at: new Date().toISOString(), cwd: process.cwd() }) + "\n");
} else if (action === "off") {
  try { fs.unlinkSync(flag); } catch {}
}

const on = fs.existsSync(flag);
console.log(`rtk モード: ${on ? "有効" : "無効"} (session ${sid})`);
console.log(version ? `rtk: ${version}` : "rtk: PATH に見つからない (書き換えは起きない。アプリを再起動して PATH を読み直す)");
