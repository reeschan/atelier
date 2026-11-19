外部の CLI (claude・codex・agy・opencode・dsh)。登録は ~/.config/atelier/agents.json か環境変数 ATELIER_<RUNNER>_CMD。G3 で送り先が承認されていないと起こさない。作業の根 (work root) を cwd にして動き、結果 JSON を results/ に書く。
