<!--
  This pull request template has ONE job: adding a mod (.rmod) to the pack.
  Two steps: (1) upload your .rmod into source/example_mods/, (2) tell us which build it's for.
  On the next build your mod is pulled in, baked into the app, and shipped to everyone.
  (For bug reports or ideas, open an Issue instead.)
-->

## 1. Upload your mod

Add your `.rmod` file to this pull request, and tell us about it:

**Mod name:**
<!-- e.g. Endless Defence -->

**What it does:** (a line or two)
<!-- e.g. Adds a survival mode where waves of enemies attack your base. -->

## 2. Which build is it for?

Your mod is made for one version of the game. Each version has its own folder (named by its
**build id**), and you know it by its **branch name** — the same name the Mod Manager shows at
the top. **Tick the one that matches**, and put your file in that folder:

- [ ] **public** — the normal, up-to-date game → `source/example_mods/v24003166/YourMod.rmod`
- [ ] **compat-2** → `source/example_mods/v23661872/YourMod.rmod`
- [ ] **compat-3** → `source/example_mods/v23660935/YourMod.rmod`
- [ ] **compat-4** → `source/example_mods/v23738184/YourMod.rmod`
- [ ] **compat** — the original, pre-remaster RUSE → `source/example_mods/v3591/YourMod.compat.rmod`

> **Not sure which one?** Open the Mod Manager — the branch name is shown at the top. Most
> players are on **public**.
>
> **Naming:** name the file `YourMod.rmod`. Only the original **compat** build uses
> `.compat.rmod` (see the last row).
>
> Made your mod for more than one build? Tick each one and add a copy to each folder.

## Checklist

- [ ] My `.rmod` is in the `source/example_mods/` folder that matches the build I ticked above.
- [ ] I tested it in-game on that build and it works.
- [ ] It's my mod, or I have the author's OK to share it.
- [ ] This PR only adds `.rmod` file(s) — nothing else is changed.

<!--
  After a maintainer merges this, the next build automatically picks up your file from
  source/example_mods/, bundles it into the app, and packages it into the next release.
  Thank you for contributing!
-->
