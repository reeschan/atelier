`runners.run_external(spec, timeout)`。stdout と stderr を results/<id>.json.stdout・.stderr に書き、codex と claude には指示 1 行を stdin で渡す。timeout (drive の --timeout、既定 600 秒) を過ぎると止める。終わったら使用量と使えない理由を返す。
