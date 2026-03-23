# Next Session Notes - Per-Cut Refactoring

## Current State
- Worktree: `confident-pasteur`
- FreeCAD loads from this worktree
- File: `FreekiCAD/FreekiCAD/LinkedObject.py`

## What Works
- Per-cut micro-bends (one mi per S-cut face)
- Per-segment S/M via topology-based pairing (shared wedge piece with exactly 2 bend faces)
- Unique M face IDs for BFS distinguishability
- Per-mi XOR (no cancellation by orig_bi)
- Per-mi bend_sign (topology, no cache)
- Curl-back S/M naturally handled by per-segment BFS
- Pieces 0-23 correctly positioned
- p13 transformation correct (was the first broken piece, now fixed)

## Display Issue: Segment Indices
S and M face indices are counted separately per bend.
Curl-back faces show same index as first crossing M faces (9.2S vs 9.2M).
They ARE different global segments internally — just confusing display.
Fix: use single counter per bend for both S and M faces.

## Remaining Issues

### 1. Wedge d_offset OUT in inner loop
p29, p31, p37 have d_offset far OUT → 0 slices.
These are wedges in the curl-back region where the piece
positions are affected by multiple rotations.

### 2. Component D2 positioning
Component rotation uses `first_mi_of_bend` for dedup.
Different segments have different pivots. Component should
rotate around segment nearest to component.

### 3. Several wedges have s_mult=0
p22, p27, p31, p35 — the s_mult sum across bend mi's gives 0.
This means the piece crossed the bend an even number of times
(different segments cancel in the sum).

## Architecture Summary
1. Group S/M face PAIRS by topology (shared piece with exactly 2 bend faces)
2. Each pair gets unique segment ID for preliminary BFS
3. Preliminary BFS with segment IDs → S/M per segment
4. Phase 2b-2: S faces get mi, M faces get unique negative ID
5. Final BFS with per-cut labels
6. Phase 3: rotation/correction/wedge all per-mi
7. s_mult: sum across all mi's of same bend (mod 2)
8. Component rotation: first_mi_of_bend dedup (needs fix)

## Key Files Changed
- `LinkedObject.py` in `confident-pasteur` worktree
- Preliminary BFS section (~line 2205-2260)
- Phase 2b-2 S/M assignment (~line 2265-2310)
- Phase 3 rotation/correction/wedge all use per-mi
- _classify_pieces_bfs uses m_face_to_bend and mi_seg_idx params
