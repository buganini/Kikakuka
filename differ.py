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
import tempfile
import atexit
import shutil
import git

if platform.system() == "Darwin":
    kicad_cli = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
elif platform.system() == "Windows":
    kicad_cli = "C:/Program Files/KiCad/9.0/bin/kicad-cli.exe"
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


PCB_LAYERS = [
    "Edge.Cuts",
    # "F.Paste",
    "F.Silkscreen",
    # "F.Mask",
    "F.Cu",
    *[f"In{i+1}.Cu" for i in range(16)],
    "B.Cu",
    # "B.Mask",
    "B.Silkscreen",
    # "B.Paste"
]

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

    if not os.path.exists(os.path.join(outpath, "png")):
        yield f"Exporting PNG for {os.path.basename(path)}..."
        os.makedirs(os.path.join(outpath, "png"), exist_ok=True)
        pdf = pdfium.PdfDocument(pdfpath)
        for p, page in enumerate(pdf):
            opencv_image = page.render(
                scale=7,  # 72*x DPI is the default PDF resolution
                rotation=0
            ).to_numpy()
            opencv_image = cv2.cvtColor(opencv_image, cv2.COLOR_RGBA2RGB)
            cv2.imwrite(os.path.join(outpath, "png", f"sch_{p:02d}.png"), opencv_image)

def convert_pcb(path, outpath):
    os.makedirs(outpath, exist_ok=True)

    pdfpath = os.path.join(outpath, f"pcb_pdf")
    if not os.path.exists(pdfpath):
        yield f"Exporting PDF for {os.path.basename(path)}..."
        cmd = [kicad_cli, "pcb", "export", "pdf", "--mode-separate", "--layers", ",".join(PCB_LAYERS), "-o", pdfpath, path]
        kwargs = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.run(cmd, **kwargs)

    if os.path.isdir(pdfpath):
        os.makedirs(os.path.join(outpath, "png"), exist_ok=True)
        for layer in PCB_LAYERS:
            png_path = os.path.join(outpath, "png", f"{layer}.png")
            layerpdfpath = glob.glob(os.path.join(pdfpath, f"*{layer.replace('.', '_')}.pdf"))
            if layerpdfpath:
                yield f"Exporting {layer} to PNG for {os.path.basename(path)}..."
                pdf = pdfium.PdfDocument(layerpdfpath[0])
                opencv_image = pdf[0].render(
                    fill_color=(255, 255, 255, 0),
                    scale=7,  # 72*x DPI is the default PDF resolution
                    rotation=0
                ).to_numpy()
                cv2.imwrite(png_path, opencv_image)


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
        self.mousehold = False

    def setup(self):
        self.state = State()
        self.state.scale = None
        self.state.splitter_x = 0.5
        self.state.overlap = 0.05

    def autoScale(self, canvas_width, canvas_height):
        mask = os.path.join(self.main.temp_dir, "mask.png")
        if not os.path.exists(mask):
            return

        try:
            mask = cv2.imread(mask, cv2.IMREAD_UNCHANGED)  # IMREAD_UNCHANGED preserves alpha if present
            if mask is None:  # OpenCV returns None if image loading fails
                return

            # In OpenCV, shape is (height, width, channels) or (height, width) for grayscale
            # So we need to swap compared to PIL's size which is (width, height)
            dh, dw = mask.shape[:2]  # Get first two dimensions (height, width)
        except:
            return
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
        self.main.state.build_time

        (Canvas(self.painter).layout(weight=1)
         .style(bgColor=0xF5F4EE)
         .mousemove(self.mousemove)
         .mousedown(self.mousedown)
         .mouseup(self.mouseup)
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

    def painter(self, canvas):
        if self.state.scale is None:
            self.autoScale(canvas.width, canvas.height)
            return

        path = os.path.join(self.main.state.cached_file_a, "png", self.main.state.page_a)
        if self.path_a.set(path):
            self.image_a = canvas.loadImage(path)

        path = os.path.join(self.main.state.cached_file_b, "png", self.main.state.page_b)
        if self.path_b.set(path):
            self.image_b = canvas.loadImage(path)

        path = os.path.join(self.main.temp_dir, "darker.png")
        if self.darker_mtime.set(os.path.getmtime(path)):
            self.darker = canvas.loadImage(path)

        path = os.path.join(self.main.temp_dir, "mask.png")
        if self.mask_mtime.set(os.path.getmtime(path)):
            self.mask = canvas.loadImage(path)

        xL = min(self.diff_width, max(0, self.diff_width*(self.state.splitter_x - self.state.overlap)))
        xR = max(0, min(self.diff_width, self.diff_width*(self.state.splitter_x + self.state.overlap)))

        # A
        x1, y1 = 0, 0
        x2, y2 = xL, self.diff_height
        cx1, cy1 = self.toCanvas(x1, y1)
        cx2, cy2 = self.toCanvas(x2, y2)
        canvas.drawImage(self.image_a,
                         cx1, cy1, width=(cx2 - cx1 + 1), height=(cy2 - cy1 + 1),
                         src_x=x1, src_y=y1, src_width=(x2-x1 + 1), src_height=(y2-y1 + 1))

        # B
        x1, y1 = xR, 0
        x2, y2 = self.diff_width, self.diff_height
        cx1, cy1 = self.toCanvas(x1, y1)
        cx2, cy2 = self.toCanvas(x2, y2)
        canvas.drawImage(self.image_b,
                         cx1, cy1, width=(cx2 - cx1 + 1), height=(cy2 - cy1 + 1),
                         src_x=x1, src_y=y1, src_width=(x2-x1 + 1), src_height=(y2-y1 + 1))

        # Darker
        x1, y1 = xL, 0
        x2, y2 = xR, self.diff_height
        cx1, cy1 = self.toCanvas(x1, y1)
        cx2, cy2 = self.toCanvas(x2, y2)
        ox1, ox2 = cx1, cx2
        canvas.drawImage(self.darker,
                         cx1, cy1, width=(cx2 - cx1 + 1), height=(cy2 - cy1 + 1),
                         src_x=x1, src_y=y1, src_width=(x2-x1 + 1), src_height=(y2-y1 + 1))

        # Mask
        if self.main.state.highlight_changes:
            x1, y1 = 0, 0
            x2, y2 = self.diff_width, self.diff_height
            cx1, cy1 = self.toCanvas(x1, y1)
            cx2, cy2 = self.toCanvas(x2, y2)
            canvas.drawImage(self.mask, cx1, cy1, width=(cx2 - cx1 + 1), height=(cy2 - cy1 + 1),
                             src_x=x1, src_y=y1, src_width=(x2-x1 + 1), src_height=(y2-y1 + 1), opacity=0.08)

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
        self.mousehold = False

    def setup(self):
        self.state = State()
        self.state.scale = None
        self.state.splitter_x = 0.5
        self.state.overlap = 0.05

    def autoScale(self, canvas_width, canvas_height):
        mask = os.path.join(self.main.temp_dir, "mask.png")
        if not os.path.exists(mask):
            return

        try:
            mask = cv2.imread(mask, cv2.IMREAD_UNCHANGED)  # IMREAD_UNCHANGED preserves alpha if present
            if mask is None:  # OpenCV returns None if image loading fails
                return

            # In OpenCV, shape is (height, width, channels) or (height, width) for grayscale
            # So we need to swap compared to PIL's size which is (width, height)
            dh, dw = mask.shape[:2]  # Get first two dimensions (height, width)
        except:
            return
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
        self.main.state.build_time

        (Canvas(self.painter).layout(weight=1)
         .style(bgColor=0x001124)
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

    def painter(self, canvas):
        if self.state.scale is None:
            self.autoScale(canvas.width, canvas.height)
            return

        layers = self.main.state.layers

        changed = self.path_a.set(self.main.state.cached_file_a)
        if changed:
            self.image_a = {}
        for layer in layers:
            if not self.image_a.get(layer):
                try:
                    self.image_a[layer] = canvas.loadImage(os.path.join(self.main.state.cached_file_a, "png", f"{layer}.png"))
                except:
                    pass

        changed = self.path_b.set(self.main.state.cached_file_b)
        if changed:
            self.image_b = {}
        for layer in layers:
            if not self.image_b.get(layer):
                try:
                    self.image_b[layer] = canvas.loadImage(os.path.join(self.main.state.cached_file_b, "png", f"{layer}.png"))
                except:
                    pass


        mtime = [os.path.getmtime(fn) for fn in [os.path.join(self.main.temp_dir, "darker", f"{layer}.png") for layer in layers] if os.path.exists(fn)]
        if mtime and self.darker_mtime.set(max(mtime)):
            self.darker = {}
            for layer in layers:
                self.darker[layer] = canvas.loadImage(os.path.join(self.main.temp_dir, "darker", f"{layer}.png"))

        path = os.path.join(self.main.temp_dir, "mask.png")
        if self.mask_mtime.set(os.path.getmtime(path)):
            self.mask = canvas.loadImage(path)

        xL = max(0, self.diff_width*(self.state.splitter_x - self.state.overlap))
        xR = min(self.diff_width, self.diff_width*(self.state.splitter_x + self.state.overlap))

        # A
        x1, y1 = 0, 0
        x2, y2 = xL, self.diff_height
        cx1, cy1 = self.toCanvas(x1, y1)
        cx2, cy2 = self.toCanvas(x2, y2)
        if self.image_a:
            for layer in layers[::-1]:
                if not self.main.state.show_layers.get(layer, True):
                    continue
                canvas.drawImage(self.image_a[layer],
                                cx1, cy1, width=(cx2 - cx1 + 1), height=(cy2 - cy1 + 1),
                                src_x=x1, src_y=y1, src_width=(x2-x1 + 1), src_height=(y2-y1 + 1), opacity=0.8)

        # B
        x1, y1 = xR, 0
        x2, y2 = self.diff_width, self.diff_height
        cx1, cy1 = self.toCanvas(x1, y1)
        cx2, cy2 = self.toCanvas(x2, y2)
        if self.image_b:
            for layer in layers[::-1]:
                if not self.main.state.show_layers.get(layer, True):
                    continue
                canvas.drawImage(self.image_b[layer],
                                cx1, cy1, width=(cx2 - cx1 + 1), height=(cy2 - cy1 + 1),
                                src_x=x1, src_y=y1, src_width=(x2-x1 + 1), src_height=(y2-y1 + 1), opacity=0.8)

        # Darker
        x1, y1 = xL, 0
        x2, y2 = xR, self.diff_height
        cx1, cy1 = self.toCanvas(x1, y1)
        cx2, cy2 = self.toCanvas(x2, y2)
        ox1, ox2 = cx1, cx2
        for layer in layers[::-1]:
            darker = self.darker.get(layer)
            if not darker:
                continue
            if not self.main.state.show_layers.get(layer, True):
                continue
            canvas.drawImage(darker,
                                cx1, cy1, width=(cx2 - cx1 + 1), height=(cy2 - cy1 + 1),
                                src_x=x1, src_y=y1, src_width=(x2-x1 + 1), src_height=(y2-y1 + 1), opacity=0.8)

        # Mask
        if self.main.state.highlight_changes:
            x1, y1 = 0, 0
            x2, y2 = self.diff_width, self.diff_height
            cx1, cy1 = self.toCanvas(x1, y1)
            cx2, cy2 = self.toCanvas(x2, y2)
            canvas.drawImage(self.mask, cx1, cy1, width=(cx2 - cx1 + 1), height=(cy2 - cy1 + 1),
                                src_x=x1, src_y=y1, src_width=(x2-x1 + 1), src_height=(y2-y1 + 1), opacity=0.3)

        # Overlap cursor
        canvas.drawLine(ox1, 0, ox1, canvas.height, color=0x7e8792, width=1)
        canvas.drawLine(ox2, 0, ox2, canvas.height, color=0x7e8792, width=1)
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
        self.state.message = ""
        self.repo_a = None
        self.repo_b = None

        self.queue = queue.Queue()

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
        title = f"Kikakuka v{VERSION} Differ (KiCad CLI {kicad_cli_version}, Pypdfium2 {pdfium.V_PYPDFIUM2}, OpenCV {cv2.__version__}, PUI {PUI.__version__} {PUI_BACKEND})"
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
                                        if project["path"].lower().endswith(PNL_SUFFIX):
                                            continue
                                        for file in project["files"]:
                                            ComboBoxItem(os.path.basename(file["path"]), file["path"])

                            with HBox():
                                Label("File B")
                                with ComboBox(text_model=self.state("file_b")).layout(weight=1).change(lambda e: self.change_file_b()):
                                    for project in self.workspace["projects"]:
                                        if project["path"].lower().endswith(PNL_SUFFIX):
                                            continue
                                        for file in project["files"]:
                                            ComboBoxItem(os.path.basename(file["path"]), file["path"])
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
                                    ComboBoxItem(msg.split("\n")[0].rstrip()[:50], hex)
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
                                    ComboBoxItem(msg.split("\n")[0].rstrip()[:50], hex)
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
                            Label("Ctrl+Wheel to adjust overlap").layout(weight=1)
                            Label(self.state.message).layout(weight=1)
                            Checkbox("Highlight Changes", model=self.state("highlight_changes"))
                    else:
                        Label("Select two files to compare")

                if not (self.state.file_a and self.state.file_b):
                    Spacer()
                    return

                if os.path.splitext(self.state.file_a)[1].lower() != os.path.splitext(self.state.file_b)[1].lower():
                    Label("Files are different types")
                    Spacer()
                    return

                if os.path.splitext(self.state.file_a)[1].lower() == SCH_SUFFIX:
                    with HBox():
                        with Scroll().layout(width=250):
                            with VBox():
                                if self.state.cached_file_a:
                                    for i,png in enumerate(os.listdir(os.path.join(self.state.cached_file_a, "png"))):
                                        Image(os.path.join(self.state.cached_file_a, "png", png)).layout(width=240).click(lambda e, png: self.select_page_a(png), png)
                                        if png==self.state.page_a:
                                            Label(f"* Page {i+1} *")
                                        else:
                                            Label(f"Page {i+1}")
                                else:
                                    Label("Loading pages...")
                                Spacer()

                        if not self.state.page_a or not self.state.page_b:
                            with VBox():
                                with HBox():
                                    Label("Select pages to compare")
                                    Spacer()
                                Spacer()
                        else:
                            with VBox().layout(weight=1):
                                SchDiffView(self)

                        with Scroll().layout(width=250):
                            with VBox():
                                if self.state.cached_file_b:
                                    for i,png in enumerate(os.listdir(os.path.join(self.state.cached_file_b, "png"))):
                                        Image(os.path.join(self.state.cached_file_b, "png", png)).layout(width=240).click(lambda e, png: self.select_page_b(png), png)
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
            self.build()

    def open_file_b(self, e):
        fn = OpenFile("Open File B", types="KiCad PCB (*.kicad_pcb)|*.kicad_pcb|KiCad SCH (*.kicad_sch)|*.kicad_sch")
        if fn:
            self.state.file_b = fn
            self.state.logs_b = None
            self.state.cached_file_b = ""
            self.build()

    def select_page_a(self, png):
        self.state.page_a = png
        self.build()

    def select_page_b(self, png):
        self.state.page_b = png
        self.build()

    def select_commit_a(self):
        self.build()

    def select_commit_b(self):
        self.build()

    def build(self):
        self.queue.put(1)

    def pad_to_same_size(self, image_a, image_b):
        # Get dimensions - in OpenCV shape is (height, width, channels)
        height_a, width_a = image_a.shape[:2]
        height_b, width_b = image_b.shape[:2]

        target_width = max(width_a, width_b)
        target_height = max(height_a, height_b)

        # If images are already the same size, return them unchanged
        if width_a == width_b and height_a == height_b:
            return image_a, image_b

        # Check number of channels in each image
        channels_a = image_a.shape[2] if len(image_a.shape) > 2 else 1
        channels_b = image_b.shape[2] if len(image_b.shape) > 2 else 1

        # Handle alpha channel (equivalent to RGBA in PIL)
        has_alpha_a = channels_a == 4
        has_alpha_b = channels_b == 4

        # If one image has alpha and the other doesn't, convert both to have alpha
        if has_alpha_a or has_alpha_b:
            if not has_alpha_a:
                # Convert BGR to BGRA
                image_a = cv2.cvtColor(image_a, cv2.COLOR_BGR2BGRA)
            if not has_alpha_b:
                image_b = cv2.cvtColor(image_b, cv2.COLOR_BGR2BGRA)

            # Update channels after conversion
            channels_a = channels_b = 4

        # Create padded images with transparent background (255,255,255,0)
        if channels_a == 4:  # BGRA
            padded_a = np.zeros((target_height, target_width, 4), dtype=np.uint8)
            padded_a[:, :] = [255, 255, 255, 0]  # White transparent background
        elif channels_a == 3:  # BGR
            padded_a = np.ones((target_height, target_width, 3), dtype=np.uint8) * 255  # White background
        else:  # Grayscale
            padded_a = np.ones((target_height, target_width), dtype=np.uint8) * 255  # White background

        if channels_b == 4:
            padded_b = np.zeros((target_height, target_width, 4), dtype=np.uint8)
            padded_b[:, :] = [255, 255, 255, 0]
        elif channels_b == 3:
            padded_b = np.ones((target_height, target_width, 3), dtype=np.uint8) * 255
        else:
            padded_b = np.ones((target_height, target_width), dtype=np.uint8) * 255

        # Calculate center positions
        paste_x_a = (target_width - width_a) // 2
        paste_y_a = (target_height - height_a) // 2

        paste_x_b = (target_width - width_b) // 2
        paste_y_b = (target_height - height_b) // 2

        # Paste original images onto padded versions
        # In OpenCV, we use array slicing instead of paste
        padded_a[paste_y_a:paste_y_a+height_a, paste_x_a:paste_x_a+width_a] = image_a
        padded_b[paste_y_b:paste_y_b+height_b, paste_x_b:paste_x_b+width_b] = image_b

        return padded_a, padded_b

    def bg_looper(self):
        while True:
            self.queue.get()

            try:
                file_a = self.state.file_a
                file_b = self.state.file_b

                if file_a and self.state.logs_a is None:
                    self.repo_a = git.repo(file_a)
                    if self.repo_a:
                        self.state.commit_a = ""
                        self.state.logs_a = [(hex, msg) for hex,msg in git.log(self.repo_a, file_a)]
                    else:
                        self.state.logs_a = False

                if file_b and self.state.logs_b is None:
                    self.repo_b = git.repo(file_b)
                    if self.repo_b:
                        self.state.commit_b = ""
                        self.state.logs_b = [(hex, msg) for hex,msg in git.log(self.repo_b, file_b)]
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
                        git.checkout(self.repo_a, self.state.commit_a, repo_workdir)
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
                        self.state.page_a = os.listdir(os.path.join(path_a, "png"))[0]
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
                        git.checkout(self.repo_b, self.state.commit_b, repo_workdir)
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
                        self.state.page_b = os.listdir(os.path.join(path_b, "png"))[0]
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

                                # Load images
                                a = cv2.imread(os.path.join(self.state.cached_file_a, "png", page_a))
                                b = cv2.imread(os.path.join(self.state.cached_file_b, "png", page_b))

                                # Assuming self.pad_to_same_size exists, here's how it might look in OpenCV
                                # If you need this function translated too, let me know
                                a, b = self.pad_to_same_size(a, b)

                                # Create darker image (equivalent to ImageChops.darker)
                                darker = cv2.min(a, b)
                                cv2.imwrite(os.path.join(self.temp_dir, "darker.png"), darker)

                                # Create mask with the same sequence of operations
                                # 1. Get difference between images
                                diff = cv2.absdiff(a, b)
                                # 2. Convert to grayscale
                                if len(diff.shape) == 3:  # If color image
                                    diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
                                else:
                                    diff_gray = diff

                                # 3. Threshold to create binary mask (equivalent to point lambda)
                                _, binary_mask = cv2.threshold(diff_gray, 0, 255, cv2.THRESH_BINARY)

                                # 4. First Gaussian blur
                                blurred = cv2.GaussianBlur(binary_mask, (21, 21), 10)  # Kernel size 21x21, sigma=10

                                # 5. Second threshold
                                _, extended_mask = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY)

                                # 6. Final Gaussian blur
                                mask = cv2.GaussianBlur(extended_mask, (21, 21), 10)

                                # Save mask
                                cv2.imwrite(os.path.join(self.temp_dir, "mask.png"), mask)

                                self.state.diff_pair = diff_pair

                                self.state.loading_diff = False

                    elif file_a.lower().endswith(PCB_SUFFIX):
                        diff_pair = (self.state.cached_file_a, self.state.cached_file_b)
                        if self.state.diff_pair != diff_pair:
                            merged_mask = None

                            os.makedirs(os.path.join(self.temp_dir, "darker"), exist_ok=True)

                            layers = []
                            for layer in PCB_LAYERS:
                                png_a = os.path.join(self.state.cached_file_a, "png", f"{layer}.png")
                                png_b = os.path.join(self.state.cached_file_b, "png", f"{layer}.png")

                                if not os.path.exists(png_a) and not os.path.exists(png_b):
                                    continue

                                self.state.loading_diff = layer
                                layers.append(layer)
                                darker_png = os.path.join(self.temp_dir, "darker", f"{layer}.png")

                                if not os.path.exists(png_a):
                                    shutil.copy(png_b, darker_png)
                                    diff = cv2.imread(png_b, cv2.IMREAD_GRAYSCALE)
                                elif not os.path.exists(png_b):
                                    shutil.copy(png_a, darker_png)
                                    diff = cv2.imread(png_a, cv2.IMREAD_GRAYSCALE)
                                else:
                                    # Load images with alpha channel
                                    a = cv2.imread(png_a, cv2.IMREAD_UNCHANGED)
                                    b = cv2.imread(png_b, cv2.IMREAD_UNCHANGED)

                                    a, b = self.pad_to_same_size(a, b)

                                    # Ensure both images have 4 channels (BGRA)
                                    if len(a.shape) == 2:  # Grayscale
                                        a = cv2.cvtColor(a, cv2.COLOR_GRAY2BGRA)
                                    elif a.shape[2] == 3:  # BGR without alpha
                                        a = cv2.cvtColor(a, cv2.COLOR_BGR2BGRA)

                                    if len(b.shape) == 2:  # Grayscale
                                        b = cv2.cvtColor(b, cv2.COLOR_GRAY2BGRA)
                                    elif b.shape[2] == 3:  # BGR without alpha
                                        b = cv2.cvtColor(b, cv2.COLOR_BGR2BGRA)

                                    # Split channels
                                    b_a, g_a, r_a, alpha_a = cv2.split(a)
                                    b_b, g_b, r_b, alpha_b = cv2.split(b)

                                    # Apply darker operation to RGB channels and lighter to alpha
                                    darker_b = cv2.min(b_a, b_b)
                                    darker_g = cv2.min(g_a, g_b)
                                    darker_r = cv2.min(r_a, r_b)
                                    darker_alpha = cv2.max(alpha_a, alpha_b)  # Lighter for alpha

                                    # Merge channels
                                    darker = cv2.merge([darker_b, darker_g, darker_r, darker_alpha])
                                    cv2.imwrite(darker_png, darker)

                                    # Calculate difference
                                    diff = cv2.absdiff(a, b)
                                    if len(diff.shape) > 2:
                                        diff = cv2.cvtColor(diff, cv2.COLOR_BGRA2GRAY)

                                # Create mask from difference with gaussian blur operations
                                _, binary_mask = cv2.threshold(diff, 0, 255, cv2.THRESH_BINARY)
                                blurred = cv2.GaussianBlur(binary_mask, (21, 21), 10)
                                _, extended_mask = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY)
                                mask = cv2.GaussianBlur(extended_mask, (21, 21), 10)

                                # Merge masks using "lighter" (max) operation
                                if merged_mask is None:
                                    merged_mask = mask
                                else:
                                    merged_mask = cv2.max(merged_mask, mask)

                            # Update layer state
                            layers_changed = False
                            if self.state.layers != layers:
                                layers_changed = True
                            if layers_changed:
                                self.state.show_layers = {layer: True for layer in layers}
                            self.state.layers = layers

                            # Convert merged mask to RGBA
                            merged_mask_rgba = cv2.merge([merged_mask, merged_mask, merged_mask, merged_mask])
                            cv2.imwrite(os.path.join(self.temp_dir, "mask.png"), merged_mask_rgba)

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
