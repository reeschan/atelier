# Appendix: rework and stopping

Read this when the fix stage picks `fix.rework` (verify or the final review decided rework), or a stop condition is met.

## Rework

The limit is the template's `fix.max_rework` (2 in the bundled templates). Each rework starts a new worker from the head of the same route.

1. Write `docs/pbi/PBI-<n>.rework-<k>.md`:

   ```markdown
   ## Rework <k>: PBI-<n>

   - Criterion <number>: fail
     - Now: <what happens; the error log or the line>
     - Expected: <what passing looks like>
     - Repro: <a command or steps that show the failure>
   - Out-of-scope change: <file and what to revert, if any>
   ```

2. Check the note with the Jev gate `rework_quality` (names_condition, states_current_and_expected, has_repro; `appendix/jev.md`). Rewrite any item under 0.6 before sending.
3. Launch it with the prompt of `runners.md`, report name `PBI-<n>.rework-<k>`, and add one to the PBI's rework count in STATUS.md.

## Stop

Stop a PBI when it still fails after the last allowed rework, or when it needs a third continue. Raise its tier, rewrite the PBI or build it yourself only when the user picks that.

- PBIs that do not depend on it keep running. PBIs that depend on it are held.
- Tell the user, then wait:
  - which PBI stopped, and on which model
  - what failed on each attempt
  - the likely cause: the PBI said too little, the model fell short, or the design is wrong
  - the choices: raise the tier and run again, rewrite or split the PBI, revisit the design, or the user takes it over
- Raising the tier: `python <skill>/prepare.py docs/pbi --only PBI-<n> --tier <tier>` rewrites its `route.json` from the routing in force at that time; start from the head of the new route.
