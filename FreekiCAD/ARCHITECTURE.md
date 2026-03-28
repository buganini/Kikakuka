# FreekiCAD Bending Pipeline Design

## Overview

The bending pipeline transforms a flat PCB board into a 3D folded shape by:
1. Cutting the flat board along bend lines to create pieces
2. Classifying pieces via BFS to determine rotation order
3. Rotating pieces sequentially around bend centers of curvature
4. Lofting wedge strips into curved solids
5. Applying correction translations to align pieces

Entry point: `__apply_bends_impl()` in `LinkedObject.py`.

---

## Constants

- `GEOMETRY_TOLERANCE` = 0.001 mm (1 um) -- proximity threshold for adjacency tests
- `N_SLICES` = max(ceil(|angle_degrees| / 4), 8) -- per-wedge, computed dynamically

---

## Phase 1: Collect Bend Info

Iterates over active BendLine children. For each bend, extracts geometry and computes the inset (perpendicular offset from bend center to cut lines).

### Outputs

- `bend_info[bi]` = (bend_obj, p0, p1, line_dir, normal, angle_rad, radius)
  - `p0, p1`: bend line endpoints in XY plane (z=0)
  - `normal`: perpendicular vector pointing toward moving side (oriented toward board center of mass)
  - `angle_rad`: bend angle (positive = CCW when viewed from line_dir)
- `insets[bi]` = r_eff * |angle| / 2, where r_eff = radius + thickness/2

---

## Phase 2: Cut Board and Classify Pieces

### Phase 2a: 2D Cut Planning

For each bend, creates cut line segments offset from the center line by +/- inset:

1. Trim center line to board outline -> `trimmed_bend_segs[bi]` = [(sp0, sp1), ...]
2. Offset each segment by -inset (S-side) and +inset (M-side)
3. Trim offset lines to board outline independently
4. Validate: discard phantom cuts whose midpoint is far from any center segment

### Output

- `cut_plan[]` = (sp0, sp1, side, bi, angle_rad, radius, p0, normal, bend_obj, moving_normal)
  - `side`: 'S' (stationary) or 'M' (moving) -- topological label
  - One entry per validated cut segment

### Phase 2b-1: Create 3D Cutting Faces

Extrudes each 2D cut segment into a vertical rectangular face spanning the full board height. Initially labels all faces with their parent bend index.

### Output

- `cut_faces[fi]`: 3D rectangular face
- `face_topo_side[fi]`: 'S' or 'M'
- `face_bend[fi]`: bi
- `face_to_micro[fi]`: initially bi (updated in Phase 2b-2)
- `cut_plan_data[fi]`: (angle_rad, bend_obj, cut_mid, normal, radius, bi)

### Phase 2c: Cut Board, Preliminary BFS

1. `generalFuse(board, cut_faces)` -> compound solid
2. Extract `pieces` (solids with volume > 1e-6)
3. Create `piece_slices` (2D wire at z=half_t) and `cut_segments` (2D edge at z=half_t) for fast distance checks
4. Run **BFS #1 (preliminary)**: no S/M distinction, uses face labels = bi
   - Determines `seg_parent_pi[sid]` and `seg_bfs_side[sid]` for each cut segment pair
5. Pair S/M faces of same bend that share a wedge piece -> assign segment IDs (`face_to_seg`)

### Phase 2b-2: Assign Micro-Bends

Using BFS #1 results:

1. Each S-face creates a unique micro-bend (mi >= 0) in `micro_bend_info`
2. Each M-face gets a negative ID in `m_face_to_bend`
3. The normal is oriented per-cut: pointing from BFS parent piece into the wedge
4. Identify `strip_pieces` (wedge pieces within inset distance of center line)
5. Run **BFS #2 (final)**: uses micro-bend labels, skips through wedges

### Key Data Structures

- `micro_bend_info[mi]` = (angle_rad, bend_obj, cut_mid, normal, radius, orig_bi)
  - One entry per S-face (S-faces carry the rotation; M-faces are geometry-only)
- `mi_seg_idx[mi]` = segment index within bend (0, 1, 2, ...)
- `strip_pieces`: set of wedge piece indices
- `strip_to_bend[pi]` = bi
- `piece_bend_sets[pi]`: set of bend indices the piece crosses
- `bfs_tree[pi]` = (parent_pi, mis_crossed_set, wedge_pi)
- `adjacency[pi]` = [(nbr_pi, mi, fi), ...]
- `face_to_bend`: reassigned as alias for `face_to_micro` after Phase 2b-2

---

## Phase 3: Apply Bends Sequentially

Processes micro-bends in BFS traversal order (`micro_order`). For each mi:

1. Find S-parent piece of the wedge for this mi
2. Build `virtual_plc = piece_plc[s_parent] * plc_original`
   - `piece_plc[pi]`: accumulated Placement per piece (identity initially)
   - `plc_original`: bend's original Placement saved before Phase 3 starts
3. Transform to current space: cur_normal, cur_up, cur_p0
4. Compute center of curvature (CoC):
   - `r_eff = radius + half_t`
   - `bend_sign = -1 if angle > 0 else 1`
   - `pivot = stat_edge_mid + cur_up * (r_eff * bend_sign)`
5. Save pivot data in `micro_pivots[mi]` for wedge loft
6. Save pre-rotation wedge shape in `wedge_pre_shapes[pi]`
   - For M-entry: rebuilt from flat piece using S-side parent's piece_plc
7. Rotate all pieces where `mi in piece_mi_set[pi]` by micro_angle around pivot
8. Compose rotation into `piece_plc[pi]` for each rotated piece
9. Rotate bend lines and components by same rotation (with multiplier for multi-segment bends)

### M-Entry Handling

When BFS reaches a wedge from the M-side (source != S-neighbor):
- The wedge's piece_shapes has been rotated by the M-side path
- Rebuild `wedge_pre_shapes[pi]` from the flat piece with S-side neighbor's piece_plc

---

## Wedge Loft

For each wedge piece, creates a curved 3D solid by lofting cross-sections along the bend arc.

### Algorithm

1. Retrieve saved pivot data (virtual_plc, cur_p0, cur_normal, cur_up, bend_axis, coc)
2. Get `positioned_flat` = `wedge_pre_shapes[pi]` or `piece_shapes[pi]`
3. Build sorted d-values along cur_normal:
   - N_SLICES+1 uniform positions in [gt, 2*ins - gt]
   - Vertex projections of positioned_flat (captures all edges)
   - Deduplicate within min_sep
4. For each d-value:
   - Slice positioned_flat perpendicular to cur_normal at distance d from cur_p0
   - Compute `frac = (d - gt) / (2*ins - 2*gt)` and `slice_angle = frac * sweep_angle`
   - Translate slice back by -d along cur_normal (to stationary edge)
   - Rotate slice by slice_angle around CoC along bend_axis
5. `Part.makeLoft(wires_list, True, True)` -> curved solid
6. If volume < 0: reverse the solid (inside-out fix)

### Failure Modes

- `BRep_API: command not done`: OpenCASCADE loft construction failure (complex/degenerate wires)
- `d_offset OUT`: positioned_flat center is outside [0, 2*ins] slice range (transform mismatch)
- Volume = 0: degenerate loft (wires too close or identical)

---

## Correction

After wedge loft, corrects alignment of moving pieces:

For each mi:
1. Compute expected M-edge position: rotate S-edge by full angle around CoC
2. Compute actual M-edge position: rotate flat M-edge position by full angle
3. Correction = expected - actual
4. Apply translation to all non-wedge pieces crossing this mi, and to bend lines/components

---

## Adjacency

### `_build_geometric_adjacency(pieces, cut_faces, cut_plan, piece_slices, cut_segments)`

O(n^2) piece-pair check using `distToShape` (shortest 3D distance, not center-to-center).

For each pair (i, j) within GEOMETRY_TOLERANCE:
1. Find all cut faces where both pieces are within tolerance
2. Verify pieces are on opposite sides of cut segment (cross-product test)
3. Rank by 2D distance from cut midpoint to piece-pair midpoint
4. Return best_touch_fi (or None if no cut face found)

Returns: list of (i, j, best_touch_fi) -- includes None entries for touching pieces with no matching cut face ("(-) edges").

### `_classify_pieces_bfs(..., strip_pieces)`

Two modes:

**With strip_pieces (final BFS)**: Skips through wedge pieces to build piece-to-piece connectivity.
- Wedges are visited but immediately traversed
- Side test ensures pieces are on opposite sides of cut
- BFS tree: 3-tuple (parent_pi, mis_crossed, wedge_pi)

**Without strip_pieces (preliminary BFS)**: Simpler, includes all pieces equally.
- BFS tree: 2-tuple (parent_pi, mi_crossed)
- Used for initial S/M assignment

BFS root: non-wedge piece closest to board center of mass.

---

## Key Invariants

1. Each S-face creates exactly one micro-bend; M-faces create none
2. Wedge pieces are between S and M faces of the same bend segment
3. piece_plc tracks accumulated rotations in correct interleaved order (fixes rotation non-commutativity)
4. virtual_plc = piece_plc[s_parent] * plc_original (not chain composition)
5. Normal is oriented per-cut from BFS parent into wedge
6. bend_sign = -1 if angle > 0 else 1
7. Preliminary BFS needs full edge set (including None entries) for correct seg_parent_pi

---

## Open Issues

- `(-)` edge removal: currently applied in `_classify_pieces_bfs` adjacency building (uncommitted). Preliminary BFS still needs full edges.
- Wedge loft failures on complex models: `BRep_API: command not done` and `d_offset OUT`
- Phantom cuts: bend line passes through area but no wedge piece is created
- Sub cuts: long cut faces span multiple piece pairs; adjacency matching can be imprecise
