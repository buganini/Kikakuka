# Next Session Notes - Per-Cut Refactoring

## Current State
- Worktree: `cool-feynman`
- File: `FreekiCAD/FreekiCAD/LinkedObject.py`

## What was done (this session)

### Root cause: rotation order in chain composition
The chain composition prepended same-bend mi rotations as the outermost
transform in virtual_plc. But in the piece's actual transform chain,
same-bend rotations happen at their correct micro_order position
interleaved with cross-bend rotations. Since 3D rotations don't commute,
the chain composition produced wrong results for deeper wedges.

Example: for mi=12 (bend 7 seg 1) with S-parent p26:
- Chain composition: virtual_plc = rot(mi=11) * rot(mi=14) * rot(mi=17) * rot(mi=16) * rot(mi=5) * plc
- Actual piece transform: rot(mi=14) * rot(mi=17) * rot(mi=16) * rot(mi=11) * rot(mi=5)
- mi=11 is in the wrong position → d_offset OUT

### Fix: piece transform tracking (replaces chain composition)
1. **piece_plc array**: Track accumulated Placement per piece. Each time
   a piece is rotated in Phase 3, also compose the rotation into
   piece_plc[pi]. This preserves the correct interleaved order.

2. **virtual_plc = piece_plc[s_parent] * plc_original**: Instead of
   chain composition, multiply the S-parent's accumulated transform by
   the bend's ORIGINAL placement (saved before Phase 3 starts). This
   correctly maps the bend's local coordinates to the current 3D space.

3. **M-entry pre_shape**: For re-entry wedges, rebuild pre_shape from
   the flat piece with the S-side neighbor's piece_plc transform (since
   the wedge's own piece_shapes was rotated by the M-side path's mi_set).

### Key data structures added
- `piece_plc[pi]`: Accumulated Placement (identity initially)
- `bend_plc_original[name]`: Original bend placements before Phase 3

### Affected wedges (all should now have d_offset IN)
- p27 (W7, s_mi=12): was d_offset=0.4453 OUT, s_mult=2
- p29 (W6, s_mi=9): was d_offset=-0.6075 OUT
- p31 (W6, s_mi=8): was d_offset=0.6560 OUT, s_mult=2
- p33 (W7, s_mi=10): was d_offset=-0.3587 OUT, s_mult=3
- p35 (W8, s_mi=13): was d_offset=0.0783 OUT, s_mult=2
- p37 (W9, s_mi=15): was d_offset=0.0610 IN but only 66/91 slices

### Previous session work
- Chain-based virtual_plc in Phase 3 (now replaced)
- Saved in micro_pivots so wedge loft uses consistent values
- bend_sign: `-1 if micro_angle > 0 else 1` (per-cut from angle)
- Normal oriented per-cut from BFS parent piece position
- No swapping/curl-back/negation/sign/flip/skip/cancel/XOR/mult

## Needs Testing
- ALL wedges should now have d_offset IN (including inner loop ones)
- p37_w9: should now get full 91/91 slices and correct placement
- p27, p28: p27 should loft correctly, p28 correction should be right
- Previously working wedges should still work
- Components should still be correctly positioned

## Architecture
- Each mi: angle, normal (from parent), cut_mid, radius, bend_index
- Normal: per-cut, oriented from BFS parent into wedge
- Phase 3: virtual_plc = piece_plc[s_parent] * plc_original
  - piece_plc tracks accumulated rotations per piece
  - plc_original saved before Phase 3 modifies bend placements
  - For M-entry: s_parent = S-side neighbor via adjacency
  - For M-entry pre_shape: rebuilt from flat with S-side piece_plc
- Pieces rotate around virtual pivot
- Wedge loft uses saved virtual_plc values (no re-composition)
- Only special case: bend line doesn't rotate by its own bend
