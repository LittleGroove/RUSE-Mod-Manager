# Screenshots

Project-wide, reusable screenshots of the RUSE Mod Manager — **real captures only, no**
**AI-generated images**. Reused by docs/guide, the README, and community announcements.

Each row is a *named slot* (a stable hook). Capture with `python shots.py <key>` (or
`python shots.py --all`); anything referencing `screenshots/<key>.png` keeps working when
you overwrite the file. Add slots by editing `IMAGE_SLOTS` in `shots.py`.

| Slot | Shows | Captured? |
| --- | --- | --- |
| `main-window` | The Mod Manager main window with the mod list — the hero shot. | main-window.png |
| `mod-list` | Close-up of the mod list with several mods enabled. | mod-list.png |
| `convert-tab` | The Convert tab turning a raw mod into an .rmod. | convert-tab.png |
| `mod-editor` | The Mod Editor open on a mod project. | mod-editor.png |
| `share-mod` | The Share Mod button / share flow. | share-mod.png |
| `settings` | The Settings tab (game path auto-detect, language). | settings.png |

## Category folders (for guides)

Guide step-by-step images live in subfolders, captured with a slashed key:
`python shots.py guide/convert/step1` -> `screenshots/guide/convert/step1.png`. They don't
need a registry row — the guides reference them directly.

## Annotation

Add arrows/boxes/numbers/labels with `annotate.py`: `python annotate.py --new
screenshots/<img>.png` writes a `<img>.ann.json` you edit, then `python annotate.py
screenshots/<img>.png` renders `<img>.annotated.png`. Reference the `.annotated.png` in a
guide. `python annotate.py --all` re-renders everything after re-capturing.
