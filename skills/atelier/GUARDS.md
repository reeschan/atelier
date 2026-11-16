# 守りの規則 G1〜G14

止め方：①起動前、②進行役が拒否、③実行後の検査、④指示。検査欄は確認済みの範囲を示し、未結合のものは明記する。

| 規則 | 工程 | 止め方 | 機械での形・現状 | 検査（skills/atelier からのパス） |
|---|---|---|---|---|
| G1 作業ルート外に書かない | 役の起動・完了 | ①③ | ランナーの設定と `snapshot` / `compare`；ルート外と無視ファイルの完全な監視は未実装 | `tests/test_runners.py::test_guard_detects_changed_existing_untracked_and_lock`（許可外パスのみ） |
| G2 履歴操作・投稿をしない | 役の起動・完了 | ①③ | deny 設定と HEAD・branch・stash の比較。push・投稿の事後検出は未実装 | `tests/test_flow_rules.py::test_agent_launch_snapshot_at_next_compare_at_done`（変更検出の経路） |
| G3 承認外への送信禁止 | 外部役の起動前 | ② | `sends_approved` と承認時に固定した PBI → runner 表。進行役への結合は未実装 | `tests/test_guards.py::test_sends_require_approved_pbi_runner_pair` |
| G4 秘密の送信・記録禁止 | 送信・記録前 | ③ | `scan_secrets` の型と行番号。検出は近似で、進行役への結合は未実装 | `tests/test_guards.py::test_secrets_are_detected_without_values` |
| G5 インストール・全体設定は承認後 | 起動前・完了後 | ①② | deny 設定、`install_attempts` の依存ファイル差分。全体設定の検出と承認の結合は未実装 | `tests/test_guards.py::test_dependency_snapshot_changes` |
| G6 答えを作らない | 回答 | ② | `answer` の入力だけを回答として記録 | `tests/test_conductor.py::test_g6_answer_only_via_answer_command` |
| G7 未完了 run を潰さない | 開始 | ② | 明示的 force なしの開始を拒否 | `tests/test_conductor.py::test_g7_start_refuses_unfinished_run_unless_force` |
| G8 合格済み PBI の再実行・二重記録を防ぐ | 起動・完了 | ② | PBI の状態と完了済み起動 ID を確認 | `tests/test_conductor.py::test_g8_passed_pbi_never_relaunched_or_double_recorded` |
| G9 終了コードで合否判定 | 検査完了 | ③ | 結果の `exit_code` | `tests/test_conductor.py::test_multi_wave_autofix_and_rebuild_retry` |
| G10 文字化けした承認を出さない | 承認ページ生成 | ② | draft の文字化け検査・描画エラー拒否。進行役への結合は未実装 | `tests/test_pages.py::test_render_refuses_corruption_before_launch` |
| G11 禁止引数を渡さない | 部品起動前 | ① | `forbidden_args` と部品設定。起動処理への結合は未実装 | `tests/test_guards.py::test_component_forbidden_arguments` |
| G12 削除・外部変更の前に聞く | 差分検査・判断 | ③④ | `destructive_changes` が削除・migration・schema を列挙。検出後に承認を求める結合とルート外監視は未実装 | `tests/test_guards.py::test_diff_deletions_migrations_and_schema` |
| G13 拒否の回避禁止 | 役の起動 | ④① | 役への指示とランナー権限設定。回避全般の機械的検証は未実装 | `tests/test_plan.py::test_worker_prompt_has_only_g12_and_g13_rules`（指示の検査） |
| G14 途中で止まらない | 役の完了 | ③ | 指定 PID と非空の結果を `compare` で検査。子孫の自動発見は未実装 | `tests/test_runners.py::test_guard_detects_changed_existing_untracked_and_lock`（結果欠落） |

