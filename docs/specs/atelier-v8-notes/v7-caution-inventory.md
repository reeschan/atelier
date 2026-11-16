# atelier v7.15 の「注意書き」棚卸し

対象: `~/.claude/skills/atelier/versions/v7.15` の SKILL.md・designer.md・worker.md・CONTEXT.md・runners.md・appendix/*.md・episode/*.md・prompts/*.md・agents/*.md (約 3,400 行)。
由来は `~/.claude/skills/atelier/CHANGELOG.md` と同梱の `docs/specs/*.md` の Problem Statement から拾った。
件数はおおよその数 (never / do not / always / refuse などを含む規則の行を手で数えた)。全体で約 200 件。

判定 (仮。決めるのはユーザー):
- **A**: どう作り直しても要る。データの消失・リポジトリの外への書き込み・秘密や外部送信・ユーザーの承認を守る規則。
- **B**: v7 の仕組み (caller / conductor / designer の 3 層、毎回新しい設計者、Kiln、外部 runner、tests-at-end など) があるから要る規則。設計を簡単にすれば消える。
- **C**: 古い・ほかの規則と重複・スクリプトがすでに強制していて文章が要らない。

## 事件と版の対応 (CHANGELOG と specs から)

| 事件 | 版 | 生まれた規則の主な系統 |
|---|---|---|
| W-004 wave 3: 作業者が test:unit を 19〜96 回流し、背景のテストを待ったまま報告せず終了 | v3 | テストの暴走、前景で待つ |
| jevlint の外部送信 (方針として) | v4 | 送信の承認、作業者は jevlint を流さない |
| fnm/nvm で jevlint が見つからない | v7.1 (#38) | 道具の有無は flow.py/tools_check.py で決める |
| P-102: Codex の設計者が Windows で書いた日本語が `?` に化けた (PowerShell 5.1 の管) | v7.4→v7.5 (#43) | 文字化け、Writing files 行、warn garbled |
| run 0eb7ab1f: 承認ページが読みにくい / 2026-11-11 に caller が markdown を通さずに描いた | v7.6 (#44)・v7.11 (#55, #58) | 問いの形、present.py の強制、パイプを組むな |
| P-109: WSL の bash、E2E の流しすぎ、`pnpm -- --spec`、grill に設計者が自分で答えた、REPORT.md が書けない | v7.7 (#48) | Windows のシェル、E2E、grill は全部ユーザー、report_body |
| P-103: 最終レビューの差し戻しが 6 回終わらない / 設計者 1.37 億トークン / auto mode が codex 起動を 3 回拒否 | v7.13 (#62)・v7.14 (#63, #64)・v7.15 (#65) | レビュー 1 回、fix の出番、codex サンドボックス、Read first |
| agy の headless がコマンドを自動で拒否 | v7.10 (#52) | agy.settings.json と既知の問題 |

指定の例のうち「a stray file `error` was removed」は v7.15 の指示ファイルには見つからなかった (別の版か docs 側の話の可能性)。

---

## 1. 文字化け (UTF-8)  約 5 件  [A 1 / B 4 / C 0]

- SKILL.md:103 「`warn garbled:` means a file or summary lost its non-ASCII text (`???`)」
- SKILL.md:104-105 approve なら `--revise` で「Rewrite ... as UTF-8 and show the approval again」、ほかは「Do not decide yet」
- runners.md:117 codex の Writing files 行 (「Windows PowerShell 5.1 sends text piped ... as US-ASCII」)
- 由来: P-102、v7.5 (PR #43)。化けの再現は CHANGELOG:135。
- 判定: 化けの原因は codex+Windows PowerShell の管という v7 固有の経路 → **B**。ただし「読めない承認のまま承認させない」は承認を守る規則なので、形を変えて **A** で 1 件残す価値あり。

## 2. 呼び出し側 (caller) の越権  約 10 件  [A 1 / B 7 / C 2]

- SKILL.md:13 「Do not read the PBI, the spec or the diff.」
- SKILL.md:83 「Never build the pipe yourself (`design_record.py markdown --draft ... | am render -`)」
- SKILL.md:55-58 running 中は起動しない、「a second caller may own the episode」
- SKILL.md:75 `then:`・`format:`・`log:`・`page:` を見せない / :68 `# relaunch` の行をユーザーに見せない
- 由来: v7 (3 層化) と 2026-11-11 に caller が照合なしのページを出した件 (docs/specs/present-with-am.md)、v7.11 (#55)。
- 判定: 3 層の分担そのものの規則なので **B**。パイプの禁止は `answer` が shown の記録なしを拒否 (appendix/conductor.md「not shown」) するので文章は **C** 寄り。「Run `--force` only when the user says to」(SKILL.md:33) は未完了の run を潰さないので **A**。

## 3. 再開・二重起動・Re-entry  約 18 件  [A 3 / B 10 / C 5]

- designer.md:60 「Never redo finished steps.」、:62 「it is never the approval's answer, so do not treat it as one」、:63-64 approve の答えで understand から route をやり直さない / stale の再承認の扱い、:67 「never runs a second final review」、:68 「Never record again an attempt already in runs.jsonl」
- prompts/designer-episode.md:27-28、episode/_kiln.md:63、appendix/kiln.md:42、appendix/fix.md:54 (同じ趣旨の繰り返し)
- appendix/episodes.md (## Episode result) 結果のパスに run id を入れる理由「a designer of an earlier run (even one `start --force` could not stop) cannot answer for a new one」
- appendix/handoff.md:55 「Never pick the newest yourself.」、:58 ブランチが合わなければ止めて聞く
- 由来: v7 (出番ごとに新しい設計者)、v7.9 (stale)、v7.13 (fix の出番)、v7.2 (ゾンビの子、コード側)。
- 判定: 文脈を持たない設計者を毎回起こすから必要な規則 → **B**。二重記録・合格済み PBI のやり直し禁止 (費用の二重払い) と handoff の取り違え (別のリポジトリを触る) は **A**。designer.md と designer-episode.md と _kiln.md と kiln.md に同じ「やり直すな」が 4 回ある → **C**。

## 4. 外部 CLI の癖 (opencode / codex / agy / dsh / claude / mcp)  約 30 件  [A 5 / B 22 / C 3]

- runners.md:93 opencode「A denial still exits 0.」、:89 `database is locked`、:50「start each one 30 seconds after the previous」
- runners.md:129 codex の MXC / `elevated` / `spawn EPERM` / pnpm が見つからない、:114「never above `high`」
- runners.md:154-158 agy: `--settings` なしだと自動拒否、`\s` `\b` が使えない、「`rg foo` waits on standard input」
- runners.md:188-189 dsh: named pipes、子プロセスを起こせない
- runners.md:264 `claude -p` が 403 `oauth_not_allowed_for_organization`
- 由来: 2026-10 の確認 (opencode 2.0.x、codex 0.153、dsh 0.2.0-rc)、v7.10 (#52 agy)、v7.14 (#63 codex)。
- 判定: CLI の版に縛られた知識なので **B** (作り直すならスクリプトと試験に入れて文章から外す)。A は「never point `CODEX_HOME` ... at a folder of your own」(:21)、opencode の deny 設定と「Never pass `--auto`」(:79)、jev-mcp の「never expose it to other machines」(:237)、「Never continue an earlier conversation」(:23、隔離)。`--skip` と runner_skip.py の違いの説明 (:49) と :15 の重複は **C**。

## 5. サンドボックス・権限・リポジトリの外への書き込み  約 15 件  [A 11 / B 3 / C 1]

- runners.md:112「Never widen the sandbox on your own, and never get round a refused launch by changing its form; show the refusal to the user.」
- SKILL.md:66 auto mode が `run --detach` を拒んだら、拒否を見せて許可か手動実行を頼む
- runners.md:28「Work here and write nothing outside it.」、agents/atelier-designer.md:16「Do not edit files outside the work root」
- designer.md:19 / appendix/light.md:23 破壊的操作 (データ削除・force push・リポジトリの外の変更) の前に止まる
- runners.md:256 role の `--sandbox danger-full-access` は「only when the user asks」、`read-only` は拒否
- 由来: v7.4 (P-102 の試作で workspace-write が動かず danger-full-access に)、v7.14 (#63、P-103 で auto mode が 3 回拒否)。
- 判定: ほぼ **A**。codex 設計者だけ danger-full-access になる理由の説明 (appendix/conductor.md:10、appendix/episodes.md:20 の同文) は **B**、2 か所の同文は **C**。

## 6. 秘密・外部への送信  約 12 件  [A 11 / B 1 / C 0]

- appendix/jev.md:21「Never source code, diffs, environment variables, credentials or customer data.」
- designer.md:20「Right before jevlint sends file contents to an outside model, every time」(appendix/lint.md の send approval)
- worker.md:19「Never run jevlint (or any tool that sends file contents to an outside model).」
- appendix/events.md:69「Do not put prose, a path, a prompt or a secret in `data`」
- runners.md:174 鍵の値は見せない、:207 claude_run が API キーを子に渡さない、:60 と CONTEXT.md:154 sensitive な PBI を外部 runner や無料モデルに送らない、appendix/priority-templates.md:24 ページに個人データを埋めない
- 由来: v4 (jevlint 送信の承認、sends の記録)、v7.7 (#46 events)。
- 判定: **A**。jevlint 固有の手順 (記録のハッシュなど) だけ **B**。

## 7. インストール・設定変更の承認  約 9 件  [A 7 / B 1 / C 1]

- appendix/setup.md:148「Downloads and global installs need that yes; never install on your own.」
- appendix/map.md:24「never run `npm install`, `omm view`, a commit, or anything that leaves `.omm/` in the working tree」
- appendix/kiln.md:27「Never install it yourself.」、appendix/setup.md:128 rtk の settings.json は見せてから承認後に書く
- runners.md:113 sandbox で `pnpm add` が失敗 → 作業者は Open に書き、設計者が入れる
- 由来: v4 (推奨の道具)、v7.3 (OMM)、v7.14 (codex sandbox)。
- 判定: **A**。:113 の「あなたが入れる」は codex sandbox 由来 → **B**。setup.md・kiln.md・adapters.md:36 で同じ趣旨が 3 回 → 1 件 **C**。

## 8. 質問の形・ユーザーに聞くこと  約 18 件  [A 4 / B 9 / C 5]

- designer.md:15「You never ask the user.」(question の結果で止まる)、:32「Ask the user only what changes what they get, what it costs, how long it takes or what can go wrong」
- appendix/design.md:35 grill は「the user answers every question of the design tree, and you answer none of them」、agents/atelier-designer.md:12「never record an answer the user did not give」
- appendix/questions.md:7・:51 見出しの順と「Every `ask` sits inside `## 決めること`」、:22「never function names」、appendix/approve.md:19 stage record は draft に入れない
- prompts/final-review.md:18「If the skill asks you a question, do not answer it. Stop and write BLOCKED.」
- 由来: v7.6 (#44、run 0eb7ab1f)、v7.7 (#48、P-109 で設計者が grill に自分で答えた)、v7.11 (#58)、v7.15 (#65 問いをまとめる)。
- 判定: 「ユーザーの答えを捏造しない」「grill は全部ユーザー」「聞く範囲の基準」は **A**。見出し・ask の置き場所は `design_record.py lint` と `done` が強制する (appendix/questions.md:66) ので文章は **C**、ほかの形の決まりは **B**。designer.md:32・approve.md:30・questions.md:55 が同文 → **C**。

## 9. 道具の有無・PATH・Jev の判定  約 9 件  [A 0 / B 6 / C 3]

- designer.md:41「never from STATUS.md, HANDOFF.md, memory, or a bare `jevlint` / `which` / `Get-Command` in the shell」
- appendix/lint.md:13「Never call `jevlint` (or `which jevlint`) in the shell to see whether it is there」
- designer.md:42 / appendix/handoff.md:59 Jev の到達性は今回の run の確認だけ、古い行は証拠にしない
- runners.md:62「`which opencode` failing does not prove it is missing」
- 由来: v7.1 (#38 fnm/nvm)、v7.11 (#56 Jev の診断)。
- 判定: **B** (fnm/nvm 前提とスクリプトの存在に依る)。designer.md・lint.md・handoff.md・engine.md の繰り返しは **C**。

## 10. Windows・シェル固有  約 12 件  [A 0 / B 8 / C 4]

- worker.md:21「the Bash tool fails with `unexpected EOF` when one command has both a `<` redirect inside `$( )` and `$'...'`」(appendix/plan.md:63 と同文。この報告を書くときにも実際に踏んだ)
- appendix/plan.md:62 `MSYS_NO_PATHCONV=1`、:64 `/tmp` が `C:\tmp` になる、:66 dev server が `&` と `kill` で止まらない、:67 `pnpm run <script> -- --spec x` で絞り込みが外れる、:68 WSL の bash.exe を使わない
- runners.md:44「a cd through a variable stops at Claude Code's permission check」、designer.md:118・worker.md:21 heredoc を使わない
- 由来: P-109 (v7.7 #48、WSL の bash と `--spec`)、ほかは日付なし。
- 判定: 環境の知識で、危険を守るものではない → **B** (作り直すなら参照ファイル 1 枚か verify.py に寄せる)。worker.md:21 と plan.md:63、heredoc の 2 か所は **C**。

## 11. テスト・E2E の暴走  約 14 件  [A 2 / B 10 / C 2]

- worker.md:13「When the prompt lists `Deferred verify commands`, never run those numbers」、:15「Never end your turn while a command you started is still running.」
- appendix/tests-at-end.md:53 build の verify で E2E を流さない (verify.py が `final` を拒否)、:14 作業者はテストファイルを作らない・変えない
- prompts/tester.md:12「Do not stash or reset to find a cause.」、:21 絞った run の spec 数を確かめ、多ければ止まる
- appendix/tests-at-end.md:139「never mock the unit under test」
- 由来: v3 (W-004)、v7.7 (#48 P-109 の E2E の流しすぎ)、v7.12 (#59, #60)、v7.13 (E2E は最後に 1 回)。
- 判定: stash/reset の禁止 (作業ツリーの消失) と前景で待つ (報告が消える) は **A**。ほかは tests-at-end の仕組みと E2E の費用の方針 → **B**。verify.py の拒否と文章の二重 → **C**。

## 12. レビューの作法・回数の上限  約 16 件  [A 2 / B 11 / C 3]

- appendix/fix.md:42「Never run the final review again, and never send a finding back to build」
- appendix/rework.md:9 PBI のレビューは 2 回まで、appendix/verify.md:39「never rework it」
- appendix/verify.md:37 / prompts/pbi-review.md:17 pass/fail は exit code、「never your judgement」
- appendix/review.md:89「A reviewer's report is not proof.」、:96 ADR との食い違いは捨てない
- CONTEXT.md:28 / designer.md:43「A review never runs on the model that built what it reviews」、prompts/final-review.md:17「Never pass --fix, --comment, ultra or --max-findings.」
- 由来: v7.8 (#49 レビュー 2 回)、v7.13 (#62、P-103 の 6 回のループ)、v7.12 (#60)。
- 判定: 合否の正本は exit code、最終レビューで `--comment` 等を渡さない (PR への投稿 = 外への書き込み) は **A**。回数の上限と差し戻し先は方針 → **B**。同じモデルでレビューしない規則は CONTEXT・designer・episodes の 3 か所 → **C**。

## 13. Kiln・文脈の大きさ  約 14 件  [A 1 / B 11 / C 2]

- appendix/kiln.md:51-56「## Do not」(作業者の途中で冷やさない、設計者の会話を渡さない、ログを飛ばさない、SAC を使わない)
- episode/_kiln.md:25「Never use `/compact`.」(ただし appendix/kiln.md:40 の inline では `/compact` を使う)
- prompts/kiln-chamber.md:8-9「Never compact or summarise your own conversation」「never mid-edit, never with a command still running」
- designer.md:111-119 (wc -lc で大きさを見る、長い出力は verify/ へ)
- 由来: v4 (Kiln 既定化、W-005 の compact-event)、v7.14 (#64 tablet-check)。
- 判定: Kiln の仕組みそのもの → **B**。「編集の途中で止まらない」は半端な変更を残さないので **A**。_kiln.md の「/compact 禁止」と kiln.md の inline の「/compact を使う」は条件付きだが読み違えやすい → **C** (矛盾に見える)。

## 14. 共有メモリ  約 9 件  [A 0 / B 6 / C 3]

- worker.md:9「Never search or write shared memory」(agents/pbi-worker.md:13 と同文)
- runners.md:45「The refs are copied below. Do not use memory.」、:173 dsh は「You may `get` only the ids listed above.」
- appendix/memory.md:22「Never copy a CLAUDE.md fact into a ref」、:34 write の直後に get しない
- 判定: 作業者の文脈を絞るための決まりで **B**。worker.md と pbi-worker.md、designer.md:125 と memory.md:22 の重複は **C**。

## 15. ファイルの置き場所・作業ツリーを汚さない  約 11 件  [A 4 / B 5 / C 2]

- appendix/map.md:28「never go on to the lint survey with `.omm/` left」、:43「Atelier never commits it」
- appendix/lint.md:58 一時 worktree に「no `node_modules` is linked; never link one」
- episode/design.md:16 worker.md を docs/pbi に写す理由「External runners cannot read outside the repository.」
- appendix/priority-templates.md:13 home は「Never commit it; it is personal.」、appendix/events.md:12 spool は commit・push しない、appendix/light.md:19「Do not commit.」
- SKILL.md:80 リンクだけでなくファイルを渡す (page と docs/pbi がアプリの開くフォルダの外にあることがある)
- 由来: v4 (OMM キャッシュ)、v7.3 (repo/cache/skip)、v7.10 (#54 ページはファイルで渡す)。
- 判定: 個人設定・イベントを commit しない、勝手に commit しないは **A**。.omm と worktree の扱いは道具固有 → **B**。

## 16. 外部スキル・部品・入れ子の深さ  約 12 件  [A 2 / B 8 / C 2]

- appendix/episodes.md:26-27「An agent at depth 3 has no Agent tool」「do not use that skill; run the built-in fallback」、appendix/setup.md:20 `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=3`
- appendix/engine.md:28-34 `cli missing`・`skill missing` で「never」、「A component's `forbid` lists arguments never to pass」
- appendix/engine.md:95「Workers never run plugin skills themselves.」、worker.md:18「Do not call review skills or subagents.」
- appendix/optional.md:52 security スキルのサンドボックスを「Do not loosen that.」
- 由来: v7.7 (#47 サブエージェントの深さ)、v4・v6 (forbid、unslop)。
- 判定: forbid (例: 動画・有料生成は頼まれたときだけ) とサンドボックスを緩めないは **A**。深さの計算は v7 の 3 層構造のせい → **B**。「review skills を呼ばない」は worker.md・pbi-worker.md・design-review.md・pbi-review.md・tester.md の 5 か所 → **C**。

## 17. git の破壊的操作  約 8 件  [A 8 / B 0 / C 0]

- runners.md:79 opencode 設定が `git push`・`git reset --hard`・`git clean`・`rm -rf` を拒否、:150 agy は commit/push/reset/rebase/checkout/stash ... を拒否
- prompts/final-review.md:9「do not edit, commit, push or post anything」、prompts/lint-fallback.md:9・map-fallback.md:9「Never commit, install or post anything.」
- appendix/lint.md:10 `jevlint fix`・`pr`・`commits`・`baseline` を流さない
- 判定: すべて **A** (ただし実装は deny 設定に寄せられる)。

---

## 全体の所見 (仮)

- A に当たるのは約 60 件で、テーマは「外への書き込み・破壊的操作」「秘密と外部送信」「インストールと設定の承認」「ユーザーの答えを捏造しない」の 4 つにまとまる。作り直すなら 1 か所 (例: 共通の Guardrails 節 1 枚 + runner の deny 設定) に集めると、5〜6 ファイルへの重複が消える。
- B は約 110 件で、最大の塊は「外部 CLI の癖」「再開・Re-entry」「caller の越権」「Kiln」。いずれも 3 層 (caller / conductor / 新しい設計者) と多数の runner を持つ構造から来ている。runner を絞り、設計者を出番ごとに作り直さない設計にすれば大半が消える。
- C は約 35 件。同じ規則が designer.md・episode/*.md・prompts/*.md・agents/*.md・appendix に何度も書かれている (Read first にまとめる v7.14 の仕組みと、外部 runner が skill を読めないため prompt に写す事情の両方が原因)。スクリプトがすでに強制している規則 (present の shown、questions の lint、verify の final 拒否) の文章も C。
- 矛盾に見える箇所: `/compact` (episode/_kiln.md:25 は禁止、appendix/kiln.md:40 の inline は使う)、「You never ask the user」(designer.md:15) と inline の「ask the user directly」(designer.md:11)。どちらも条件付きで正しいが、読み手が条件を見落としやすい。