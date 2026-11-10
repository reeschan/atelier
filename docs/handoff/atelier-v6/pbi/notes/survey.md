# 静的サーベイ (skills/atelier)

対象 8 ファイル、計 2422 行。測定は ast による機械計測と目視。

## 点の低い順

1. skills/atelier/record_run.py (752 行): 400 行超。100 行超の main (127 行、入れ子 5)、引数 5 つの read_memory。トークン読み取り・メモリ・git・保全・書き込みが同居。
2. skills/atelier/agent_stats.py (450 行): 400 行超。analyze の入れ子が 5。jsonl 読み取りと集計・整形が同居。
3. skills/atelier/verify.py (215 行): 引数 5 と 6 の関数 3 つ (run_one, merge_results, run_deferred)。実行・ログ書き込み・表整形が run_one に同居。
4. skills/atelier/claude_run.py (132 行): main が引数検証・起動・記録を兼ね、子プロセスの失敗は終了コードに写すのみ。
5. skills/atelier/common.py (153 行): 指摘は軽微 (一時ファイル削除の握りつぶし。意図的)。
6. skills/atelier/design_record.py (339 行)、kiln_log.py (134 行)、agents.py (247 行): 指摘なし。ただし追記書き込みの排他なし (下記)。

## 100 行以上の関数

- skills/atelier/record_run.py:622 main (127 行、入れ子 5)

## 弱い問い

- 400 行超のファイル: record_run.py (752), agent_stats.py (450)
- 入れ子が 4 より深い: record_run.py:622 main (5), agent_stats.py:154 analyze (5)
- 引数が 4 つより多い: record_run.py:477 read_memory (5), verify.py:88 run_one (5), verify.py:105 merge_results (5), verify.py:129 run_deferred (6)
- 責務の混在: record_run.py (読み取り・集計・git・書き込み), agent_stats.py (入力解析と集計)
- 名前: 特に誤解を招く名前は見つからず

## bug-hunt の候補

- skills/atelier/record_run.py:218-222 と 248-253 (握りつぶし): run.json の壊れ・欠落を except で捨て、時刻が null になるだけで呼び出し側に伝わらない。read_claude と read_gemini 系に同じ処理が重複。直し方: 共通関数に切り出し、失敗時は警告を返して記録に載せる。
- skills/atelier/record_run.py:225-231, agent_stats.py:62-64, 108-110 (握りつぶし): 壊れた jsonl 行を黙って continue。件数が出ないので欠損に気づけない。直し方: 読み飛ばした行数を数えて結果に含める。
- skills/atelier/record_run.py:605-610 head_commit (サブプロセス失敗): git の失敗は None を返すのみで理由が残らない。None が「git 無し」と「失敗」を区別できない。直し方: 理由を併せて返す。
- skills/atelier/record_run.py:736 (check-then-act / 共有状態): runs.jsonl への追記が排他なしで、同時実行の記録が混ざる恐れがある (他に design_record.py:304, kiln_log.py:126 も同様)。直し方: 1 回の write に収め、必要なら lock を取る。
- skills/atelier/record_run.py:728-735 (check-then-act): 保全の書き込みに失敗しても追記が続く。archive_info に残るので意図的だが、runs.jsonl 側と保全側が食い違い得る。直し方: 食い違いを検査する工程を足す。
- skills/atelier/claude_run.py:120-122 (サブプロセス): 起動失敗 (OSError) と timeout がなく、launcher が無い場合に例外で止まり run.json が書かれない。直し方: OSError を捕らえ exit を記録して終える。timeout を付ける。
- skills/atelier/verify.py:91-95 (サブプロセス): OSError (bash が無い) は捕らえず例外のまま。TimeoutExpired の出力は部分出力を捨てる。直し方: OSError を code に写し、exc.stdout を使う。
- skills/atelier/record_run.py:510 (欠損値): results.json が壊れていると checked False で通す。検証が走らなかったことと未検証が同じ表示になる。直し方: 「壊れている」を区別して返す。

Source: fallback prompt (no jevlint)
