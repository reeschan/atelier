runner が agent の launch。conductor は起こさず、呼び手が起こす。drive は launch を `next` に返して止まり、呼び手は裏でサブエージェントを起こしてターンを終え、終わりの知らせで起きて `drive --done <launch_id>` を裏で流す。1 本ごとに親の会話が 3 回起きる。
