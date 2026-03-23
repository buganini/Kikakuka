# Next Session Notes - Per-Cut Refactoring

## Current State
- Worktree: `epic-cerf`
- File: `FreekiCAD/FreekiCAD/LinkedObject.py`
- Result: "not entirely correct, but closer overall speaking"

## What was done this session
- Removed swapping concept: no normal/angle negation at micro_bend_info creation
- All segments of same bend share identical angle, normal, axis
- **Key insight**: geo-M entry mi's fold BACK (opposite direction)
  - Phase 3: `eff_angle = micro_angle * sign` where sign=-1 for geo-M entry
  - Wedge loft: sweep negated + cur_normal flipped for geo-M entry
  - Correction: mi_angle negated for geo-M entry
- Removed mult concept: per-cut uses 0/1 check only (no accumulation/XOR/cancel)
- Simplified piece_micro_mult: just sets 1 for each mi in BFS chain
- Removed per-bend S-side logging, curl-back fix, bend_m_seg_mids
- Per-bend sign cache for bend_sign (first topology result reused)
- Added per-cut logging: mi creation with bfs-S label, per-mi CoC pivot
- Removed redundant bendline chain log

## What Works
- Per-cut micro-bends (one mi per S-cut face)
- geo-M entry wedges (p22, p25, p27) work correctly (d_offset IN)
- p24 now has correct fold-back behavior (crosses bend 9 twice, net ≈ 0)
- Corrections consistent per-bend

## Remaining Issues

### 1. p22/p24 not entirely correct
- "closer but not entirely correct" — need to investigate what's still off
- p22 wedge sweep direction may need verification
- p24 position changed but may still have residual error

### 2. Wedge d_offset OUT (p29, p31, p33, p37)
- p29 (mi=9, bend 6): d_offset=-0.1942 OUT
- p31 (mi=8, bend 6): d_offset=0.2700 OUT
- p33 (mi=10, bend 7): d_offset=0.1178 OUT
- p37 (mi=15, bend 9): d_offset=0.1192 OUT
- These are deeper pieces with accumulated transformations

### 3. bend_sign direction
- Currently uses angle sign as fallback, topology with parent_cm dot cur_up
- User noted: "global up direction vs rotation direction" concern
- "arrow vector" approach suggested but not yet implemented

## Architecture Summary
- No more "swapping", "curl-back", or "mult" concepts
- Each mi is independent per-cut
- Normal always from bend geometry (same for all segments of same bend)
- mi_bfs_side: tracks which geo side is BFS-S
  - Phase 3: sign=-1 for geo-M (fold back)
  - Wedge loft: flip cur_normal + negate sweep for geo-M
  - Correction: negate angle for geo-M
- piece_micro_mult: simple 0/1 per (piece, mi) — no accumulation
- bend_sign_cache: per-bend, first topology result reused
