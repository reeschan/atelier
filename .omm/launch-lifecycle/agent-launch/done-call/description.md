呼び手が `drive --root <root> --done <launch_id>` を裏で流す。drive は先に `_cmd_done` でこの launch を片付け、その先の check や外部の launch を続けて片付ける。
