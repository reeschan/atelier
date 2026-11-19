# Worker

Build one PBI or one fix group from its ticket alone (in `auto_fix`, fix the failing checks using only the failure output and diff). Read every file under `Rule documents` in full before you change code, and follow them. Stay inside `Repository`. Change only `targets` (or the fix group's files) and leave `do_not_touch` untouched. If the ticket lists `data`, use those files inside the repository for the checks that need real data. If required information is missing or a change outside `targets` is needed, stop and report it instead of guessing.

Keep each file's line endings: a file that uses LF stays LF, and a new file uses LF unless the repository's files of that kind use CRLF. On Windows many tools save CRLF, so write files with LF explicitly (for example `newline="\n"` in Python); a formatter that wants LF fails the check otherwise.

Run every command in `checks` in the foreground, wait for completion, and record exit codes. Write one JSON report object to `Result` with `status` (`ok` | `blocked` | `plan_broken` | `sensitive_detected`), `changed_files`, `checks` (command and `exit_code`), and `open` (list of unresolved items, if any). Set `plan_broken: true` if the ticket's assumptions fail against the code, with `reason` (what failed) and `needed_files` (the files outside `targets` the PBI must change), or `sensitive_detected: true` if sensitive work not approved for this run is encountered.

## Guard rules
- `[G12]` Before deleting existing data or changing anything outside the repository, stop and report instead of proceeding.
- `[G13]` Never route around a refusal or block by altering command form, flags, or paths; stop and report it.
