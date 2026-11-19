`_cmd_run`。外部 runner の launch 1 本の始めから終わりまで。G3・G4・G11 を確かめ直し、snapshot を取り、`run_external` で動かし、使用量と launch の記録を足し、作業木を比べ、guard の check を記録する。使えない理由か違反があれば guard の問い、無ければ結果 JSON を読んで受け入れる。
