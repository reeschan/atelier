`next --root`。`pending_action` があればそれを返し、無ければ `actions.ensure_pending_action` で次の行動を作る（冪等）。agent の起動か一斉起動なら、このときに `pending_snapshot` を取る（`_ensure_snapshot`）。テンプレートの切り替えの期限もここで見る。
