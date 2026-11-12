---
name: harness-log
description: スキルのイベントを許可リストで点検し、外枠 (harness.event/1) を付けて手元にため、許したときだけ private のリポジトリへ送る送り手。スキルに指標のイベントを出させたいとき、送り始め・止めるとき、送る中身を確かめるときに使う。
---

# harness-log

スキルが出したイベントを、そのスキルの許可リストで点検してから、手元のスプールにためる。`enable` を打つまでは外に何も出さない。標準ライブラリと git だけで動く (`harness_log.py`)。

## いつ使うか

- スキルから指標のイベントを出すとき: 関数 `emit(schema_path, type, data, run_id=..., seq=..., envelope=...)` を呼ぶか、CLI の `emit` を打つ。
- 出番や run の終わりに `flush` を呼ぶ。失敗しても例外を投げず、標準エラーに 1 行で終了コード 0 (呼び出し側を止めない)。
- 送り始める前・送っている中身を確かめたいときに `preview`。

## 設定

- 場所は `~/.claude/harness-log.json`。環境変数 `HARNESS_LOG_CONFIG` で変えられる。ファイルが無ければ、emit も flush も何もしない。
- 項目: `remote`・`enabled`・`key` (ハッシュの鍵。`init` が作る)・`show_repos` (repo 名をそのまま出す glob)・`spool_dir`・`clone_dir`・`embedding` (`url`・`model`・`provider`・`dims`・`normalize`・`prefix`・`timeout`。欠けは既定で補う)。知らない項目は消さない。

## 許可リスト (スキルが持つ JSON)

`{"skill", "data_schema", "phase", "types": {"<type>": {"fields": {"<name>": <spec>}}}}`。
spec の `type` は `int`・`number`・`bool`・`id`・`enum` (`values`)・`hash` (改行の無い空でない文字列を長さを問わず `h:<12 桁>` にして送る)・`object` (`fields`)・`map` (`keys`・`values`)・`list` (`items`・`max`)。null はどれも通す。
合わない値・知らない項目は落とし、名前だけを `dropped` に入れる。知らない type のイベントは捨てる。

外枠の任意キー `split`・`split_set_id`・`change_id`・`config_fp`・`trial`・`unit_id` は送り手が共通で持つ。許可リストには書かず、`envelope` (CLI は `--envelope <json>`) で渡す。

## コマンド

```text
python harness_log.py init --remote <URL> [--show-repo <glob>]...   # enabled:false・鍵・clone を作る。送らない
python harness_log.py emit --schema <path> --type <t> --data <json> [--run-id --seq --stage --pbi --skill-version --envelope <json>]
python harness_log.py preview    # スプールの行と、push していないコミットをそのまま見る
python harness_log.py enable     # 送り始める
python harness_log.py flush      # enabled のときだけ、1 回のコミットにして push する
python harness_log.py disable    # 送るのを止める。止めている間もスプールにはたまる
python harness_log.py hash <value>   # その値の h:<12 桁> を出す
```

## 送り始めの流れ

1. `init` で設定と clone を作る (この時点では送らない)。
2. しばらく使ってから `preview` で、たまった行を自分の目で見る。
3. 中身に問題が無ければ `enable`。以後の `flush` で送られる。
4. 送れなかった分 (remote に届かない・push の失敗) はスプールに残り、`errors.log` に 1 行を書いて、次の `flush` で remote の先頭に積み直して送られる。

送り先には `<phase>/<skill>/<YYYY-MM>/<run_id>.jsonl` (run_id が無ければ `adhoc-<日付>`)・`<phase>/<skill>/coverage.json`・`SCHEMA.md` が並ぶ。
