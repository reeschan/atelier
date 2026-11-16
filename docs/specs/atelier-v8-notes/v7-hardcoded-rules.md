# v7.15 でテンプレート・agents.json から変えられない決まり（2026-11-16 洗い出し）

Explore サブエージェントの報告の要点。select_model.py:92-107、fix_plan.py:373、templates.py:45 は本セッションで原文を確かめた。

## 実装の振り分け
- 段は haiku/sonnet/opus の 3 つに固定（common.py:17）
- 決定表（select_model.py:92-107）: novelty=2 かつ ambiguity=2 → opus が sensitive より先。sensitive でも opus になりうる（PBI-3 はこれ）
- Jev なしは facts だけ、opus にしない。MANY_FILES=4、MANY_MODULES=2。confidence<0.7 で 1 下げる
- sensitive: route はその段の agent だけ（:254）、優先テンプレートを当てない（:298）、--override でも agent だけ（:469）
- どの段も最後は agent 1 つ（:147-153）。優先テンプレートでも最後に routing の agent が付く
- 優先テンプレートが変えられるのは tiers だけ（templates.py:45 TEMPLATE_KEYS）。見直し・roles・工程には効かない
- 実行先の種類は 6 つに固定（common.py:18-20）
- dsh: docstring と検査が食い違う（推）

## 見直し
- deep の条件は sensitive か coupling==2（:386）。Jev なしでは事実上 sensitive だけ
- route.json が無い PBI は deep 扱い
- deep のときだけ作ったモデル≠見直し役を検査。モデル名の文字列で判定
- 同じなら other に替える、両方同じなら opus/sonnet の名前で固定の入れ替え（md だけ）
- 1 PBI の見直しは 2 回まで（REVIEW_ROUNDS=2、fix.max_rework と無関係）
- 最後の見直しの skip 条件（diff 300 行・PBI 2 件・deep/sensitive なし）、security は sensitive/deep で run

## 設計役・担当
- 設計役と episodes.* の実行先は agent/codex/claude だけ
- 担当を持てる出番は test と fix だけ
- 既定値: designer=opus、reviewer.deep=opus、reviewer.other=sonnet/high、tester=sonnet、episodes=sonnet/high

## 修正
- fix の PBI は常に sensitive: False（fix_plan.py:373）→ 元が sensitive でも外部に出うる（不具合）
- 作業者は最大 3 人、fix.max_rework は 0〜5

## 工程
- 9 工程すべて必須、オプション 7 種固定、出番のつながりの表は conductor.py に固定

## その他
- light lane の条件と SENSITIVE_WORDS（19 語）は lane.py に固定
- 実行先が使えないとき agent に落とすのはユーザーに聞いてから
- sandbox は workspace-write 固定（agents.json で danger-full-access だけ開ける）
