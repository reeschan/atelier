Git の作業の根なら snapshot を取る。1 本の起動は `_launch_snapshot`（その起動の allowed_paths・結果のファイル・外の変更の規則を入れる）。一斉起動は `guards.snapshot` を 1 つだけ取り、起動ごとの場所と規則は比べるときに足す。Git でなければ取らない（結果のファイルの有無だけを見る）。
