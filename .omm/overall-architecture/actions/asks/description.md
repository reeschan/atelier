問いの行動。`_make_ask` は問いのファイルと選択肢、`_make_approval_ask` は仕様・計画・直す一覧の承認ページ（pages.py）。guard の問い（`_guard_ask`）は retry・next_step・stop を出す。一斉起動で違反した起動が 2 本以上なら、retry は「違反した起動をもう一度試す」になり、next_step は出さない。
