# codex の作業者と役割をサンドボックスの中で動かす

2026-11-15 に、P-103 の run で起きたことから起こした。用語は `skills/atelier/CONTEXT.md` に従う。`docs/specs/atelier-role-runners.md` と `docs/specs/atelier-run-fixes.md` の codex の部分を、この文書で置き換える。

## Problem Statement

- Atelier は、codex の作業者・レビュー担当・テスト担当を `codex -a never exec ... -s danger-full-access`（承認なし・サンドボックスなし）で起動していた。
- Claude Code の auto mode の classifier は、この起動を「監督を弱める操作（Undermining Oversight）」として止めることがある（毎回ではない）。P-103 では 3 回止まった。
- 止まると作業者が起きず、run が問いで止まり、ユーザーが道具を選び直し、designer の段が 1 回余計にかかる。
- `danger-full-access` にしていたのは、2026-11-11 の試作で、`codex sandbox` の `workspace-write` では pnpm と uv の Python が起動できなかったからだった。

ユーザーは、設定に許可を足す道ではなく、Atelier 側で codex をサンドボックス付きで起動する道を選んだ（2026-11-15）。

## Solution

- 作業者と役割（レビュー担当・テスト担当）の codex は、既定で `-s workspace-write` にする。
- ネットワークは開けない。作業ツリーの外に書く場所も足さない（`--add-dir` は使わない）。
  - ユーザーの `~/.codex/config.toml` の `sandbox_workspace_write.network_access = true` と `writable_roots` は、`-s workspace-write` でも効き残る（試した）。
  - そこで、その回だけ `-c sandbox_workspace_write.network_access=false` と `-c sandbox_workspace_write.writable_roots=[]` で閉じる。
- Windows では `-c windows.sandbox="elevated" -c features.prefer_mxc=true` も渡し、MXC のサンドボックスを選ぶ。
  - MXC を使えないときの戻り先は、ユーザーの `windows.sandbox` によらず elevated に決める。unelevated に戻ると、`network_access=false` でもネットワークが開くため（試した）。
- サンドボックスが塞ぐのは書き込みとネットワークで、読み取りは塞がない。
- サンドボックスを外す（`danger-full-access`）のは、home の `agents.json` の `runners.codex.sandbox` で明示したときと、`role_run.py --sandbox danger-full-access` を渡したときだけにする。
- 設計者の codex は、既定の `danger-full-access` を残す。理由は下の「設計者」に書く。

### 起動の形

- 作業者（`runners.md` の `## codex`。設計者が Bash で流す）:

  ```bash
  codex -a never exec -m <model> -c model_reasoning_effort='"<effort>"' -s workspace-write -c sandbox_workspace_write.network_access=false -c 'sandbox_workspace_write.writable_roots=[]' -c 'windows.sandbox="elevated"' -c features.prefer_mxc=true -o docs/pbi/codex/<report>.last.md - < docs/pbi/codex/<report>.prompt.md > docs/pbi/codex/<report>.log 2>&1
  ```

  - `-s workspace-write` から後のサンドボックスの引数は、`prepare.py` が表のあとに出す `codex sandbox: <引数> (<出所>)` の行から写す。
    - 引数は bash にそのまま貼れるよう引用する（`runner_cli.shell_words`）。Windows 以外では、`windows.sandbox` と `prefer_mxc` の 2 つは付かない。
    - 行は、表の PBI の route に codex の手順があるときだけ出る。
    - 出所は `default`・`agents.json`・`agents.json invalid` のどれか。
  - `prepare.py` を通さない場合（`select_model.py` を直に流した場合）は、既定の引数を使う。
- 役割（`role_run.py`）: `runner_cli.codex_args` が同じ引数を組む。
  - サンドボックスは `--sandbox`、`agents.json` の `runners.codex.sandbox`、`workspace-write` の順に取る。
  - `.run.json` に `sandbox` を残す。
- `agents.json` の `runners.codex.sandbox` に置ける値は `workspace-write` と `danger-full-access` だけにする。
  - ほかの値（`read-only` を含む）は `workspace-write` に戻す。`prepare.py` は `(agents.json invalid)` と出し、`role_run.py` は warn を出す。
  - 読めない値のときに、サンドボックスを開ける側には倒さない。

### 開けた範囲と理由

| 範囲 | 開けたか | 理由 |
|---|---|---|
| 作業ツリーの外の読み取り | 塞がない（`workspace-write` の仕様） | 道具とその設定を読むのに要る。塞ぐのは書き込みとネットワーク |
| 作業ツリーの中への書き込み | 開ける（`workspace-write`） | 作業者の仕事そのもの。役割は結果の JSON をリポジトリの中に書く |
| システムの一時フォルダ（`%TEMP%`） | 開ける（codex の既定のまま） | テストの道具が一時ファイルを書く |
| `~/.config/atelier`（run の記録） | 開けない | 記録（`record_run.py` など）は設計者が書く。作業者は書かない |
| git worktree の共通の `.git` | 開けない | 作業者はコミットしない。`git status`・`git diff` は書けなくても動く（試した） |
| ユーザーの `writable_roots` | 閉じる（`writable_roots=[]`） | ユーザーの設定で足された書き込み先は、MXC でも効き残った。作業者の境界を作業ツリーと `%TEMP%` に揃える |
| ネットワーク | 開けない（`network_access=false`。ユーザーの設定が true でも閉じる） | 依存を足す `pnpm add` は、ネットワークを開けても通らない（下の表）。依存を足すのは設計者の仕事にする。`pnpm test` の lockfile の検証は、ネットワーク無しでも通った |
| pnpm の store（`%LOCALAPPDATA%\pnpm\store`） | 開けない | `writable_roots` に足しても、新しいパッケージの取り込みが拒まれた。足しても効かない場所は開けない |
| Windows のサンドボックスの実装 | MXC を選び、戻り先を elevated に決める（`windows.sandbox="elevated"`・`features.prefer_mxc=true`） | この PC の `config.toml` は `windows.sandbox = "elevated"`。elevated では pnpm を読めない。unelevated ではネットワークが塞がらない。MXC なら pnpm・Biome・`node --test` が動き、ネットワークも塞がる。MXC を使えない PC での動きは試していない |

### 設計者

- 設計者の codex は、`roles.designer.permission_mode` がサンドボックスの名前でなければ `danger-full-access` のまま起動する。
- 設計者は作業者と役割を起こし、run の記録を home に書き、各 CLI の home（`~/.codex`・`~/.claude`・`DSH_HOME` など）を使う子を起こす。
- 試した結果、サンドボックスの中から起こした codex は、自分のコマンドを起こせなかった。
  - `~/.codex` を書けないと、起動の時点で落ちる（`failed to initialize in-process app-server client`）。
  - `~/.codex` を書けるようにしても、入れ子の codex のコマンドは「プロセス作成エラー」（`setup refresh had errors`）になった。入れ子の codex に `prefer_mxc` を渡しても同じだった。
- このため、サンドボックスの中の設計者は codex の作業者を回せない。設計者は直せないので、既定を残し、`SKILL.md` の「auto mode が `run --detach` を止めることがある」の注意を設計者に限って残す。
- 設計者が `danger-full-access` で動いていても、そこから起こす作業者と役割は、それぞれ自分のサンドボックスの中で動く。外側にサンドボックスが無いので、入れ子の問題は起きない。
  - ただし、この組み合わせ（`danger-full-access` の codex の設計者から、サンドボックス付きの codex の作業者を起こす形）は試していない。試すには `danger-full-access` の codex を起こすことになり、それがこの変更で避けたい操作だからである。

## Windows で試したこと

2026-11-15、Windows 11 Home 10.0.26200、codex-cli 0.165.0、Node v22.23.5、pnpm 12.3.4、Biome 2.2.4。

- 試し場: 一時フォルダの中の小さな pnpm のリポジトリと、その git worktree。
- 試し方:
  - モデルを使わずに、`codex sandbox -c sandbox_mode="workspace-write" ...` から PowerShell のスクリプトを流した。
  - 最後に、新しい形の `codex -a never exec ...`（上の起動の形の引数すべて）を `gpt-6.1-terra`（effort low）で流した。
    - Claude Code（auto mode）の Bash から流し、止められなかった。

| 試したこと | elevated（この PC の設定） | unelevated | MXC（`prefer_mxc`） |
|---|---|---|---|
| サンドボックスの中の利用者 | `CodexSandboxOffline`（別の利用者） | 今の利用者の制限付きトークン | AppContainer |
| `pnpm --version` | 見つからない（`%LOCALAPPDATA%\pnpm` を読めない） | 動く | 動く |
| `pnpm test`（`node --test`） | pnpm が無い。`node --test` を直に流せば通る | 落ちる（`spawn EPERM`。子を起こせない） | 通る |
| `pnpm lint`（Biome） | pnpm が無い。`node_modules\.bin\biome.cmd` を直に流せば動く | 動く | 動く |
| ネットワーク（`network_access` なし） | 塞がる（`EACCES`） | 塞がらない（200） | 塞がる（`ENOTFOUND`） |
| 作業ツリーの外（`~/dev`）への書き込み | 拒まれる | 拒まれる | 拒まれる |
| `git status`（worktree） | 動く（`~/.config/git/ignore` を読めない警告） | 動く | 動く |
| `python`（WindowsApps の入口） | 見つからない | 動く | 起動できない |
| `uv`（uv の管理する Python） | キャッシュを作れない | キャッシュを開けない | 管理する Python を調べられない |

新しい形の `codex exec` の結果（作業者と同じ起動）:

- 通った: `node --version`・`pnpm --version`・`pnpm test`（pass 1）・`pnpm lint`（Biome、No fixes applied）・`node --test`・`git status --short`・作業ツリーの中への書き込み・`uv --version`
- 拒まれた: `~/dev` への書き込み（アクセス拒否）・ネットワーク（`ENOTFOUND`）・`python --version`（WindowsApps の入口）

ユーザーの設定が境界を広げる場合の試し（`CODEX_HOME` に、`windows.sandbox = "unelevated"`・`network_access = true`・`writable_roots = [<外のフォルダ>]` を書いた一時の `config.toml` を置いた）:

| | `-s workspace-write` だけ | MXC と `network_access=false`（`writable_roots` は上書きなし） | 起動の形の引数すべて |
|---|---|---|---|
| 選ばれた実装 | unelevated（`node --test` が落ちる） | MXC | MXC |
| ネットワーク | 開く（200） | 塞がる | 塞がる |
| `writable_roots` のフォルダへの書き込み | 書ける | 書ける | 拒まれる |

依存を足す試し（MXC、`network_access=true`）:

- store に無いパッケージの `pnpm add` は、store から node_modules への取り込みで拒まれた。
- `writable_roots` に store を足しても、`npm_config_package_import_method=copy` を付けても、同じだった（store の書き込みか取り込みで拒否）。
- このため、ネットワークを開けても依存は足せない。

## 既知の制限

- Windows で Python を verify に使うリポジトリは、作業者の中で verify が通らない（WindowsApps の入口も uv の管理する Python も起動できない）。`runners.md` の `## Common rules` どおり、verify のコマンドだけが落ちたときは設計者が流す。
- 依存を足す PBI では、作業者が Open に書き、設計者が入れる。
- MXC を使えない Windows での動きは試していない。codex の文書では MXC は「対応する PC で勧める」実装とされ、ほかに `elevated`・`unelevated` がある。戻り先の elevated ではネットワークは塞がるが、pnpm が見つからない。そのときは verify を設計者が流すか、ユーザーが `runners.codex.sandbox` を `danger-full-access` にする。elevated の準備（管理者の承認）がされていない PC では、codex が起動で止まるかもしれない。そのときは使えない実行先として扱う。
- macOS・Linux では試していない。`-s workspace-write` と `sandbox_workspace_write` の 2 つを渡す（`windows.sandbox` と `prefer_mxc` は Windows だけ）。

## Out of Scope

- Claude Code の権限の設定（`settings.json` の `permissions`・`autoMode`）を変えること。
- ユーザーの `~/.codex/config.toml` を書き換えること（`-c` でその回だけ上書きする）。
- 止められた起動を、形を変えてくぐり抜けること。止められたら、そのままユーザーに見せる。
- 設計者の codex をサンドボックスの中で動かすこと（上の「設計者」）。
- 作業者の codex の起動をスクリプトにすること（今の `runners.md` の 1 行のまま）。

## Testing Decisions

- `test_role_run.py`:
  - 既定が `-s workspace-write` で、`network_access=false`・`writable_roots=[]` が付き、Windows では `windows.sandbox="elevated"`・`prefer_mxc` も付くこと。
  - `.run.json` の `sandbox`。
  - `--sandbox danger-full-access` で開け、そのときはサンドボックスの `-c` を付けないこと。
  - `agents.json` の `runners.codex.sandbox` が効き、`--sandbox` がそれより強いこと。
  - 読めない値（`read-only`・別の名前・数）は閉じたままで warn を出すこと。
  - `runner_cli.codex_sandbox_args` の組み立て。
  - `runners.md` の作業者の起動の行に `danger-full-access` が無いこと。
- `test_prepare.py`:
  - codex の route があるときの `codex sandbox:` の行（既定・`agents.json`・`agents.json invalid`）。
  - codex が無いときは行を出さないこと。
- `test_conductor.py`: 設計者の既定（`danger-full-access`）と `permission_mode` での指定は、今までのテストのまま通る。
