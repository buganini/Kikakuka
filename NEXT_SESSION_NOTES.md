# Next Session Notes - Per-Cut Refactoring

## Current State
- Worktree: `epic-cerf`
- File: `FreekiCAD/FreekiCAD/LinkedObject.py`
- Bend 9 re-entry wedge WORKS: d_offset IN, 91/91 slices
- M-piece beyond re-entry correctly positioned
- All normal (non-re-entry) wedges work
- Components correct

## What was done
- Chain-based virtual_plc in Phase 3: compose same-bend mi rotations
  that affect the S-side parent but not the bend_obj
- Saved in micro_pivots so wedge loft uses consistent values
- bend_sign: `-1 if micro_angle > 0 else 1` (per-cut from angle)
- Normal oriented per-cut from BFS parent piece position
- No swapping/curl-back/negation/sign/flip/skip/cancel/XOR/mult

## Remaining Issue: bend 7 re-entry
- Bend 7 re-entry wedge (mi=12, seg 1): d_offset OUT
- Chain composition in Phase 3 changes the pivot for mi=12
- But the composed result doesn't match the actual wedge piece position
- Need to investigate: the composition might need to account for
  additional same-bend mi's in the chain (bend 7 has 3 segments)
- Also deeper wedges (bend 6, bend 7 inner loop) still OUT

## Architecture
- Each mi: angle, normal (from parent), cut_mid, radius, bend_index
- Normal: per-cut, oriented from BFS parent into wedge
- Phase 3: virtual_plc = plc + same-bend mi rotations from parent chain
- Pieces rotate around virtual pivot
- Wedge loft uses saved virtual_plc values (no re-composition)
- Only special case: bend line doesn't rotate by its own bend
