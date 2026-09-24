# Differ Rendering

The PCB differ starts from KiCad's per-layer vector PDFs. Two renderer blocks
are intentionally kept in the repository: the legacy full-page renderer is a
stable comparison reference, while the viewport renderer is used by the
application.

The schematic differ uses the same viewport scheduler and drawing pipeline.
Its renderer consumes the colored multi-page schematic PDF directly instead
of KiCad's separate black-and-white PCB layer PDFs.

## Revision history

The A and B revision selectors load history independently for their selected
files. `WORKING` uses the current file on disk. Switching between schematic
and PCB mode reloads the corresponding revision lists.

- For a PCB (`.kicad_pcb`), the list contains commits that added, changed, or
  deleted the selected board path. The file's Git blob and mode are compared
  with the commit's first parent; commits that changed only other files are
  omitted. History does not follow a board renamed from another path. A
  deletion commit can appear in the list, but cannot be exported as a board.
- For a schematic (`.kicad_sch`), the list contains all repository commits.
  A root schematic's exported PDF can change when a separate hierarchical
  sheet changes, even if the root file does not.

PCB filtering tracks changes to the saved board file, not every possible
change to the exported PDF. Existing PCB footprints and schematic symbols
are embedded in their respective design files, so an external library edit
alone does not update them. Project variables, drawing sheets, or export
settings may nevertheless affect a PCB PDF without changing the board file.

## Common input

`kicad-cli pcb export pdf --mode-separate --black-and-white` produces one
vector PDF for every enabled PCB layer. Each layer is geometry rather than
display color, so the viewport renderer rasterizes it directly to one-channel
grayscale coverage. PDF export is shared by both renderers and should be timed
separately when comparing them.

Gerber inputs use a Differ-only conversion mode. When a circular Paste flash is
concentric with a plated drill, the temporary board associates that flash with
the synthetic PTH pad. KiCad's normal PDF drill overlay can then render the
source board and imported Paste Gerber consistently without changing regular
Gerber conversion or requiring a separate Paste export pass.

The pair metadata contains:

- the PDF path for each layer on side A and side B
- each PDF page size in PDF points
- a common canvas sized to the larger page, with a smaller page centered on it
- the union of A and B layers, so a layer present on only one board is retained

## Legacy full-page renderer

The reference implementation is `PcbLegacyRenderer` in
`legacy_pcb_diff.py`. Commit
`3e66601edc4a8be2bf5efba3a2d96aba210bebe0` is the last revision before the
viewport renderer was introduced.

`PcbLegacyRenderer.render_full_page()` reproduces the former pipeline:

1. Rasterize every layer PDF over the complete page at scale 7, or about
   504 DPI.
2. Write full-page A and B PNGs for every available layer.
3. For each layer, build an overlap image using the minimum RGB values and
   maximum alpha value from A and B, then write another full-page PNG.
4. Compute the absolute A/B difference and convert it to grayscale.
5. Threshold, Gaussian-blur, threshold, and Gaussian-blur each layer mask.
6. Merge the already blurred masks with a pixel-wise maximum.
7. Load every full-page image in the UI and rescale it whenever the zoom
   changes.

This block preserves legacy details for regression tests, including the old
alpha-blind grayscale comparison and one-sided-layer mask behavior. It is not
imported by the application.

## Shared viewport scheduler

`PdfTileScheduler` in `pdf_tile_scheduler.py` is shared by PCB and schematic
diffs. It owns the background priority queue, generation changes, cancellation,
result LRU, pinned coarse tile, retained low-resolution cache, and nearest-LOD
fallback selection. Renderers receive a tile task and return the same A, B,
overlap, and highlight-mask result shape; the scheduler does not depend on PCB
layers or PUI. A renderer generation change closes the previous pair's cached
PDF pages and documents before loading the new pair.

The common `PdfTileDiffView` maps those results to the canvas. Both differ
modes therefore use physical-pixel LOD selection, visible-area requests,
cursor and splitter priority, viewport cancellation, progressive redraw, and
coarse/nearest cached fallbacks. PCB includes the visible-layer tuple in its
cache key; schematic tiles use an empty variant because each selected PDF page
is already composited.

Ctrl+Wheel adjusts the comparison overlap linearly by one percentage point per
wheel notch, from 0% to 30% (default 13%). The percentage is the total overlap
width relative to the viewport width, so zooming does not change its on-screen
width. The overlap is centered on the splitter and clipped at the page edges.
Only the displayed overlap tile receives a red/blue accent: A's red channel
and B's blue channel are each raised by 128 before the channel-wise minimum
is taken. Identical pixels retain their original color because the other
image supplies the unchanged channel. PCB tiles are compared as straight BGRA
colors and then premultiplied for display; the non-overlap A/B images and
difference mask are unchanged.

## PCB viewport tile renderer

The application uses `PcbTileRenderer` in `pcb_diff_tiles.py`:

1. Keep layer PDFs as vectors; do not create full-page PNGs after export.
2. Express the document and viewport in PDF points.
3. Multiply the logical view scale by `canvas.pixel_density`, then select the
   first discrete raster level greater than or equal to that effective scale
   from 0.25, 0.5, 1, 2, 4, or 8 pixels per PDF point. Values above 8 are
   clamped to 8. Viewport, pointer, and drawing coordinates remain logical
   pixels; only PDF raster resolution follows the physical display density.
4. Convert the canvas, centered page offsets, tile boundaries, gutter, and
   paste positions onto one global integer pixel grid for that raster level.
   PDFium crop values are derived from this grid only at the API boundary;
   individual tiles never round PDF-point positions independently.
5. Split only the visible document area into 512 x 512-pixel tiles. Submit the
   cursor tile and its immediate neighborhood first, followed by tiles crossed
   by the two comparison splitter lines, then the remaining tiles in their
   original cursor-distance order. Before the cursor enters the canvas, the
   viewport center is used. Pending tiles are reprioritized when the cursor or
   a splitter crosses into another tile, or a later pan or zoom changes the
   visible ordering;
   pending tiles that leave the viewport are cancelled immediately, and their
   superseded queue entries are discarded before rendering. If a tile was
   already being rendered when it left the viewport, its result is discarded.
6. In a background worker, crop the required area directly from each vector
   PDF. A 24-pixel gutter is rendered around the tile before mask processing
   and removed afterward, preventing blur seams. Each layer PDF's page handle
   is cached by path and reused for every tile crop, then closed before its
   owning document when the renderer shuts down. For a crop that fills its
   requested tile, PDFium's `bitmap_maker` writes directly into reusable A/B
   grayscale work buffers. Padded edge crops retain the temporary fallback.
7. Alpha-composite the selected layers into one A image, one B image, and one
   overlap image per tile. PDFium renders opaque one-channel grayscale; white
   is converted to zero coverage and black to full coverage. KiCad's standard
   theme color is then applied from the layer's canonical name, so a custom
   name such as `F.Stiffener` retains its underlying `User.1` color. Each
   output uses one uint32 array whose four 8-bit lanes contain premultiplied
   BGRA. The application allocates `Format_ARGB32_Premultiplied` QImages first,
   exposes their owned pixel buffers as uint32 NumPy views, and composites only
   each layer's non-white bounding region directly into those buffers. This
   avoids final-array allocation, straight-alpha conversion, and QImage pixel
   copies. Reusable uint8 and uint32 work buffers, NumPy `out`, and OpenCV
   `dst` operations also avoid allocating darker, occupancy, merged-mask,
   blur, and alpha-compositing arrays for every layer and tile. The independent
   renderer can still convert to straight BGRA and
   write PNG artifacts for tests and benchmarks. The standard color table is
   built in and does not depend on the user's KiCad theme files.
8. Threshold A and B into binary geometry separately for each visible layer.
   Before merging the layer masks, discard one-sided geometry whose boundary
   is within 5 µm of the other side when the **5 µm Tolerance** option is
   enabled. The renderer converts this physical
   tolerance to pixels at the tile's raster level and accounts for the fact
   that the distance transform measures between pixel centers. This suppresses
   the narrow highlight bands produced when equivalent arcs are discretized
   differently. It is a boundary-distance test rather than an area filter, so
   an isolated new feature remains highlighted even when it is small. Merge
   the tolerated binary differences from all visible layers, then run the
   threshold/blur sequence once for the tile.
9. Cache results by diff generation, raster level, tile coordinate, and the
   visible-layer tuple. The memory cache uses a byte limit rather than an
   image-count limit. The complete-page coarse tile is pinned, and a reserved
   low-resolution cache retains recently used tiles at LOD 1 or below so
   high-resolution tiles cannot evict every fallback. Queued work outside the
   latest viewport is skipped.
10. The worker writes through `QImage.bits()` before publishing independently
    owned image resources to the UI. Once published, the images are immutable
    and PUI only reads them. This avoids PNG encoding, filesystem traffic, PNG
    decoding, and an extra memory copy in the application path. The path-based
    compatibility loader uses a short time budget instead of loading exactly
    one image per paint. The painter returns `True` while work or path loading
    remains, requesting an immediate redraw without performing PDF or OpenCV
    work on the UI thread.
11. Map every complete tile to one fixed destination rectangle. Moving the
    comparison cursor changes only a QPainter screen-space clip rectangle; it
    never recalculates a cropped source rectangle or resamples the tile.
12. When zoom selects another LOD, always draw the complete-page coarse tile
    first, then draw already loaded tiles from the nearest cached LOD over it.
    Newly exposed areas therefore remain covered while corresponding new LOD
    tiles progressively replace both fallback layers. Each fallback is clipped
    out of regions already covered by a loaded higher-priority image, so
    transparent tiles never draw the same text or line at two resolutions.

Changing a layer checkbox creates or reuses tiles for that exact layer set.
Pan and zoom reuse cached tiles at the same raster level and request only newly
visible tiles.

Each enabled layer shows **Visible Similarity (%)** for the detailed tiles that
intersect the current viewport. The renderer measures binary geometry after
applying the optional 5 µm tolerance described above. The score is the A/B
intersection area divided by their union area, equivalently 100% minus the
tolerated difference area divided by the union area. A score of `100.00`
therefore means that no difference remains in those tiles. Disabled layers
show no score. The score displays `...` until every visible tile at the current
raster level is
ready, and is recalculated after panning, zooming, or changing layer visibility.
Because it follows the current raster level and complete tiles intersecting the
viewport, it is a viewing aid rather than a whole-board metrology result.

Immediately after pair metadata is ready, the worker renders one pinned coarse
tile covering the complete page. It uses at most scale 0.5 and automatically
selects a lower scale when necessary to keep the entire page inside 512 x 512
pixels. This task cannot be cancelled by the first viewport request or evicted
from the current diff's memory cache. The UI may draw it as the initial
placeholder, and it remains available for zoom or pan before another LOD has
sufficient coverage.

## Schematic viewport tile renderer

`kicad-cli sch export pdf` produces one colored multi-page vector PDF per
revision. `convert_sch()` also makes small page thumbnails for the page
selectors, but those PNGs are not used by the main comparison canvas.

For each selected page pair, `SchematicTileRenderer` in `sch_diff_tiles.py`:

1. Caches the PDF documents and selected page handles.
2. Centers differently sized pages on a common PDF-point canvas using the same
   global integer pixel-grid rules as PCB tiles.
3. Renders only the requested tile plus a 24-pixel mask gutter. PDFium's
   `bitmap_maker` writes BGRx pixels directly into reusable A/B buffers when
   the crop fills the requested tile. Centered partial-page edge crops use a
   temporary PDFium bitmap and copy only their intersection.
4. Builds the overlap with `cv2.min`, computes the color difference and
   red highlight mask in reusable OpenCV destination buffers, removes the
   gutter, and copies the three base images and mask into QImage-owned tile
   buffers. Tiles with no difference skip both Gaussian blurs and do not retain
   a mask image.
5. Publishes the same result structure consumed by `PdfTileDiffView`, so page
   changes reset one scheduler generation and immediately queue a pinned
   complete-page coarse preview before detailed viewport tiles.

This removes the former fixed scale-7 full-page schematic raster, complete-page
rescaling on every zoom, and full-page A/B/overlap/mask PNG intermediates.

## Intentional differences

| Behavior | Legacy | Viewport |
| --- | --- | --- |
| Raster area | Complete page and every layer | Current visible tiles and visible layers |
| Raster resolution | Fixed scale 7 | Ceiling physical-pixel viewport LOD, scale 0.25 through 8 |
| Layer images on disk | Three full-page images per layer | None in the application; four composited PNGs per tile in test/benchmark mode |
| Mask processing | Blur every layer, then merge | Optionally apply a 5 µm per-layer geometry tolerance, merge binary layers, then blur once |
| Alpha-only changes | Ignored by grayscale conversion | Included in the binary difference |
| Layer visibility | Changes display only | Changes both display and difference mask |
| UI work | Loads and rescales full pages | Draws worker-produced in-memory tile images |

## Unit-test blocks

The renderer and scheduler blocks are independent of PUI and can be imported
directly:

```python
from legacy_pcb_diff import PcbLegacyRenderer
from pcb_diff_tiles import PcbTileRenderer
from pdf_tile_scheduler import PdfTileScheduler
from sch_diff_tiles import SchematicTileRenderer
```

The smaller processing blocks are also public where their legacy semantics
matter:

- `legacy_compare_layer()`
- `legacy_finish_layer_mask()`
- `combine_layer_images()`
- `finish_merged_mask()`
- `visible_tile_indices()`
- `choose_render_scale()`
- `choose_coarse_render_scale()`
- `choose_fallback_scale()`
- `clipped_tile_geometry()`

Run their unit tests from the repository workdir:

```sh
env/bin/python -m unittest \
  tests.test_pcb_diff_tiles \
  tests.test_pdf_tile_scheduler \
  tests.test_sch_diff_tiles
```

The tests use small in-memory arrays and mocked PDF rendering, so they do not
start KiCad or a GUI.

## Direct performance comparison

Run both renderer blocks against real boards from the repository workdir:

```sh
env/bin/python tests/benchmark_pcb_differ.py \
  samples/fpc.kicad_pcb samples/fpc2.kicad_pcb
```

Defaults are the historical scale 7 for the legacy renderer and a 1200 x 800
viewport for the new renderer. PDF export time is printed but excluded from
the renderer speedup. To retain PDFs and rendered images for inspection:

```sh
env/bin/python tests/benchmark_pcb_differ.py \
  samples/fpc.kicad_pcb samples/fpc2.kicad_pcb \
  --output /tmp/kikakuka-differ-benchmark
```

Use `--legacy-scale`, `--viewport-width`, `--viewport-height`, and
`--pixel-density` to exercise other conditions. For example, append
`--pixel-density 2` to model a 2x HiDPI display. Compare results on the same
machine and with the same input commits; filesystem and PDFium caches can
affect short runs.

### Reference run

On 2026-09-19, an Apple M2 MacBook Air with 16 GB RAM produced this result for
the command above. The comparison used the 26-layer union of `fpc` and `fpc2`:

| Measurement | Result |
| --- | ---: |
| PDF export, excluded from comparison | 0.941 s |
| Legacy full page, scale 7 | 16.103 s |
| Viewport, 1200 x 800, scale 1, four tiles | 0.970 s |
| First viewport tile | 0.466 s |
| Visible-area speedup | 16.60x |
| Legacy artifacts | 8.0 MiB |
| Viewport artifacts | 43.5 KiB |

These values document one reference run, not a pass/fail threshold. The fixed
scale-7 legacy workload is intentional because that is what the application
used before the viewport renderer.
