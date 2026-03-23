# Next Session Notes - Per-Cut Refactoring

## Current State
- Worktree: `epic-cerf`
- File: `FreekiCAD/FreekiCAD/LinkedObject.py`

## What was done this session
- Removed swapping concept entirely: normal/angle no longer negated at micro_bend_info creation
- All segments of same bend share identical angle, normal, axis
- Wedge loft flips cur_normal locally for geo-M BFS-entry segments (mi_bfs_side dict)
- Removed per-bend S-side logging, curl-back fix, bend_m_seg_mids
- Wedge sweep uses per-mi angle (micro_angle_s) consistent with Phase 3
- Correction section uses per-mi angle (consistent per-bend)
- Per-bend sign cache for bend_sign (first topology result reused for all segments)
- Added per-cut logging: mi creation with bfs-S label, per-mi CoC pivot
- Removed redundant bendline chain log
- Updated bendline CoC log to reference mi instead of bend index

## What Works
- Per-cut micro-bends (one mi per S-cut face)
- geo-M entry wedges (p22, p25, p27) now work correctly
- Phase 3 rotation identical for all segments (Rotation invariant to axis/angle sign)
- Corrections consistent per-bend (no sign flip for geo-M entries)

## Remaining Issues

### 1. Wedge d_offset OUT (p29, p31, p37)
These wedges have wedge_pre_shapes that don't align with slicing params.
- p29 (mi=9, bend 6): d_offset=-0.1371
- p31 (mi=8, bend 6): d_offset=0.2700
- p37 (mi=15, bend 9): d_offset=1.1513
Root cause: wedge_pre_shapes rotated by earlier same-bend mi's but slicing params (from bend_obj Placement) weren't. This is a coordinate space mismatch — NOT curl-back, just normal per-cut behavior.

### 2. bend_sign direction
- Currently uses angle sign as fallback, topology with parent_cm dot cur_up
- User suggested "arrow vector" approach instead
- Global up direction vs rotation direction concern — to discuss

### 3. CoC colinearity
- Pivots for segments on different geo sides are slightly non-colinear (~2*inset offset)
- Negligible for small insets

## Architecture Summary
- No more "swapping" or "curl-back" concepts
- Each mi is independent per-cut
- Normal always from bend geometry (same for all segments of same bend)
- mi_bfs_side tracks which geo side is BFS-S (used only in wedge loft for slicing direction flip)
- bend_sign_cache: per-bend, first topology result reused
- Phase 3 rotation: uses micro_angle and bend_axis from micro_bend_info (same for all segs)
- Wedge loft: uses saved micro_pivots params, flips cur_normal for geo-M entry
