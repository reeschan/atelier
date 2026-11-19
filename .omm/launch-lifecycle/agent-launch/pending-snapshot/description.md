`_ensure_snapshot`。待つ行動が agent の launch なら、その launch の allowed_paths・結果のファイル・外の変更の規則で snapshot を取り、launch id を添えて `run["pending_snapshot"]` に置く。同じ launch なら取り直さない。
