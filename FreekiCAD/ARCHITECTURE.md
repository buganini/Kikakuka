# FreekiCAD Bending Pipeline Design

## Overview

The bending pipeline transforms a flat PCB board into a 3D folded shape by:
1. Planning trimmed center/cut segments from bend lines
2. Cutting the flat board into rigid pieces and wedge strips
3. Running a preliminary non-wedge parent search, then a wedge-aware BFS to determine rotation order
4. Rotating pieces sequentially around bend centers of curvature
5. Building wedge geometry
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

### Output

- `cut_plan[]` = (sp0, sp1, side, bi, angle_rad, radius, p0, normal, bend_obj, moving_normal)
  - `side`: 'A' or 'B' -- neutral topological label (no directional meaning)
  - Stationary/moving role is determined later by BFS chain selection
  - One entry per cut segment

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
- For **wedges**: start from the BFS parent's chain, then append the wedge's canonical entry mi (`strip_to_mi[wpi]`) only when that mi is not already implied by the parent link.
  - If the parent chain already ends with the same positive mi, reuse the parent chain unchanged.
  - If the parent BFS link already carries the matching synthesized moving-side exit `-(mi + 2)`, reuse the parent chain unchanged. This is the trailing leaf-strip case: the sliver hangs off the parent's moving-side exit and must not get an extra rigid bend step.
- `strip_to_mi` is usually chosen from the wedge's BFS record as the first positive crossing on the wedge's own bend.
  - Promoted leaf strips may instead seed `strip_to_mi` from parent-side candidate data when their own BFS entry is incomplete or borrowed from a neighboring wedge corridor.

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

## Wedge Build

For each wedge piece, creates either a curved 3D solid or an analytic wire preview along the bend arc.

### Wedge Modes

`WedgeMode` is a user-facing rendering/build preset with two values:

- `Smooth`: hybrid curved wedge solid rebuilt from bent source topology
- `Wireframe`: analytic slice-wire preview only

Current dispatch behavior:

- `Wireframe` builds only the bent analytic slice wires
- `Smooth` tries the hybrid curved-solid builder; if that fails, it falls back to analytic wireframe

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
7. Build wedge output:
   - in wireframe mode: compound all slice wires
   - in smooth mode: rebuild the wedge from bent source faces instead of turning the slice stack directly into one user-facing solid
8. Apply remaining Phase 3 rotations (`piece_plc[pi] * wedge_post_mi_plc[pi]^-1`) to catch rotations that happened after the wedge's own mi

### Smooth Hybrid Rebuild

`Smooth` rebuilds the wedge from bent versions of the flat source faces instead of relying only on a single raw loft. The active strategy stack is:

The smooth wedge builder uses a local frame for each wedge:

- `d`: projection along `cur_normal` (across the bend / inset direction)
- `s`: projection along `bend_axis` (along the bend line / segment direction)
- `up`: projection along `cur_up` (board thickness direction)

0. Rebuild constant-`d` sweep-boundary side faces as exact rigid transforms first (`anchored-sweep`)
1. Rebuild source topology with analytic side faces, normal cap preferences, and local triangle fallback only for source faces that cannot be rebuilt

Notes:

- Top or bottom caps can collapse to a line after bending; these are reported as `collapsed-to-line`
- Cap collapse is detected from sampled bent boundary points, not just by counting distinct end vertices
- `collapsed-to-line` and `dropped` are different outcomes:
  `collapsed-to-line` means the cap legitimately degenerates to a line and is omitted from the shell
  `dropped` means no acceptable rebuilt face or local triangle fallback could be produced
- Constant-`d` side faces are rebuilt as anchored rigid faces (`anchored-sweep`)
- Constant-`s` side faces (the two sweep-boundary faces) are rebuilt as planar `sweep-boundary-plane` faces
- If a sweep-boundary face shares a collapsed cap edge, the rebuild first tries to reuse surviving exact bent edges and only synthesizes the collapsed edge as a projected line segment if necessary
- A sampled-boundary polygon path remains only as a last resort for sweep-boundary faces whose exact-edge wire still cannot be formed
- Side faces do not use the generic n-sided filled-face patch path, because that can synthesize poles or cone-like peaks that were not source vertices
- Near the 180° singular limit, simple non-constant side faces can still be rebuilt from their exact bent boundary wire before falling back to pair surfaces
- In practice, side faces are rebuilt from anchored rigid faces, sweep-boundary planar faces, exact bent-boundary fills in the near-180° case, ruled/loft pair surfaces, or explicit triangle patches
- Individual rebuilt faces can fall back to triangle patches; these are reported as `tri-fallback`
- Triangle fallback density is intentionally lighter than the normal `Smooth` target: local fallback faces use one subdivision level less than the configured smooth split count
- `Smooth` attempts a single source-topology rebuild for each wedge
- If shell building fails, the smooth wedge rebuild stops at that single source-topology attempt
- Smooth solid selection now ranks repaired shell candidates by volume error first, then by anchor alignment
- If collapsed faces, dropped faces, or local triangle fallback were involved, a source-topology solid with high `vol_rel` is rejected outright
- If every smooth-stage solid attempt fails, the user-facing `Smooth` mode falls back to analytic wireframe for that wedge

### Smooth Strategy Inventory

`Smooth` uses a small set of labels that fall into four buckets: whole-wedge setup, side-face rebuilds, cap rebuilds, and per-face fallback.

#### Whole-Wedge

| Label | Kind | Used for |
| --- | --- | --- |
| `vertex-weld` | preprocessing | Runs before face rebuild. Welds bent edge vertices so neighboring rebuilt faces share endpoints and shell construction has a better chance to close cleanly. |
| `source-topology` | whole-wedge strategy | The only top-level `Smooth` rebuild path. Rebuilds the wedge from the original top/bottom/side face decomposition rather than tessellating the whole wedge into triangles. |

#### Side-Face Rebuilds

| Label | Kind | Used for |
| --- | --- | --- |
| `anchored-sweep` | side-face rebuild | Used for constant-`d` side faces. Rebuilds the face by rigidly transforming the source face to its bent position at a fixed inset distance. |
| `sweep-boundary-plane` | side-face rebuild | Used for constant-`s` side faces. Rebuilds the two sweep-boundary planes, preferring exact bent edges and only falling back to projected sampled edges if the exact wire cannot be closed. |
| `side-ruled` | side-pair rebuild | Used for non-constant side faces when matched top/bottom edge pairs are available and `Part.makeRuledSurface(...)` succeeds. This is the preferred pair-based side rebuild. |
| `side-loft` | side-pair fallback | Used only when a valid side pair exists but `side-ruled` does not succeed. |
| `surface=exact` | exact boundary rebuild | Used when a face can still be reconstructed from its exact bent boundary wire and filled directly. In practice this mainly covers residual non-side faces plus simple near-180° side faces whose exact boundary is more reliable than a ruled patch. |

#### Cap Rebuilds

| Label | Kind | Used for |
| --- | --- | --- |
| `span-first` | cap preference | Default cap policy when a top/bottom source wire has 4 or fewer edges. Prefer a span-based rebuild from the two long sweep-boundary edges before trying a generic fill. |
| `fill-first` | cap preference | Default cap policy when a top/bottom source wire has more than 4 edges. Prefer a filled boundary rebuild before trying span reconstruction. |
| `span-loft` | cap surface build | Used when the cap builder can identify two strong sweep-boundary edges and a loft between them succeeds. Preferred for simple ribbon-like caps. |
| `span-ruled` | cap surface fallback | Used when the same span edges are available but `span-loft` fails and a ruled surface succeeds instead. |
| `filled-face` | cap surface build | Used when a cap wire can be rebuilt as a closed bent wire and filled directly. This is the main non-span cap strategy, and the first fill-based choice when `fill-first` is active. |
| `dense-filled-face` | cap surface fallback | Used when the ordinary filled cap wire is too sparse or fragile for OCC, but a denser sampled wire produces a valid fill. |

#### Per-Face Fallback

| Label | Kind | Used for |
| --- | --- | --- |
| `tri-fallback` | per-face fallback | Used only for an individual source face that could not be rebuilt analytically or exactly. This is no longer a whole-wedge retry stage. |

### Smooth Status And Diagnostic Labels

| Label | Meaning | Used when / use case |
| --- | --- | --- |
| `collapsed-to-line` | status | A cap legitimately degenerates to a line after bending. Use case: keep the shell valid by omitting a singular cap instead of treating it as a reconstruction error. |
| `dropped` | status | A source face could not be rebuilt analytically, exactly, or via local triangle fallback. Use case: mark the whole `source-topology` attempt as failed for that wedge. |
| `side-pairs-missing` | diagnostic | A non-constant side face had no pair metadata. Use case: explain why pair-based side reconstruction was unavailable at all. |
| `side-pairs-failed` | diagnostic | Pair metadata existed, but neither ruled nor loft reconstruction produced a usable side surface. Use case: explain why the side face had to fall through to exact or local triangle fallback. |
| `sweep-boundaries=fallback` | diagnostic | The cap span search could not confidently identify two constant-`s` sweep-boundary edges. Use case: explain why the cap builder skipped span reconstruction and had to rely on fill-based methods. |
| `source=sampled` | diagnostic | A `sweep-boundary-plane` face could not close from exact bent edges, so the implementation projected sampled/source edges onto the constant-`s` plane instead. |
| `rejected after fallback faces` | diagnostic | A `source-topology` solid was assembled, but its volume error was too large after using collapsed faces, dropped faces, or local triangle fallback. Use case: reject fallback-heavy solids that are geometrically plausible but materially wrong. |

### Shell Candidate Inventory

`_solidify_surface_faces(...)` tries several shell-building sub-strategies before giving up on a face set:

| Label | Layer | Used when / use case |
| --- | --- | --- |
| `makeShell` | shell candidate | First direct OCC shell attempt from the oriented rebuilt faces. Best case when the faces already form a clean closed shell. |
| `Shell` | shell candidate | Alternate OCC shell constructor used when `makeShell` does not yield a usable shell. |
| `compound` | shell candidate | Builds a compound of faces and extracts any shells already implicit in that compound. Useful when direct shell constructors miss a shell the topology already implies. |
| `sewShape` | shell candidate | Stitches the face compound before re-extracting shells. Use case: the rebuilt faces are close enough to sew, but are not yet connected as one shell. |
| `sewShape+fix` | shell candidate fallback | Applies OCC `fix(...)` after sewing. Last shell-building pass before the candidate face set is rejected. |

Related diagnostics from the same stage:

| Label | Meaning | Used when / use case |
| --- | --- | --- |
| `solid repaired` | diagnostic | `_repair_valid_solid_candidate(...)` had to heal or rebuild the solid after shell construction. |
| `solid suspicious` | diagnostic | A valid solid exists, but its volume error is large enough to warn about before ranking candidates. |
| `solid off-anchor` | diagnostic | A candidate solid misses the expected near/far anchor locations by more than the configured tolerance and is discarded. |
| `solidify failed` | diagnostic | None of the shell candidates produced an acceptable solid. |

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
| **wedge** / **wedge piece** | piece | A narrow strip of material between the A and B cut faces of a bend segment. Lives within the inset zone. Built into either curved solid geometry or a wire preview during the wedge build phase. Some helpers still use a `strip_` prefix (e.g., `strip_pieces`, `strip_to_bend`). |
| **d** | local wedge coordinate | Projection along `cur_normal`: the across-bend direction used to measure inset span and sweep progress. |
| **s** | local wedge coordinate | Projection along `bend_axis`: the along-bend direction used to identify the two sweep-boundary side faces. |
| **up** | local wedge coordinate | Projection along `cur_up`: the board-thickness direction in the local wedge frame. |
| **collapsed-to-line** | status | A cap face whose bent boundary degenerates to a line. This is treated as a legitimate singular limit of the wedge, not as a face reconstruction failure. |
| **dropped face** | status | A source face for which neither exact rebuild nor local triangle fallback produced an acceptable face. Dropped faces cause the source-topology smooth rebuild to fail for that wedge. |
| **sweep-boundary-plane** | face rebuild mode | A constant-`s` side face rebuilt as a planar face on one of the two sweep-boundary planes. These are the side faces orthogonal to the anchored constant-`d` side faces. |
| **joint** / **sid** | index | A center-segment-centric grouping. Each joint corresponds to one center segment and contains: the center segment endpoints, zero or more A faces, zero or more B faces, and zero or more wedge pieces. A and B faces are assigned to joints by projecting their midpoints onto center segments. Index into `joints[]`. |
| **micro-bend** / **mi** | index | One atomic fold operation used by Phase 3. In the current code, mi is assigned per joint/segment (`sid`) and can be shared by paired A/B faces of the same center segment. |
| **micro_bend_info[mi]** | tuple | Per-mi data: (angle_rad, bend_obj, cut_mid, normal, radius, orig_bi). Geometry is taken from the stationary-side face when available. |
| **mi_seg_idx[mi]** | int | Which segment index (0, 1, 2, ...) within its parent bend this mi belongs to. |
| **piece_mi_list[pi]** | list of mi | Ordered chain of mi's from root to piece `pi`. Phase 3 iterates chain positions; each piece rotates by its mi at that position. Borrowed trailing leaf strips can intentionally reuse the parent chain unchanged, without appending a new mi, when the parent already crossed the matching moving-side exit. |
| **bfs_tree[pi]** | tuple | BFS parent info: (parent_pi, mis_crossed_set, wedge_pi). |
| **crossing** | concept | An adjacency link between two pieces that share a cut face. Each crossing records the neighbor piece, the micro-bend label, and the cut face index. Crossings form the graph traversed by BFS to determine rotation order. |
| **adjacency[pi]** | list of (nbr_pi, mi, fi) | All crossings of piece `pi`: neighbors with the connecting mi label and cut face index. Built from joints. |
| **stationary_idx** | pi | The BFS root piece — the non-wedge piece closest to the board center of mass. Remains fixed; all other pieces rotate relative to it. |
| **piece_plc[pi]** | Placement | Accumulated rotation/translation for piece `pi` through Phase 3. Starts as identity. |
| **wedge_pre_shapes[pi]** | Shape | Snapshot of a wedge piece's shape before its canonical `strip_to_mi[pi]` rotation. Used as input for wedge building. |
| **wedge_post_mi_plc[pi]** | Placement | Snapshot of `piece_plc[pi]` right after the wedge's canonical `strip_to_mi[pi]` rotation. Remaining Phase 3 rotations are applied to the loft afterward. |
| **micro_pivots[mi]** | tuple | Saved pivot geometry for wedge building: (virtual_plc, cur_p0, cur_normal, cur_up, bend_axis, coc). |
| **wedge_pieces** | set of pi | All wedge piece indices. Code: `strip_pieces`. |
| **wedge_to_bend[pi]** | bi | Which bend a wedge piece belongs to. Code: `strip_to_bend`. |
| **strip_to_mi[pi]** | mi | Maps a wedge to its canonical positive entry mi, usually chosen from the wedge's BFS record as the first positive crossing on the wedge's own bend. Promoted leaf strips may borrow this from parent-side candidate data. |
| **strip_seed_parent[pi]** | pi | Immediate rigid parent recorded when a promoted leaf strip borrows a parent-side crossing. Used to preserve the attachment frame for trailing slivers. |
| **strip_seed_source[pi]** | pi | Source piece used when resolving the rigid frame for a promoted leaf strip's canonical mi. Trailing exit slivers reuse the immediate parent here rather than an earlier stationary ancestor. |
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
6. A promoted trailing leaf strip must stay attached to its immediate parent frame and must not receive a duplicate positive mi if the parent already crossed the matching synthesized exit
7. `bend_sign = -1` if angle > 0, else 1
8. All adjacency crossings have a matching cut face; non-cut connections are not emitted
9. Wedge chains are rooted on first BFS discovery; later traversals may add exit crossings but do not replace wedge parents
10. Wedge geometry is built in the pre-mi frame; remaining Phase 3 rotations are applied via `wedge_post_mi_plc`
