`state.record_usage` で run.json の使用量に足し、`_record_launch_event` で record.jsonl に launch の記録 (読んだ部品を含む) を足す。記録の行に秘密があれば G4 の違反に足す。PBI ごとの launch id を `pbi_launches` に残す (wave の検査の記録が参照する)。
