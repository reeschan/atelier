---
name: pbi-orchestration-jev
description: Old name of the atelier skill. Use /atelier instead; this name only points there and passes the request on.
---

# Renamed to atelier

This skill is now **atelier**. Reply in the user's language.

1. If `atelier` is in your skill list, tell the user in one line that `/pbi-orchestration-jev` is now `/atelier`, then run the atelier skill with the same arguments (the Skill tool, skill `atelier`). Do nothing else here.
2. If it is not, tell the user the skill was renamed and how to install it, then stop:

   ```sh
   git clone https://github.com/reeschan/pbi-orchestration-jev.git atelier
   cp -r atelier/skills/atelier ~/.claude/skills/
   ```

   Then restart Claude Code and run `/atelier`.

Nothing moves on its own: the home `~/.config/pbi-orchestration-jev` is still used while `~/.config/atelier` does not exist, `PBI_HOME` is still read (`ATELIER_HOME` is the new name), and `docs/pbi/` work resumes with `/atelier resume`. An rtk hook in `settings.json` must point to `skills/atelier/rtk/rtk-gate.mjs`.
