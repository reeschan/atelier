修正の段。subphase build では `fix_group_index` のグループの作業者を 1 本起こし、返るたびに index を進める。全グループが終わったら subphase check で修正の検査を 1 回。通れば E2E へ、落ちたら index を 0 に戻して全グループをやり直す。E2E の失敗から来たときはグループが無く、FIX-E2E の作業者 1 本。
