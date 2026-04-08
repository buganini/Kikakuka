# FreekiCAD Bending Pipeline Design

## Overview

The bending pipeline transforms a flat PCB board into a 3D folded shape by:
1. Planning trimmed center/cut segments from bend lines
2. Cutting the flat board into rigid pieces and wedge strips
3. Running a preliminary non-wedge parent search, then a wedge-aware BFS to determine rotation order
4. Rotating pieces sequentially around bend centers of curvature
5. Lofting wedges into curved solids
6. Applying correction translations, visual bend-line offsets, and final assembly/debug output

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
4. Validate: discard cuts whose midpoint is far from any center segment

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

### Phase 2c: Cut Board and Build Topology

1. `generalFuse(board, cut_faces)` -> compound solid
2. Extract `pieces` (solids with volume > 1e-6)
3. Create `piece_slices` (2D wire at z=half_t) and `cut_segments` (2D edge at z=half_t) for fast distance checks
4. Build `joints` from trimmed center segments:
   - each joint corresponds to one trimmed center segment (`sid`)
   - A/B faces are assigned to the joint by midpoint projection onto that center segment
   - wedge pieces are assigned by center-of-mass distance to the center segment
5. Build adjacency graph from joints (see below)
6. Compute `cut_owner_piece` for debug cut visualization
7. Run a preliminary non-wedge BFS to determine `fi_parent` / stationary-side ownership for each crossing face

### Adjacency Graph Construction

Produces `(i, j, fi)` crossings from the joint structure:

1. For each cut face `fi` in every joint, find all pieces within `GEOMETRY_TOLERANCE` using 2D `distToShape` → `face_pieces[fi]`
2. For each pair of pieces touching the same face, apply a **side test**: compute cross-product signed distance of each piece's center of mass relative to the cut segment line. Only connect pieces on opposite sides (`ci * cj < 0`)
3. Emit deduplicated `(i, j, fi)` crossings

The **side test** is the key filter — it prevents connecting two pieces that both touch the same face but are on the same side of it rather than separated by it.

### Phase 2b-2: Assign Micro-Bends and Final BFS Labels

After the preliminary parent search:

1. Stationary/moving role is **per crossing face**: `fi_parent[fi]` records the non-wedge source piece for that face
2. Micro-bends are assigned per **joint/segment** (`sid`), not per face:
   - faces that share the same `sid` reuse the same positive micro-bend id via `sid_to_mi`
   - if a moving-side face created the mi first and a stationary-side partner is seen later, the code keeps the same mi but replaces its geometry with the stationary-side face's geometry
3. `face_to_micro[fi]` is rewritten from generic bend indices (`bi`) to shared positive mi labels
4. Negative labels are **not** stored in `face_to_micro`; they are synthesized later by the wedge-aware BFS when traversing out of a wedge (`-(mi + 2)`)
5. Run the final **wedge-aware BFS** using `face_to_micro` labels

### BFS Chain Building

#### BFS (wedge-skipping)

- Uses positive mi labels from `face_to_micro`
- **Wedge pass-through**: when BFS hits a wedge piece, it records the positive entry mi on the wedge, then traverses through the wedge to candidate non-wedge neighbors; the exit side is recorded as a synthesized negative crossing `-(mi + 2)`
- BFS is strict first-visit: first path wins, no revisiting / re-queuing
- Wedges are special:
  - the wedge itself gets a BFS entry when first reached
  - later traversals can append extra exit crossings to the wedge's `mis_crossed_set`
  - later traversals do **not** replace the wedge's parent
- BFS tree entry: `(parent_pi, mis_crossed_set, wedge_pi)`
  - for wedges themselves, `wedge_pi` is `None`
  - for non-wedge pieces reached through a wedge, `wedge_pi` records the corridor wedge used for that edge

#### piece_mi_list construction

- For each **non-wedge** piece: walk the BFS parent chain from piece to root, collecting `mi >= 0` from each link's `mis_crossed_set`, then reverse to get root-to-piece order.
- For **wedges**: first BFS parent's chain + the wedge's canonical entry mi (`strip_to_mi[wpi]`)
- `strip_to_mi` is chosen from the wedge's BFS record as the first positive crossing on the wedge's own bend

### Key Data Structures

- `micro_bend_info[mi]` = (angle_rad, bend_obj, cut_mid, normal, radius, orig_bi)
  - One entry per joint/segment crossing (`sid`); paired A/B faces can share the same mi
  - Geometry is anchored to the stationary-side face when one is available
- `mi_seg_idx[mi]` = segment index within bend (0, 1, 2, ...)
- `wedge_pieces`: set of wedge piece indices (code: `strip_pieces`)
- `wedge_to_bend[pi]` = bi (code: `strip_to_bend`)
- `piece_bend_sets[pi]`: set of bend indices the piece crosses
- `piece_mi_list[pi]`: ordered chain of mi's from root to piece
- `bfs_tree[pi]` = (parent_pi, mis_crossed_set, wedge_pi)
- `adjacency[pi]` = [(nbr_pi, mi, fi), ...]
- `face_to_bend`: reassigned as alias for `face_to_micro` after Phase 2b-2
- `fi_parent[fi]`: non-wedge source piece for crossing face `fi`, computed by the preliminary non-wedge BFS

---

## Phase 3: Apply Bends Sequentially

Iterates chain positions; at each position collects distinct mi's across all pieces and processes each. For each `(step_pos, mi)`:

1. Find the stationary/source piece for this specific positive-mi crossing via `mi_to_stationary_pi`
2. Build `virtual_plc = piece_plc[s_parent] * plc_original` when an `s_parent` exists; otherwise use `plc_original`
   - `piece_plc[pi]`: accumulated Placement per piece (identity initially)
   - `plc_original`: bend's original Placement saved before Phase 3 starts
3. Transform to current space: cur_normal, cur_up, cur_p0
4. Compute center of curvature (CoC):
   - `r_eff = radius + half_t`
   - `bend_sign = -1 if angle > 0 else 1`
   - `pivot = stat_edge_mid + cur_up * (r_eff * bend_sign)`
5. **(First occurrence of mi only)** Save pivot data in `micro_pivots[mi]` for wedge loft
6. **(First occurrence of mi only)** Save `wedge_pre_shapes[wpi]` for wedges whose canonical `strip_to_mi[wpi] == mi`
7. Rotate all pieces where `piece_mi_list[pi][step_pos] == mi` by `micro_angle` around the pivot
8. Compose rotation into `piece_plc[pi]` for each rotated piece
9. **(First occurrence of mi only, after rotation)** Save `wedge_post_mi_plc[wpi]`
10. Rotate bend lines and components by the same transform, using piece-based multipliers where available
11. Apply inset correction inside the Phase 3 loop to the affected non-wedge pieces, bend lines, and components

---

## Wedge Loft

For each wedge piece, creates a curved 3D solid by lofting cross-sections along the bend arc.

### Algorithm

1. Retrieve saved pivot data (virtual_plc, cur_p0, cur_normal, cur_up, bend_axis, coc)
2. Get `positioned_flat` = `wedge_pre_shapes[pi]` or `piece_shapes[pi]`
3. If the bend has multiple center segments, choose the nearest saved segment midpoint for this wedge and recompute `cur_p0` / pivot for that segment
4. Build d-values:
   - `d_uniform`: N_SLICES+1 uniform positions in [gt, 2*ins - gt]
   - Vertex projections of positioned_flat → `vertex_proj_ds` (split points where cross-section topology may change)
5. Split d-range into sub-ranges at vertex projection planes. Each sub-range has consistent cross-section topology.
6. For each sub-range:
   - Collect uniform d-values falling within the sub-range, plus boundary d-values
   - For each d-value:
     - Slice positioned_flat perpendicular to cur_normal at distance d from cur_p0
     - Compute `frac = (d - gt) / (2*ins - 2*gt)` and `slice_angle = frac * sweep_angle`
     - Translate slice back by -d along cur_normal (to stationary edge)
     - Rotate slice by slice_angle around CoC along bend_axis
     - Use the first returned wire from `slice()` for that cross-section
   - Reorder vertices: OCCT's `slice()` can return vertices in different cyclic order depending on slice position; align each wire to match the first wire by trying all cyclic rotations and both directions
   - Collect as one loft segment
7. Build loft:
   - in wireframe mode: compound all slice wires
   - otherwise: `Part.makeLoft(seg, True, not smooth_wedge)` per segment, then fuse segments together
8. If volume < 0: reverse the solid (inside-out fix)
9. Apply remaining Phase 3 rotations (`piece_plc[pi] * wedge_post_mi_plc[pi]^-1`) to catch rotations that happened after the wedge's own mi

### Failure Modes

- `BRep_API: command not done`: OpenCASCADE loft construction failure (complex/degenerate wires)
- `d_offset OUT`: positioned_flat center is outside [0, 2*ins] slice range (transform mismatch)
- Volume = 0: degenerate loft (wires too close or identical)

---

## Correction and Final Assembly

During Phase 3, after each mi rotation, the code computes an inset correction:

1. Compute expected M-edge position: rotate the stationary edge by the full angle around CoC
2. Compute actual M-edge position: rotate the flat M-edge position by the same transform
3. Correction = expected - actual
4. Apply the translation to affected non-wedge pieces, bend lines, and components

After wedge loft:

5. Move each bend line visually to the center of the first wedge on that bend (`coc_offsets`)
6. Optionally draw debug arrows, debug cuts, and debug piece objects
7. Replace `board_obj.Shape` with a compound of the final `piece_shapes`

---

## Adjacency

### `_build_geometric_adjacency(pieces, cut_faces, cut_plan, piece_slices, cut_segments, joints)`

Builds adjacency from the joint structure. For each cut face in every joint, finds all pieces within `GEOMETRY_TOLERANCE` using 2D `distToShape`. Pairs of pieces sharing a face are connected only if they are on opposite sides (cross-product side test).

Returns: list of (i, j, fi) tuples.

**Note:** Adjacency is between all pieces, including wedges; the cut face is attached as extra info. There are two side filters:
- adjacency building uses a coarse center-of-mass side test to avoid connecting pieces on the same side of a cut face
- BFS traversal applies `_side_test`, which uses the nearest off-cut vertex to the bend center segment for a more local and robust side classification

### `_classify_pieces_bfs(..., wedge_pieces)`

Skips through wedge pieces to build piece-to-piece connectivity.
- BFS root is the non-wedge piece closest to board center of mass
- Wedges are recorded on first visit, then used as pass-through corridors
- First path wins; BFS does not revisit or re-queue pieces
- Side test during traversal uses local near-cut vertices rather than center of mass
- BFS tree entries are 3-tuples: `(parent_pi, mis_crossed, wedge_pi)`

---

## Concept Hierarchy

```
Bend (user-drawn bend line, bi)
 └── Joint / Segment (center line trimmed to board outline, split at intersections; sid)
      ├── Cut pair (A at -inset, B at +inset from center)
      │    ├── Face (3D rectangle extruded to full board height, fi)
      │    └── Crossing (adjacency link between two pieces sharing a face)
      │         └── Micro-bend (one per stationary-side crossing; atomic fold operation, mi)
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
| **micro-bend** / **mi** | index | One atomic fold operation used by Phase 3. In the current code, mi is assigned per joint/segment (`sid`) and can be shared by paired A/B faces of the same center segment. |
| **micro_bend_info[mi]** | tuple | Per-mi data: (angle_rad, bend_obj, cut_mid, normal, radius, orig_bi). Geometry is taken from the stationary-side face when available. |
| **mi_seg_idx[mi]** | int | Which segment index (0, 1, 2, ...) within its parent bend this mi belongs to. |
| **piece_mi_list[pi]** | list of mi | Ordered chain of mi's from root to piece `pi`. Phase 3 iterates chain positions; each piece rotates by its mi at that position. |
| **bfs_tree[pi]** | tuple | BFS parent info: (parent_pi, mis_crossed_set, wedge_pi). |
| **crossing** | concept | An adjacency link between two pieces that share a cut face. Each crossing records the neighbor piece, the micro-bend label, and the cut face index. Crossings form the graph traversed by BFS to determine rotation order. |
| **adjacency[pi]** | list of (nbr_pi, mi, fi) | All crossings of piece `pi`: neighbors with the connecting mi label and cut face index. Built from joints. |
| **stationary_idx** | pi | The BFS root piece — the non-wedge piece closest to the board center of mass. Remains fixed; all other pieces rotate relative to it. |
| **piece_plc[pi]** | Placement | Accumulated rotation/translation for piece `pi` through Phase 3. Starts as identity. |
| **wedge_pre_shapes[pi]** | Shape | Snapshot of a wedge piece's shape before its canonical `strip_to_mi[pi]` rotation. Used as input for wedge loft. |
| **wedge_post_mi_plc[pi]** | Placement | Snapshot of `piece_plc[pi]` right after the wedge's canonical `strip_to_mi[pi]` rotation. Remaining Phase 3 rotations are applied to the loft afterward. |
| **micro_pivots[mi]** | tuple | Saved pivot geometry for wedge loft: (virtual_plc, cur_p0, cur_normal, cur_up, bend_axis, coc). |
| **wedge_pieces** | set of pi | All wedge piece indices. Code: `strip_pieces`. |
| **wedge_to_bend[pi]** | bi | Which bend a wedge piece belongs to. Code: `strip_to_bend`. |
| **strip_to_mi[pi]** | mi | Maps a wedge to its canonical positive entry mi, chosen from the wedge's BFS record as the first positive crossing on the wedge's own bend. |
| **face_to_seg[fi]** | sid | Which joint/segment a cut face belongs to. |
| **seg_to_bend[sid]** | bi | Which bend a segment/joint belongs to. |
| **face_to_micro[fi]** | mi | Maps each cut face to its shared positive micro-bend label after Phase 2b-2. Negative exit labels are synthesized only inside the final wedge-aware BFS. |
| **face_topo_side[fi]** | 'A' or 'B' | Which topological side of the bend a cut face is on. |
| **face_bend[fi]** | bi | Which bend a cut face belongs to. |
| **fi_parent[fi]** | pi | The non-wedge source/parent piece for crossing face `fi`, computed by the preliminary non-wedge BFS. |
| **cut_owner_piece[fi]** | pi | Rigid piece used to place debug cut geometry so debug cut lines follow the same transform as the adjacent rigid piece. |
| **CoC** | point | Center of curvature. The pivot point for a bend rotation: `stat_edge_mid + cur_up * (r_eff * bend_sign)`. |
| **r_eff** | float | Effective bend radius: `radius + half_t` (outer fiber radius). |
| **bend_sign** | int | -1 if angle > 0, else 1. Determines which side of the cut the CoC is on. |

---

## Key Invariants

1. Stationary/moving is per crossing face, but micro-bends are shared per joint/segment (`sid`) when paired A/B faces belong to the same center segment
2. Wedge pieces are between A and B faces of the same bend segment
3. `piece_plc` tracks accumulated transforms in execution order, avoiding non-commutative chain-composition errors
4. `virtual_plc = piece_plc[s_parent] * plc_original` when an `s_parent` exists
5. Normals used for `micro_bend_info` are anchored on the stationary-side geometry selected through `fi_parent`
6. bend_sign = -1 if angle > 0 else 1
7. All adjacency crossings have a matching cut face; non-cut connections are not emitted
8. Wedge chains are rooted on first BFS discovery; later traversals may add exit crossings but do not replace wedge parents
9. Wedge loft is built in the pre-mi frame; remaining Phase 3 rotations are applied via `wedge_post_mi_plc`
