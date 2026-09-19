import os
from PUI.PySide6 import *
import PUI
from common import *
import json
import platform
import subprocess
from threading import Thread
import hashlib
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
import time
from collections import OrderedDict
from PySide6 import QtCore, QtGui

from pcb_diff_tiles import (
    PcbTileRenderer,
    build_pair_metadata,
    choose_render_scale,
    clipped_tile_geometry,
    tile_pixel_bounds,
    visible_tile_indices,
)
from pdf_tile_scheduler import PdfTileScheduler
from sch_diff_tiles import (
    SchematicTileRenderer,
    build_schematic_pair_metadata,
    synchronized_page_shift,
)


PDF_TILE_IMAGE_LOAD_BUDGET_SECONDS = 0.004
PDF_TILE_CACHE_BYTES = 384 * 1024 * 1024
PDF_TILE_LOW_RES_CACHE_BYTES = 64 * 1024 * 1024
PDF_TILE_LOW_RES_MAX_SCALE = 1.0


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


def get_pcb_canonical_layers(path):
    board = pcbnew.LoadBoard(path)
    return {
        board.GetLayerName(layer): pcbnew.LayerName(layer)
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
    zoom_limit = 8

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

    def setup(self):
        self.state = State()
        self.state.scale = None
        self.state.splitter_x = 0.5
        self.state.overlap = 0.0005
        self.state.mousepos = None

    @property
    def scheduler(self):
        return getattr(self.main, self.scheduler_attribute)

    def page_size(self):
        return getattr(self.main.state, self.page_size_attribute)

    def tile_variant(self):
        return ()

    def autoScale(self, canvas_width, canvas_height):
        page_size = self.page_size()
        if not page_size:
            return False

        dw, dh = page_size
        self.diff_width, self.diff_height = dw, dh
        self.canvas_width, self.canvas_height = canvas_width, canvas_height

        if dw == 0 or dh == 0:
            return False

        cw = canvas_width
        ch = canvas_height
        sw = cw / dw
        sh = ch / dh
        scale = min(sw, sh) * 0.75
        self.scale = scale
        offx = (cw - (dw) * scale) / 2
        offy = (ch - (dh) * scale) / 2
        self.state.scale = (offx, offy, scale)
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
        self.state.overlap
        self.state.scale
        self.tile_variant()
        self.main.state.highlight_changes
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
            zoom_factor = 1.7  # Factor for smoother zooming
            noverlap = self.state.overlap * (zoom_factor ** (e.v_delta / 120))
            self.state.overlap = max(0.0005, min(0.1, noverlap))
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
        if self.generation != generation:
            self.generation = generation
            self.tile_images.clear()
            self.state.scale = None
            self.canvas_width = None
            self.canvas_height = None

        if self.state.scale is None or (self.canvas_width, self.canvas_height) != (canvas.width, canvas.height):
            return self.autoScale(canvas.width, canvas.height)

        immediate = False
        offx, offy, scale = self.state.scale
        render_scale = choose_render_scale(scale, canvas.pixel_density)
        variant = self.tile_variant()
        x_left = min(
            self.diff_width,
            max(0.0, self.diff_width * (
                self.state.splitter_x - self.state.overlap
            )),
        )
        x_right = max(
            0.0,
            min(self.diff_width, self.diff_width * (
                self.state.splitter_x + self.state.overlap
            )),
        )
        tile_indices = visible_tile_indices(
            (self.diff_width, self.diff_height),
            (canvas.width, canvas.height),
            self.state.scale,
            render_scale,
            priority_point=self.state.mousepos,
            priority_lines=(x_left, x_right),
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
            max(0.0, (0.0 - offx) / scale),
            max(0.0, (0.0 - offy) / scale),
            min(self.diff_width, (canvas.width - offx) / scale),
            min(self.diff_height, (canvas.height - offy) / scale),
        )
        fallback_results = self.scheduler.fallback(
            render_scale, variant, viewport_bounds
        )

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
                self.state.scale,
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
            if bounds[0] < x_left:
                draw_region(
                    load_image(image_paths["a"], allow_load),
                    bounds, 0.0, x_left, pixel_size, opacity=1.0,
                    exclude_region=exclude_region,
                )
            if bounds[0] + bounds[2] > x_right:
                draw_region(
                    load_image(image_paths["b"], allow_load),
                    bounds, x_right, self.diff_width, pixel_size,
                    opacity=1.0,
                    exclude_region=exclude_region,
                )
            if bounds[0] < x_right and bounds[0] + bounds[2] > x_left:
                draw_region(
                    load_image(image_paths["darker"], allow_load),
                    bounds, x_left, x_right, pixel_size, opacity=1.0,
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
            if bounds[0] < x_left:
                visible_images.append((image_paths["a"], 0.0, x_left))
            if bounds[0] + bounds[2] > x_right:
                visible_images.append((
                    image_paths["b"], x_right, self.diff_width
                ))
            if bounds[0] < x_right and bounds[0] + bounds[2] > x_left:
                visible_images.append((
                    image_paths["darker"], x_left, x_right
                ))
            for source, region_left, region_right in visible_images:
                if not image_is_loaded(source):
                    continue
                geometry = clipped_tile_geometry(
                    bounds,
                    pixel_size,
                    self.state.scale,
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
        return tuple(
            layer for layer in self.main.state.layers
            if self.main.state.show_layers.get(layer, True)
        )


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
        self.state.logs_a = None
        self.state.logs_b = None
        self.state.commit_a = ""
        self.state.commit_b = ""
        self.state.page_a = 0
        self.state.page_b = 0
        self.state.diff_pair = None
        self.state.layers = []
        self.state.highlight_changes = True
        self.state.build_time = 0
        self.state.use_workspace = False
        self.state.cached_file_a = ""
        self.state.cached_file_b = ""
        self.state.pcb_page_size = None
        self.state.sch_page_size = None
        self.state.message = ""
        self.repo_a = None
        self.repo_b = None

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
            self.state.file_a = os.path.abspath(argv[0])
            self.state.file_b = os.path.abspath(argv[1])
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
                            if self.state.file_a:
                                Label(self.state.file_a).layout(weight=1)
                            Button("Open").click(self.open_file_a)
                            if not self.state.file_a:
                                Spacer()
                        with HBox():
                            Label("File B")
                            if self.state.file_b:
                                Label(self.state.file_b).layout(weight=1)
                            Button("Open").click(self.open_file_b)
                            if not self.state.file_b:
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
                    elif self.state.file_a and self.state.file_a:
                            if os.path.splitext(self.state.file_a)[1].lower() == SCH_SUFFIX:
                                Button("PCB Diff").click(self.pcb_diff)
                                if os.path.splitext(
                                        self.state.file_b
                                )[1].lower() == SCH_SUFFIX:
                                    Button("◀").click(
                                        lambda e: self.shift_sch_pages(-1)
                                    )
                                    Button("▶").click(
                                        lambda e: self.shift_sch_pages(1)
                                    )
                            elif os.path.splitext(self.state.file_a)[1].lower() == PCB_SUFFIX:
                                Button("SCH Diff").click(self.sch_diff)
                            Label("Ctrl+Wheel to adjust overlap").layout(weight=1)
                            Label(self.state.message).layout(weight=1)
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
                            with VBox():
                                Label("Display Layers")
                                for layer in self.state.layers:
                                    Checkbox(layer, model=self.state.show_layers(layer))
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
        self.state.file_a = os.path.splitext(self.state.file_a)[0] + PCB_SUFFIX
        self.state.file_b = os.path.splitext(self.state.file_b)[0] + PCB_SUFFIX
        self.state.log_a = None
        self.state.log_b = None
        self.state.cached_file_a = None
        self.state.cached_file_b = None
        self.build()

    def sch_diff(self, e):
        self.state.file_a = os.path.splitext(self.state.file_a)[0] + SCH_SUFFIX
        self.state.file_b = os.path.splitext(self.state.file_b)[0] + SCH_SUFFIX
        self.state.log_a = None
        self.state.log_b = None
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
                    self.state.file_a = fn
                    self.state.log_a = None
                    self.build()
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
                    self.state.file_b = fn
                    self.state.log_b = None
                    self.build()
                    event.accept()
                    return True
        event.ignore()
        return False

    def change_file_a(self):
        self.state.logs_a = None
        self.state.cached_file_a = ""
        self.build()

    def change_file_b(self):
        self.state.logs_b = None
        self.state.cached_file_b = ""
        self.build()

    def open_file_a(self, e):
        fn = OpenFile("Open File A", types="KiCad PCB (*.kicad_pcb)|*.kicad_pcb|KiCad SCH (*.kicad_sch)|*.kicad_sch")
        if fn:
            self.state.file_a = fn
            self.change_file_a()

    def open_file_b(self, e):
        fn = OpenFile("Open File B", types="KiCad PCB (*.kicad_pcb)|*.kicad_pcb|KiCad SCH (*.kicad_sch)|*.kicad_sch")
        if fn:
            self.state.file_b = fn
            self.change_file_b()

    def select_page_a(self, png):
        self.state.page_a = png
        self.build()

    def select_page_b(self, png):
        self.state.page_b = png
        self.build()

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
                    self.repo_a = githelper.repo(file_a)
                    if self.repo_a:
                        self.state.commit_a = ""
                        self.state.logs_a = [(hex, msg) for hex,msg in githelper.log(self.repo_a)]
                    else:
                        self.state.logs_a = False

                if file_b and self.state.logs_b is None:
                    self.repo_b = githelper.repo(file_b)
                    if self.repo_b:
                        self.state.commit_b = ""
                        self.state.logs_b = [(hex, msg) for hex,msg in githelper.log(self.repo_b)]
                    else:
                        self.state.logs_b = False

                if self.state.logs_a and self.state.commit_a is None:
                    continue

                if self.state.logs_b and self.state.commit_b is None:
                    continue

                # A
                hex = hashlib.sha256(file_a.encode("utf-8")).hexdigest()

                ## Checkout
                if self.state.commit_a:
                    self.state.loading_a = f"Checking out {self.state.commit_a}..."
                    path_a = os.path.join(self.temp_dir, f"{hex}_{self.state.commit_a}")
                    repo_workdir = os.path.join(path_a, "workdir")
                    if not os.path.exists(path_a):
                        dir = os.path.dirname(file_a)
                        githelper.checkout(self.repo_a, self.state.commit_a, repo_workdir)
                    file_a = os.path.relpath(file_a, self.repo_a).replace("\\", "/")
                    file_a = os.path.join(repo_workdir, file_a)
                else:
                    path_a = os.path.join(self.temp_dir, hex)

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
                hex = hashlib.sha256(file_b.encode("utf-8")).hexdigest()

                ## Checkout
                if self.state.commit_b:
                    self.state.loading_b = f"Checking out {self.state.commit_b}..."
                    path_b = os.path.join(self.temp_dir, f"{hex}_{self.state.commit_b}")
                    repo_workdir = os.path.join(path_b, "workdir")
                    if not os.path.exists(path_b):
                        dir = os.path.dirname(file_b)
                        githelper.checkout(self.repo_b, self.state.commit_b, repo_workdir)
                    file_b = os.path.relpath(file_b, self.repo_b).replace("\\", "/")
                    file_b = os.path.join(repo_workdir, file_b)
                else:
                    path_b = os.path.join(self.temp_dir, hex)

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
                            layers_a = get_pcb_layers(file_a)
                            layers_b = get_pcb_layers(file_b)
                            layers = list(layers_a)
                            layers.extend(
                                layer for layer in layers_b
                                if layer not in layers
                            )
                            metadata = build_pair_metadata(
                                self.state.cached_file_a,
                                self.state.cached_file_b,
                                layers,
                                {
                                    **get_pcb_canonical_layers(file_a),
                                    **get_pcb_canonical_layers(file_b),
                                },
                            )
                            if self.state.layers != layers:
                                self.state.show_layers = {
                                    layer: True for layer in layers
                                }
                            self.state.layers = layers
                            self.state.pcb_page_size = metadata["canvas_size"]
                            self.state.sch_page_size = None
                            self.sch_tiles.reset(None)
                            self.pcb_tiles.reset(metadata)
                            self.pcb_tiles.prime_coarse(layers)
                            self.state.diff_pair = diff_pair
                            self.state.loading_diff = False

                if file_a == file_b and page_a == page_b and page_a and page_b:
                    self.state.message = "A === B"
                else:
                    self.state.message = ""

                self.state.build_time = time.time()
            except:
                import traceback
                traceback.print_exc()
