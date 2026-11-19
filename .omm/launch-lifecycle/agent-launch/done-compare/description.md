`_cmd_done` の前半。`_validate_pending_launch` で launch id を確かめ、`pending_snapshot` があれば `_collect_post_launch_violations` で比べる。違反があれば (結果があれば使用量を記録してから) guard の check を記録し guard の問い。
