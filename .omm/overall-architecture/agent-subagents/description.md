runner が `agent` の起動。進行役は起こさず、呼び手が Agent ツールでサブエージェント（atelier-role）を起こす。進行役は次の行動を出すときに pending_snapshot を取り、`done` で作業木と比べる。一斉起動なら、呼び手は全員を 1 つのメッセージで前景で同時に起こして待ち、`drive --done <一斉起動の番号>` を 1 回流す。
