"""Release hold-backs: features whose code ships but whose buttons are hidden until proven.

One switch per held-back area, so bringing a feature back is one line here.  A held-back feature's
engine code and its tests stay in place and keep running; only the button that opens it is hidden
(not greyed out, so nobody is left wondering why a button does nothing).
"""

# Everything in the mod editor that makes a copy of a mission, scenario, unit or game object, plus
# Migrate to nation.  Hidden for the release after 1.1.9 (decided 2026-09-22): none of it is proven
# in-game yet.  Gates: map editor "New Operation" + "New scenario...", units editor "Duplicate this
# unit" + "Migrate to nation...", Raw / Asset editor object "Duplicate".
# NOT gated (checked, not cloning in this sense): copying a placement on the map, the HQ camera-path
# copy, "Duplicate this ammo", and Duplicate inside the list editors.
# Re-enable checklist: plans/Action Reports/2026-09-22 release readiness - hide cloning and fix blockers.md
CLONING_ENABLED = False
