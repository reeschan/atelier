呼び手が `drive --root <root> --done <番号>` を裏で流す（一斉起動なら一斉起動の番号を 1 回）。drive は先に `_cmd_done` でこの次の行動を片付け、その先の check や外部の起動を続けて片付ける。
