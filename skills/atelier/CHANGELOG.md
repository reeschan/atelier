# atelier CHANGELOG

追記のみ。過去の行は書き換えない。

## Unreleased

- Kiln docs: when Kiln is on and `docs/pbi/kiln.json` is absent, thresholds come from the enabled workflow template; copying the sample overrides them. Setup keeps an existing `kiln.json` unless the user approves a replace. Handoff stores `Run id:` and resume restores it. While Kiln is on, measure at stage boundaries as well as wave boundaries.
- Kiln (窯): optional context-capacity constraint. Off unless `docs/pbi/kiln.json` exists or the workflow template sets `kiln.enabled`. See `appendix/kiln.md` and the sample `examples/kiln.json`. Bundled templates (`workflows/jev-routed.json`, `workflows/high-model.json`, `examples/workflows/recommended.json`) carry a `kiln` stub with `prompt_install` that offers [sac-strategic-compact](https://github.com/reeschan/sac-strategic-compact). The stub does not turn kiln on and does not install the skill.
- README.md and README.ja.md: Highlights describe optional Kiln and link `appendix/kiln.md` and sac-strategic-compact.
