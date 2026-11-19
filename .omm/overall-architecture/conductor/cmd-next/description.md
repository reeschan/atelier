`next --root`。`pending_action` があればそれを返し、無ければ `actions.ensure_pending_action` で作る (冪等)。runner が agent の launch なら、このときに `pending_snapshot` を取る (`_ensure_snapshot`)。テンプレートの切り替えの期限もここで見る。
