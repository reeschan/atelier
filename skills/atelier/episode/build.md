# Episode: build

The steps of the build episode: one wave, or the PBIs a later episode sent back.

## The episode

- The wave in the prompt's `Wave:` line; with a `PBIs:` line, only those PBIs. Run `episode/one-pbi.md` for each, repeat attempts for reworks inside the episode, then with tests at the end on, the tester for the deferred commands (kind `wave`, no E2E) and reconcile it. End with `done`: next the next wave, or after the last wave `test` (tests at the end on) or `review`. E2E does not run after the waves: it runs once at the very end.
- A build with a `PBIs:` line (a rework sent back by test) never names the next wave: end with `done`, next the episode on the prompt's `Sent by:` line (`test`, or `review` in a run started before the final review stopped sending back), with the same wave. Any other next is refused.
- A PBI at `hold (stale; …)` in STATUS.md is waiting for a re-approval: `episode/one-pbi.md` (the `stale` step) and `episode/approve.md` say what to redo and how to ask.
