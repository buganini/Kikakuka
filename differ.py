import os
from PUI.PySide6 import *
import PUI
from common import *
import json
import platform
import subprocess
from threading import Thread
import queue
import glob
import pypdfium2 as pdfium
import cv2
import numpy as np
import tempfile
import atexit
import shutil
import githelper
import pcbnew
from pcb_open import open_kicad_file
from gerber import is_gerber_dir, is_gerber_zip
from differ_input import prepare_differ_source
from differ_source import source_paths
from differ_view_geometry import (
    DEFAULT_OVERLAP_PERCENT, adjust_overlap_percent, canvas_priority_point,
    clipped_view_transform, overlap_bounds,
)
from differ_overlap import apply_overlap_color_shift
import time
from collections import OrderedDict
from PySide6 import QtCore, QtGui

from pcb_diff_tiles import (
    PCB_LAYER_PRESETS,
    PcbTileRenderer,
    build_pair_metadata,
    choose_render_scale,
    clipped_tile_geometry,
    comparison_regions,
    layers_for_preset,
    layer_label_color,
    mirrored_view_transform,
    paired_layer_label,
    prioritize_selected_layer,
    sort_layers_in_kicad_ui_order,
    toggle_selected_layer,
    tile_pixel_bounds,
    visible_tile_indices,
)
from pdf_tile_scheduler import PdfTileScheduler
from sch_diff_tiles import (
    SchematicTileRenderer,
    build_schematic_pair_metadata,
    corresponding_schematic_page,
    matched_page_shift,
    synchronized_page_shift,
)


PDF_TILE_IMAGE_LOAD_BUDGET_SECONDS = 0.004
PDF_TILE_CACHE_BYTES = 384 * 1024 * 1024
PDF_TILE_LOW_RES_CACHE_BYTES = 64 * 1024 * 1024
PDF_TILE_LOW_RES_MAX_SCALE = 1.0
DIFFER_TILE_LOG_ENABLED = os.environ.get("KIKAKUKA_DIFFER_TILE_LOG") == "1"
PCB_DIFF_TOLERANCE_UM = 10.0


class LayerList(VBox):
    def update(self, prev):
        super().update(prev)
        self.qtlayout.setContentsMargins(0, 0, 0, 0)
        self.qtlayout.setSpacing(0)


def premultiplied_image_resource(width, height):
    """Create a QImage and expose its owned premultiplied pixels to NumPy."""
    qimage = QtGui.QImage(
        width,
        height,
        QtGui.QImage.Format.Format_ARGB32_Premultiplied,
    )
    if qimage.isNull():
        raise RuntimeError("Could not create PCB tile QImage")
    pixels_per_line = qimage.bytesPerLine() // np.dtype(np.uint32).itemsize
    buffer = np.frombuffer(
        qimage.bits(),
        dtype=np.uint32,
        count=pixels_per_line * height,
    ).reshape(height, pixels_per_line)[:, :width]
    resource = ImageResource()
    resource.qimage = qimage
    return resource, buffer

if platform.system() == "Darwin":
    kicad_cli = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
elif platform.system() == "Windows":
    cands = glob.glob("C:/Program Files/KiCad/*/bin/kicad-cli.exe")
    kicad_cli = cands[0] if cands else "C:/Program Files/KiCad/bin/kicad-cli.exe"
else:
    kicad_cli = "/usr/bin/kicad-cli"

try:
    base_path = sys._MEIPASS
    cands = None
    if platform.system() == "Darwin":
        cands = glob.glob(os.path.join(os.path.abspath(base_path, "..", "MacOS"), "kicad-cli*"))
    elif platform.system() == "Windows":
        cands = glob.glob(os.path.join(base_path, "KiCad", "bin", "kicad-cli*"))
    if cands:
        kicad_cli = cands[0]
except Exception:
    pass

kicad_cli_version = "Error"
try:
    kicad_cli_version = subprocess.check_output([kicad_cli, "--version"]).decode().strip()
except Exception:
    pass

def convert_sch(path, outpath):
    os.makedirs(outpath, exist_ok=True)

    pdfpath = os.path.join(outpath, "sch.pdf")
    if not os.path.exists(pdfpath):
        yield f"Exporting PDF for {os.path.basename(path)}..."
        cmd = [kicad_cli, "sch", "export", "pdf", "-o", pdfpath, path]
        kwargs = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.run(cmd, **kwargs)

    if not os.path.exists(os.path.join(outpath, "sch")):
        yield f"Exporting page thumbnails for {os.path.basename(path)}..."
        os.makedirs(os.path.join(outpath, "sch"), exist_ok=True)
        pdf = pdfium.PdfDocument(pdfpath)
        try:
            for p, page in enumerate(pdf):
                try:
                    page_width, _page_height = page.get_size()
                    thumbnail_scale = min(1.0, 480.0 / page_width)
                    opencv_image = page.render(
                        scale=thumbnail_scale,
                        rotation=0,
                        fill_color=(255, 255, 255, 255),
                        prefer_bgrx=True,
                    ).to_numpy()
                    cv2.imwrite(
                        os.path.join(outpath, "sch", f"sch_{p:02d}.png"),
                        opencv_image,
                    )
                finally:
                    page.close()
        finally:
            pdf.close()

def get_pcb_layers(path):
    board = pcbnew.LoadBoard(path)
    return [board.GetLayerName(layer) for layer in board.GetEnabledLayers().Seq()]


def get_pcb_layer_names(path):
    board = pcbnew.LoadBoard(path)
    return {
        pcbnew.LayerName(layer): board.GetLayerName(layer)
        for layer in board.GetEnabledLayers().Seq()
    }

def convert_pcb(path, outpath):
    os.makedirs(outpath, exist_ok=True)

    pdfpath = os.path.join(outpath, f"pcb_pdf")
    if not os.path.exists(pdfpath):
        yield f"Exporting PDF for {os.path.basename(path)}..."
        cmd = [
            kicad_cli,
            "pcb", "export", "pdf",
            "--mode-separate",
            "--black-and-white",
            "--layers", ",".join(get_pcb_layers(path)),
            "-o", pdfpath,
            path,
        ]
        kwargs = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.run(cmd, **kwargs)


class PdfTileDiffView(PUIView):
    scheduler_attribute = None
    page_size_attribute = None
    background_color = 0x001124
    cursor_color = 0x7e8792
    mask_opacity = 0.3
    zoom_limit = 64

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.canvas_width = None
        self.canvas_height = None
        self.diff_width = None
        self.diff_height = None
        self.generation = None
        self.tile_images = OrderedDict()
        self.mousehold = False
        self._last_tile_log = 0.0
        self._last_tile_log_incomplete = False

    def setup(self):
        self.state = State()
        self.state.scale = None
        self.state.splitter_x = 0.5
        self.state.mousepos = None

    @property
    def scheduler(self):
        return getattr(self.main, self.scheduler_attribute)

    def page_size(self):
        return getattr(self.main.state, self.page_size_attribute)

    def tile_variant(self):
        return ()

    def flip_horizontal(self):
        return False

    def autoScale(self, canvas_width, canvas_height):
        page_size = self.page_size()
        if not page_size:
            return False

        dw, dh = page_size
        self.diff_width, self.diff_height = dw, dh
        self.canvas_width, self.canvas_height = canvas_width, canvas_height
        fitted = clipped_view_transform(
            self.state.scale, page_size, (canvas_width, canvas_height),
            self.zoom_limit,
        )
        if fitted is None:
            return False
        self.state.scale, self.scale = fitted
        return True

    def toCanvas(self, x, y):
        """
        Convert global coordinate system to canvas coordinate system
        """
        offx, offy, scale = self.state.scale
        return x * scale + offx, y * scale + offy

    def fromCanvas(self, x, y):
        """
        Convert canvas coordinate system to global coordinate system
        """
        offx, offy, scale = self.state.scale
        return (x - offx)/scale, (y - offy)/scale

    def content(self):
        # register update
        self.main.state.diff_pair
        self.state.splitter_x
        self.main.state.overlap_percent
        self.state.scale
        self.tile_variant()
        self.main.state.highlight_changes
        self.flip_horizontal()
        self.main.state.build_time

        (Canvas(self.painter).layout(weight=1)
         .style(bgColor=self.background_color)
         .mousedown(self.mousedown)
         .mouseup(self.mouseup)
         .mousemove(self.mousemove)
         .wheel(self.wheel))

    def mousedown(self, e):
        self.state.mousepos = e.x, e.y
        self.mousehold = True

    def mouseup(self, e):
        self.mousehold = False

    def mousemove(self, e):
        if self.state.scale is None:
            return
        if self.canvas_width is None:
            return

        if self.mousehold:
            pdx = e.x - self.state.mousepos[0]
            pdy = e.y - self.state.mousepos[1]

            offx, offy, scale = self.state.scale
            offx += pdx
            offy += pdy
            self.state.scale = offx, offy, scale
        else:
            x, _ = self.fromCanvas(e.x, 0)
            self.state.splitter_x = x / self.diff_width
        self.state.mousepos = e.x, e.y

    def wheel(self, e):
        if e.modifiers & KeyModifier.CTRL:
            self.main.state.overlap_percent = adjust_overlap_percent(
                self.main.state.overlap_percent, e.v_delta
            )
            return

        if self.state.scale is None:
            return

        offx, offy, scale = self.state.scale
        zoom_factor = 1.2  # Factor for smoother zooming

        nscale = scale * (zoom_factor ** (e.v_delta / 120))

        # Limit the scale
        nscale = min(self.scale*self.zoom_limit, max(self.scale/8, nscale))

        # Calculate new offsets
        offx = e.x - (e.x - offx) * nscale / scale
        offy = e.y - (e.y - offy) * nscale / scale

        self.state.scale = offx, offy, nscale

    def painter(self, canvas):
        generation = self.scheduler.generation
        generation_changed = self.generation != generation
        if generation_changed:
            self.generation = generation
            self.tile_images.clear()
            self._last_tile_log = 0.0
            self._last_tile_log_incomplete = False

        page_size = self.page_size()
        if not page_size:
            return False
        if (generation_changed or self.state.scale is None or
                (self.canvas_width, self.canvas_height) !=
                (canvas.width, canvas.height) or
                (self.diff_width, self.diff_height) != page_size):
            return self.autoScale(canvas.width, canvas.height)

        immediate = False
        offx, offy, scale = self.state.scale
        flipped = self.flip_horizontal()
        view_transform = (
            mirrored_view_transform(
                canvas.width, self.diff_width, self.state.scale
            ) if flipped else self.state.scale
        )
        view_offx = view_transform[0]
        render_scale = choose_render_scale(scale, canvas.pixel_density)
        variant = self.tile_variant()
        x_left, x_right = overlap_bounds(
            self.diff_width, self.state.splitter_x, canvas.width, scale,
            self.main.state.overlap_percent,
        )
        region_a, region_b, region_darker = comparison_regions(
            self.diff_width, x_left, x_right, flipped
        )
        cursor = canvas.ui.mapFromGlobal(QtGui.QCursor.pos())
        priority_point = canvas_priority_point(
            (cursor.x(), cursor.y()), (canvas.width, canvas.height)
        )
        if flipped:
            priority_point = (
                canvas.width - priority_point[0], priority_point[1]
            )
        tile_indices = visible_tile_indices(
            (self.diff_width, self.diff_height),
            (canvas.width, canvas.height),
            view_transform,
            render_scale,
            priority_point=priority_point,
            priority_lines=(region_darker[0], region_darker[1]),
        )
        tile_results = []
        tile_keys = self.scheduler.request(
            render_scale, tile_indices, variant
        )
        for key in tile_keys:
            result = self.scheduler.get(key)
            if result is None:
                immediate = True
            elif "error" not in result:
                tile_results.append(result)

        viewport_bounds = (
            max(0.0, (0.0 - view_offx) / scale),
            max(0.0, (0.0 - offy) / scale),
            min(self.diff_width, (canvas.width - view_offx) / scale),
            min(self.diff_height, (canvas.height - offy) / scale),
        )
        fallback_results = self.scheduler.fallback(
            render_scale, variant, viewport_bounds
        )

        if DIFFER_TILE_LOG_ENABLED and isinstance(self, PcbDiffView):
            incomplete = len(tile_results) < len(tile_keys)
            now = time.monotonic()
            if (now - self._last_tile_log >= 1.0 or
                    self._last_tile_log_incomplete and not incomplete):
                cache = self.scheduler.cache_snapshot(tile_keys)
                render_ms = cache["render_ms_avg"]
                render_ms_text = (
                    f"{render_ms:.1f}" if render_ms is not None else "n/a"
                )
                pdf_ms = cache["pdf_render_ms_avg"]
                pdf_ms_text = f"{pdf_ms:.1f}" if pdf_ms is not None else "n/a"
                composite_ms = cache["composite_ms_avg"]
                composite_ms_text = (
                    f"{composite_ms:.1f}"
                    if composite_ms is not None else "n/a"
                )
                print(
                    "[differ tiles] "
                    f"zoom={scale / self.scale:.2f}x "
                    f"view={scale:.2f}x dpr={canvas.pixel_density:.2f} "
                    f"raster={render_scale:.2f}x "
                    f"visible={len(tile_keys)} "
                    f"cached={cache['ready']}/{len(tile_keys)} "
                    f"pending={cache['pending']} errors={cache['errors']} "
                    f"fallback={len(fallback_results)} "
                    f"layers={len(variant)} "
                    f"cache={cache['bytes'] / 1048576:.1f}/"
                    f"{cache['limit_bytes'] / 1048576:.0f}MiB "
                    f"entries={cache['entries']} "
                    f"evictions={cache['evictions']} "
                    f"queue={cache['queue_depth']} "
                    f"render_ms={render_ms_text} "
                    f"pdf_ms={pdf_ms_text} "
                    f"composite_ms={composite_ms_text}",
                    flush=True,
                )
                self._last_tile_log = now
                self._last_tile_log_incomplete = incomplete

        load_started = time.perf_counter()
        loaded_images = [0]

        def load_image(source, allow_load=True):
            nonlocal immediate
            if source is None:
                return None
            if not isinstance(source, (str, bytes, os.PathLike)):
                return source
            path = os.fspath(source)
            image = self.tile_images.get(path)
            if image is not None:
                self.tile_images.move_to_end(path)
                return image
            if not allow_load:
                return None
            if (loaded_images[0] and
                    time.perf_counter() - load_started >=
                    PDF_TILE_IMAGE_LOAD_BUDGET_SECONDS):
                immediate = True
                return None
            try:
                image = canvas.loadImage(path)
            except Exception:
                return None
            self.tile_images[path] = image
            loaded_images[0] += 1
            immediate = True
            while len(self.tile_images) > 384:
                self.tile_images.popitem(last=False)
            return image

        def image_is_loaded(source):
            if source is None:
                return False
            if isinstance(source, (str, bytes, os.PathLike)):
                return os.fspath(source) in self.tile_images
            return True

        def draw_region(image, bounds, region_left, region_right,
                        pixel_size, opacity=0.8, exclude_region=None):
            if image is None:
                return
            geometry = clipped_tile_geometry(
                bounds,
                pixel_size,
                view_transform,
                region_left,
                region_right,
            )
            if geometry is None:
                return
            dest_x, dest_y, dest_width, dest_height = geometry["destination"]
            src_x, src_y, src_width, src_height = geometry["source"]
            clip_x, clip_y, clip_width, clip_height = geometry["clip"]
            clip_region = QtGui.QRegion(QtCore.QRect(
                clip_x, clip_y, clip_width, clip_height
            ))
            if exclude_region is not None:
                clip_region = clip_region.subtracted(exclude_region)
            if clip_region.isEmpty():
                return
            canvas.qpainter.save()
            try:
                canvas.qpainter.setRenderHint(
                    QtGui.QPainter.RenderHint.SmoothPixmapTransform, True
                )
                if flipped:
                    canvas.qpainter.translate(canvas.width, 0)
                    canvas.qpainter.scale(-1, 1)
                canvas.qpainter.setClipRegion(
                    clip_region,
                    QtCore.Qt.ClipOperation.IntersectClip,
                )
                canvas.drawImage(
                    image,
                    dest_x,
                    dest_y,
                    width=dest_width,
                    height=dest_height,
                    src_x=src_x,
                    src_y=src_y,
                    src_width=src_width,
                    src_height=src_height,
                    opacity=opacity,
                )
            finally:
                canvas.qpainter.restore()

        def draw_result(result, allow_load, draw_mask,
                        exclude_region=None):
            bounds = result["bounds"]
            pixel_size = result["pixel_size"]
            image_paths = result["images"]
            if (bounds[0] < region_a[1] and
                    bounds[0] + bounds[2] > region_a[0]):
                draw_region(
                    load_image(image_paths["a"], allow_load),
                    bounds, *region_a, pixel_size, opacity=1.0,
                    exclude_region=exclude_region,
                )
            if (bounds[0] < region_b[1] and
                    bounds[0] + bounds[2] > region_b[0]):
                draw_region(
                    load_image(image_paths["b"], allow_load),
                    bounds, *region_b, pixel_size,
                    opacity=1.0,
                    exclude_region=exclude_region,
                )
            if (bounds[0] < region_darker[1] and
                    bounds[0] + bounds[2] > region_darker[0]):
                draw_region(
                    load_image(image_paths["darker"], allow_load),
                    bounds, *region_darker, pixel_size, opacity=1.0,
                    exclude_region=exclude_region,
                )

            if (draw_mask and self.main.state.highlight_changes and
                    result["mask"]):
                draw_region(
                    load_image(result["mask"], allow_load),
                    bounds, 0.0, self.diff_width, pixel_size,
                    opacity=self.mask_opacity,
                    exclude_region=exclude_region,
                )

        def loaded_base_region(result):
            bounds = result["bounds"]
            pixel_size = result["pixel_size"]
            image_paths = result["images"]
            region = QtGui.QRegion()
            visible_images = []
            for name, source_region in (
                ("a", region_a),
                ("b", region_b),
                ("darker", region_darker),
            ):
                if (bounds[0] < source_region[1] and
                        bounds[0] + bounds[2] > source_region[0]):
                    visible_images.append((image_paths[name], *source_region))
            for source, region_left, region_right in visible_images:
                if not image_is_loaded(source):
                    continue
                geometry = clipped_tile_geometry(
                    bounds,
                    pixel_size,
                    view_transform,
                    region_left,
                    region_right,
                )
                if geometry is not None:
                    region = region.united(QtGui.QRegion(
                        QtCore.QRect(*geometry["clip"])
                    ))
            return region

        exact_coverage = QtGui.QRegion()
        for result in tile_results:
            exact_coverage = exact_coverage.united(
                loaded_base_region(result)
            )

        detailed_fallbacks = [
            result for result in fallback_results
            if not result.get("coarse")
        ]
        detailed_coverage = QtGui.QRegion(exact_coverage)
        for result in detailed_fallbacks:
            detailed_coverage = detailed_coverage.united(
                loaded_base_region(result)
            )

        for result in fallback_results:
            if not result.get("coarse"):
                continue
            draw_result(
                result,
                allow_load=True,
                draw_mask=False,
                exclude_region=detailed_coverage,
            )
        for result in detailed_fallbacks:
            draw_result(
                result,
                allow_load=False,
                draw_mask=False,
                exclude_region=exact_coverage,
            )
        for result in tile_results:
            draw_result(result, allow_load=True, draw_mask=True)

        cursor_left = round(offx + x_left * scale)
        cursor_right = round(offx + x_right * scale)
        canvas.drawLine(
            cursor_left, 0, cursor_left, canvas.height,
            color=self.cursor_color, width=1,
        )
        canvas.drawLine(
            cursor_right, 0, cursor_right, canvas.height,
            color=self.cursor_color, width=1,
        )

        return immediate


class PcbDiffView(PdfTileDiffView):
    scheduler_attribute = "pcb_tiles"
    page_size_attribute = "pcb_page_size"

    def tile_variant(self):
        return self.main.pcb_layer_variant()

    def flip_horizontal(self):
        return self.main.state.flip_board_view


class SchDiffView(PdfTileDiffView):
    scheduler_attribute = "sch_tiles"
    page_size_attribute = "sch_page_size"
    background_color = 0xF5F4EE
    cursor_color = 0
    zoom_limit = 4


class DifferUI(Application):
    def __init__(self, *argv):
        super().__init__(icon=resource_path("icon.ico"))

        self.temp_dir = tempfile.mkdtemp(prefix="kikakuka_differ_")
        atexit.register(self.cleanup)

        self.state = State()
        self.state.show_layers = {}
        self.state.loading_diff = False
        self.state.loading_a = False
        self.state.loading_b = False
        self.state.file_a = ""
        self.state.file_b = ""
        self.state.source_a = ""
        self.state.source_b = ""
        self.state.logs_a = None
        self.state.logs_b = None
        self.state.commit_a = ""
        self.state.commit_b = ""
        self.state.page_a = 0
        self.state.page_b = 0
        self.state.sync_page = True
        self.state.diff_pair = None
        self.state.layers = []
        self.state.layer_labels = {}
        self.state.layer_preset = "All Layers"
        self.state.selected_layer = None
        self.state.highlight_changes = True
        self.state.flip_board_view = False
        self.state.overlap_percent = DEFAULT_OVERLAP_PERCENT
        self.state.build_time = 0
        self.state.use_workspace = False
        self.state.cached_file_a = ""
        self.state.cached_file_b = ""
        self.state.pcb_page_size = None
        self.state.sch_page_size = None
        self.state.message = ""
        self.repo_a = None
        self.repo_b = None
        self._pending_revision_a = None
        self._pending_revision_b = None

        self.queue = queue.Queue()
        scheduler_options = {
            "cache_bytes": PDF_TILE_CACHE_BYTES,
            "low_res_cache_bytes": PDF_TILE_LOW_RES_CACHE_BYTES,
            "low_res_max_scale": PDF_TILE_LOW_RES_MAX_SCALE,
            "on_result": self.tile_ready,
        }
        self.pcb_tiles = PdfTileScheduler(
            PcbTileRenderer, self.render_pcb_tile, **scheduler_options
        )
        self.sch_tiles = PdfTileScheduler(
            SchematicTileRenderer, self.render_sch_tile,
            **scheduler_options,
        )

        Thread(target=self.bg_looper, daemon=True).start()

        if len(argv) == 1:
            filepath = argv[0]
            with open(filepath, "r") as f:
                self.state.use_workspace = True
                self.base_dir = os.path.dirname(os.path.abspath(filepath))
                self.workspace = json.load(f)
                for project in self.workspace["projects"]:
                    if not os.path.isabs(project["path"]):
                        project["path"] = os.path.join(self.base_dir, project["path"])
                findFiles(self.workspace, self.base_dir, [SCH_SUFFIX, PCB_SUFFIX])
        elif len(argv) == 2:
            warnings = []
            self.state.source_a = os.path.abspath(argv[0])
            self.state.file_a, errors = prepare_differ_source(
                self.state.source_a, "a", self.temp_dir)
            warnings.extend(errors)
            self.state.source_b = os.path.abspath(argv[1])
            self.state.file_b, errors = prepare_differ_source(
                self.state.source_b, "b", self.temp_dir)
            warnings.extend(errors)
            if warnings:
                self.state.message = (
                    "Gerber conversion warnings: " + "; ".join(warnings)
                )
            self.build()

    def cleanup(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def content(self):
        title = f"Kikakuka v{VERSION} Differ (KiCad CLI {kicad_cli_version}, Pypdfium2 {pdfium.version.PYPDFIUM_INFO}, OpenCV {cv2.__version__}, PUI {PUI.__version__} {PUI_BACKEND})"
        with Window(maximize=True, title=title, icon=resource_path("icon.ico")):
            with VBox():
                if not os.path.exists(kicad_cli):
                    Label("KiCad CLI not found")
                    Spacer()
                    return
                with HBox():
                    if self.state.use_workspace:
                            with HBox():
                                Label("File A")
                                with ComboBox(text_model=self.state("file_a")).layout(weight=1).change(lambda e: self.change_file_a()):
                                    for project in self.workspace["projects"]:
                                        if project["path"].lower().endswith(PNL_SUFFIXES):
                                            continue
                                        for file in project["files"]:
                                            folder_name = os.path.basename(os.path.dirname(file["path"]))
                                            file_name = os.path.basename(file["path"])
                                            folder_file = f"{folder_name}/{file_name}"
                                            ComboBoxItem(folder_file, file["path"])

                            with HBox():
                                Label("File B")
                                with ComboBox(text_model=self.state("file_b")).layout(weight=1).change(lambda e: self.change_file_b()):
                                    for project in self.workspace["projects"]:
                                        if project["path"].lower().endswith(PNL_SUFFIXES):
                                            continue
                                        for file in project["files"]:
                                            folder_name = os.path.basename(os.path.dirname(file["path"]))
                                            file_name = os.path.basename(file["path"])
                                            folder_file = f"{folder_name}/{file_name}"
                                            ComboBoxItem(folder_file, file["path"])
                    else:
                        with HBox():
                            Label("File A")
                            if self.state.source_a:
                                Label(self.state.source_a).layout(weight=1)
                                Button("Clear").click(self.clear_file_a)
                            else:
                                Button("Open KiCad File").click(
                                    self.open_file_a)
                                Button("Open Gerber Folder").click(
                                    self.open_gerber_folder_a)
                                Button("Open Gerber Zip").click(
                                    self.open_gerber_zip_a)
                                Spacer()
                        with HBox():
                            Label("File B")
                            if self.state.source_b:
                                Label(self.state.source_b).layout(weight=1)
                                Button("Clear").click(self.clear_file_b)
                            else:
                                Button("Open KiCad File").click(
                                    self.open_file_b)
                                Button("Open Gerber Folder").click(
                                    self.open_gerber_folder_b)
                                Button("Open Gerber Zip").click(
                                    self.open_gerber_zip_b)
                                Spacer()

                with HBox():
                    with HBox().layout(weight=1):
                        Label("Revision")
                        if self.state.file_a and self.state.logs_a is None:
                            Label("Loading...").layout(weight=1)
                        elif self.state.logs_a:
                            with ComboBox(text_model=self.state("commit_a")).layout(weight=1).change(lambda e: self.select_commit_a()):
                                ComboBoxItem("WORKING", "")
                                for hex, msg in self.state.logs_a:
                                    ComboBoxItem(msg.split("\n")[0].rstrip()[:250], hex)
                        else:
                            Label("N/A").layout(weight=1)

                    with HBox().layout(weight=1):
                        Label("Revision")
                        if self.state.file_b and self.state.logs_b is None:
                            Label("Loading...").layout(weight=1)
                        elif self.state.logs_b:
                            with ComboBox(text_model=self.state("commit_b")).layout(weight=1).change(lambda e: self.select_commit_b()):
                                ComboBoxItem("WORKING", "")
                                for hex, msg in self.state.logs_b:
                                    ComboBoxItem(msg.split("\n")[0].rstrip()[:250], hex)
                        else:
                            Label("N/A").layout(weight=1)


                with HBox():
                    if self.state.loading_diff is True:
                        Spacer()
                        Label("Loading diff...")
                        Spacer()
                    elif self.state.loading_diff:
                        Spacer()
                        Label(f"Loading diff for {self.state.loading_diff}...")
                        Spacer()
                    elif self.state.loading_a or self.state.loading_b:
                            Label(self.state.loading_a or "").layout(weight=1)
                            Label(self.state.loading_b or "").layout(weight=1)
                    elif self.state.file_a and self.state.file_b:
                            file_type = os.path.splitext(
                                self.state.file_a
                            )[1].lower()
                            if file_type == SCH_SUFFIX:
                                Button("PCB Diff").click(self.pcb_diff)
                            elif file_type == PCB_SUFFIX:
                                Button("SCH Diff").click(self.sch_diff)
                            if file_type in (SCH_SUFFIX, PCB_SUFFIX):
                                Button("Open File A").click(
                                    self.open_selected_file_a
                                )
                                Button("Open File B").click(
                                    self.open_selected_file_b
                                )
                            if (file_type == SCH_SUFFIX and
                                    os.path.splitext(
                                        self.state.file_b
                                    )[1].lower() == SCH_SUFFIX):
                                Button("◀").click(
                                    lambda e: self.shift_sch_pages(-1)
                                )
                                Button("▶").click(
                                    lambda e: self.shift_sch_pages(1)
                                )
                                Checkbox("Sync Page", model=self.state("sync_page")).click(
                                    self.sync_sch_pages
                                )
                            if self.state.message:
                                Label(self.state.message).layout(weight=1)
                            else:
                                Label(
                                    f"Overlap: {self.state.overlap_percent:.1f}% "
                                    "(Ctrl+Wheel)"
                                ).layout(weight=1)
                            if file_type == SCH_SUFFIX:
                                Checkbox("Highlight Changes", model=self.state("highlight_changes"))
                    else:
                        Spacer()
                        Label("Select two files to compare")
                        Spacer()

                if self.state.file_a and self.state.file_b:
                    if os.path.splitext(self.state.file_a)[1].lower() != os.path.splitext(self.state.file_b)[1].lower():
                        Label("Files are different types")
                        Spacer()
                        return

                    if os.path.splitext(self.state.file_a)[1].lower() == SCH_SUFFIX and os.path.splitext(self.state.file_b)[1].lower() == SCH_SUFFIX:
                        with HBox():
                            with Scroll().layout(width=250):
                                with VBox():
                                    if self.state.cached_file_a:
                                        for i,png in enumerate(sorted(os.listdir(os.path.join(self.state.cached_file_a, "sch")))):
                                            Image(os.path.join(self.state.cached_file_a, "sch", png)).layout(width=240).click(lambda e, png: self.select_page_a(png), png)
                                            if png==self.state.page_a:
                                                Label(f"* Page {i+1} *")
                                            else:
                                                Label(f"Page {i+1}")
                                    else:
                                        Label("Loading pages...")
                                    Spacer()

                            if not self.state.page_a or not self.state.page_b:
                                Spacer()
                            else:
                                with VBox().layout(weight=1):
                                    SchDiffView(self)

                            with Scroll().layout(width=250):
                                with VBox():
                                    if self.state.cached_file_b:
                                        for i,png in enumerate(sorted(os.listdir(os.path.join(self.state.cached_file_b, "sch")))):
                                            Image(os.path.join(self.state.cached_file_b, "sch", png)).layout(width=240).click(lambda e, png: self.select_page_b(png), png)
                                            if png==self.state.page_b:
                                                Label(f"* Page {i+1} *")
                                            else:
                                                Label(f"Page {i+1}")
                                    else:
                                        Label("Loading pages...")
                                    Spacer()
                    elif os.path.splitext(self.state.file_a)[1].lower() == PCB_SUFFIX:
                        with HBox():
                            with VBox().layout(weight=1):
                                PcbDiffView(self)
                            with Scroll(horizontal=None).layout(width=250):
                                with VBox():
                                    Checkbox("Highlight Changes", model=self.state("highlight_changes"))
                                    Checkbox("Flip board view", model=self.state("flip_board_view"))
                                    Label("Presets")
                                    with ComboBox(
                                        text_model=self.state("layer_preset")
                                    ).change(self.apply_layer_preset):
                                        if self.state.layer_preset == "Custom":
                                            ComboBoxItem("Custom")
                                        for preset in PCB_LAYER_PRESETS:
                                            ComboBoxItem(preset)
                                    Label("Display Layers")
                                    with LayerList():
                                        for layer in self.state.layers:
                                            with HBox():
                                                Checkbox(
                                                    "", model=self.state.show_layers(layer)
                                                ).qt(
                                                    StyleSheet={"spacing": "0px"}
                                                ).click(
                                                    self.layer_visibility_changed,
                                                    layer,
                                                )
                                                Label("■").style(
                                                    color=layer_label_color(layer)
                                                ).click(
                                                    self.select_pcb_layer, layer
                                                )
                                                selected = self.state.selected_layer == layer
                                                layer_label = Label(
                                                    self.state.layer_labels.get(
                                                        layer, layer
                                                    )
                                                ).layout(
                                                    weight=1
                                                ).click(
                                                    self.select_pcb_layer, layer
                                                )
                                                if selected:
                                                    layer_label.qt(
                                                        StyleSheet={
                                                            "font-weight": "bold"
                                                        }
                                                    )
                                    Spacer()
                else:
                    with HBox():
                        with VBox().dragEnter(self.handleDragEnter).drop(self.drop_file_a):
                            Spacer()
                            with HBox():
                                Spacer()
                                Label("Drop File Here").style(fontSize=36)
                                Spacer()
                            Spacer()
                        with VBox().dragEnter(self.handleDragEnter).drop(self.drop_file_b):
                            Spacer()
                            with HBox():
                                Spacer()
                                Label("Drop File Here").style(fontSize=36)
                                Spacer()
                            Spacer()

    def pcb_diff(self, e):
        self._pending_revision_a = self.state.commit_a
        self._pending_revision_b = self.state.commit_b
        if self.state.source_a == self.state.file_a:
            self.state.source_a = (
                os.path.splitext(self.state.source_a)[0] + PCB_SUFFIX)
        if self.state.source_b == self.state.file_b:
            self.state.source_b = (
                os.path.splitext(self.state.source_b)[0] + PCB_SUFFIX)
        self.state.file_a = os.path.splitext(self.state.file_a)[0] + PCB_SUFFIX
        self.state.file_b = os.path.splitext(self.state.file_b)[0] + PCB_SUFFIX
        self.state.logs_a = None
        self.state.logs_b = None
        self.state.cached_file_a = None
        self.state.cached_file_b = None
        self.build()

    def sch_diff(self, e):
        self._pending_revision_a = self.state.commit_a
        self._pending_revision_b = self.state.commit_b
        if self.state.source_a == self.state.file_a:
            self.state.source_a = (
                os.path.splitext(self.state.source_a)[0] + SCH_SUFFIX)
        if self.state.source_b == self.state.file_b:
            self.state.source_b = (
                os.path.splitext(self.state.source_b)[0] + SCH_SUFFIX)
        self.state.file_a = os.path.splitext(self.state.file_a)[0] + SCH_SUFFIX
        self.state.file_b = os.path.splitext(self.state.file_b)[0] + SCH_SUFFIX
        self.state.logs_a = None
        self.state.logs_b = None
        self.state.cached_file_a = None
        self.state.cached_file_b = None
        self.build()

    def handleDragEnter(self, event):
        if event.mimeData().hasUrls():
            if len(event.mimeData().urls()) == 1:
                fn = event.mimeData().urls()[0].toLocalFile()
                ext = os.path.splitext(fn)[1].lower()
                if ext in [SCH_SUFFIX, PCB_SUFFIX]:
                    event.accept()
                    return True
        event.ignore()
        return False

    def drop_file_a(self, event):
        if event.mimeData().hasUrls():
            if len(event.mimeData().urls()) == 1:
                fn = event.mimeData().urls()[0].toLocalFile()
                ext = os.path.splitext(fn)[1].lower()
                if ext in [SCH_SUFFIX, PCB_SUFFIX]:
                    self.state.source_a = os.path.abspath(fn)
                    self.state.file_a = self.state.source_a
                    self.change_file_a()
                    event.accept()
                    return True
        event.ignore()
        return False

    def drop_file_b(self, event):
        if event.mimeData().hasUrls():
            if len(event.mimeData().urls()) == 1:
                fn = event.mimeData().urls()[0].toLocalFile()
                ext = os.path.splitext(fn)[1].lower()
                if ext in [SCH_SUFFIX, PCB_SUFFIX]:
                    self.state.source_b = os.path.abspath(fn)
                    self.state.file_b = self.state.source_b
                    self.change_file_b()
                    event.accept()
                    return True
        event.ignore()
        return False

    def change_file_a(self):
        if self.state.use_workspace:
            self.state.source_a = self.state.file_a
        self._pending_revision_a = None
        self.state.logs_a = None
        self.state.cached_file_a = ""
        if not self.state.file_b:
            self.state.file_b = self.state.file_a
            self.state.source_b = self.state.source_a
            self.state.logs_b = None
            self.state.cached_file_b = ""
        self.build()

    def change_file_b(self):
        if self.state.use_workspace:
            self.state.source_b = self.state.file_b
        self._pending_revision_b = None
        self.state.logs_b = None
        self.state.cached_file_b = ""
        if not self.state.file_a:
            self.state.file_a = self.state.file_b
            self.state.source_a = self.state.source_b
            self.state.logs_a = None
            self.state.cached_file_a = ""
        self.build()

    def open_file_a(self, e):
        fn = OpenFile("Open File A", types="KiCad PCB (*.kicad_pcb)|*.kicad_pcb|KiCad SCH (*.kicad_sch)|*.kicad_sch")
        if fn:
            self.state.source_a = os.path.abspath(fn)
            self.state.file_a = self.state.source_a
            self.change_file_a()

    def open_file_b(self, e):
        fn = OpenFile("Open File B", types="KiCad PCB (*.kicad_pcb)|*.kicad_pcb|KiCad SCH (*.kicad_sch)|*.kicad_sch")
        if fn:
            self.state.source_b = os.path.abspath(fn)
            self.state.file_b = self.state.source_b
            self.change_file_b()

    def clear_file_a(self, _event):
        self._clear_file("a")

    def clear_file_b(self, _event):
        self._clear_file("b")

    def _clear_file(self, side):
        setattr(self, f"_pending_revision_{side}", None)
        setattr(self.state, f"source_{side}", "")
        setattr(self.state, f"file_{side}", "")
        setattr(self.state, f"logs_{side}", False)
        setattr(self.state, f"commit_{side}", "")
        setattr(self.state, f"cached_file_{side}", "")
        setattr(self.state, f"loading_{side}", False)
        setattr(self.state, f"page_{side}", 0)
        setattr(self, f"repo_{side}", None)
        self.state.diff_pair = None
        self.state.loading_diff = False
        self.state.pcb_page_size = None
        self.state.sch_page_size = None
        self.state.message = ""
        self.pcb_tiles.reset(None)
        self.sch_tiles.reset(None)

    def open_gerber_folder_a(self, _event):
        self._open_gerber("a", "folder")

    def open_gerber_folder_b(self, _event):
        self._open_gerber("b", "folder")

    def open_gerber_zip_a(self, _event):
        self._open_gerber("a", "zip")

    def open_gerber_zip_b(self, _event):
        self._open_gerber("b", "zip")

    def _open_gerber(self, side, source_type):
        if source_type == "folder":
            source = OpenDirectory("Open Gerber Folder")
            valid = is_gerber_dir
            description = "folder"
        else:
            source = OpenFile(
                "Open Gerber Zip", types="Gerber Zip (*.zip)|*.zip")
            valid = is_gerber_zip
            description = "zip"
        if not source:
            return
        if not valid(source):
            Critical(
                f"Invalid Gerber {description}: {source}",
                f"Invalid Gerber {description}",
            )
            return

        try:
            output, errors = prepare_differ_source(
                source, side, self.temp_dir)
        except Exception as exc:
            Critical(
                f"Could not convert Gerber {description}: {exc}",
                "Gerber conversion failed",
            )
            return

        if errors:
            Critical(
                "Gerber conversion completed with warnings:\n\n"
                + "\n".join(errors),
                "Gerber conversion warnings",
            )
        setattr(self.state, f"source_{side}", os.path.abspath(source))
        setattr(self.state, f"file_{side}", output)
        getattr(self, f"change_file_{side}")()

    def open_selected_file_a(self, _event):
        self._open_selected_file(
            self.state.file_a, self.repo_a, self.state.commit_a, "A"
        )

    def open_selected_file_b(self, _event):
        self._open_selected_file(
            self.state.file_b, self.repo_b, self.state.commit_b, "B"
        )

    def _open_selected_file(self, filepath, repo_root, revision, label):
        if not filepath:
            Critical(f"File {label} not selected", f"Open File {label}")
            return
        try:
            _, selected_path, display_path = source_paths(
                filepath, repo_root, revision, self.temp_dir
            )
        except ValueError as exc:
            Critical(f"Could not open File {label}: {exc}", f"Open File {label}")
            return
        if not os.path.isfile(selected_path):
            location = f"revision {str(revision)[:12]}" if revision else "working tree"
            Critical(
                f"File {label} not found in {location}: {display_path}",
                f"Open File {label}",
            )
            return
        Thread(
            target=self._open_selected_file_worker,
            args=(selected_path, label), daemon=True,
        ).start()

    def _open_selected_file_worker(self, filepath, label):
        try:
            open_kicad_file(filepath)
        except Exception as exc:
            self.state.message = f"Could not open File {label}: {exc}"

    def select_page_a(self, png):
        self.state.page_a = png
        if self.state.sync_page:
            matched = self._corresponding_sch_page(self.state.cached_file_b, png)
            if matched is not None:
                self.state.page_b = matched
        self.build()

    def select_page_b(self, png):
        self.state.page_b = png
        if self.state.sync_page:
            matched = self._corresponding_sch_page(self.state.cached_file_a, png)
            if matched is not None:
                self.state.page_a = matched
        self.build()

    @staticmethod
    def _corresponding_sch_page(cache_dir, selected_page):
        if not cache_dir:
            return None
        try:
            pages = sorted(os.listdir(os.path.join(cache_dir, "sch")))
        except OSError:
            return None
        return corresponding_schematic_page(pages, selected_page)

    def sync_sch_pages(self, _event):
        if self.state.sync_page and self.state.page_a:
            self.select_page_a(self.state.page_a)

    def shift_sch_pages(self, offset):
        if not self.state.cached_file_a or not self.state.cached_file_b:
            return
        try:
            pages_a = sorted(os.listdir(os.path.join(
                self.state.cached_file_a, "sch"
            )))
            pages_b = sorted(os.listdir(os.path.join(
                self.state.cached_file_b, "sch"
            )))
        except OSError:
            return
        if self.state.sync_page:
            shifted = matched_page_shift(
                pages_a, self.state.page_a, pages_b, offset
            )
        else:
            shifted = synchronized_page_shift(
                pages_a,
                self.state.page_a,
                pages_b,
                self.state.page_b,
                offset,
            )
        if shifted is None:
            return
        self.state.page_a, self.state.page_b = shifted
        self.build()

    def select_commit_a(self):
        self.build()

    def select_commit_b(self):
        self.build()

    def pcb_layer_variant(self):
        visible_layers = (
            layer for layer in self.state.layers
            if self.state.show_layers.get(layer, True)
        )
        return prioritize_selected_layer(
            visible_layers, self.state.selected_layer
        )

    def layer_visibility_changed(self, _event, layer):
        self.state.layer_preset = "Custom"
        if (self.state.selected_layer == layer and
                not self.state.show_layers.get(layer, True)):
            self.state.selected_layer = next(
                (
                    candidate for candidate in self.state.layers
                    if self.state.show_layers.get(candidate, True)
                ),
                None,
            )
        self.pcb_tiles.prime_coarse(self.pcb_layer_variant())

    def select_pcb_layer(self, _event, layer):
        if not self.state.show_layers.get(layer, True):
            self.state.layer_preset = "Custom"
        self.state.show_layers[layer] = True
        self.state.selected_layer = toggle_selected_layer(
            self.state.selected_layer, layer
        )
        self.pcb_tiles.prime_coarse(self.pcb_layer_variant())

    def apply_layer_preset(self, _event):
        preset = self.state.layer_preset
        if preset not in PCB_LAYER_PRESETS:
            return
        visible_layers = layers_for_preset(
            self.state.layers,
            preset,
        )
        visible = set(visible_layers)
        self.state.show_layers = {
            layer: layer in visible for layer in self.state.layers
        }
        preferred_layers = {
            "Front Assembly View": "F.Silkscreen",
            "Front Layers": "F.Cu",
            "Back Assembly View": "B.Silkscreen",
            "Back Layers": "B.Cu",
        }
        preferred = preferred_layers.get(preset)
        selected = next(
            (
                layer for layer in visible_layers
                if layer == preferred
            ),
            None,
        )
        if selected is None and self.state.selected_layer in visible:
            selected = self.state.selected_layer
        if selected is None:
            selected = next(iter(visible_layers), None)
        self.state.selected_layer = selected
        self.pcb_tiles.prime_coarse(self.pcb_layer_variant())

    def build(self):
        self.queue.put(1)

    def tile_ready(self):
        self.state.build_time = time.time()

    @staticmethod
    def tile_resources(pixel_width, pixel_height):
        image_resources = {}
        image_buffers = {}
        for name in ("a", "b", "darker"):
            resource, packed = premultiplied_image_resource(
                pixel_width, pixel_height
            )
            image_resources[name] = resource
            image_buffers[name] = packed.view(np.uint8).reshape(
                pixel_height, pixel_width, 4
            )
        mask_resource, mask_packed = premultiplied_image_resource(
            pixel_width, pixel_height
        )
        mask_buffer = mask_packed.view(np.uint8).reshape(
            pixel_height, pixel_width, 4
        )
        return image_resources, image_buffers, mask_resource, mask_buffer

    def render_pcb_tile(self, renderer, task):
        self.prepare_tile_renderer(renderer, task["generation"])
        _pixel_x, _pixel_y, pixel_width, pixel_height = tile_pixel_bounds(
            task["metadata"]["canvas_size"],
            task["render_scale"],
            task["tile_x"],
            task["tile_y"],
        )
        image_resources, image_buffers, mask_resource, mask_buffer = (
            self.tile_resources(pixel_width, pixel_height)
        )
        composite_buffers = {
            name: buffer.view(np.uint32).reshape(pixel_height, pixel_width)
            for name, buffer in image_buffers.items()
        }
        result = renderer.render_tile(
            task["metadata"],
            task["variant"],
            task["render_scale"],
            task["tile_x"],
            task["tile_y"],
            composite_buffers=composite_buffers,
            mask_buffer=mask_buffer,
            tolerance_um=PCB_DIFF_TOLERANCE_UM,
        )
        apply_overlap_color_shift(
            image_buffers["a"], image_buffers["b"], image_buffers["darker"],
            premultiplied=True,
        )
        return self.finish_tile_resources(
            result, image_resources, mask_resource
        )

    def render_sch_tile(self, renderer, task):
        self.prepare_tile_renderer(renderer, task["generation"])
        _pixel_x, _pixel_y, pixel_width, pixel_height = tile_pixel_bounds(
            task["metadata"]["canvas_size"],
            task["render_scale"],
            task["tile_x"],
            task["tile_y"],
        )
        image_resources, image_buffers, mask_resource, mask_buffer = (
            self.tile_resources(pixel_width, pixel_height)
        )
        result = renderer.render_tile(
            task["metadata"],
            task["render_scale"],
            task["tile_x"],
            task["tile_y"],
            image_buffers=image_buffers,
            mask_buffer=mask_buffer,
        )
        apply_overlap_color_shift(
            image_buffers["a"], image_buffers["b"], image_buffers["darker"],
            premultiplied=False,
        )
        return self.finish_tile_resources(
            result, image_resources, mask_resource
        )

    @staticmethod
    def prepare_tile_renderer(renderer, generation):
        if getattr(renderer, "_scheduler_generation", None) == generation:
            return
        renderer.close()
        renderer._scheduler_generation = generation

    @staticmethod
    def finish_tile_resources(result, image_resources, mask_resource):
        result["images"] = image_resources
        result["mask"] = mask_resource if result.pop("has_mask") else None
        resources = list(result["images"].values())
        if result["mask"] is not None:
            resources.append(result["mask"])
        result["memory_bytes"] = sum(
            resource.qimage.sizeInBytes() for resource in resources
        )
        return result

    def bg_looper(self):
        while True:
            self.queue.get()

            try:
                file_a = self.state.file_a
                file_b = self.state.file_b

                if file_a and self.state.logs_a is None:
                    pending_revision = self._pending_revision_a
                    self._pending_revision_a = None
                    self.repo_a = githelper.repo(file_a)
                    if self.repo_a:
                        history_path = (
                            file_a if file_a.lower().endswith(PCB_SUFFIX) else None
                        )
                        logs = [
                            (hex, msg)
                            for hex, msg in githelper.log(self.repo_a, history_path)
                        ]
                        self.state.commit_a = githelper.revision_at_or_before(
                            pending_revision, logs, githelper.log(self.repo_a)
                        )
                        self.state.logs_a = logs
                    else:
                        self.state.commit_a = ""
                        self.state.logs_a = False

                if file_b and self.state.logs_b is None:
                    pending_revision = self._pending_revision_b
                    self._pending_revision_b = None
                    self.repo_b = githelper.repo(file_b)
                    if self.repo_b:
                        history_path = (
                            file_b if file_b.lower().endswith(PCB_SUFFIX) else None
                        )
                        logs = [
                            (hex, msg)
                            for hex, msg in githelper.log(self.repo_b, history_path)
                        ]
                        self.state.commit_b = githelper.revision_at_or_before(
                            pending_revision, logs, githelper.log(self.repo_b)
                        )
                        self.state.logs_b = logs
                    else:
                        self.state.commit_b = ""
                        self.state.logs_b = False

                if self.state.logs_a and self.state.commit_a is None:
                    continue

                if self.state.logs_b and self.state.commit_b is None:
                    continue

                # A
                path_a, selected_file_a, display_file_a = source_paths(
                    file_a, self.repo_a, self.state.commit_a, self.temp_dir
                )
                if self.state.commit_a:
                    self.state.loading_a = f"Checking out {self.state.commit_a}..."
                    repo_workdir = os.path.join(path_a, "workdir")
                    if not os.path.isdir(repo_workdir):
                        githelper.checkout(self.repo_a, self.state.commit_a, repo_workdir)
                file_a = selected_file_a

                if not os.path.isfile(file_a):
                    revision = self.state.commit_a
                    location = (
                        f"revision {str(revision)[:12]}"
                        if revision else "working tree"
                    )
                    raise FileNotFoundError(
                        f"File A not found in {location}: {display_file_a}"
                    )

                ## Convert
                if self.state.cached_file_a != path_a:
                    if file_a.lower().endswith(SCH_SUFFIX):
                        for l in convert_sch(file_a, path_a):
                            self.state.loading_a = l
                        self.state.cached_file_a = path_a
                        self.state.page_a = sorted(os.listdir(
                            os.path.join(path_a, "sch")
                        ))[0]
                    if file_a.lower().endswith(PCB_SUFFIX):
                        for l in convert_pcb(file_a, path_a):
                            self.state.loading_a = l
                        self.state.cached_file_a = path_a

                # B
                path_b, selected_file_b, display_file_b = source_paths(
                    file_b, self.repo_b, self.state.commit_b, self.temp_dir
                )
                if self.state.commit_b:
                    self.state.loading_b = f"Checking out {self.state.commit_b}..."
                    repo_workdir = os.path.join(path_b, "workdir")
                    if not os.path.isdir(repo_workdir):
                        githelper.checkout(self.repo_b, self.state.commit_b, repo_workdir)
                file_b = selected_file_b

                if not os.path.isfile(file_b):
                    revision = self.state.commit_b
                    location = (
                        f"revision {str(revision)[:12]}"
                        if revision else "working tree"
                    )
                    raise FileNotFoundError(
                        f"File B not found in {location}: {display_file_b}"
                    )

                ## Convert
                if self.state.cached_file_b != path_b:
                    if file_b.lower().endswith(SCH_SUFFIX):
                        for l in convert_sch(file_b, path_b):
                            self.state.loading_b = l
                        self.state.cached_file_b = path_b
                        self.state.page_b = sorted(os.listdir(
                            os.path.join(path_b, "sch")
                        ))[0]
                    if file_b.lower().endswith(PCB_SUFFIX):
                        for l in convert_pcb(file_b, path_b):
                            self.state.loading_b = l
                        self.state.cached_file_b = path_b

                self.state.loading_a = False
                self.state.loading_b = False

                page_a = self.state.page_a
                page_b = self.state.page_b

                if os.path.splitext(file_a)[1].lower() == os.path.splitext(file_b)[1].lower():
                    if file_a.lower().endswith(SCH_SUFFIX):
                        if page_a and page_b:
                            diff_pair = (self.state.cached_file_a, self.state.cached_file_b, page_a, page_b)
                            if self.state.diff_pair != diff_pair:
                                self.state.loading_diff = True
                                metadata = build_schematic_pair_metadata(
                                    self.state.cached_file_a,
                                    self.state.cached_file_b,
                                    page_a,
                                    page_b,
                                )
                                self.state.sch_page_size = metadata[
                                    "canvas_size"
                                ]
                                self.state.pcb_page_size = None
                                self.pcb_tiles.reset(None)
                                self.sch_tiles.reset(metadata)
                                self.sch_tiles.prime_coarse()
                                self.state.diff_pair = diff_pair
                                self.state.loading_diff = False

                    elif file_a.lower().endswith(PCB_SUFFIX):
                        diff_pair = (self.state.cached_file_a, self.state.cached_file_b)
                        if self.state.diff_pair != diff_pair:
                            self.state.loading_diff = True
                            layer_names_a = get_pcb_layer_names(file_a)
                            layer_names_b = get_pcb_layer_names(file_b)
                            layers = sort_layers_in_kicad_ui_order(
                                dict.fromkeys((
                                    *layer_names_a, *layer_names_b
                                ))
                            )
                            metadata = build_pair_metadata(
                                self.state.cached_file_a,
                                self.state.cached_file_b,
                                layers,
                                layer_names_a=layer_names_a,
                                layer_names_b=layer_names_b,
                            )
                            if self.state.layers != layers:
                                self.state.show_layers = {
                                    layer: True for layer in layers
                                }
                                self.state.layer_preset = "All Layers"
                            if (self.state.selected_layer not in layers and
                                    (self.state.selected_layer is not None or
                                     not self.state.layers)):
                                self.state.selected_layer = (
                                    layers[0] if layers else None
                                )
                            self.state.layers = layers
                            self.state.layer_labels = {
                                layer: paired_layer_label(
                                    layer,
                                    layer_names_a.get(layer),
                                    layer_names_b.get(layer),
                                )
                                for layer in layers
                            }
                            self.state.pcb_page_size = metadata["canvas_size"]
                            self.state.sch_page_size = None
                            self.sch_tiles.reset(None)
                            self.pcb_tiles.reset(metadata)
                            self.pcb_tiles.prime_coarse(
                                self.pcb_layer_variant()
                            )
                            self.state.diff_pair = diff_pair
                            self.state.loading_diff = False

                if file_a == file_b and page_a == page_b and page_a and page_b:
                    self.state.message = "A === B"
                else:
                    self.state.message = ""

                self.state.build_time = time.time()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                self.state.loading_a = False
                self.state.loading_b = False
                self.state.loading_diff = False
                self.state.cached_file_a = ""
                self.state.cached_file_b = ""
                self.state.diff_pair = None
                self.state.page_a = 0
                self.state.page_b = 0
                self.state.pcb_page_size = None
                self.state.sch_page_size = None
                self.pcb_tiles.reset(None)
                self.sch_tiles.reset(None)
                self.state.message = str(exc) or type(exc).__name__
                self.state.build_time = time.time()
