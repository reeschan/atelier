# atelier CHANGELOG

追記のみ。過去の行は書き換えない。

## Unreleased

- Kiln (窯): optional context-capacity constraint. Off unless `docs/pbi/kiln.json` exists or the workflow template sets `kiln.enabled`. See `appendix/kiln.md` and the sample `examples/kiln.json`. Bundled templates (`workflows/jev-routed.json`, `workflows/high-model.json`, `examples/workflows/recommended.json`) carry a `kiln` stub with `prompt_install` that offers [sac-strategic-compact](https://github.com/reeschan/sac-strategic-compact). The stub does not turn kiln on and does not install the skill.
- README.md and README.ja.md: Highlights describe optional Kiln and link `appendix/kiln.md` and sac-strategic-compact.
