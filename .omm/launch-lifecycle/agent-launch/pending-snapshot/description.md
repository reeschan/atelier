`_ensure_snapshot`。次の行動が agent の起動なら、その起動の allowed_paths・結果のファイル・外の変更の規則で snapshot を取る。agent の一斉起動なら、作業木の snapshot を 1 つだけ取る。どちらも番号を添えて `run["pending_snapshot"]` に置き、同じ番号なら取り直さない。
