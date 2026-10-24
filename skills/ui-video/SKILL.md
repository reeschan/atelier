---
name: ui-video
description: Records a short walkthrough video of UI pages (HTML mockups or a running app) from a storyboard, with captions, as MP4. Use to show a UI design or a built screen in motion. Not for narrated explainers or editing footage.
---

# UI video

Make a short video that walks through one or more screens: open each page, show a caption, hover, click, type and scroll, then hold. Reply in the user's language.

Needs Node 18 or newer and Playwright (`npm install -g playwright && npx playwright install chromium`). With ffmpeg on PATH the output is MP4 (H.264); without it, WebM.

## Steps

1. **Pick the pages.** HTML files (mockups from a design skill, `docs/pbi/ui/<slug>/`) or a URL of a running app. For a dev server, start it first and use its local URL.
2. **Write the storyboard** next to the output, as `storyboard.json`. Keep it short: 3 to 8 scenes, 20 to 60 seconds in all. One idea per scene, and a caption that says what the viewer should notice.

   ```json
   {
     "viewport": {"width": 1280, "height": 800},
     "out": "walkthrough.mp4",
     "scenes": [
       {"page": "settings.html", "caption": "Settings: one page for every account option", "hold": 1500},
       {"page": "settings.html", "caption": "Saving shows a toast", "steps": [{"click": "#save"}, {"wait": 800}]},
       {"page": "http://localhost:5173/billing", "caption": "Billing", "steps": [{"scroll": 600}], "hold": 1200}
     ]
   }
   ```

   - `page`: a path (from the storyboard's folder) or an `http(s)://` / `file://` URL.
   - `steps`, each one of: `{"wait": ms}`, `{"scroll": px}`, `{"hover": "css"}`, `{"click": "css"}`, `{"type": ["css", "text"]}`.
   - `hold`: ms to stay on the scene after its steps (default 1500, at most 10000). Captions are at most 140 characters.
3. **Check, then record.**

   ```bash
   node <skill>/scripts/record.mjs <folder>/storyboard.json --dry-run
   node <skill>/scripts/record.mjs <folder>/storyboard.json
   ```

   Exit codes: 0 written, 2 bad storyboard or a missing page (the `error` lines say which), 3 Playwright not installed (print the install command and stop), 4 recording failed.
4. **Report** the video's path and its length in one line. Do not open or describe the video frame by frame. When it runs inside Atelier's `ui-design` stage, also list the video in `docs/pbi/ui/<slug>/README.md`.

## Rules

- Only record pages the user's work owns, or public pages they asked for. Never sign in or type secrets in a recorded page.
- The script writes only the output file (and a temporary folder it deletes). It does not change the pages.
