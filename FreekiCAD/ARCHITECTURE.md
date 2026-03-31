# FreekiCAD Bending Pipeline Design

## Overview

The bending pipeline transforms a flat PCB board into a 3D folded shape by:
1. Cutting the flat board along bend lines to create pieces
2. Classifying pieces via BFS to determine rotation order
3. Rotating pieces sequentially around bend centers of curvature
4. Lofting wedges into curved solids
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
4. Build adjacency graph (see below)
5. Run **BFS #1 (preliminary)**: no A/B distinction, uses face labels = bi
   - Determines `seg_parent_pi[sid]` and `seg_parent_side[sid]` for each cut segment pair
6. Pair A/B faces of same bend that share a wedge piece -> assign segment IDs (`face_to_seg`)

### Adjacency Graph Construction

Produces `(i, j, fi)` edges from the joint structure:

1. For each cut face `fi` in every joint, find all pieces within `GEOMETRY_TOLERANCE` using 2D `distToShape` → `face_pieces[fi]`
2. For each pair of pieces touching the same face, apply a **side test**: compute cross-product signed distance of each piece's center of mass relative to the cut segment line. Only connect pieces on opposite sides (`ci * cj < 0`)
3. Emit deduplicated `(i, j, fi)` edges

The **side test** is the key filter — it prevents connecting two pieces that both touch the same face but are on the same side of it rather than separated by it.

### Phase 2b-2: Assign Micro-Bends

Using BFS #1 results:

1. Each stationary-side face creates a unique micro-bend (mi >= 0) in `micro_bend_info`
2. Each moving-side face gets a negative ID in `m_face_to_bend`
3. The normal is oriented per-cut: pointing from BFS parent piece into the wedge
4. Stationary/moving role is determined by which topo side (A or B) the BFS parent is on
4. Identify wedge pieces (within inset distance of center line) → `wedge_pieces`
5. Run **BFS #2 (final)**: uses micro-bend labels, skips through wedges

### BFS Chain Building

#### Preliminary BFS (BFS #1)

- Treats all pieces equally (no wedge awareness), traverses the adjacency graph using segment IDs (not mi)
- Purpose: determine which topo side (A or B) of each joint is the stationary side
- Output: `seg_parent_pi[sid]` and `seg_parent_side[sid]`

#### Final BFS (BFS #2, wedge-skipping)

- Uses micro-bend labels (`mi >= 0` for stationary, `mi < 0` for moving)
- **Wedge pass-through**: when BFS hits a wedge piece, it does not stop — it immediately traverses *through* the wedge to reach the non-wedge piece on the other side. Both the entry mi and exit mi are recorded in the crossed set.
- BFS tree entry: `(parent_pi, mis_crossed_set, wedge_pi)` — the 3rd element records which wedge was traversed

#### piece_mi_set accumulation

- For each **non-wedge** piece: walk the BFS parent chain back to the root, collecting all `mi >= 0` from each link's `mis_crossed_set`
- For **wedges**: copy the mi set from the destination piece reached through that wedge (or from the stationary neighbor + own mi if no destination)

#### micro_order derivation

- A second BFS walk from the root, visiting children in tree order
- At each piece, any new `mi >= 0` from its crossed set is appended to `micro_order`
- This gives the processing sequence for Phase 3 rotations
- **Phantom mi's** (stationary faces with no associated wedge) are detected and removed from `micro_order` and all `piece_mi_set`

### Key Data Structures

- `micro_bend_info[mi]` = (angle_rad, bend_obj, cut_mid, normal, radius, orig_bi)
  - One entry per stationary-side face (carries the rotation; moving-side faces are geometry-only)
- `mi_seg_idx[mi]` = segment index within bend (0, 1, 2, ...)
- `wedge_pieces`: set of wedge piece indices (code: `strip_pieces`)
- `wedge_to_bend[pi]` = bi (code: `strip_to_bend`)
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
   - For moving-side entry: rebuilt from flat piece using stationary-side parent's piece_plc
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

### `_build_geometric_adjacency(pieces, cut_faces, cut_plan, piece_slices, cut_segments, joints)`

Builds adjacency from the joint structure. For each cut face in every joint, finds all pieces within `GEOMETRY_TOLERANCE` using 2D `distToShape`. Pairs of pieces sharing a face are connected only if they are on opposite sides (cross-product side test).

Returns: list of (i, j, fi) tuples.

**Note:** Adjacency is between all pieces, including wedges; the cut face is attached as extra info. Traversal (BFS) is piece-to-piece (non-wedges), with the opposite-side test applied only during BFS, not during adjacency building.

### `_classify_pieces_bfs(..., wedge_pieces)`

Two modes:

**With wedge_pieces (final BFS)**: Skips through wedge pieces to build piece-to-piece connectivity.
- Wedges are visited but immediately traversed
- Side test ensures pieces are on opposite sides of cut (applied during traversal)
- BFS tree: 3-tuple (parent_pi, mis_crossed, wedge_pi)

**Without wedge_pieces (preliminary BFS)**: Simpler, includes all pieces equally.
- BFS tree: 2-tuple (parent_pi, mi_crossed)
- Used for initial stationary/moving assignment

BFS root: non-wedge piece closest to board center of mass.

---

## Concept Hierarchy

```
Bend (user-drawn bend line, bi)
 └── Joint / Segment (center line trimmed to board outline, split at intersections; sid)
      ├── Cut pair (A at -inset, B at +inset from center)
      │    └── Face (3D rectangle extruded to full board height, fi)
      │         ├── Micro-bend (one per stationary-side face; atomic fold operation, mi)
      │         └── Edge (adjacency link between two pieces sharing this face)
      └── Wedge (strip piece between A and B faces)
```

---

## Glossary

| Term | Type | Description |
|------|------|-------------|
| **bend** / **bi** | index | A physical bend line on the PCB. Index into `bend_info[]`. One bend can span the full board width and be trimmed into multiple segments. |
| **bend_info[bi]** | tuple | Per-bend geometry: (bend_obj, p0, p1, line_dir, normal, angle_rad, radius). |
| **inset** / **ins** | float | Half the material width consumed by a bend: `r_eff * |angle| / 2`. The distance from the center line to each cut line. |
| **center segment** | 2D line (sp0, sp1) | A segment of the bend center line after trimming to the board outline and splitting at intersections with other bends. Stored in `trimmed_bend_segs[bi]`. One bend can produce multiple center segments. |
| **trimmed_bend_segs[bi]** | list of (sp0, sp1) | All center segments for bend `bi`. |
| **A line** / **A segment** | 2D line (sp0, sp1) | A cut line offset from the center segment by `-inset` along the bend normal. Trimmed independently to the board outline (`a_segs`). There can be a different number of A segments than B segments for the same bend (M:N relationship). |
| **B line** / **B segment** | 2D line (sp0, sp1) | A cut line offset from the center segment by `+inset` along the bend normal. Trimmed independently to the board outline (`b_segs`). |
| **cut face** / **fi** | index | A 3D rectangular face extruded from a 2D A or B segment, spanning the full board height. Used to slice the board into pieces. Index into `cut_faces[]`. |
| **cut_plan[fi]** | tuple | Per-cut-face planning data: (sp0, sp1, side, bi, angle_rad, radius, p0, normal, bend_obj, moving_normal). |
| **cut_segments[fi]** | 2D Edge | The 2D line of cut face `fi` at z=half_t. Used for fast distance checks during adjacency building. |
| **topo side** (A/B) | label | Neutral label for the two offset sides of a bend segment. 'A' and 'B' have no inherent directional meaning; stationary/moving roles are assigned later by BFS. |
| **piece** / **pi** | index | A solid fragment after the board is sliced by all cut faces. Index into `pieces[]`. |
| **piece_slices[pi]** | 2D Compound | The 2D cross-section of piece `pi` at z=half_t. Used for fast distance checks during adjacency building. |
| **wedge** / **wedge piece** | piece | A narrow strip of material between the A and B cut faces of a bend segment. Lives within the inset zone. Lofted into a curved solid in the wedge loft phase. Code uses `strip_` prefix for historical reasons (e.g., `strip_pieces`, `strip_to_bend`). |
| **joint** / **sid** | index | A center-segment-centric grouping. Each joint corresponds to one center segment and contains: the center segment endpoints, zero or more A faces, zero or more B faces, and zero or more wedge pieces. A and B faces are assigned to joints by projecting their midpoints onto center segments. Index into `joints[]`. Replaces the old A/B pairing logic. |
| **micro-bend** / **mi** | index | One fold operation at one specific stationary-side cut face. Index into `micro_bend_info[]`. Each stationary-side face creates exactly one mi. Phase 3 iterates mi's in order, applying one rotation per mi. |
| **micro_bend_info[mi]** | tuple | Per-mi data: (angle_rad, bend_obj, cut_mid, normal, radius, orig_bi). `cut_mid` is the pivot point; `normal` is oriented from stationary side into the wedge. |
| **mi_seg_idx[mi]** | int | Which segment index (0, 1, 2, ...) within its parent bend this mi belongs to. |
| **micro_order** | list of mi | The sequence in which mi's are processed in Phase 3. Derived from BFS traversal order. |
| **piece_mi_set[pi]** | set of mi | The set of micro-bends that must rotate piece `pi`. Built during BFS: each piece accumulates the mi's of all cuts between it and the stationary root. |
| **bfs_tree[pi]** | tuple | BFS parent info. Final BFS: (parent_pi, mis_crossed_set, wedge_pi). Preliminary BFS: (parent_pi, mi_crossed). |
| **edge** | concept | An adjacency link between two pieces that share a cut face. Each edge records the neighbor piece, the micro-bend label, and the cut face index. Edges form the graph traversed by BFS to determine rotation order. |
| **adjacency[pi]** | list of (nbr_pi, mi, fi) | All edges of piece `pi`: neighbors with the connecting mi label and cut face index. Built from joints. |
| **stationary_idx** | pi | The BFS root piece — the non-wedge piece closest to the board center of mass. Remains fixed; all other pieces rotate relative to it. |
| **piece_plc[pi]** | Placement | Accumulated rotation/translation for piece `pi` through Phase 3. Starts as identity. |
| **wedge_pre_shapes[pi]** | Shape | Snapshot of a wedge piece's shape before its own mi rotation. Used as input for wedge loft. |
| **wedge_post_mi_plc[pi]** | Placement | Snapshot of `piece_plc[pi]` right after the wedge's own mi. Remaining Phase 3 rotations are applied to the lofted solid afterward. |
| **micro_pivots[mi]** | tuple | Saved pivot geometry for wedge loft: (virtual_plc, cur_p0, cur_normal, cur_up, bend_axis, coc). |
| **wedge_pieces** | set of pi | All wedge piece indices. Code: `strip_pieces`. |
| **wedge_to_bend[pi]** | bi | Which bend a wedge piece belongs to. Code: `strip_to_bend`. |
| **strip_to_mi[pi]** | mi | Maps a wedge to ONE mi (the first A-side match). Broken for multi-connection wedges — slated for removal. |
| **face_to_seg[fi]** | sid | Which joint/segment a cut face belongs to. |
| **seg_to_bend[sid]** | bi | Which bend a segment/joint belongs to. |
| **face_to_micro[fi]** | mi or neg ID | Maps cut face to its micro-bend label. Stationary faces get mi >= 0; moving faces get unique negative IDs. |
| **face_topo_side[fi]** | 'A' or 'B' | Which topological side of the bend a cut face is on. |
| **face_bend[fi]** | bi | Which bend a cut face belongs to. |
| **m_face_to_bend[neg_id]** | (bi, seg_idx) | Maps negative moving-face IDs back to their bend and segment. |
| **seg_parent_pi[sid]** | pi | The non-wedge piece on the BFS-parent side of segment `sid`. Used to orient normals. |
| **seg_parent_side[sid]** | 'A' or 'B' | Which topo side the BFS parent is on for segment `sid`. Determines stationary/moving assignment. |
| **phantom mi** | concept | An mi whose segment has no associated wedge piece (e.g., from overlapping inset zones, or from a multi-connection wedge mapped to a different mi). Currently detected and skipped, but the detection is too coarse for multi-connection wedges. |
| **CoC** | point | Center of curvature. The pivot point for a bend rotation: `stat_edge_mid + cur_up * (r_eff * bend_sign)`. |
| **r_eff** | float | Effective bend radius: `radius + half_t` (outer fiber radius). |
| **bend_sign** | int | -1 if angle > 0, else 1. Determines which side of the cut the CoC is on. |

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


