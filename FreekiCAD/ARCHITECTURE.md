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
2. Offset each segment by -inset (A-side) and +inset (B-side)
3. Trim offset lines to board outline independently
4. Validate: discard phantom cuts whose midpoint is far from any center segment

### Output

- `cut_plan[]` = (sp0, sp1, side, bi, angle_rad, radius, p0, normal, bend_obj, moving_normal)
  - `side`: 'A' or 'B' -- neutral topological label (no directional meaning)
  - Stationary/moving role is determined later by BFS chain selection
  - One entry per validated cut segment

### Phase 2b-1: Create 3D Cutting Faces

Extrudes each 2D cut segment into a vertical rectangular face spanning the full board height. Initially labels all faces with their parent bend index.

### Output

- `cut_faces[fi]`: 3D rectangular face
- `face_topo_side[fi]`: 'A' or 'B'
- `face_bend[fi]`: bi
- `face_to_micro[fi]`: initially bi (updated in Phase 2b-2)
- `cut_plan_data[fi]`: (angle_rad, bend_obj, cut_mid, normal, radius, bi)

### Phase 2c: Cut Board, Preliminary BFS

1. `generalFuse(board, cut_faces)` -> compound solid
2. Extract `pieces` (solids with volume > 1e-6)
3. Create `piece_slices` (2D wire at z=half_t) and `cut_segments` (2D edge at z=half_t) for fast distance checks
4. Run **BFS #1 (preliminary)**: no A/B distinction, uses face labels = bi
   - Determines `seg_parent_pi[sid]` and `seg_parent_side[sid]` for each cut segment pair
5. Pair A/B faces of same bend that share a wedge piece -> assign segment IDs (`face_to_seg`)

### Phase 2b-2: Assign Micro-Bends

Using BFS #1 results:

1. Each stationary-side face creates a unique micro-bend (mi >= 0) in `micro_bend_info`
2. Each moving-side face gets a negative ID in `m_face_to_bend`
3. The normal is oriented per-cut: pointing from BFS parent piece into the wedge
4. Stationary/moving role is determined by which topo side (A or B) the BFS parent is on
4. Identify `strip_pieces` (wedge pieces within inset distance of center line)
5. Run **BFS #2 (final)**: uses micro-bend labels, skips through wedges

### Key Data Structures

- `micro_bend_info[mi]` = (angle_rad, bend_obj, cut_mid, normal, radius, orig_bi)
  - One entry per stationary-side face (carries the rotation; moving-side faces are geometry-only)
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
   - For M-entry: rebuilt from flat piece using stationary-side parent's piece_plc
7. Save `wedge_post_mi_plc[pi]` = current `piece_plc[pi]` (snapshot before remaining rotations)
8. Rotate all pieces where `mi in piece_mi_set[pi]` by micro_angle around pivot
9. Compose rotation into `piece_plc[pi]` for each rotated piece
10. Rotate bend lines and components by same rotation (with multiplier for multi-segment bends)
11. Apply inset correction within the Phase 3 loop (not deferred to post-loop)

### M-Entry Handling

When BFS reaches a wedge from the moving side (source != stationary neighbor):
- The wedge's piece_shapes has been rotated by the moving-side path
- Rebuild `wedge_pre_shapes[pi]` from the flat piece with stationary-side neighbor's piece_plc

---

## Wedge Loft

For each wedge piece, creates a curved 3D solid by lofting cross-sections along the bend arc.

### Algorithm

1. Retrieve saved pivot data (virtual_plc, cur_p0, cur_normal, cur_up, bend_axis, coc)
2. Get `positioned_flat` = `wedge_pre_shapes[pi]` or `piece_shapes[pi]`
3. Build d-values:
   - `d_uniform`: N_SLICES+1 uniform positions in [gt, 2*ins - gt]
   - Vertex projections of positioned_flat → `vertex_proj_ds` (split points where cross-section topology may change)
4. Split d-range into sub-ranges at vertex projection planes. Each sub-range has consistent cross-section topology.
5. For each sub-range:
   - Collect uniform d-values falling within the sub-range, plus boundary d-values
   - For each d-value:
     - Slice positioned_flat perpendicular to cur_normal at distance d from cur_p0
     - Compute `frac = (d - gt) / (2*ins - 2*gt)` and `slice_angle = frac * sweep_angle`
     - Translate slice back by -d along cur_normal (to stationary edge)
     - Rotate slice by slice_angle around CoC along bend_axis
   - Reorder vertices: OCCT's `slice()` can return vertices in different cyclic order depending on slice position; align each wire to match the first wire by trying all cyclic rotations and both directions
   - Collect as one loft segment
6. Build loft: `Part.makeLoft(seg, True, False)` per segment, fuse segments together
7. If volume < 0: reverse the solid (inside-out fix)
8. Apply remaining Phase 3 rotations (`wedge_post_mi_plc`) to catch rotations that happened after the wedge's own mi

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
2. Rank by 2D distance from cut midpoint to piece-pair midpoint
3. Only emit edges that have a matching cut face

Returns: list of (i, j, best_touch_fi) -- only pairs with a matching cut face are included.

**Note:** Adjacency is between all pieces, including wedges; the cut face is attached as extra info. Traversal (BFS) is piece-to-piece (non-wedges), with the opposite-side test applied only during BFS, not during adjacency building.

### `_classify_pieces_bfs(..., strip_pieces)`

Two modes:

**With strip_pieces (final BFS)**: Skips through wedge pieces to build piece-to-piece connectivity.
- Wedges are visited but immediately traversed
- Side test ensures pieces are on opposite sides of cut (applied during traversal)
- BFS tree: 3-tuple (parent_pi, mis_crossed, wedge_pi)

**Without strip_pieces (preliminary BFS)**: Simpler, includes all pieces equally.
- BFS tree: 2-tuple (parent_pi, mi_crossed)
- Used for initial stationary/moving assignment

BFS root: non-wedge piece closest to board center of mass.

---

## Key Invariants

1. Each stationary-side face creates exactly one micro-bend; moving-side faces create none
2. Wedge pieces are between A and B faces of the same bend segment
3. piece_plc tracks accumulated rotations in correct interleaved order (fixes rotation non-commutativity)
4. virtual_plc = piece_plc[s_parent] * plc_original (not chain composition)
5. Normal is oriented per-cut from BFS parent (stationary side) into wedge; `seg_parent_pi` walks past wedge parents to find the non-wedge piece
6. bend_sign = -1 if angle > 0 else 1
7. All adjacency edges have a matching cut face; non-cut connections are not emitted
8. Wedge loft is built in the pre-mi frame; remaining Phase 3 rotations are applied via `wedge_post_mi_plc`


