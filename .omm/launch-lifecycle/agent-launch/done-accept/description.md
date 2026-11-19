`_cmd_done` の後半。結果 JSON を役ごとに検証し (計画なら validate_plan と waves など)、使用量と launch の記録を足し、guard の check を pass で記録し、`_finalize_completion` で受け入れる。
