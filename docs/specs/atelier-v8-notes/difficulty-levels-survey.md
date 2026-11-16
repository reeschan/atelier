# 難しさの段とモデルの対応づけ — 他の道具・研究の調べ (2026-11-16)

目的: Atelier の haiku/sonnet/opus という段 (ベンダーのモデル名) を、ベンダーに依らない 5 段ほどの難しさの尺度に替えるべきかを判断する材料。
読んだのは各 URL の 2026-11-16 時点の内容。二次資料しか取れなかったものは「(二次)」と書く。

## 一覧

| 出典 | 段・尺度 | 使う軸 | モデル名に結びつくか | モデルへの対応 | 較正 (結果から直すか) | URL |
|---|---|---|---|---|---|---|
| Taskmaster `analyze-complexity` | 1–10 の整数。閾値の既定 5 | 実装の手間・技術的な難所・依存・テストの要否 (LLM が採点) | いいえ | モデルは選ばない。閾値以上を細分化 (`recommendedSubtasks`)。モデルは main/research/fallback の役割で固定 | なし | https://raw.githubusercontent.com/eyaltoledano/claude-task-master/main/src/prompts/analyze-complexity.json , https://raw.githubusercontent.com/eyaltoledano/claude-task-master/main/README.md |
| superpowers `subagent-driven-development` | 3 段 (cheap / standard / most capable) + 役割別 | 触るファイル数・仕様の完全さ・統合の有無・設計判断の要否。レビューは差分の大きさ・複雑さ・リスク | いいえ (段は抽象。具体モデルは利用者が当てる) | 機械的 1–2 ファイル→安い、多ファイル統合・デバッグ→標準、設計・最終レビュー→最上位 | 静的。ただし詰まったら「1 段上」で再派遣、文脈不足なら同じ段で文脈を足す | https://raw.githubusercontent.com/obra/superpowers/main/skills/subagent-driven-development/SKILL.md |
| Claude Code effort / `opusplan` | low / medium / high / xhigh / max (5 段) | 用途の説明 (明確な範囲・検証が要る・無監督で難問) | effort は抽象。`opusplan` は工程 (計画/実装) でモデル名を切替 | モデルごとに使える段が違い、無い段は下の段へ丸める | なし (「広く使う前に試せ」) | https://code.claude.com/docs/en/model-config |
| OpenAI `reasoning_effort` | none / minimal / low / medium / high / xhigh / max (最大 7 段) | 用途 (分類・検索・エージェント的コーディング・難しいデバッグ・セキュリティレビュー) | いいえ | 同上。対応する値はモデルごと | 「評価で利点が出たときだけ xhigh」 | https://developers.openai.com/api/docs/guides/reasoning |
| Gemini `thinking_level` | minimal / low / medium / high / xhigh (モデルごとに 2〜5 段) | 易 (事実検索・分類)/中 (比較)/難 (高度なコーディング・多段計画) | いいえ | モデルごとに使える段が違う | なし | https://aistudio.google.com/docs/thinking |
| GitHub Copilot Auto | 利用者が選ぶ優先度 3 つ (Efficiency / Balance / Intelligence)。難しさの段は非公開 | 課題の複雑さの評価 + 稼働状況 | いいえ | 社内の分類器。同じモデル群から選ぶ | 非公開 | https://docs.github.com/en/copilot/concepts/auto-model-selection |
| Cursor Auto (Cursor Router) | Cost / Balance / Intelligence。難しさの段は非公開 | 課題とその時点の会話の状態 (二次) | いいえ | ML 分類器が毎ターン選ぶ | 非公開 | https://cursor.com/help/models-and-usage/cursor-router |
| RouteLLM | 2 段 (strong / weak) + 連続の閾値 | 「強いモデルが勝つ確率」の予測のみ | いいえ (モデルは差し替え可) | 予測勝率 > 閾値なら strong | `calibrate_threshold --strong-model-pct` で「strong に回す割合」から閾値を逆算。自分の問い合わせで較正せよと README | https://github.com/lm-sys/RouteLLM |
| Hybrid LLM (ICLR 2024) | 2 段 + 連続の難しさ予測 | 問い合わせの難しさ (小モデルと大モデルの品質差の予測) | いいえ | 品質目標で閾値を動かす | 閾値を品質と費用の目標で調整 | https://arxiv.org/abs/2404.14618 |
| BEST-Route (ICML 2025) | モデル × 生成数 n | 難しさ + 品質の閾値 | いいえ | 小モデル + best-of-n で大モデル相当を狙う | 学習した予測器 | https://arxiv.org/abs/2506.22716 |
| FrugalGPT | 段の数 = カスケードの長さ | 応答の品質の採点 (結果を見て止める) | いいえ | 安い順に呼び、採点が足りれば止める | 採点器と順序を学習 (二次) | https://web.stanford.edu/~shuhuiq/blog/review-routing-cascades.html |
| RouterBench | — (評価基盤) | 費用と性能 | いいえ | — | 405k の推論結果で事前に評価。単純な基準線を有意に超える router は無かった (二次) | https://arxiv.org/abs/2403.12031 |
| Router-R1 | 連続 (多段の判断) | モデルの記述子 (価格・規模など) | いいえ。記述子だけで未知のモデルにも汎化 | LLM が think と route を交互に | 強化学習 | https://arxiv.org/abs/2506.09033 |
| Arch-Router (katanemo) | 利用者が定義する方針 (分野 × 動作) | 難しさではなく「何の作業か」 | いいえ。方針→好みのモデル の表 | 1.5B の分類器が方針に当てる | モデルを足しても再学習不要 | https://arxiv.org/abs/2506.16655 |
| LiteLLM complexity router (Beta) | 4 段 SIMPLE / MEDIUM / COMPLEX / REASONING。境界 0.15 / 0.35 / 0.60 | 7 つの重み付き信号 (長さ・コード・推論の語・技術用語など) | いいえ。`tier_labels` で Fast/Standard/Premium/Deep 等に改名可。各段は設定でモデルに当てる | 段→モデルの表 (段に複数モデルのプールも可) | 境界と重みは手で調整。`adaptive: true` は段の中のモデル選びだけを結果から学ぶ (段の判定は学ばない) | https://docs.litellm.ai/docs/proxy/auto_routing |
| OpenRouter Auto Router | 連続の `cost_quality_tradeoff` 0–10 → 名前付き `cost_tier` | 作業の種類 + 費用 | いいえ | 旧版は NotDiamond、新版は作業種別ごとの直近 7 日の利用実績 | 市場の利用実績で毎週動く | https://openrouter.ai/docs/guides/routing/routers/auto-router |
| Not Diamond | 連続 (分類器) | 学習データ次第 | いいえ | 候補モデルごとの評価スコアで学習した router | 自分の評価データで再学習 (custom router) | https://docs.notdiamond.ai/docs/router-training-quickstart (二次) |
| SWE-bench Verified | 4 段 (<15 分 / 15 分–1 時間 / 1–4 時間 / >4 時間) | 熟練者が解決策を決めて実装する時間。別に仕様の曖昧さ・テストの妥当性を注記 | いいえ | — | 39% / 52% / 8% / 3 件。変更行の平均 5 / 14 / 50 | https://epoch.ai/blog/what-skills-does-swe-bench-verified-evaluate |
| SWE-Lancer | 連続 (報酬 $250〜$32,000) | 市場価格 = 難しさの勾配 | いいえ | — | — | https://arxiv.org/abs/2502.12115 |
| METR time horizon | 連続 (人の所要時間) | 人の専門家の所要時間 | いいえ。モデルの能力を同じ尺度 (50% 成功の時間) で表す | 成功率をロジスティックで当てはめ、50% を切る長さ = そのモデルの限界 | 観測した成否から当てはめる | https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/ |
| ストーリーポイント (Fibonacci) | 1,2,3,5,8,13,20,40,100 | 相対的な大きさ (不確かさ込み) | いいえ | — | チームの実績 (velocity) | https://www.mountaingoatsoftware.com/blog/why-the-fibonacci-sequence-works-well-for-estimating |
| Cynefin | 4 + 1 (clear / complicated / complex / chaotic + confusion) | 因果が事前に分かるか (順序立つ/立たない) | いいえ | 段ごとに進め方が違う (complex は safe-to-fail の試行) | — | https://en.wikipedia.org/wiki/Cynefin_framework (二次) |

## 共通のパターン

1. **段にベンダーのモデル名を使う例は見つからなかった。** 段は抽象 (cheap/standard/most capable、SIMPLE〜REASONING、low〜max) で、具体モデルは設定の表で当てる。
   - superpowers は「最も力の弱いモデルで足りるものを」とだけ書き、段は役割と能力で呼ぶ: https://raw.githubusercontent.com/obra/superpowers/main/skills/subagent-driven-development/SKILL.md
   - LiteLLM は段→モデルを設定で当て、段の表示名さえ `tier_labels` で替えられる: https://docs.litellm.ai/docs/proxy/auto_routing
   - Router-R1 はモデルを価格などの記述子で表し、未知のモデルにも汎化する: https://arxiv.org/abs/2506.09033
   - 例外は Claude Code の `opusplan` で、工程 (計画/実装) とモデル名を直結している。ただしこれは 1 ベンダー内の別名: https://code.claude.com/docs/en/model-config
2. **「難しさ」と「どのモデルか」を 2 段に分ける。** 難しさを判定 → 表でモデルへ。表はモデルが入れ替わるたびに直すが、判定の規則は変えない (LiteLLM、Taskmaster の閾値と細分化)。
   - https://docs.litellm.ai/docs/proxy/auto_routing , https://raw.githubusercontent.com/eyaltoledano/claude-task-master/main/src/prompts/analyze-complexity.json
3. **モデルの選択と「推論の量 (effort)」は別の軸。** 3 社とも effort を 4〜7 段の抽象名で出し、使える段はモデルごとに違い、無い段は丸める。
   - https://developers.openai.com/api/docs/guides/reasoning , https://aistudio.google.com/docs/thinking , https://code.claude.com/docs/en/model-config
4. **難しさとリスクを分けている例は少ない。** はっきり分けているのは superpowers のレビュー (差分の「大きさ・複雑さ・リスク」で選ぶ、並行処理の微妙な変更は最上位) くらい。ルーター類はリスクを扱わない。SWE-bench Verified は所要時間とは別に「仕様の曖昧さ」を注記する。
   - https://raw.githubusercontent.com/obra/superpowers/main/skills/subagent-driven-development/SKILL.md , https://epoch.ai/blog/what-skills-does-swe-bench-verified-evaluate
5. **事前の予測が外れたときの昇格 (エスカレーション) を組み込む。** superpowers は詰まったら 1 段上、文脈不足なら同じ段で文脈を足す。FrugalGPT は結果を採点して足りなければ上へ。
   - https://raw.githubusercontent.com/obra/superpowers/main/skills/subagent-driven-development/SKILL.md , https://web.stanford.edu/~shuhuiq/blog/review-routing-cascades.html
6. **ソフトウェアの課題の難しさは「熟練者の所要時間」で測るのが主流。** SWE-bench Verified の 4 段、METR の連続値。変更行数は所要時間とよく相関する。
   - https://epoch.ai/blog/what-skills-does-swe-bench-verified-evaluate , https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/
7. **較正は「回す割合」か「観測した成否」から。** RouteLLM は strong に回す割合から閾値を逆算し、自分の問い合わせで較正せよとする。METR は成否のロジスティック当てはめでモデルの限界の難しさを出す。Not Diamond は自分の評価データで router を学習。
   - https://github.com/lm-sys/RouteLLM , https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/ , https://docs.notdiamond.ai/docs/router-training-quickstart

## 段の数についての根拠

- 実装例の段の数は 2 (RouteLLM, Hybrid LLM)、3 (superpowers, Copilot/Cursor の優先度)、4 (LiteLLM, SWE-bench Verified, Cynefin)、5 (Claude Code effort)、最大 7 (OpenAI effort)、10 (Taskmaster)、連続 (METR, SWE-Lancer, Router-R1)。決まった数は無い。
- モデルへの対応に使う段は、実際に使い分けるモデルの数より多くない (2〜4)。細かい尺度 (Taskmaster の 1–10) はモデル選びでなく「細分化するか」の閾値に使っている。
  - https://raw.githubusercontent.com/eyaltoledano/claude-task-master/main/src/prompts/analyze-complexity.json , https://docs.litellm.ai/docs/proxy/auto_routing
- LLM に採点させる尺度では 0–5 が人との一致が最も高く、0–10 が最も低かった (3 段は未検証)。
  - https://arxiv.org/abs/2601.03444
- 小規模な検証で、5 段のルーブリックが判定者どうしの一致が最も高かった。
  - https://arxiv.org/abs/2602.01146
- 人の評定尺度では 2〜4 段は弱く、7 段ほどまで指標が上がり、10 段を超えると再検査信頼性が下がる (二次)。最適な段数は未決着とする論文もある。
  - https://www.rangevoting.org/optinumb.pdf , https://arxiv.org/abs/2502.02846
- 見積もりの段は等間隔より比例 (Weber の法則) の方が見分けやすい — Fibonacci の根拠。
  - https://www.mountaingoatsoftware.com/blog/why-the-fibonacci-sequence-works-well-for-estimating
- 難しさの分布は偏る。SWE-bench Verified では 91% が 1 時間以内で、上の 2 段はほぼ空。段を増やしても上の段に入る PBI は少ないはず。
  - https://epoch.ai/blog/what-skills-does-swe-bench-verified-evaluate

## Atelier への示唆

1. **段の名前をモデル名から外す。** 調べた範囲でベンダー名を段にする例は無い。段 (例: L1〜L5) と「段→(ベンダー, モデル, effort)」の表を分け、表だけをモデルの入れ替えで直す。
   - https://docs.litellm.ai/docs/proxy/auto_routing , https://raw.githubusercontent.com/obra/superpowers/main/skills/subagent-driven-development/SKILL.md
2. **5 段は「難しさの判定」には妥当、「モデルの数」には多すぎるかもしれない。** 判定は 5 段にして、表では隣り合う段が同じモデルを指してよい (effort だけ変える) とすれば両立する。effort も段の表に入れられる (3 社とも抽象の段を持つ)。
   - https://arxiv.org/abs/2601.03444 , https://code.claude.com/docs/en/model-config , https://developers.openai.com/api/docs/guides/reasoning
3. **リスクは難しさと別の軸にする。** 難しさ (新しさ・曖昧さ・結合・規模) でモデルを選び、リスク (取り消しにくさ・影響範囲・並行処理やセキュリティ) は下限 (「この段より下は不可」) やレビューの強さに使う。superpowers のレビューの選び方がこれに近い。
   - https://raw.githubusercontent.com/obra/superpowers/main/skills/subagent-driven-development/SKILL.md
4. **軸の一部を所要時間・変更規模の見積もりで置き換えられる。** 「熟練者なら何分か」と変更行数の見当は SWE-bench Verified と METR の軸で、外部の成績と突き合わせやすい。
   - https://epoch.ai/blog/what-skills-does-swe-bench-verified-evaluate , https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/
5. **昇格の規則を明文化する。** 失敗 → 1 段上、文脈不足 → 同じ段で文脈を足す。段の数が多いほど昇格の刻みが細かくなり、5 段の利点になる。
   - https://raw.githubusercontent.com/obra/superpowers/main/skills/subagent-driven-development/SKILL.md
6. **表は観測した結果で直す。** 「段 × モデル」ごとに初回の合格率・昇格の回数・手戻りを記録し、METR のように段ごとの成功率がある値 (例 80%) を下回るモデルを、その段から外す。RouteLLM のように「上位モデルに回す割合」を予算から決めて閾値を逆算する手もある。RouterBench の結果から、凝った分類器より単純な表 + 昇格で十分な可能性がある。
   - https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/ , https://github.com/lm-sys/RouteLLM , https://arxiv.org/abs/2403.12031
7. **判定を LLM に任せるなら段に言葉の定義 (ルーブリック) を付ける。** Taskmaster は 1–10 の各値の意味を定義しておらず、採点がぶれやすい形。0–5 程度の段に例付きの定義を置く方が一致しやすい。
   - https://raw.githubusercontent.com/eyaltoledano/claude-task-master/main/src/prompts/analyze-complexity.json , https://arxiv.org/abs/2601.03444

## 確かめられなかったこと

- Kiro・spec-kit・cc-sdd・OpenHands・Devin・Windsurf に難しさの段やモデルの自動選択があるか (検索で一次資料が出なかった)。
- Copilot Auto と Cursor Router の分類器の段の数・信号 (どちらも非公開)。
- Martian と Unify のルーターの仕様。
- SWE-bench Verified の注記のルーブリック本文 (OpenAI の記事は 403 で読めず、Epoch の分析で代えた)。
- FrugalGPT・Preston & Colman の原文 (二次資料のみ)。Not Diamond の学習結果の形は API の要約 (二次) のみ。
- 3 段と 5 段を同じ課題で比べた研究 (LLM 判定の研究は 0–5 / 0–10 / 0–100 の比較で、3 段は含まない)。
- Taskmaster の `expand` が complexityScore からどう細分化数を決めるかのコード上の詳細 (プロンプトの定義だけ確認)。
