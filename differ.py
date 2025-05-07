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
from PIL import Image as PILImage, ImageChops, ImageFilter
import tempfile
import atexit
import shutil

if platform.system() == "Darwin":
    kicad_cli = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
elif platform.system() == "Windows":
    kicad_cli = "C:/Program Files/KiCad/9.0/bin/kicad-cli.exe"
else:
    kicad_cli = "kicad-cli"

PCB_LAYERS = ["Edge.Cuts", "F.Cu", "F.Silkscreen", "F.Mask", "F.Paste", "B.Cu", "B.Silkscreen", "B.Mask", "B.Paste"]

def convert_sch(path, outpath):
    os.makedirs(outpath, exist_ok=True)

    pdfpath = os.path.join(outpath, "sch.pdf")
    if not os.path.exists(pdfpath):
        cmd = [kicad_cli, "sch", "export", "pdf", "-o", pdfpath, path]
        subprocess.run(cmd)

    if not os.path.exists(os.path.join(outpath, "png")):
        os.makedirs(os.path.join(outpath, "png"), exist_ok=True)
        pdf = pdfium.PdfDocument(pdfpath)
        for p, page in enumerate(pdf):
            pil_image = page.render(
                scale=8,  # 72*x DPI is the default PDF resolution
                rotation=0
            ).to_pil()
            pil_image.save(os.path.join(outpath, "png", f"sch_{p:02d}.png"))

def convert_pcb(path, outpath):
    os.makedirs(outpath, exist_ok=True)

    pdfpath = os.path.join(outpath, f"pcb.pdf")
    if not os.path.exists(pdfpath):
        cmd = [kicad_cli, "pcb", "export", "pdf", "--mode-multipage", "--layers", ",".join(PCB_LAYERS), "-o", pdfpath, path]
        subprocess.run(cmd)

    if os.path.isdir(pdfpath):
        pdfpath = glob.glob(os.path.join(pdfpath, f"*.pdf"))[0]

    for p, layer in enumerate(PCB_LAYERS):
        png_path = os.path.join(outpath, "png", f"{layer}.png")
        if not os.path.exists(png_path):
            os.makedirs(os.path.join(outpath, "png"), exist_ok=True)
            pdf = pdfium.PdfDocument(pdfpath)
            pil_image = pdf[p].render(
                fill_color=(255, 255, 255, 0),
                scale=8,  # 72*x DPI is the default PDF resolution
                rotation=0
            ).to_pil().convert("RGBA")

            pil_image.save(png_path)


class SchDiffView(PUIView):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.path_a = Prop()
        self.path_b = Prop()
        self.mask_mtime = Prop()
        self.darker_mtime = Prop()
        self.canvas_width = None
        self.canvas_height = None
        self.diff_width = None
        self.diff_height = None

    def setup(self):
        self.state = State()
        self.state.scale = None
        self.state.splitter_x = 0.5
        self.state.overlap = 0.05

    def autoScale(self, canvas_width, canvas_height):
        mask = PILImage.open(os.path.join(self.main.temp_dir, "mask.png"))
        dw, dh = mask.size
        self.diff_width, self.diff_height = dw, dh
        self.canvas_width, self.canvas_height = canvas_width, canvas_height

        if dw == 0 or dh == 0:
            return

        cw = canvas_width
        ch = canvas_height
        sw = cw / dw
        sh = ch / dh
        scale = min(sw, sh) * 0.75
        self.scale = scale
        offx = (cw - (dw) * scale) / 2
        offy = (ch - (dh) * scale) / 2
        self.state.scale = (offx, offy, scale)

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
        self.main.state.highlight_changes

        (Canvas(self.painter).layout(weight=1)
         .mousemove(self.mousemove)
         .wheel(self.wheel))

    def mousemove(self, e):
        if self.state.scale is None:
            return
        if self.canvas_width is None:
            return

        self.state.splitter_x = e.x / self.canvas_width

    def wheel(self, e):
        if e.modifiers & KeyModifier.CTRL:
            zoom_factor = 1.7  # Factor for smoother zooming
            noverlap = self.state.overlap * (zoom_factor ** (e.v_delta / 120))
            self.state.overlap = max(0.0001, min(0.1, noverlap))
            return

        if self.state.scale is None:
            return

        offx, offy, scale = self.state.scale
        zoom_factor = 1.2  # Factor for smoother zooming

        nscale = scale * (zoom_factor ** (e.v_delta / 120))

        # Limit the scale
        nscale = min(self.scale*4, max(self.scale/8, nscale))

        # Calculate new offsets
        offx = e.x - (e.x - offx) * nscale / scale
        offy = e.y - (e.y - offy) * nscale / scale

        self.state.scale = offx, offy, nscale
        self.state.overlap *= nscale/scale

    def painter(self, canvas):
        if self.state.scale is None:
            self.autoScale(canvas.width, canvas.height)
            return

        path = os.path.join(self.main.cached_file_a, "png", self.main.state.page_a)
        if self.path_a.set(path):
            self.image_a = canvas.loadImage(path)

        path = os.path.join(self.main.cached_file_b, "png", self.main.state.page_b)
        if self.path_b.set(path):
            self.image_b = canvas.loadImage(path)

        path = os.path.join(self.main.temp_dir, "darker.png")
        if self.darker_mtime.set(os.path.getmtime(path)):
            self.darker = canvas.loadImage(path)

        path = os.path.join(self.main.temp_dir, "mask.png")
        if self.mask_mtime.set(os.path.getmtime(path)):
            self.mask = canvas.loadImage(path)

        # A
        x = max(0, canvas.width*(self.state.splitter_x - self.state.overlap))
        x1, y1 = self.fromCanvas(0, 0)
        x2, y2 = self.fromCanvas(x, canvas.height)
        canvas.drawImage(self.image_a,
                         0, 0, width=x, height=canvas.height,
                         src_x=x1, src_y=y1, src_width=(x2-x1), src_height=(y2-y1))

        # B
        x = min(canvas.width, canvas.width*(self.state.splitter_x + self.state.overlap))
        x1, y1 = self.fromCanvas(x, 0)
        x2, y2 = self.fromCanvas(canvas.width, canvas.height)
        canvas.drawImage(self.image_b,
                         x, 0, width=canvas.width-x, height=canvas.height,
                         src_x=x1, src_y=y1, src_width=(x2-x1), src_height=(y2-y1))

        # Darker
        ox1 = max(0, canvas.width*(self.state.splitter_x - self.state.overlap))
        ox2 = min(canvas.width, canvas.width*(self.state.splitter_x + self.state.overlap))
        x1, y1 = self.fromCanvas(ox1, 0)
        x2, y2 = self.fromCanvas(ox2, canvas.height)
        canvas.drawImage(self.darker,
                         ox1, 0, width=ox2-ox1, height=canvas.height,
                         src_x=x1, src_y=y1, src_width=(x2-x1), src_height=(y2-y1))

        # Mask
        if self.main.state.highlight_changes:
            x1, y1 = self.fromCanvas(0, 0)
            x2, y2 = self.fromCanvas(canvas.width, canvas.height)
            canvas.drawImage(self.mask, 0, 0, width=canvas.width, height=canvas.height, src_x=x1, src_y=y1, src_width=(x2-x1), src_height=(y2-y1), opacity=0.08)

        # Overlap cursor
        canvas.drawLine(ox1, 0, ox1, canvas.height, color=0, width=1)
        canvas.drawLine(ox2, 0, ox2, canvas.height, color=0, width=1)

class PcbDiffView(PUIView):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.path_a = Prop()
        self.path_b = Prop()
        self.mask_mtime = Prop()
        self.darker_mtime = Prop()
        self.canvas_width = None
        self.canvas_height = None
        self.diff_width = None
        self.diff_height = None
        self.image_a = None
        self.image_b = None
        self.darker = None
        self.mask = None

    def setup(self):
        self.state = State()
        self.state.scale = None
        self.state.splitter_x = 0.5
        self.state.overlap = 0.05

    def autoScale(self, canvas_width, canvas_height):
        mask = PILImage.open(os.path.join(self.main.temp_dir, "mask.png"))
        dw, dh = mask.size
        self.diff_width, self.diff_height = dw, dh
        self.canvas_width, self.canvas_height = canvas_width, canvas_height

        if dw == 0 or dh == 0:
            return

        cw = canvas_width
        ch = canvas_height
        sw = cw / dw
        sh = ch / dh
        scale = min(sw, sh) * 0.75
        self.scale = scale
        offx = (cw - (dw) * scale) / 2
        offy = (ch - (dh) * scale) / 2
        self.state.scale = (offx, offy, scale)

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
        self.main.state.show_layers
        self.main.state.highlight_changes
        (Canvas(self.painter).layout(weight=1)
         .style(bgColor=0xFFFFFF)
         .mousemove(self.mousemove)
         .wheel(self.wheel))

    def mousemove(self, e):
        if self.state.scale is None:
            return
        if self.canvas_width is None:
            return

        x, _ = self.fromCanvas(e.x, 0)
        self.state.splitter_x = x / self.diff_width

    def wheel(self, e):
        if e.modifiers & KeyModifier.CTRL:
            zoom_factor = 1.7  # Factor for smoother zooming
            noverlap = self.state.overlap * (zoom_factor ** (e.v_delta / 120))
            self.state.overlap = max(0.0001, min(0.1, noverlap))
            return

        if self.state.scale is None:
            return

        offx, offy, scale = self.state.scale
        zoom_factor = 1.2  # Factor for smoother zooming

        nscale = scale * (zoom_factor ** (e.v_delta / 120))

        # Limit the scale
        nscale = min(self.scale*8, max(self.scale/8, nscale))

        # Calculate new offsets
        offx = e.x - (e.x - offx) * nscale / scale
        offy = e.y - (e.y - offy) * nscale / scale

        self.state.scale = offx, offy, nscale
        self.state.overlap *= nscale/scale

    def painter(self, canvas):
        if self.state.scale is None:
            self.autoScale(canvas.width, canvas.height)
            return

        if self.path_a.set(self.main.cached_file_a):
            self.image_a = {}
            for layer in PCB_LAYERS:
                self.image_a[layer] = canvas.loadImage(os.path.join(self.main.cached_file_a, "png", f"{layer}.png"))

        if self.path_b.set(self.main.cached_file_b):
            self.image_b = {}
            for layer in PCB_LAYERS:
                self.image_b[layer] = canvas.loadImage(os.path.join(self.main.cached_file_b, "png", f"{layer}.png"))


        mtime = [os.path.getmtime(fn) for fn in [os.path.join(self.main.temp_dir, "darker", f"{layer}.png") for layer in PCB_LAYERS] if os.path.exists(fn)]
        if mtime and self.darker_mtime.set(max(mtime)):
            self.darker = {}
            for layer in PCB_LAYERS:
                self.darker[layer] = canvas.loadImage(os.path.join(self.main.temp_dir, "darker", f"{layer}.png"))

        path = os.path.join(self.main.temp_dir, "mask.png")
        if self.mask_mtime.set(os.path.getmtime(path)):
            self.mask = canvas.loadImage(path)

        # A
        x1, x2 = 0, max(0, self.diff_width*(self.state.splitter_x - self.state.overlap))
        y1, y2 = 0, self.diff_height
        cx1, cy1 = self.toCanvas(x1, y1)
        cx2, cy2 = self.toCanvas(x2, y2)
        if self.image_a:
            for layer in PCB_LAYERS[::-1]:
                if not self.main.state.show_layers.get(layer, True):
                    continue
                canvas.drawImage(self.image_a[layer],
                                cx1, cy1, width=(cx2 - cx1), height=(cy2 - cy1),
                                src_x=x1, src_y=y1, src_width=(x2-x1), src_height=(y2-y1), opacity=0.8)

        # B
        x1, y1 = min(self.diff_width, self.diff_width*(self.state.splitter_x + self.state.overlap)), 0
        x2, y2 = self.diff_width, self.diff_height
        cx1, cy1 = self.toCanvas(x1, y1)
        cx2, cy2 = self.toCanvas(x2, y2)
        if self.image_b:
            for layer in PCB_LAYERS[::-1]:
                if not self.main.state.show_layers.get(layer, True):
                    continue
                canvas.drawImage(self.image_b[layer],
                                cx1, cy1, width=(cx2 - cx1), height=(cy2 - cy1),
                                src_x=x1, src_y=y1, src_width=(x2-x1), src_height=(y2-y1), opacity=0.8)

        # Darker
        x1, y1 = max(0, self.diff_width*(self.state.splitter_x - self.state.overlap)), 0
        x2, y2 = min(self.diff_width, self.diff_width*(self.state.splitter_x + self.state.overlap)), self.diff_height
        cx1, cy1 = self.toCanvas(x1, y1)
        cx2, cy2 = self.toCanvas(x2, y2)
        ox1, ox2 = cx1, cx2
        for layer in PCB_LAYERS[::-1]:
            darker = self.darker.get(layer)
            if not darker:
                continue
            if not self.main.state.show_layers.get(layer, True):
                continue
            canvas.drawImage(darker,
                                cx1, cy1, width=(cx2 - cx1), height=(cy2 - cy1),
                                src_x=x1, src_y=y1, src_width=(x2-x1), src_height=(y2-y1), opacity=0.8)

        # Mask
        if self.main.state.highlight_changes:
            x1, y1 = self.fromCanvas(0, 0)
            x2, y2 = self.fromCanvas(canvas.width, canvas.height)
            canvas.drawImage(self.mask, 0, 0, width=canvas.width, height=canvas.height, src_x=x1, src_y=y1, src_width=(x2-x1), src_height=(y2-y1), opacity=0.08)

        # Overlap cursor
        canvas.drawLine(ox1, 0, ox1, canvas.height, color=0, width=1)
        canvas.drawLine(ox2, 0, ox2, canvas.height, color=0, width=1)
class DifferUI(Application):
    def __init__(self, filepath):
        super().__init__(icon=resource_path("icon.ico"))

        self.temp_dir = tempfile.mkdtemp(prefix="kikakuka_differ_")
        atexit.register(self.cleanup)

        self.state = State()
        self.state.show_layers = {l: True for l in PCB_LAYERS[1:]}
        self.state.loading = False
        self.state.loading_diff = False
        self.state.file_a = ""
        self.state.file_b = ""
        self.state.page_a = 0
        self.state.page_b = 0
        self.state.diff_pair = None
        self.state.highlight_changes = True

        self.cached_file_a = ""
        self.cached_file_b = ""

        self.queue = queue.Queue()

        Thread(target=self.bg_looper, daemon=True).start()

        with open(filepath, "r") as f:
            self.base_dir = os.path.dirname(os.path.abspath(filepath))
            self.workspace = json.load(f)
            for project in self.workspace["projects"]:
                if not os.path.isabs(project["path"]):
                    project["path"] = os.path.join(self.base_dir, project["path"])
        findFiles(self.workspace, self.base_dir, [SCH_SUFFIX, PCB_SUFFIX])

    def cleanup(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def content(self):
        title = f"Kikakuka v{VERSION} Differ (PUI {PUI.__version__} {PUI_BACKEND})"
        with Window(maximize=True, title=title, icon=resource_path("icon.ico")):
            with VBox():
                if not os.path.exists(kicad_cli):
                    Label("KiCad CLI not found")
                    Spacer()
                    return
                with HBox():
                    with ComboBox(text_model=self.state("file_a")).change(lambda e: self.build()):
                        ComboBoxItem("")
                        for project in self.workspace["projects"]:
                            if project["path"].lower().endswith(PNL_SUFFIX):
                                continue
                            for file in project["files"]:
                                ComboBoxItem(os.path.basename(file["path"]), file["path"])

                    with ComboBox(text_model=self.state("file_b")).change(lambda e: self.build()):
                        ComboBoxItem("")
                        for project in self.workspace["projects"]:
                            if project["path"].lower().endswith(PNL_SUFFIX):
                                continue
                            for file in project["files"]:
                                ComboBoxItem(os.path.basename(file["path"]), file["path"])

                if not (self.state.file_a and self.state.file_b):
                    Label("Select two files to compare")
                    Spacer()
                    return

                if os.path.splitext(self.state.file_a)[1].lower() != os.path.splitext(self.state.file_b)[1].lower():
                    Label("Files are different types")
                    Spacer()
                    return

                if self.state.loading:
                    Label("Loading...")
                    Spacer()
                    return

                if os.path.splitext(self.state.file_a)[1].lower() == SCH_SUFFIX:
                    with HBox():
                        with Scroll().layout(width=250):
                            with VBox():
                                for png in os.listdir(os.path.join(self.cached_file_a, "png")):
                                    Image(os.path.join(self.cached_file_a, "png", png)).layout(width=240).click(self.select_a, png)
                                Spacer()

                        if self.state.loading_diff:
                            with VBox():
                                with HBox():
                                    Label("Loading diff...")
                                    Spacer()
                                Spacer()
                        elif not self.state.page_a or not self.state.page_b:
                            with VBox():
                                with HBox():
                                    Label("Select pages to compare")
                                    Spacer()
                                Spacer()
                        else:
                            with VBox().layout(weight=1).id("sch-diff-view"): # set id to workaround PUI bug (doesn't update weight)
                                with HBox():
                                    Label("Ctrl+Wheel to adjust overlap")
                                    Checkbox("Highlight Changes", model=self.state("highlight_changes"))
                                SchDiffView(self)

                        with Scroll().layout(width=250):
                            with VBox():
                                for png in os.listdir(os.path.join(self.cached_file_b, "png")):
                                    Image(os.path.join(self.cached_file_b, "png", png)).layout(width=240).click(self.select_b, png)
                                Spacer()
                elif os.path.splitext(self.state.file_a)[1].lower() == PCB_SUFFIX:
                    if self.state.loading_diff:
                        with VBox():
                            with HBox():
                                Label(f"Loading diff for {self.state.loading_diff}...")
                                Spacer()
                            Spacer()
                    else:
                        with HBox():
                            with VBox().layout(weight=1).id("pcb-diff-view"): # set id to workaround PUI bug (doesn't update weight)
                                with HBox():
                                    Label("Ctrl+Wheel to adjust overlap")
                                    Checkbox("Highlight Changes", model=self.state("highlight_changes"))
                                PcbDiffView(self)
                            with VBox():
                                Label("Display Layers")
                                for layer in PCB_LAYERS[1:]:
                                    Checkbox(layer, model=self.state.show_layers(layer))
                                Spacer()

    def select_a(self, e, png):
        self.state.page_a = png
        self.build()

    def select_b(self, e, png):
        self.state.page_b = png
        self.build()

    def build(self):
        if not self.state.file_a or not self.state.file_b:
            return
        self.queue.put(1)

    def pad_to_same_size(self, image_a, image_b):
        width_a, height_a = image_a.size
        width_b, height_b = image_b.size
        target_width = max(width_a, width_b)
        target_height = max(height_a, height_b)

        if width_a == width_b and height_a == height_b:
            return image_a, image_b
        # Create new images with padding
        # Ensure we maintain the original image mode if possible
        mode_a = image_a.mode
        mode_b = image_b.mode

        # If one image has alpha and the other doesn't, convert both to RGBA
        if 'A' in mode_a or 'A' in mode_b:
            if 'A' not in mode_a:
                image_a = image_a.convert('RGBA')
            if 'A' not in mode_b:
                image_b = image_b.convert('RGBA')
            mode_a = mode_b = 'RGBA'

        # Create new blank images with the target size
        padded_a = Image.new(mode_a, (target_width, target_height), (255, 255, 255, 0))
        padded_b = Image.new(mode_b, (target_width, target_height), (255, 255, 255, 0))

        # Calculate where to paste original images (center them)
        paste_x_a = (target_width - width_a) // 2
        paste_y_a = (target_height - height_a) // 2

        paste_x_b = (target_width - width_b) // 2
        paste_y_b = (target_height - height_b) // 2

        # Paste original images onto padded versions
        if 'A' in mode_a:
            # If the image has an alpha channel, use it as mask
            padded_a.paste(image_a, (paste_x_a, paste_y_a), image_a)
        else:
            padded_a.paste(image_a, (paste_x_a, paste_y_a))

        if 'A' in mode_b:
            padded_b.paste(image_b, (paste_x_b, paste_y_b), image_b)
        else:
            padded_b.paste(image_b, (paste_x_b, paste_y_b))

        return padded_a, padded_b

    def bg_looper(self):
        while True:
            self.queue.get()

            try:
                self.state.loading = True

                path_a = os.path.join(self.temp_dir, hashlib.sha256(self.state.file_a.encode("utf-8")).hexdigest())
                if self.cached_file_a != path_a:
                    if self.state.file_a.lower().endswith(SCH_SUFFIX):
                        convert_sch(self.state.file_a, path_a)
                        self.cached_file_a = path_a
                        self.state.page_a = 0
                    if self.state.file_a.lower().endswith(PCB_SUFFIX):
                        convert_pcb(self.state.file_a, path_a)
                        self.cached_file_a = path_a
                        self.state.page_a = 0

                path_b = os.path.join(self.temp_dir, hashlib.sha256(self.state.file_b.encode("utf-8")).hexdigest())
                if self.cached_file_b != path_b:
                    if self.state.file_b.lower().endswith(SCH_SUFFIX):
                        convert_sch(self.state.file_b, path_b)
                        self.cached_file_b = path_b
                        self.state.page_b = 0
                    if self.state.file_b.lower().endswith(PCB_SUFFIX):
                        convert_pcb(self.state.file_b, path_b)
                        self.cached_file_b = path_b
                        self.state.page_b = 0

                self.state.loading = False


                if os.path.splitext(self.state.file_a)[1].lower() == os.path.splitext(self.state.file_b)[1].lower():
                    if self.state.file_a.lower().endswith(SCH_SUFFIX):
                        if self.state.page_a and self.state.page_b:
                            diff_pair = (self.cached_file_a, self.cached_file_b, self.state.page_a, self.state.page_b)
                            if self.state.diff_pair != diff_pair:
                                self.state.loading_diff = True

                                a = PILImage.open(os.path.join(self.cached_file_a, "png", self.state.page_a))
                                b = PILImage.open(os.path.join(self.cached_file_b, "png", self.state.page_b))

                                a, b = self.pad_to_same_size(a, b)

                                darker = ImageChops.darker(a, b)
                                darker.save(os.path.join(self.temp_dir, "darker.png"))

                                mask = (ImageChops.difference(a, b).convert("L").point(lambda x: 255 if x else 0) # diff mask
                                        .filter(ImageFilter.GaussianBlur(radius=10)).point(lambda x: 255 if x else 0) # extend mask
                                        .filter(ImageFilter.GaussianBlur(radius=10))) # blur
                                mask.save(os.path.join(self.temp_dir, "mask.png"))
                                self.state.diff_pair = diff_pair

                                self.state.loading_diff = False

                    elif self.state.file_a.lower().endswith(PCB_SUFFIX):
                        diff_pair = (self.cached_file_a, self.cached_file_b)
                        if self.state.diff_pair != diff_pair:
                            merged_mask = None

                            os.makedirs(os.path.join(self.temp_dir, "darker"), exist_ok=True)

                            for layer in PCB_LAYERS:
                                self.state.loading_diff = layer
                                a = PILImage.open(os.path.join(self.cached_file_a, "png", f"{layer}.png"))
                                b = PILImage.open(os.path.join(self.cached_file_b, "png", f"{layer}.png"))

                                a, b = self.pad_to_same_size(a, b)

                                aa = a.split()
                                bb = b.split()
                                darker = []
                                for i in range(3):
                                    darker.append(ImageChops.darker(aa[i], bb[i]))
                                darker.append(ImageChops.lighter(aa[3], bb[3]))
                                darker = PILImage.merge("RGBA", darker)
                                darker.save(os.path.join(self.temp_dir, "darker", f"{layer}.png"))

                                mask = (ImageChops.difference(a, b).convert("L").point(lambda x: 255 if x else 0) # diff mask
                                        .filter(ImageFilter.GaussianBlur(radius=10)).point(lambda x: 255 if x else 0) # extend mask
                                        .filter(ImageFilter.GaussianBlur(radius=10))) # blur

                                if merged_mask is None:
                                    merged_mask = mask
                                else:
                                    merged_mask = ImageChops.lighter(merged_mask, mask)

                            merged_mask.save(os.path.join(self.temp_dir, "mask.png"))

                            self.state.diff_pair = diff_pair

                            self.state.loading_diff = False
            except:
                pass