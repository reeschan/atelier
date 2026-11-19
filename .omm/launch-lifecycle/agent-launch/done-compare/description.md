`_cmd_done` の前半。`_validate_pending_launch` で番号を確かめる。1 本の起動は `pending_snapshot` と `_collect_post_launch_violations` で比べ、違反があれば guard の問い。一斉起動は `_finish_batch` に渡し、起動ごとに比べる。
