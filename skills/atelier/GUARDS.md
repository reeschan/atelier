# 守りの規則 G1〜G14

止め方：①起動前、②進行役が拒否、③実行後の検査、④指示。検査欄は確認済みの範囲を示し、未結合のものは明記する。

| 規則 | 工程 | 止め方 | 機械での形・現状 | 検査（skills/atelier からのパス） |
|---|---|---|---|---|
| G1 作業ルート外に書かない | 役の起動・完了 | ①②③ | ランナーの設定と `snapshot` / `compare`。外のデータの置き先（`data_needs.dest`）は計画の検査でリポジトリの中だけに限る。`approve-edit` で登録した、ユーザー承認済みの変更（登録した中身のまま）は通す。実装役による既にあるファイルの中身の変更は、許可外でも通して報告に載せる（`do_not_touch`・足す・消すは止める。修正の PBI はテストのファイル（`is_test_file`）を変えられない）。一斉起動では、起動の前の snapshot 1 つと起動ごとに比べ、仲間の `targets` と、仲間だけが `outside_edits` で申告したファイルの変更は除く。2 本が同じファイルを申告したら両方の違反にする（仲間の `targets` への書き込みは見抜けない。指示で避け、wave の検査で受け止める）。`acceptance_first` の run では、planner の確認の後、実装・E2E の PBI を起動する前に鍵をかける（`locks.lock`：`docs/pbi/locks.json` と鍵のファイルだけの鍵のコミット、`<git common dir>/atelier/<run>/` の写しとコミットの id、`ATELIER_LOCKS_HOME`（既定 `~/.config/atelier/locks/`）の写しの sha256）。鍵の前に kind `build`・`e2e` の PBI を起動すると `flow.check_invariants` が拒む（never BuildBeforeLock）。鍵の後は、どの起動の snapshot も鍵のコミットの blob を持ち、`compare` が鍵のファイルと `locks.json` を `targets` の内外を問わず git の blob（`git hash-object`、改行の変換を含む）で比べて止め（`locked file changed`）、進行役が鍵のコミットの中身に戻す（`locks.restore`、`lock_restored` に記録）。E2E の PBI が wave の検査に通ると、その `targets` のうち在るファイルを鍵に足して鍵のコミットを進め、外の写し・コミットの id・ハッシュを書き直す（`locks.extend`、`lock_extensions` に記録）。以後の起動は足したファイルも比べる。例外は鍵を外す修正だけ：振り返りで鍵を外すと決まったケースを直す修正の起動に限り、そのケースの受け入れテストのファイル（`unlock.acceptance_files`）を比べない（`locks.without`）。その起動でも、ほかの鍵のファイル・一覧・ソースの変更は止める。直した行は鍵のコミットの一時的な worktree で確かめてから鍵をかけ直す（`locks.relock`、一覧の `unlocks` に記録）；ルート外と無視ファイルの完全な監視は未実装 | `tests/test_runners.py::test_guard_detects_changed_existing_untracked_and_lock`（許可外パスのみ）、`tests/test_review_loops.py::test_dest_outside_the_repository_is_refused`（外のデータの置き先）、`tests/test_user_decisions.py::ApprovedEditTests`、`tests/test_carry_over.py::OutsideEditTests`（対象の外の変更と修正の PBI のテスト）、`tests/test_launch_batches.py::AgentBatchTests`（一斉起動の比較と申告のぶつかり）、`tests/test_locks.py::LockTests::test_a_worker_that_changes_a_locked_test_is_stopped_by_g1_and_the_file_is_put_back`・`test_a_worker_that_rewrites_the_list_or_deletes_a_locked_file_is_stopped_and_both_come_back`・`test_a_launch_batch_member_that_changes_a_locked_test_is_stopped_and_the_file_is_put_back`（鍵のファイルと一覧の変更を止めて戻す）、`test_a_line_ending_change_of_a_locked_file_is_not_a_violation`（blob で比べる）、`test_no_build_or_e2e_pbi_is_launched_before_the_lock`（鍵の前の起動）、`test_review_ok_writes_the_list_and_commits_only_the_locked_files`・`test_copy_and_commit_id_go_outside_the_work_tree_and_the_hash_to_the_locks_home`（鍵のコミット・外の写し・ハッシュ）、`tests/test_e2e_pbi.py::LockedE2ePbiTests`（E2E のファイルを鍵に足し、後の起動の変更を G1 で止めて戻す）、`tests/test_locks.py::LockExtendTests`（鍵への追加と鍵のコミットの進め方）、`tests/test_unlock.py::UnlockTicketAndGuardTests`（鍵を外す修正の例外：開いたファイルだけを通し、ほかの鍵のファイルとソースの変更を止める）、`tests/test_unlock.py::UnlockCheckTests`（一時的な worktree での確かめと鍵のかけ直し） |
| G2 履歴操作・投稿をしない | 役の起動・完了 | ①③ | deny 設定と HEAD・branch・stash の比較。push・投稿の事後検出は未実装 | `tests/test_flow_rules.py::test_agent_launch_snapshot_at_next_compare_at_done`（変更検出の経路） |
| G3 承認外への送信禁止 | 外部役の起動前 | ② | `sends_approved` と承認時に固定した PBI → runner 表。進行役への結合は未実装 | `tests/test_guards.py::test_sends_require_approved_pbi_runner_pair` |
| G4 秘密の送信・記録禁止 | 送信・記録前 | ③ | `scan_secrets` の型と行番号。検出は近似で、進行役への結合は未実装 | `tests/test_guards.py::test_secrets_are_detected_without_values` |
| G5 インストール・全体設定は承認後 | 起動前・完了後 | ①② | deny 設定、`install_attempts` の依存ファイル差分。全体設定の検出と承認の結合は未実装 | `tests/test_guards.py::test_dependency_snapshot_changes` |
| G6 答えを作らない | 回答 | ② | `answer` の入力だけを回答として記録 | `tests/test_conductor.py::test_g6_answer_only_via_answer_command` |
| G7 未完了 run を潰さない | 開始 | ② | 明示的 force なしの開始を拒否 | `tests/test_conductor.py::test_g7_start_refuses_unfinished_run_unless_force` |
| G8 合格済み PBI の再実行・二重記録を防ぐ | 起動・完了 | ② | PBI の状態と完了済み起動 ID を確認 | `tests/test_conductor.py::test_g8_passed_pbi_never_relaunched_or_double_recorded` |
| G9 終了コードで合否判定 | 検査完了 | ③ | 結果の `exit_code` | `tests/test_conductor.py::test_multi_wave_carry_over_holds_the_failing_fix_pbi_as_unresolved`（wave の検査の終了コードで落とし、2 回落ちた修正の PBI は聞かずに未解決）、`tests/test_final_check.py::FinalCheckTests::test_a_failed_final_check_is_left_over_and_no_fix_worker_is_launched`（最後の検査の終了コードで残りにする） |
| G10 文字化けした承認を出さない | 承認ページ生成 | ② | draft の文字化け検査・描画エラー拒否。進行役への結合は未実装 | `tests/test_pages.py::test_render_refuses_corruption_before_launch` |
| G11 禁止引数を渡さない | 部品起動前 | ① | `forbidden_args` と部品設定。起動処理への結合は未実装 | `tests/test_guards.py::test_component_forbidden_arguments` |
| G12 削除・外部変更の前に聞く | 差分検査・判断 | ③④ | `destructive_changes` が削除・migration・schema を列挙。検出後に承認を求める結合とルート外監視は未実装 | `tests/test_guards.py::test_diff_deletions_migrations_and_schema` |
| G13 拒否の回避禁止 | 役の起動 | ④① | 役への指示とランナー権限設定。回避全般の機械的検証は未実装 | `tests/test_plan.py::test_worker_prompt_has_only_g12_and_g13_rules`（指示の検査） |
| G14 途中で止まらない | 役の完了 | ③ | 指定 PID・非空の結果を `compare` で検査し、実装役の後は `garbled_changes` で文字化け（`U+FFFD`・新規 `???`・`CJK?CJK`）を検出。子孫の自動発見は未実装 | `tests/test_runners.py::test_guard_detects_changed_existing_untracked_and_lock`（結果欠落）、`tests/test_guards.py::test_garbled_changes_detects_fffd_q_runs_and_cjk_split_ignoring_base_binary_and_fakes`、`tests/test_integration.py::test_worker_garbled_text_raises_guard_question` |

## never の規則

状態の図の never の規則は、進行役が状態に入るたびに `flow.check_invariants` で拒み、遷移表に道を置かないことで塞ぐ。

| never の規則 | 機械での形 | 検査（skills/atelier からのパス） |
|---|---|---|
| 承認した計画なしに作る（BuildWithoutPlan）・承認した仕様なしに計画する（PlanWithoutSpec） | `check_invariants` | `tests/test_flow_rules.py::TestFlowRulesSeam1::test_never_rules` |
| 承認した外のデータが置かれる前に作る（BuildBeforeData） | `check_invariants` | `tests/test_review_loops.py::DataPreparationTests::test_building_before_data_is_in_place_breaks_a_never_rule` |
| 受け入れを先に置かない run で受け入れテストを作る（AcceptanceWithoutToggle） | `check_invariants` | `tests/test_acceptance_red.py::AcceptanceStageTests::test_acceptance_stage_is_never_entered_without_the_toggle` |
| Red の確認が済む前に作る（BuildBeforeRed） | `check_invariants` | `tests/test_acceptance_red.py::AcceptanceStageTests::test_building_before_the_red_check_is_done_is_refused` |
| 鍵をかける前に、実装・E2E の PBI を起動する（BuildBeforeLock） | `check_invariants` | `tests/test_locks.py::LockTests::test_no_build_or_e2e_pbi_is_launched_before_the_lock` |
| 作業者が鍵のファイルを変えたまま、次の起動へ進む | G1 の `compare` と `locks.restore`（上の G1）、wave の検査での鍵の比較 | 上の G1 の鍵の検査、`tests/test_e2e_pbi.py::LockedE2ePbiTests::test_a_change_of_the_e2e_file_that_gets_past_g1_is_put_back_by_the_wave_check` |
| 見直しを 2 回する（SecondReview） | `check_invariants` | `tests/test_flow_rules.py::TestFlowRulesSeam1::test_never_rules` |
| 承認した直す一覧なしに修正の段に入る（FixWithoutList。E2E の失敗は理由にならない） | `check_invariants`、E2E から修正の段への遷移を置かない | `tests/test_flow_rules.py::TestFlowRulesSeam1::test_never_rules` |
| 修正の段に 2 回入る（SecondFixStage） | `check_invariants`、直す一覧の承認を 1 回だけ通す遷移 | `tests/test_final_check.py::NeverRuleTests::test_a_second_entry_into_the_fix_stage_is_refused`・`test_the_fix_list_cannot_be_approved_a_second_time` |
| 最後の検査で落ちたものを、もう一度直しに行く（FixAfterFinalCheck） | `check_invariants`、最後の検査から報告への遷移だけを置く | `tests/test_final_check.py::NeverRuleTests::test_going_back_to_the_fix_stage_after_the_final_check_is_refused`・`test_the_final_check_goes_only_to_the_report`、`tests/test_final_check.py::FinalCheckTests::test_a_failed_final_check_is_left_over_and_no_fix_worker_is_launched` |
| ユーザーに聞かずに、途中で決める役に戻る（ReturnWithoutAsking） | `check_invariants`、問いの状態からだけ計画へ戻る遷移 | `tests/test_flow_rules.py::TestFlowRulesSeam1::test_never_rules` |

