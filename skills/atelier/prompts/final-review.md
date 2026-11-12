# Prompt: final review or security review

Give this to an `atelier-reviewer` subagent (not `pbi-worker`, which has no Skill tool) to run a review skill over the whole diff (`appendix/review.md`). Fill in the brackets.

```text
description: FINAL.review <short text>   (FINAL.security for the security review)
subagent_type: atelier-reviewer
prompt:
Final review of this repository's work. Report only: do not edit, commit, push or post anything.
- base: <merge-base sha>; head: <HEAD sha>; spec: <docs/specs/<slug>.md, or none>
- Put these lines in the skill's request as well, for skills that read them (lens-review does):
  Base: <merge-base sha>
  Target: the working tree compared with Base. The changes are not committed, so `Base..HEAD` shows nothing.
  Spec: <the spec path, or none>
  Rules: <docs/pbi/notes/review-rules.md, or none>
- Call the Skill tool with <skill name>. For the built-in code-review, pass the argument medium; for the others, pass no argument.
- Never pass --fix, --comment, ultra or --max-findings.
- If the skill asks you a question, do not answer it. Stop and write BLOCKED.
- Lint recheck: <the path of docs/pbi/jevlint/recheck.md and its report rows, or none>. Look at those places; do not run jevlint.
- <the implementer block of prompts/kiln-chamber.md, when kiln is on>
- Write <docs/pbi/reviews/final.review.md, or docs/pbi/reviews/final.security.md for security>.
  Line 1: findings: <critical>,<major>,<minor>
  Then one line per finding: - [critical|major|minor] <file>:<line> — <why> — <repro>
  critical: an acceptance criterion fails, something breaks, or stored data or an outside system is affected.
  major: the change drifts from the spec's intent, decides a behavior the spec leaves open, or contradicts an accepted decision record (ADR).
  minor: wording or a small mismatch; the behavior still matches the spec.
  If you stopped on a question, line 1 is BLOCKED: <reason>, with no finding lines.
- When the skill prints its list in this same line format (`- [severity] file:line — ... — ...`), copy those lines as they are. Raise a finding to critical when it breaks an acceptance criterion.
- When the skill also prints details for each finding (why, evidence, fix), copy them to <docs/pbi/reviews/final.review.detail.md, or final.security.detail.md>. This is the only other file you may write.
- Your final message: the three counts and one line per finding, nothing else.
```
