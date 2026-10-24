#!/usr/bin/env node
// Claude Code の PreToolUse フック (matcher: Bash|PowerShell)。
// rtk モードが有効なセッションだけ、コマンドを `rtk <cmd>` に書き換えて出力を圧縮する。
// 有効かどうかは ~/.claude/state/rtk/<session_id>.on の有無で決める (rtk-mode.mjs on|off で切り替える)。
// 無効・rtk が PATH に無い・rtk が失敗したときは何も出さずに素通し (exit 0)。許可の判断はしない。

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const FLAG_DIR = path.join(os.homedir(), ".claude", "state", "rtk");

try {
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  if (!["Bash", "PowerShell"].includes(input.tool_name)) process.exit(0);
  const sid = String(input.session_id || "");
  if (!/^[\w-]+$/.test(sid) || !fs.existsSync(path.join(FLAG_DIR, `${sid}.on`))) process.exit(0);

  // rtk grep では -h がヘルプの意味になり、grep の結果の代わりに rtk のヘルプが出る。
  // grep / rg に -h (短いオプションの束に h を含むもの) があるコマンドは書き換えない
  const command = String(input.tool_input?.command || "");
  if (/(^|[\s;&|(])(grep|egrep|fgrep|rg)\b[^;&|\n]*\s-[A-Za-z]*h/.test(command)) process.exit(0);

  // rtk hook claude は Bash の形の入力を受ける。PowerShell も同じ形で渡し、書き換え後の command だけ使う
  const payload = JSON.stringify({ ...input, tool_name: "Bash" });
  // rtk を名前で起動する: フックと Bash / PowerShell ツールは同じ PATH なので、ここで見つからなければ書き換えない
  const r = spawnSync("rtk", ["hook", "claude"], { input: payload, encoding: "utf8", timeout: 5000, windowsHide: true });
  if (r.error || r.status !== 0 || !r.stdout.trim()) process.exit(0);

  const cmd = JSON.parse(r.stdout)?.hookSpecificOutput?.updatedInput?.command;
  if (typeof cmd !== "string" || !cmd.trim()) process.exit(0);
  // command 以外の入力 (timeout・run_in_background など) はそのまま残す
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecisionReason: "rtk モード (atelier)",
      updatedInput: { ...input.tool_input, command: cmd },
    },
  }));
} catch {
  process.exit(0);
}
