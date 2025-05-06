import os
from PUI.PySide6 import *
import PUI
from common import *
import json
import platform
import subprocess
from threading import Thread
import hashlib
import pypdfium2 as pdfium
from PIL import Image as PILImage, ImageChops

if platform.system() == "Darwin":
    kicad_cli = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
else:
    kicad_cli = "kicad-cli"

def convert_sch(path, outpath):
    os.makedirs(outpath, exist_ok=True)

    pdfpath = os.path.join(outpath, "sch.pdf")
    if not os.path.exists(pdfpath):
        cmd = [kicad_cli, "sch", "export", "pdf", "-o", pdfpath, path]
        subprocess.run(cmd)

    os.makedirs(os.path.join(outpath, "png"), exist_ok=True)
    if not os.path.exists(os.path.join(outpath, "png")):
        pdf = pdfium.PdfDocument(pdfpath)
        for p, page in enumerate(pdf):
            pil_image = page.render(
                scale=8,  # 72*x DPI is the default PDF resolution
                rotation=0
            ).to_pil()
            pil_image.save(os.path.join(outpath, "png", f"sch_{p:02d}.png"))


class DiffView(PUIView):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.splitter_x = 0.5
        self.overlap = 0.1

    def content(self):
        print("diffview update")

        # register update
        self.main.state.diff_pair

        Image("diff.png").layout(weight=1, width=240)
        # Canvas(self.painter).layout(weight=1)

    # def painter(self, canvas):
    #     canvas.drawImage(self.main.state.page_a, 0, 0)

class DifferUI(Application):
    def __init__(self, filepath):
        super().__init__(icon=resource_path("icon.ico"))
        self.state = State()
        self.state.loading = False
        self.state.loading_diff = False
        self.state.file_a = ""
        self.state.file_b = ""
        self.state.page_a = 0
        self.state.page_b = 0
        self.state.diff_pair = None

        self.cached_file_a = ""
        self.cached_file_b = ""
        self.cached_page_a = ""
        self.cached_page_b = ""

        with open(filepath, "r") as f:
            self.base_dir = os.path.dirname(os.path.abspath(filepath))
            self.workspace = json.load(f)
            for project in self.workspace["projects"]:
                if not os.path.isabs(project["path"]):
                    project["path"] = os.path.join(self.base_dir, project["path"])
        findFiles(self.workspace, self.base_dir, [SCH_SUFFIX, PCB_SUFFIX])

    def content(self):
        title = f"Kikakuka v{VERSION} Differ (PUI {PUI.__version__} {PUI_BACKEND})"
        with Window(size=(1300, 768), title=title, icon=resource_path("icon.ico")).keypress(self.keypress):
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
                            Label("Loading diff...")
                            Spacer()
                        else:
                            DiffView(self).layout(weight=1)

                        with Scroll().layout(width=250):
                            with VBox():
                                for png in os.listdir(os.path.join(self.cached_file_b, "png")):
                                    Image(os.path.join(self.cached_file_b, "png", png)).layout(width=240).click(self.select_b, png)
                                Spacer()

    def select_a(self, e, png):
        print("select_a", png)
        self.state.page_a = png
        self.build()

    def select_b(self, e, png):
        print("select_b", png)
        self.state.page_b = png
        self.build()

    def build(self):
        if not self.state.file_a or not self.state.file_b:
            return

        Thread(target=self._build, daemon=True).start()

    def _build(self):
        self.state.loading = True

        path_a = hashlib.sha256(self.state.file_a.encode("utf-8")).hexdigest()
        if self.cached_file_a != path_a:
            convert_sch(self.state.file_a, path_a)
            self.cached_file_a = path_a
            self.state.page_a = 0

        path_b = hashlib.sha256(self.state.file_b.encode("utf-8")).hexdigest()
        if self.cached_file_b != path_b:
            convert_sch(self.state.file_b, path_b)
            self.cached_file_b = path_b
            self.state.page_b = 0

        self.state.loading = False

        if not self.state.page_a or not self.state.page_b:
            self.state.loading = False
            return

        if self.cached_page_a != self.state.page_a or self.cached_page_b != self.state.page_b:
            a = PILImage.open(os.path.join(self.cached_file_a, "png", self.state.page_a))
            b = PILImage.open(os.path.join(self.cached_file_b, "png", self.state.page_b))
            self.state.diff_pair = (self.cached_file_a, self.cached_file_b, self.state.page_a, self.state.page_b)
            diff = ImageChops.difference(a, b)
            diff.save("diff.png")
            print("diff updated")

