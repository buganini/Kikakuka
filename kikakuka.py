import sys
import os

if getattr(sys, 'frozen', False):
    import kikit.common
    kikit.common.KIKIT_LIB = os.path.join(sys._MEIPASS, "kikit.pretty")

import pcbnew
import kikit
from kikit import panelize, substrate
from kikit.defs import Layer
from kikit.units import mm, mil
from kikit.common import *
from kikit.substrate import NoIntersectionError, TabFilletError, closestIntersectionPoint, biteBoundary
import numpy as np
import shapely
from shapely.geometry import Point, Polygon, MultiPolygon, LineString, GeometryCollection, box
from shapely import transform, distance, affinity
import pcbnew
import math
from enum import Enum
import traceback
import json
import itertools
from shootly import *
from PUI.PySide6 import *
# from PUI.wx import *
import PUI
import wx

VERSION = "3.5"

MIN_SPACING = 0.1
VC_EXTENT = 3
PNL_SUFFIX = ".kikit_pnl"
PCB_SUFFIX = ".kicad_pcb"

class Tool(Enum):
    END = -1
    NONE = 0
    TAB = 1
    HOLE = 2


class Direction(Enum):
    Up = 0
    Down = 1
    Left = 2
    Right = 3

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath("resources")

    return os.path.join(base_path, relative_path)

def extrapolate(x1, y1, x2, y2, r, d):
    dx = x2 - x1
    dy = y2 - y1
    n = math.sqrt(dx*dx + dy*dy)
    if n == 0:
        return x1, y1
    l = n*r + d
    return x1 + dx*l/n, y1 + dy*l/n

class PCB(StateObject):
    def __init__(self, boardfile):
        super().__init__()
        boardfile = os.path.realpath(boardfile)
        self.file = boardfile
        board = pcbnew.LoadBoard(boardfile)

        panel = panelize.Panel("")
        panel.appendBoard(
            boardfile,
            pcbnew.VECTOR2I(0, 0),
            origin=panelize.Origin.TopLeft,
            tolerance=panelize.fromMm(1),
            rotationAngle=pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T),
            inheritDrc=False
        )
        s = panel.substrates[0]
        bbox = s.bounds()

        if isinstance(s.substrates, MultiPolygon):
            self._shapes = s.substrates.geoms
        elif isinstance(s.substrates, Polygon):
            self._shapes = [s.substrates]
        else:
            self._shapes = []

        folder = os.path.basename(os.path.dirname(boardfile))
        name = os.path.splitext(os.path.basename(boardfile))[0]
        if folder != name:
            name = os.path.join(folder, name)
        self.ident = name

        self.disable_auto_tab = False

        self.off_x = 0
        self.off_y = 0

        self.x = 0
        self.y = 0
        self.width = bbox[2] - bbox[0]
        self.height = bbox[3] - bbox[1]
        self.rotate = 0
        self._tabs = []

    @property
    def shapes(self):
        """
        Return shapes in global coordinate system
        """
        ret = []
        for shape in self._shapes:
            shape = affinity.rotate(shape, self.rotate*-1, origin=(0,0))
            shape = transform(shape, lambda x: x+[self.x+self.off_x, self.y+self.off_y])
            ret.append(shape)
        return ret

    def tabs(self):
        """
        Return tab anchors in global coordinate system
        """
        ret = []
        for p in self._tabs:
            p = affinity.rotate(Point(*p), self.rotate*-1, origin=(0,0))
            p = transform(p, lambda x: x+[self.x+self.off_x, self.y+self.off_y])
            shortest = None
            for shape in self.shapes:
                s = shapely.shortest_line(p, shape.exterior)
                if shortest is None or s.length < shortest.length:
                    shortest = s

            if shortest:
                t0 = shortest.coords[0]
                t1 = shortest.coords[1]
                ret.append((*t0, *t1))
        return ret

    def clone(self):
        pcb = PCB(self.file)
        pcb.rotate = self.rotate
        pcb.disable_auto_tab = self.disable_auto_tab
        pcb._tabs = list(self._tabs)
        return pcb

    def contains(self, p):
        for shape in self.shapes:
            if shape.contains(p):
                return True
        return False

    def distance(self, obj):
        mdist = None
        if type(obj) is PCB:
            objs = obj.shapes
        else:
            objs = [obj]
        for shape, obj in itertools.product(self.shapes, objs):
            dist = distance(shape, obj)
            if mdist is None:
                mdist = dist
            else:
                mdist = min(mdist, dist)
        return mdist

    def directional_distance(self, obj, direction):
        mdist = None
        if type(obj) is PCB:
            objs = obj.shapes
        else:
            objs = [obj]
        for shape, obj in itertools.product(self.shapes, objs):
            c = collision(shape, obj, direction)
            if c:
                dist = LineString(c).length
                if mdist is None:
                    mdist = dist
                else:
                    mdist = min(mdist, dist)
        return mdist

    def rotateBy(self, deg=90):
        x, y = self.center
        self.rotate = self.rotate + deg
        self.setCenter((x, y))

    def setTop(self, top):
        x1, y1, x2, y2 = self.bbox
        self.y = self.y - y1 + top

    def setBottom(self, bottom):
        x1, y1, x2, y2 = self.bbox
        self.y = self.y - y2 + bottom

    def setLeft(self, left):
        x1, y1, x2, y2 = self.bbox
        self.x = self.x - x1 + left

    def setRight(self, right):
        x1, y1, x2, y2 = self.bbox
        self.x = self.x - x2 + right

    @property
    def center(self):
        p = Polygon([(0, 0), (self.width, 0), (self.width, self.height), (0, self.height)])
        p = affinity.rotate(p, self.rotate*-1, origin=(0,0))
        b = p.bounds
        x1, y1, x2, y2 = self.x+b[0], self.y+b[1], self.x+b[2], self.y+b[3]
        return (x1+x2)/2, (y1+y2)/2

    def setCenter(self, value):
        x0, y0 = self.center
        self.x = self.x - x0 + value[0]
        self.y = self.y - y0 + value[1]

    @property
    def rwidth(self):
        x1, y1, x2, y2 = self.bbox
        return x2 - x1

    @property
    def rheight(self):
        x1, y1, x2, y2 = self.bbox
        return y2 - y1

    @property
    def bbox(self):
        p = MultiPolygon(self.shapes)
        return p.bounds

    def addTab(self, x, y):
        p = affinity.rotate(Point(x - self.x - self.off_x, y - self.y - self.off_y), self.rotate*1, origin=(0,0))
        self._tabs.append((p.x, p.y))

class Hole(StateObject):
    def __init__(self, coords):
        super().__init__()
        polygon = Polygon(coords)
        b = polygon.bounds
        self.off_x = 0
        self.off_y = 0
        self.x = b[0]
        self.y = b[1]
        self._polygon = transform(polygon, lambda x: x-[self.x, self.y])

    @property
    def polygon(self):
        return transform(self._polygon, lambda x: x+[self.x+self.off_x, self.y+self.off_y])

    def contains(self, p):
        return self.polygon.contains(p)

# Modified from tab() in kikit:
# 1. Don't stop at first hit substrate, it may not be the closest one
# 2. Fix origin inside a hole
def autotabs(boardSubstrate, origin, direction, width,
            maxHeight=pcbnew.FromMM(50), fillet=0):
    """
    Create a tab for the substrate. The tab starts at the specified origin
    (2D point) and tries to penetrate existing substrate in direction (a 2D
    vector). The tab is constructed with given width. If the substrate is
    not penetrated within maxHeight, exception is raised.

    When partitionLine is specified, the tab is extended to the opposite
    side - limited by the partition line. Note that if tab cannot span
    towards the partition line, then the tab is not created - it returns a
    tuple (None, None).

    If a fillet is specified, it allows you to add fillet to the tab of
    specified radius.

    Returns a pair tab and cut outline. Add the tab it via union - batch
    adding of geometry is more efficient.
    """
    boardSubstrate.orient()

    if boardSubstrate.substrates.contains(Point(origin)) and not boardSubstrate.substrates.boundary.contains(Point(origin)):
        print(origin, direction, ["Tab annotation is placed inside the board. It has to be on edge or outside the board."])
        return []

    origin = np.array(origin)
    direction = np.around(normalize(direction), 4)
    epsilon = direction * float(SHP_EPSILON)
    origin -= epsilon
    tabs = []
    for geom in listGeometries(boardSubstrate.substrates):
        sideOriginA = origin + makePerpendicular(direction) * width / 2
        sideOriginB = origin - makePerpendicular(direction) * width / 2
        try:
            boundary = geom.exterior
            splitPointA = closestIntersectionPoint(sideOriginA, direction,
                boundary, maxHeight)
            splitPointB = closestIntersectionPoint(sideOriginB, direction,
                boundary, maxHeight)
            tabFace = biteBoundary(boundary, splitPointB, splitPointA)

            tab = Polygon([p + epsilon for p in tabFace.coords] + [sideOriginA, sideOriginB])
            tabs.append(boardSubstrate._makeTabFillet(tab, tabFace, fillet))
        except NoIntersectionError as e:
            pass
        except TabFilletError as e:
            pass

        for boundary in geom.interiors:
            try:
                splitPointA = closestIntersectionPoint(sideOriginA, direction,
                    boundary, maxHeight)
                splitPointB = closestIntersectionPoint(sideOriginB, direction,
                    boundary, maxHeight)
                tabFace = biteBoundary(boundary, splitPointB, splitPointA)

                tab = Polygon([p + epsilon for p in tabFace.coords] + [sideOriginA, sideOriginB])
                tabs.append(boardSubstrate._makeTabFillet(tab, tabFace, fillet))
            except NoIntersectionError as e:
                pass
            except TabFilletError as e:
                pass
    return tabs

def autotab(boardSubstrate, origin, direction, width,
            maxHeight=pcbnew.FromMM(50), fillet=0):
    tabs = autotabs(boardSubstrate, origin, direction, width, maxHeight, fillet)
    if tabs:
        tabs = [(tab[0].area, tab) for tab in tabs]
        tabs.sort(key=lambda t: t[0])
        return tabs[0][1]
    return None

class UI(Application):
    def __init__(self):
        super().__init__(icon=resource_path("icon.ico"))

        self.unit = mm
        self.off_x = 20 * self.unit
        self.off_y = 20 * self.unit

        self.state = State()
        self.state.hide_outside_reference_value = True
        self.state.debug = False
        self.state.show_conflicts = True
        self.state.show_pcb = True
        self.state.show_hole = True
        self.state.show_mb = True
        self.state.show_vc = True

        self.state.pcb = []
        self.state.scale = (0, 0, 1)

        self.state.target_path = ""
        self.state.export_path = ""

        self.state.canvas_width = 800
        self.state.canvas_height = 800

        self.state.focus = None
        self.state.focus_tab = None

        self.state.netRenamePattern = "Board_{n}-{orig}"
        self.state.refRenamePattern = "Board_{n}-{orig}"

        self.state.vcuts = []
        self.state.bites = []
        self.state.dbg_points = []
        self.state.dbg_rects = []
        self.state.dbg_polygons = []
        self.state.dbg_text = []
        self.state.substrates = []
        self.state.holes = []
        self.state.conflicts = []

        self.state.use_frame = True
        self.state.tight = True
        self.state.auto_tab = True
        self.state.spacing = 1.6
        self.state.max_tab_spacing = 50.0
        self.state.cut_method = "vc_or_mb"
        self.state.mb_diameter = 0.6
        self.state.mb_spacing = round(0.3 + self.state.mb_diameter, 1)
        self.state.mb_offset = 0.0
        mb_count = 5
        self.state.tab_width = math.ceil((self.state.mb_spacing * (mb_count-1)) * 10) / 10
        self.state.vc_layer = "Cmts.User"
        self.state.merge_vcuts = True
        self.state.merge_vcuts_threshold = 0.4
        self.state.frame_width = 100
        self.state.frame_height = 100
        self.state.frame_top = 5
        self.state.frame_bottom = 5
        self.state.frame_left = 0
        self.state.frame_right = 0
        self.state.mill_fillets = 0.5
        self.state.export_mill_fillets = False

        self.state.boardSubstrate = None

        self.mousepos = None
        self.mouse_dragging = None
        self.mousehold = False
        self.mousemoved = 0
        self.tool = Tool.NONE
        self.state.edit_polygon = None

    def autoScale(self):
        x1, y1 = 0, 0
        x2, y2 = self.state.frame_width * self.unit, self.state.frame_height * self.unit
        for pcb in self.state.pcb:
            bbox = pcb.bbox
            x1 = min(x1, bbox[0] - self.off_x)
            y1 = min(y1, bbox[1] - self.off_y)
            x2 = max(x2, bbox[2] - self.off_x)
            y2 = max(y2, bbox[3] - self.off_y)

        dw = x2-x1
        dh = y2-y1

        if dw == 0 or dh == 0:
            return

        cw = self.state.canvas_width
        ch = self.state.canvas_height
        sw = cw / dw
        sh = ch / dh
        scale = min(sw, sh) * 0.75
        self.scale = scale
        offx = (cw - (dw+x1) * scale) / 2
        offy = (ch - (dh+y1) * scale) / 2
        self.state.scale = (offx, offy, scale)

    def addPCB(self, e):
        boardfile = OpenFile("Open PCB", types="KiCad PCB (*.kicad_pcb)|*.kicad_pcb")
        if boardfile:
            p = PCB(boardfile)
            self._addPCB(p)

    def _addPCB(self, pcb):
        if len(self.state.pcb) > 0:
            last = self.state.pcb[-1]
            pcb.y = last.y + last.rheight + self.state.spacing * self.unit
        else:
            pcb.y = (self.state.frame_top + self.state.spacing if self.state.frame_top > 0 else 0) * self.unit
        pcb.off_x = self.off_x
        pcb.off_y = self.off_y
        self.state.pcb.append(pcb)
        self.autoScale()
        self.build()

    def duplicate(self, e, pcb):
        self._addPCB(pcb.clone())

    def remove(self, e, obj):
        if isinstance(obj, PCB):
            self.state.pcb = [p for p in self.state.pcb if p is not obj]
            self.autoScale()
        elif obj:
            self.state.holes = [h for h in self.state.holes if h is not obj]
        self.state.focus = None
        self.build()

    def highlight_tab(self, e, i):
        self.state.focus_tab = i

    def remove_tab(self, e, i):
        self.state.focus._tabs.pop(i)
        self.state.focus_tab = None
        self.build()

    def save(self, e, target=None):
        if target is None:
            target = SaveFile(self.state.target_path, types="KiKit Panelization (*.kikit_pnl)|*.kikit_pnl")
        if not target:
            return

        suffix = ".kikit_pnl"
        if not target.endswith(suffix):
            target += suffix

        target = os.path.realpath(target)

        self.state.target_path = target

        pcbs = []
        for pcb in self.state.pcb:
            try:
                file = os.path.relpath(pcb.file, os.path.dirname(target))
            except ValueError:
                file = pcb.file
            pcbs.append({
                "file": file,
                "x": pcb.x,
                "y": pcb.y,
                "rotate": pcb.rotate,
                "disable_auto_tab": pcb.disable_auto_tab,
                "tabs": pcb._tabs,
            })
        data = {
            "export_path": self.state.export_path,
            "hide_outside_reference_value": self.state.hide_outside_reference_value,
            "use_frame": self.state.use_frame,
            "tight": self.state.tight,
            "auto_tab": self.state.auto_tab,
            "spacing": self.state.spacing,
            "max_tab_spacing": self.state.max_tab_spacing,
            "cut_method": self.state.cut_method,
            "mb_diameter": self.state.mb_diameter,
            "mb_spacing": self.state.mb_spacing,
            "mb_offset": self.state.mb_offset,
            "tab_width": self.state.tab_width,
            "vc_layer": self.state.vc_layer,
            "merge_vcuts": self.state.merge_vcuts,
            "merge_vcuts_threshold": self.state.merge_vcuts_threshold,
            "frame_width": self.state.frame_width,
            "frame_height": self.state.frame_height,
            "frame_top": self.state.frame_top,
            "frame_bottom": self.state.frame_bottom,
            "frame_left": self.state.frame_left,
            "frame_right": self.state.frame_right,
            "mill_fillets": self.state.mill_fillets,
            "export_mill_fillets": self.state.export_mill_fillets,
            "netRenamePattern": self.state.netRenamePattern,
            "refRenamePattern": self.state.refRenamePattern,
            "pcb": pcbs,
            "hole": [list(transform(h.polygon.exterior, lambda p:p-(self.off_x, self.off_y)).coords) for h in self.state.holes],
        }
        with open(target, "w") as f:
            json.dump(data, f, indent=4)


    def load(self, e, target=None):
        if target is None:
            target = OpenFile("Load Panelization", types="KiKit Panelization (*.kikit_pnl)|*.kikit_pnl")
        if target:
            target = os.path.realpath(target)
            self.state.target_path = target
        else:
            return

        with open(target, "r") as f:
            data = json.load(f)
            if "export_path" in data:
                self.state.export_path = data["export_path"]
            if "hide_outside_reference_value" in data:
                self.state.hide_outside_reference_value = data["hide_outside_reference_value"]
            if "use_frame" in data:
                self.state.use_frame = data["use_frame"]
            if "tight" in data:
                self.state.tight = data["tight"]
            if "auto_tab" in data:
                self.state.auto_tab = data["auto_tab"]
            if "spacing" in data:
                self.state.spacing = data["spacing"]
            if "max_tab_spacing" in data:
                self.state.max_tab_spacing = data["max_tab_spacing"]
            if "cut_method" in data:
                self.state.cut_method = data["cut_method"]
            if "mb_diameter" in data:
                self.state.mb_diameter = data["mb_diameter"]
            if "mb_spacing" in data:
                self.state.mb_spacing = data["mb_spacing"]
            if "mb_offset" in data:
                self.state.mb_offset = data["mb_offset"]
            if "tab_width" in data:
                self.state.tab_width = data["tab_width"]
            if "vc_layer" in data:
                self.state.vc_layer = data["vc_layer"]
            if "merge_vcuts" in data:
                self.state.merge_vcuts = data["merge_vcuts"]
            if "merge_vcuts_threshold" in data:
                self.state.merge_vcuts_threshold = data["merge_vcuts_threshold"]
            if "frame_width" in data:
                self.state.frame_width = data["frame_width"]
            if "frame_height" in data:
                self.state.frame_height = data["frame_height"]
            if "frame_top" in data:
                self.state.frame_top = data["frame_top"]
            if "frame_bottom" in data:
                self.state.frame_bottom = data["frame_bottom"]
            if "frame_left" in data:
                self.state.frame_left = data["frame_left"]
            if "frame_right" in data:
                self.state.frame_right = data["frame_right"]
            if "mill_fillets" in data:
                self.state.mill_fillets = data["mill_fillets"]
            if "export_mill_fillets" in data:
                self.state.export_mill_fillets = data["export_mill_fillets"]
            if "netRenamePattern" in data:
                self.state.netRenamePattern = data["netRenamePattern"]
            if "refRenamePattern" in data:
                self.state.refRenamePattern = data["refRenamePattern"]
            if "hole" in data:
                holes = []
                for h in data["hole"]:
                    hole = Hole(h)
                    hole.off_x = self.off_x
                    hole.off_y = self.off_y
                    holes.append(hole)
                self.state.holes = holes

            self.state.pcb = []
            for p in data.get("pcb", []):
                file = p["file"]
                if not os.path.isabs(file):
                    file = os.path.realpath(os.path.join(os.path.dirname(target), file))
                pcb = PCB(file)
                pcb.off_x = self.off_x
                pcb.off_y = self.off_y
                pcb.x = p["x"]
                pcb.y = p["y"]
                pcb.rotate = p["rotate"]
                pcb.disable_auto_tab = p.get("disable_auto_tab", False)
                pcb._tabs = p.get("tabs", [])
                self.state.pcb.append(pcb)
            self.autoScale()
            self.build()

    def build(self, e=None, export=False):
        pcbs = self.state.pcb
        if len(pcbs) == 0:
            return

        if self.state.spacing < MIN_SPACING:
            self.state.spacing = MIN_SPACING

        spacing = self.state.spacing
        tab_width = self.state.tab_width
        max_tab_spacing = self.state.max_tab_spacing
        mb_diameter = self.state.mb_diameter
        mb_spacing = self.state.mb_spacing
        mb_offset = self.state.mb_offset

        if export is True:
            export = SaveFile(self.state.export_path, types="KiCad PCB (*.kicad_pcb)|*.kicad_pcb")
            if export:
                if not export.endswith(PCB_SUFFIX):
                    export += PCB_SUFFIX
                self.state.export_path = export
            else:
                return
        elif export:
            if not export.endswith(PCB_SUFFIX):
                export += PCB_SUFFIX
            self.state.export_path = export

        panel = panelize.Panel(self.state.export_path if export else "")
        panel.vCutSettings.layer = {
            "Cmts.User": Layer.Cmts_User,
            "Edge.Cuts": Layer.Edge_Cuts,
            "User.1": Layer.User_1,
        }.get(self.state.vc_layer, Layer.Cmts_User)


        if self.state.use_frame and self.state.frame_top > 0:
            frame_top_polygon = Polygon([
                [self.off_x, self.off_y],
                [self.off_x+self.state.frame_width*self.unit, self.off_y],
                [self.off_x+self.state.frame_width*self.unit, self.off_y+self.state.frame_top*self.unit],
                [self.off_x, self.off_y+self.state.frame_top*self.unit],
            ])
        else:
            frame_top_polygon = None

        if self.state.use_frame and self.state.frame_bottom > 0:
            frame_bottom_polygon = Polygon([
                [self.off_x, self.off_y+self.state.frame_height*self.unit],
                [self.off_x+self.state.frame_width*self.unit, self.off_y+self.state.frame_height*self.unit],
                [self.off_x+self.state.frame_width*self.unit, self.off_y+self.state.frame_height*self.unit-self.state.frame_bottom*self.unit],
                [self.off_x, self.off_y+self.state.frame_height*self.unit-self.state.frame_bottom*self.unit],
            ])
        else:
            frame_bottom_polygon = None

        if self.state.use_frame and self.state.frame_left > 0:
            frame_left_polygon = Polygon([
                [self.off_x, self.off_y],
                [self.off_x, self.off_y+self.state.frame_height*self.unit],
                [self.off_x+self.state.frame_left*self.unit, self.off_y+self.state.frame_height*self.unit],
                [self.off_x+self.state.frame_left*self.unit, self.off_y],
            ])
        else:
            frame_left_polygon = None

        if self.state.use_frame and self.state.frame_right > 0:
            frame_right_polygon = Polygon([
                [self.off_x+self.state.frame_width*self.unit, self.off_y],
                [self.off_x+self.state.frame_width*self.unit, self.off_y+self.state.frame_height*self.unit],
                [self.off_x+self.state.frame_width*self.unit-self.state.frame_right*self.unit, self.off_y+self.state.frame_height*self.unit],
                [self.off_x+self.state.frame_width*self.unit-self.state.frame_right*self.unit, self.off_y],
            ])
        else:
            frame_right_polygon = None

        boundarySubstrates = []
        if self.state.use_frame and not self.state.tight:
            if frame_top_polygon:
                panel.appendSubstrate(frame_top_polygon)
                sub = substrate.Substrate([])
                sub.union(frame_top_polygon)
                boundarySubstrates.append(sub)
            if frame_bottom_polygon:
                panel.appendSubstrate(frame_bottom_polygon)
                sub = substrate.Substrate([])
                sub.union(frame_bottom_polygon)
                boundarySubstrates.append(sub)
            if frame_left_polygon:
                panel.appendSubstrate(frame_left_polygon)
                sub = substrate.Substrate([])
                sub.union(frame_left_polygon)
                boundarySubstrates.append(sub)
            if frame_right_polygon:
                panel.appendSubstrate(frame_right_polygon)
                sub = substrate.Substrate([])
                sub.union(frame_right_polygon)
                boundarySubstrates.append(sub)

        for pcb in pcbs:
            panel.appendBoard(
                pcb.file,
                pcbnew.VECTOR2I(round(pcb.off_x + pcb.x), round(pcb.off_y + pcb.y)),
                origin=panelize.Origin.TopLeft,
                tolerance=panelize.fromMm(1),
                rotationAngle=pcbnew.EDA_ANGLE(pcb.rotate, pcbnew.DEGREES_T),
                inheritDrc=False,
                netRenamer=self.netRenamer,
                refRenamer=self.refRenamer
            )

        if self.state.hide_outside_reference_value and export:
            for fp in panel.board.GetFootprints():
                ref = fp.Reference()
                for pcb in pcbs:
                    if pcb.contains(Point(ref.GetX(), ref.GetY())):
                        break
                else:
                    ref.SetVisible(False)

                value = fp.Value()
                for pcb in pcbs:
                    if pcb.contains(Point(value.GetX(), value.GetY())):
                        break
                else:
                    value.SetVisible(False)

        if self.state.tight:
            x1, y1, x2, y2 = pcbs[0].bbox

            if self.state.use_frame:
                x1 = min(x1, self.off_x)
                y1 = min(y1, self.off_y)
                x2 = max(x2, self.off_x + self.state.frame_width*self.unit)
                y2 = max(y2, self.off_y + self.state.frame_height*self.unit)

            for pcb in pcbs[1:]:
                bbox = pcb.bbox
                x1 = min(x1, bbox[0])
                y1 = min(y1, bbox[1])
                x2 = max(x2, bbox[2])
                y2 = max(y2, bbox[3])

            # board hole
            frameBody = box(x1, y1, x2, y2)
            for s in panel.substrates:
                frameBody = frameBody.difference(s.exterior().buffer(spacing*self.unit, join_style="mitre"))

            for hole in self.state.holes:
                poly = hole.polygon
                frameBody = frameBody.difference(poly)

            panel.appendSubstrate(frameBody)

        dbg_points = []
        dbg_rects = []
        dbg_polygons = []
        dbg_text = []
        tabs = []
        cuts = []

        tab_substrates = []

        # manual tab
        for pcb in pcbs:
            for x1, y1, x2, y2 in pcb.tabs():
                tx, ty = extrapolate(x1, y1, x2, y2, 1, spacing/2*self.unit)

                # outward
                tab = autotab(panel.boardSubstrate, (tx, ty), (tx-x2, ty-y2), tab_width*self.unit)
                if tab:
                    tab_substrates.append(tab[0])
                    for pcb in pcbs:
                        dist = pcb.distance(tab[1])
                        if dist == 0:
                            cuts.append(tab[1])
                            break

                    # inward
                    tab = autotab(panel.boardSubstrate, (tx, ty), (x2-tx, y2-ty), tab_width*self.unit)
                    if tab: # tab, tabface
                        tab_substrates.append(tab[0])
                        cuts.append(tab[1])

        # auto tab

        # (x, y), inward_direction, score_divider
        tab_candidates = []

        x_parts = []
        y_parts = []
        for pcb in pcbs:
            x1, y1, x2, y2 = pcb.bbox
            x_parts.append(x1)
            y_parts.append(y1)

        if self.state.auto_tab and max_tab_spacing > 0:
            for pcb in pcbs:
                if pcb.disable_auto_tab:
                    continue
                if pcb.tabs():
                    continue
                bboxes = [p.bbox for p in pcbs if p is not pcb]
                if self.state.use_frame:
                    if self.state.tight:
                        bboxes.append((0, 0, self.state.frame_width*self.unit, self.state.frame_height*self.unit))
                    else:
                        if self.state.frame_top > 0:
                            bboxes.append((0, 0, self.state.frame_width*self.unit, self.state.frame_top*self.unit))
                        if self.state.frame_bottom > 0:
                            bboxes.append((0, self.state.frame_height*self.unit-self.state.frame_bottom*self.unit, self.state.frame_width*self.unit, self.state.frame_height*self.unit))
                        if self.state.frame_left > 0:
                            bboxes.append((0, 0, self.state.frame_left*self.unit, self.state.frame_height*self.unit))
                        if self.state.frame_right > 0:
                            bboxes.append((self.state.frame_width*self.unit-self.state.frame_right*self.unit, 0, self.state.frame_width*self.unit, self.state.frame_height*self.unit))

                x1, y1, x2, y2 = pcb.bbox
                row_bboxes = [(b[0],b[2]) for b in bboxes if LineString([(0, b[1]), (0, b[3])]).intersects(LineString([(0, y1), (0, y2)]))]
                col_bboxes = [(b[1],b[3]) for b in bboxes if LineString([(b[0], 0), (b[2], 0)]).intersects(LineString([(x1, 0), (x2, 0)]))]

                # top
                if col_bboxes and y1 != min([b[0] for b in col_bboxes]):
                    n = math.ceil((x2-x1) / (max_tab_spacing*self.unit))+1
                    for i in range(1,n):
                        p = (x1 + (x2-x1)*i/n, y1 - spacing/2*self.unit)
                        partition = len([x for x in x_parts if x < p[0]])
                        tab_candidates.append((p, (0,1), partition, (x2-x1)/n))

                # bottom
                if col_bboxes and y2 != max([b[1] for b in col_bboxes]):
                    n = math.ceil((x2-x1) / (max_tab_spacing*self.unit))+1
                    for i in range(1,n):
                        p = (x1 + (x2-x1)*i/n, y2 + spacing/2*self.unit)
                        partition = len([x for x in x_parts if x < p[0]])
                        tab_candidates.append((p, (0,-1), partition, (x2-x1)/n))

                # left
                if row_bboxes and x1 != min([b[0] for b in row_bboxes]):
                    n = math.ceil((y2-y1) / (max_tab_spacing*self.unit))+1
                    for i in range(1,n):
                        p = (x1 - spacing/2*self.unit , y1 + (y2-y1)*i/n)
                        partition = len([y for y in y_parts if y < p[1]])
                        tab_candidates.append((p, (1,0), partition, (y2-y1)/n))

                # right
                if row_bboxes and x2 != max([b[1] for b in row_bboxes]):
                    n = math.ceil((y2-y1) / (max_tab_spacing*self.unit))+1
                    for i in range(1,n):
                        p = (x2 + spacing/2*self.unit , y1 + (y2-y1)*i/n)
                        partition = len([y for y in y_parts if y < p[1]])
                        tab_candidates.append((p, (-1,0), partition, (y2-y1)/n))

        tab_candidates.sort(key=lambda t: t[3]) # sort by divided edge length

        filtered_cands = []
        for p, inward_direction, partition, score_divider in tab_candidates:
            skip = False
            for hole in self.state.holes:
                shape = hole.polygon
                if shape.contains(Point(*p)):
                    skip = True
                    break
            if skip:
                continue
            filtered_cands.append((p, inward_direction, partition, score_divider))
            dbg_points.append((p, 1))
        tab_candidates = filtered_cands

        # x, y, abs(direction), partition index
        tabs = []
        tab_dist = max_tab_spacing*self.unit/3
        for p, inward_direction, partiion, score_divider in tab_candidates:
            # prevent overlapping tabs
            if len([t for t in tabs if
                    (abs(inward_direction[0]), abs(inward_direction[1]))==(abs(t[2][0]), abs(t[2][1])) # same axis
                    and
                    t[3] == partiion # same partition
                    and
                    ( # nearby
                        ( # horizontal
                            abs(inward_direction[1]) == 1
                            and
                            abs(t[1] - p[1]) < spacing * self.unit
                            and
                            abs(t[0]-p[0]) < tab_dist
                        )
                        or
                        ( # vertical
                            abs(inward_direction[0]) == 1
                            and
                            abs(t[0] - p[0]) < spacing * self.unit
                            and
                            abs(t[1]-p[1]) < tab_dist
                        )
                    )

                ]) > 0:
                continue
            dbg_points.append((p, 5))

            # outward
            tab = autotab(panel.boardSubstrate, p, (inward_direction[0]*-1,inward_direction[1]*-1), tab_width*self.unit)
            if tab: # tab, tabface
                tabs.append((
                    p[0],
                    p[1],
                    (abs(inward_direction[0]), abs(inward_direction[1])),
                    partiion,
                ))
                tab_substrates.append(tab[0])
                for pcb in pcbs:
                    dist = pcb.distance(tab[1])
                    if dist == 0:
                        cuts.append(tab[1])
                        break

                # inward
                tab = autotab(panel.boardSubstrate, p, inward_direction, tab_width*self.unit)
                if tab: # tab, tabface
                    tab_substrates.append(tab[0])
                    cuts.append(tab[1])

        for t in tab_substrates:
            dbg_polygons.append(t.exterior.coords)
            try:
                panel.appendSubstrate(t)
            except:
                traceback.print_exc()

        conflicts = []
        shapes = [shapely.union_all(p.shapes) for p in pcbs]
        if self.state.use_frame:
            frame = Polygon([
                (self.off_x, self.off_y),
                (self.off_x+self.state.frame_width*self.unit, self.off_y),
                (self.off_x+self.state.frame_width*self.unit, self.off_y+self.state.frame_height*self.unit),
                (self.off_x, self.off_y+self.state.frame_height*self.unit),
            ])
            try:
                out_of_frame = GeometryCollection(shapes).difference(frame)
                if not out_of_frame.is_empty:
                    conflicts.append(out_of_frame)
            except:
                pass
        if frame_top_polygon:
            shapes.append(frame_top_polygon)
        if frame_bottom_polygon:
            shapes.append(frame_bottom_polygon)
        if frame_left_polygon:
            shapes.append(frame_left_polygon)
        if frame_right_polygon:
            shapes.append(frame_right_polygon)
        for i,a in enumerate(shapes):
            for b in shapes[i+1:]:
                conflict = shapely.intersection(a, b)
                if not conflict.is_empty and conflict.area > 0:
                    conflicts.append(conflict)

        for pcb in pcbs:
            shapes = pcb.shapes
            for s in shapes:
                dbg_rects.append(s.bounds)

        if not export or self.state.export_mill_fillets:
            panel.addMillFillets(self.state.mill_fillets*self.unit)

        if not export:
            self.state.conflicts = conflicts
            self.state.dbg_points = dbg_points
            self.state.dbg_rects = dbg_rects
            self.state.dbg_polygons = dbg_polygons
            self.state.dbg_text = dbg_text
            self.state.boardSubstrate = panel.boardSubstrate

        filtered_vcuts = []
        for cut in cuts:
            x1, y1, x2, y2 = cut.bounds
            if Polygon(((x1,y1), (x2,y1), (x2,y2), (x1,y2))).area == 0: # is a line
                filtered_vcuts.append(cut)
        cuts = sorted(filtered_vcuts,key=lambda cut: cut.bounds)

        # normalize linestring direction
        for i in range(len(cuts)):
            if cuts[i].coords[0][1] > cuts[i].coords[1][1]:
                cuts[i] = shapely.reverse(cuts[i])
            if cuts[i].coords[0][0] > cuts[i].coords[1][0]:
                cuts[i] = shapely.reverse(cuts[i])

        vcuts = []
        bites = []
        cut_method = self.state.cut_method

        if cut_method == "mb":
            bites.extend(cuts)
            panel.makeMouseBites(cuts, diameter=mb_diameter * self.unit, spacing=mb_spacing * self.unit - SHP_EPSILON, offset=mb_offset * self.unit, prolongation=0 * self.unit)
        elif cut_method == "vc":
            vcuts.extend(cuts)
        elif cut_method == "vc_or_mb" or cut_method == "vc_and_mb":
            for cut in cuts:
                p1 = cut.coords[0]
                p2 = cut.coords[-1]
                if p1[0]==p2[0]: # vertical
                    vc_ok = True
                    for i, pcb in enumerate(pcbs):
                        x1, y1, x2, y2 = pcb.bbox
                        if x1+SHP_EPSILON < p1[0] and p1[0] < x2-SHP_EPSILON: # cut through other pcb
                            vc_ok = False
                            break

                    do_vc = vc_ok
                    do_mb = not vc_ok or cut_method == "vc_and_mb"

                    if do_mb:
                        panel.makeMouseBites([cut], diameter=mb_diameter * self.unit - SHP_EPSILON, spacing=mb_spacing * self.unit, offset=mb_offset * self.unit, prolongation=0 * self.unit)
                        bites.append(cut)
                    if do_vc:
                        vcuts.append(cut)

                elif p1[1]==p2[1]: # horizontal
                    vc_ok = True
                    for i, pcb in enumerate(pcbs):
                        x1, y1, x2, y2 = pcb.bbox
                        if y1+SHP_EPSILON < p1[1] and p1[1] < y2-SHP_EPSILON: # cut through other pcb
                            vc_ok = False
                            break

                    do_vc = vc_ok
                    do_mb = not vc_ok or cut_method == "vc_and_mb"

                    if do_mb:
                        panel.makeMouseBites([cut], diameter=mb_diameter * self.unit - SHP_EPSILON, spacing=mb_spacing * self.unit, offset=mb_offset * self.unit, prolongation=0 * self.unit)
                        bites.append(cut)
                    if do_vc:
                        vcuts.append(cut)

                else:
                    panel.makeMouseBites([cut], diameter=mb_diameter * self.unit - SHP_EPSILON, spacing=mb_spacing * self.unit, offset=mb_offset * self.unit, prolongation=0 * self.unit)
                    bites.append(cut)

        merge_vcuts = self.state.merge_vcuts
        merge_threshold = self.state.merge_vcuts_threshold * self.unit
        if merge_vcuts:
            if self.state.debug:
                conflicts.extend(vcuts)

            horizontal_vcuts = []
            vertical_vcuts = []
            for vcut in vcuts:
                if vcut.coords[0][1] == vcut.coords[-1][1]:
                    horizontal_vcuts.append(vcut.coords[0][1])
                if vcut.coords[0][0] == vcut.coords[-1][0]:
                    vertical_vcuts.append(vcut.coords[0][0])

            # grouping
            horitonzal_groups = []
            for p in horizontal_vcuts:
                for g in horitonzal_groups:
                    if any([abs(p-p2) <= merge_threshold + SHP_EPSILON for p2 in g]):
                        if not p in g:
                            g.append(p)
                        break
                else:
                    horitonzal_groups.append([p])

            vertical_groups = []
            for p in vertical_vcuts:
                for g in vertical_groups:
                    if any([abs(p-p2) <= merge_threshold + SHP_EPSILON for p2 in g]):
                        if not p in g:
                            g.append(p)
                        break
                else:
                    vertical_groups.append([p])

            horitonzal_groups = [sum(g)/len(g) for g in horitonzal_groups]
            vertical_groups = [sum(g)/len(g) for g in vertical_groups]

            vcuts = []

            boardSubstrateBounds = panel.boardSubstrate.bounds()
            for y in horitonzal_groups:
                vcuts.append(LineString([(boardSubstrateBounds[0], y), (boardSubstrateBounds[2], y)]))
            for x in vertical_groups:
                vcuts.append(LineString([(x, boardSubstrateBounds[1]), (x, boardSubstrateBounds[3])]))

        panel.makeVCuts(vcuts)

        if not export:
            self.state.vcuts = vcuts
            self.state.bites = bites

        if export:
            panel.save()

    def addHole(self, e):
        self.tool = Tool.HOLE
        self.state.edit_polygon = []

    def align_top(self, e, pcb=None):
        todo = list(self.state.pcb)
        if not todo:
            return
        todo.sort(key=lambda pcb: pcb.bbox[1])

        topmost = (self.state.frame_top + (self.state.spacing if self.state.frame_top > 0 else 0)) * self.unit + self.off_y
        if pcb:
            ys = [topmost]
            for p in todo:
                x1, y1, x2, y2 = p.bbox
                ys.append(y1)
                ys.append(y2 + self.state.spacing * self.unit)
                ys.append(y2 - pcb.rheight)
            ys.sort()

        start = 0
        end = len(todo)
        if pcb:
            start = todo.index(pcb)
            end = start+1
        for i, p in enumerate(todo[start:end], start):
            ax1, ay1, ax2, ay2 = p.bbox
            top = None
            for d in todo[:i]:
                dist = p.directional_distance(d, (0, -1))
                if dist is not None:
                    t = ay1 - dist
                    if top is None or t > top:
                        top = t

            if pcb:
                if top is None:
                    p.setTop(([y for y in ys if y < ay1] or [ys[0]])[-1])
                else:
                    p.setTop(([y for y in ys if y < ay1 and y>=top] or [top])[-1])
            else:
                if top is None:
                    # move objects behind together to prevent overlapping
                    offset = topmost - ay1
                    for o in todo[i+1:]:
                        if o.directional_distance(p, (0, -1)):
                            o.setTop(o.bbox[1]+offset)
                    p.setTop(topmost)
                else:
                    p.setTop(max(
                        top + self.state.spacing*self.unit,
                        topmost
                    ))
        self.autoScale()
        self.build()

    def align_bottom(self, e, pcb=None):
        todo = list(self.state.pcb)
        if not todo:
            return
        todo.sort(key=lambda pcb: -pcb.bbox[3])

        bottommost = (self.state.frame_height - self.state.frame_bottom - (self.state.spacing if self.state.frame_bottom > 0 else 0)) * self.unit + self.off_y
        if pcb:
            ys = [bottommost]
            for p in todo:
                x1, y1, x2, y2 = p.bbox
                ys.append(y1 - self.state.spacing * self.unit)
                ys.append(y2)
                ys.append(y1 + pcb.rheight)
            ys.sort()

        start = 0
        end = len(todo)
        if pcb:
            start = todo.index(pcb)
            end = start+1
        for i, p in enumerate(todo[start:end], start):
            ax1, ay1, ax2, ay2 = p.bbox
            bottom = None
            for d in todo[:i]:
                dist = p.directional_distance(d, (0, 1))
                if dist is not None:
                    b = ay2 + dist
                    if bottom is None or b < bottom:
                        bottom = b

            if pcb:
                if bottom is None:
                    p.setBottom(([y for y in ys if y > ay2] or [ys[-1]])[0])
                else:
                    p.setBottom(([y for y in ys if y > ay2 and y<=bottom] or [bottom])[0])
            else:
                if bottom is None:
                    # move objects behind together to prevent overlapping
                    offset = bottommost - ay2
                    for o in todo[i+1:]:
                        if o.directional_distance(p, (0, 1)):
                            o.setBottom(o.bbox[3]+offset)
                    p.setBottom(bottommost)
                else:
                    p.setBottom(min(
                        bottom - self.state.spacing*self.unit,
                        bottommost
                    ))
        self.autoScale()
        self.build()

    def align_left(self, e, pcb=None):
        todo = list(self.state.pcb)
        if not todo:
            return
        todo.sort(key=lambda pcb: pcb.bbox[0])

        leftmost = (self.state.frame_left + (self.state.spacing if self.state.frame_left > 0 else 0)) * self.unit + self.off_x
        if pcb:
            xs = [leftmost]
            for p in todo:
                x1, y1, x2, y2 = p.bbox
                xs.append(x1)
                xs.append(x2 + self.state.spacing * self.unit)
                xs.append(x2 - pcb.rwidth)
            xs.sort()

        start = 0
        end = len(todo)
        if pcb:
            start = todo.index(pcb)
            end = start+1
        for i, p in enumerate(todo[start:end], start):
            ax1, ay1, ax2, ay2 = p.bbox
            left = None
            for d in todo[:i]:
                dist = p.directional_distance(d, (-1, 0))
                if dist is not None:
                    l = ax1 - dist
                    if left is None or l > left:
                        left = l

            if pcb:
                if left is None:
                    p.setLeft(([x for x in xs if x < ax1] or [xs[0]])[-1])
                else:
                    p.setLeft(([x for x in xs if x < ax1 and x>=left] or [left])[-1])
            else:
                if left is None:
                    # move objects behind together to prevent overlapping
                    offset = leftmost - ax1
                    for o in todo[i+1:]:
                        if o.directional_distance(p, (-1, 0)):
                            o.setLeft(o.bbox[0]+offset)
                    p.setLeft(leftmost)
                else:
                    p.setLeft(max(
                        left + self.state.spacing*self.unit,
                        leftmost
                    ))
        self.autoScale()
        self.build()

    def align_right(self, e, pcb=None):
        todo = list(self.state.pcb)
        if not todo:
            return
        todo.sort(key=lambda pcb: -pcb.bbox[2])

        rightmost = (self.state.frame_width - self.state.frame_right - (self.state.spacing if self.state.frame_right > 0 else 0)) * self.unit + self.off_x
        if pcb:
            xs = [rightmost]
            for p in todo:
                x1, y1, x2, y2 = p.bbox
                xs.append(x1 - self.state.spacing * self.unit)
                xs.append(x2)
                xs.append(x1 + pcb.rwidth)
            xs.sort()

        start = 0
        end = len(todo)
        if pcb:
            start = todo.index(pcb)
            end = start+1
        for i, p in enumerate(todo[start:end], start):
            ax1, ay1, ax2, ay2 = p.bbox
            right = None
            for d in todo[:i]:
                dist = p.directional_distance(d, (1, 0))
                if dist is not None:
                    r = ax2 + dist
                    if right is None or r < right:
                        right = r

            if pcb:
                if right is None:
                    p.setRight(([x for x in xs if x > ax2] or [xs[-1]])[0])
                else:
                    p.setRight(([x for x in xs if x > ax2 and x<=right] or [right])[0])
            else:
                if right is None:
                    # move objects behind together to prevent overlapping
                    offset = rightmost - ax2
                    for o in todo[i+1:]:
                        if o.directional_distance(p, (1, 0)):
                            o.setRight(o.bbox[2]+offset)
                    p.setRight(rightmost)
                else:
                    p.setRight(min(
                        right - self.state.spacing*self.unit,
                        rightmost
                    ))
        self.autoScale()
        self.build()

    def rotateBy(self, e, deg=90):
        pcb = self.state.focus
        if pcb:
            pcb.rotateBy(deg)
            self.build()

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

    def dblclicked(self, e):
        if self.tool == Tool.HOLE:
            polygon = list(self.state.edit_polygon)
            if len(polygon)>=2:
                polygon.append(self.fromCanvas(e.x, e.y))
                self.state.edit_polygon = []
                h = Hole(polygon)
                h.off_x = self.off_x
                h.off_y = self.off_y
                self.state.holes.append(h)
                self.tool = Tool.END
                self.build()

    def mousedown(self, e):
        self.mousepos = e.x, e.y
        self.mousehold = True
        self.mousemoved = 0

        if self.tool == Tool.TAB:
            pass
        elif self.tool == Tool.HOLE:
            x, y = self.fromCanvas(e.x, e.y)
            self.state.edit_polygon.append((x,y))
        else:
            x, y = self.fromCanvas(e.x, e.y)

            p = Point(x+self.off_x, y+self.off_y)
            self.mouse_dragging = None
            if self.state.focus and self.state.focus.contains(p):
                self.mouse_dragging = self.state.focus


    def mouseup(self, e):
        self.mousehold = False
        if self.tool == Tool.TAB:
            x, y = self.fromCanvas(e.x, e.y)
            if self.state.focus.contains(Point(x+self.off_x, y+self.off_y)):
                self.state.focus.addTab(x+self.off_x, y+self.off_y)
            self.tool = Tool.NONE
            self.build()
        elif self.tool == Tool.HOLE:
            pass
        elif self.tool == Tool.END:
            self.tool = Tool.NONE
        else:
            if self.mousemoved < 5:
                found = False
                pcbs = self.state.pcb
                x, y = self.fromCanvas(e.x, e.y)
                p = Point(x+self.off_x, y+self.off_y)

                for hole in self.state.holes:
                    if hole.polygon.contains(p):
                        found = True
                        if self.state.focus is hole:
                            continue
                        else:
                            self.state.focus = hole

                if not found:
                    for pcb in [pcb for pcb in pcbs if pcb is not self.state.focus]:
                        if pcb.contains(p):
                            found = True
                            if self.state.focus is pcb:
                                continue
                            else:
                                self.state.focus = pcb
                                self.state.focus_tab = None
                if not found:
                    self.state.focus = None
            else:
                self.build()

    def mousemove(self, e):
        if self.tool == Tool.TAB or self.tool == Tool.HOLE:
            self.state()
        elif self.mousehold:
            pdx = e.x - self.mousepos[0]
            pdy = e.y - self.mousepos[1]
            self.mousemoved += (pdx**2 + pdy**2)**0.5

            x1, y1 = self.fromCanvas(*self.mousepos)
            x2, y2 = self.fromCanvas(e.x, e.y)
            dx = x2 - x1
            dy = y2 - y1
            self.state.focus = self.mouse_dragging

            if self.mouse_dragging:
                self.mouse_dragging.x += int(dx)
                self.mouse_dragging.y += int(dy)
            else:
                offx, offy, scale = self.state.scale
                offx += pdx
                offy += pdy
                self.state.scale = offx, offy, scale
        self.mousepos = e.x, e.y

    def wheel(self, e):
        offx, offy, scale = self.state.scale
        zoom_factor = 1.2  # Factor for smoother zooming

        nscale = scale * (zoom_factor ** (e.v_delta / 120))

        # Limit the scale
        nscale = min(self.scale*2, max(self.scale/8, nscale))

        # Calculate new offsets
        offx = e.x - (e.x - offx) * nscale / scale
        offy = e.y - (e.y - offy) * nscale / scale

        self.state.scale = offx, offy, nscale

    def keypress(self, event):
        if isinstance(self.state.focus, PCB):
            if event.text == "r":
                self.rotateBy(-90)
                self.build()
            elif event.text == "R":
                self.rotateBy(90)
                self.build()

    def add_tab(self, e):
        self.tool = Tool.TAB

    def drawPCB(self, canvas, index, pcb, highlight):
        fill = 0x225522 if highlight else 0x112211
        for shape in pcb.shapes:
            self.drawShapely(canvas, transform(shape, lambda p:p-(self.off_x, self.off_y)), fill=fill)

        p = affinity.rotate(Point(10, 10), pcb.rotate*-1, origin=(0,0))
        x, y = self.toCanvas(pcb.x+p.x, pcb.y+p.y)
        canvas.drawText(x, y, f"{index}. {pcb.ident}\n{pcb.width/self.unit:.2f}*{pcb.height/self.unit:.2f}", rotate=pcb.rotate*-1, color=0xFFFFFF)

        for i, (x1, y1, x2, y2) in enumerate(pcb.tabs()):
            x2, y2 = extrapolate(x1, y1, x2, y2, 1, self.state.spacing/2*self.unit)
            x1, y1 = self.toCanvas(x1-self.off_x, y1-self.off_y)
            x2, y2 = self.toCanvas(x2-self.off_x, y2-self.off_y)
            if i == self.state.focus_tab and pcb is self.state.focus:
                width = 3
            else:
                width = 1
            canvas.drawLine(x1, y1, x2, y2, color=0xFFFF00, width=width)
            canvas.drawEllipse(x2, y2, 3, 3, stroke=0xFF0000)

    def drawLine(self, canvas, x1, y1, x2, y2, color):
        x1, y1 = self.toCanvas(x1, y1)
        x2, y2 = self.toCanvas(x2, y2)
        canvas.drawLine(x1, y1, x2, y2, color=color)

    def drawPolyline(self, canvas, polyline, *args, **kwargs):
        ps = []
        for p in polyline:
            ps.append(self.toCanvas(*p))
        canvas.drawPolyline(ps, *args, **kwargs)

    def drawPolygon(self, canvas, polygon, stroke=None, fill=None):
        ps = []
        for p in polygon:
            ps.append(self.toCanvas(*p))
        canvas.drawPolygon(ps, stroke=stroke, fill=fill)

    def drawShapely(self, canvas, shape, stroke=None, fill=None):
        offx, offy, scale = self.state.scale
        shape = transform(shape, lambda p:p * scale + (offx, offy))
        canvas.drawShapely(shape, stroke=stroke, fill=fill)

    def drawVCutV(self, canvas, x):
        x1, y1 = self.toCanvas(x-self.off_x, -VC_EXTENT*self.unit)
        x2, y2 = self.toCanvas(x-self.off_x, (self.state.frame_height+VC_EXTENT)*self.unit)
        canvas.drawLine(x1, y1, x2, y2, color=0x4396E2)

    def drawVCutH(self, canvas, y):
        x1, y1 = self.toCanvas(-VC_EXTENT*self.unit, y-self.off_y)
        x2, y2 = self.toCanvas((self.state.frame_width+VC_EXTENT)*self.unit, y-self.off_y)
        canvas.drawLine(x1, y1, x2, y2, color=0x4396E2)

    def drawMousebites(self, canvas, line):
        offx, offy, scale = self.state.scale
        mb_diameter = self.state.mb_diameter
        mb_spacing = self.state.mb_spacing
        i = 0
        line = line.parallel_offset(self.state.mb_offset * self.unit, "left")
        while i * mb_spacing * self.unit <= line.length + SHP_EPSILON:
            p = line.interpolate(i * mb_spacing * self.unit)
            x, y = self.toCanvas(p.x-self.off_x, p.y-self.off_y)
            canvas.drawEllipse(x, y, mb_diameter*self.unit/2*scale, mb_diameter*self.unit/2*scale, stroke=0xFFFF00)
            i += 1

    def painter(self, canvas):
        offx, offy, scale = self.state.scale
        pcbs = self.state.pcb

        # frame area
        if self.state.use_frame:
            x1, y1 = self.toCanvas(0, 0)
            x2, y2 = self.toCanvas(self.state.frame_width*self.unit, self.state.frame_height*self.unit)
            canvas.drawRect(x1, y1, x2, y2, fill=0x151515)

        if self.state.show_pcb:
            # pcb areas
            for i,pcb in enumerate(pcbs):
                if pcb is self.state.focus:
                    continue
                self.drawPCB(canvas, i, pcb, False)

            # focus pcb
            for i,pcb in enumerate(pcbs):
                if pcb is not self.state.focus:
                    continue
                self.drawPCB(canvas, i, pcb, True)

        boardSubstrate = self.state.boardSubstrate
        if boardSubstrate:
            if isinstance(boardSubstrate.substrates, MultiPolygon):
                geoms = boardSubstrate.substrates.geoms
            elif isinstance(boardSubstrate.substrates, Polygon):
                geoms = [boardSubstrate.substrates]
            else:
                geoms = []
            for polygon in geoms:
                coords = transform(polygon.exterior, lambda p:p-(self.off_x, self.off_y)).coords
                for i in range(1, len(coords)):
                    self.drawLine(canvas, coords[i-1][0], coords[i-1][1], coords[i][0], coords[i][1], color=0x777777)
                for interior in polygon.interiors:
                    coords = transform(interior, lambda p:p-(self.off_x, self.off_y)).coords
                    for i in range(1, len(coords)):
                        self.drawLine(canvas, coords[i-1][0], coords[i-1][1], coords[i][0], coords[i][1], color=0x777777)

        if self.state.show_hole:
            for hole in self.state.holes:
                self.drawPolygon(canvas, transform(hole.polygon.exterior, lambda p:p-(self.off_x, self.off_y)).coords, stroke=0xFFCF55 if hole is self.state.focus else 0xFF6E00)

        if self.state.show_conflicts:
            for conflict in self.state.conflicts:
                try:
                    if isinstance(conflict, Polygon):
                        coords = transform(conflict.exterior, lambda p:p-(self.off_x, self.off_y)).coords
                        self.drawPolygon(canvas, coords, fill=0xFF0000)
                    elif isinstance(conflict, LineString):
                        coords = transform(conflict, lambda p:p-(self.off_x, self.off_y)).coords
                        for i in range(1, len(coords)):
                            self.drawLine(canvas, coords[i-1][0], coords[i-1][1], coords[i][0], coords[i][1], color=0xFF0000)
                    elif isinstance(conflict, MultiPolygon):
                        for p in conflict.geoms:
                            coords = transform(p.exterior, lambda p:p-(self.off_x, self.off_y)).coords
                            self.drawPolygon(canvas, coords, fill=0xFF0000)
                    else:
                        print("Unhandled conflict type", conflict)
                except:
                    traceback.print_exc()

        if not self.mousehold or not self.mousemoved or not self.mouse_dragging:
            bites = self.state.bites
            vcuts = self.state.vcuts
            if self.state.show_mb:
                for line in bites:
                    self.drawMousebites(canvas, line)

            if self.state.show_vc:
                for line in vcuts:
                    p1 = line.coords[0]
                    p2 = line.coords[-1]

                    if p1[0]==p2[0]: # vertical
                        self.drawVCutV(canvas, p1[0])
                    elif p1[1]==p2[1]: # horizontal
                        self.drawVCutH(canvas, p1[1])

            if self.state.debug:
                for point, size in self.state.dbg_points:
                    x, y = self.toCanvas(point[0]-self.off_x, point[1]-self.off_y)
                    canvas.drawEllipse(x, y, size, size, stroke=0xFF0000)
                for rect in self.state.dbg_rects:
                    x1, y1 = self.toCanvas(rect[0]-self.off_x, rect[1]-self.off_y)
                    x2, y2 = self.toCanvas(rect[2]-self.off_x, rect[3]-self.off_y)
                    canvas.drawRect(x1, y1, x2, y2, stroke=0xFF0000)
                for polygon in self.state.dbg_polygons:
                    polygon = [self.toCanvas(p[0]-self.off_x, p[1]-self.off_y) for p in polygon]
                    canvas.drawPolygon(polygon, stroke=0xFF0000)
                for text in self.state.dbg_text:
                    x, y = self.toCanvas(text[0]-self.off_x, text[1]-self.off_y)
                    canvas.drawText(x, y, text[2])

        edit_polygon = self.state.edit_polygon
        if edit_polygon:
            edit_polygon = list(edit_polygon)
            if self.mousepos:
                edit_polygon.append(self.fromCanvas(*self.mousepos))
            self.drawPolyline(canvas, edit_polygon, color=0xFF6E00, width=1)

        drawCross = False

        if self.tool == Tool.HOLE:
            drawCross = True

        if self.tool == Tool.TAB:
            x, y = self.fromCanvas(*self.mousepos)
            p = Point(x+self.off_x, y+self.off_y)
            if self.state.focus.contains(p):
                shortest = None
                for shape in self.state.focus.shapes:
                    s = shapely.shortest_line(p, shape.exterior)
                    if shortest is None or s.length < shortest.length:
                        shortest = s
                if shortest:
                    t0 = shortest.coords[0]
                    t1 = shortest.coords[1]
                    x1, y1 = t0
                    x2, y2 = t1
                    x2, y2 = extrapolate(x1, y1, x2, y2, 1, self.state.spacing/2*self.unit)
                    x1, y1 = self.toCanvas(x1-self.off_x, y1-self.off_y)
                    x2, y2 = self.toCanvas(x2-self.off_x, y2-self.off_y)
                    canvas.drawEllipse(x2, y2, 3, 3, stroke=0xFF0000)
                    canvas.drawLine(x1, y1, x2, y2, color=0xFFFF00)

        if drawCross and self.mousepos:
            x, y = self.mousepos[0], self.mousepos[1]
            canvas.drawLine(x-10, y, x+10, y, color=0xFF0000)
            canvas.drawLine(x, y-10, x, y+10, color=0xFF0000)

    def netRenamer(self, n, orig):
        try:
            return self.state.netRenamePattern.format(n=n, orig=orig)
        except:
            return orig

    def refRenamer(self, n, orig):
        try:
            return self.state.refRenamePattern.format(n=n, orig=orig)
        except:
            return orig

    def content(self):
        title = f"KiKit UI v{VERSION} (KiCad {pcbnew.Version()}, KiKit {kikit.__version__}, Shapely {shapely.__version__}, PUI {PUI.__version__} {PUI_BACKEND})"
        with Window(size=(1300, 768), title=title, icon=resource_path("icon.ico")).keypress(self.keypress):
            with VBox():
                with HBox():
                    self.state.pcb
                    self.state.bites
                    self.state.vcuts
                    self.state.cut_method
                    self.state.mb_diameter
                    self.state.mb_spacing
                    (Canvas(self.painter)
                        .dblclick(self.dblclicked)
                        .mousedown(self.mousedown)
                        .mouseup(self.mouseup)
                        .mousemove(self.mousemove)
                        .wheel(self.wheel)
                        .layout(width=self.state.canvas_width, height=self.state.canvas_height)
                        .style(bgColor=0x000000))

                    with VBox().layout(weight=1):
                        with HBox():
                            Label("Panel")
                            Button("Load").click(self.load)
                            Button("Save").click(self.save)

                            Spacer()

                            Button("Export").click(self.build, export=True)

                        with HBox():
                            Label("Add")
                            Button("PCB").click(self.addPCB)
                            Button("Hole").click(self.addHole)
                            Spacer()

                        Label("Export Options")
                        with HBox():
                            Checkbox("Hide Out-of-Board References/Values", self.state("hide_outside_reference_value"))
                            Spacer()

                        Label("Display Options")
                        with HBox():
                            Checkbox("PCB", self.state("show_pcb"))
                            Checkbox("Hole", self.state("show_hole"))
                            Checkbox("Mousebites", self.state("show_mb"))
                            Checkbox("V-Cut", self.state("show_vc"))
                            Checkbox("Conflicts", self.state("show_conflicts")).click(self.build)
                            Spacer()
                            Checkbox("Debug", self.state("debug")).click(self.build)

                        Divider()

                        with HBox():
                            Label("Global Settings")
                            Spacer()
                            Label("Unit: mm")

                        with HBox():
                            Checkbox("Use Frame", self.state("use_frame")).click(self.build)
                            Checkbox("Tight", self.state("tight")).click(self.build)
                            Checkbox("Auto Tab", self.state("auto_tab")).click(self.build)
                            Label("Max Tab Spacing")
                            TextField(self.state("max_tab_spacing")).layout(width=50).change(self.build)
                            Spacer()

                        with HBox():
                            Label("Spacing")
                            TextField(self.state("spacing")).change(self.build)
                            Label("Tab Width")
                            TextField(self.state("tab_width")).change(self.build)

                        with HBox():
                            Label("Simulate Mill Fillets")
                            TextField(self.state("mill_fillets")).change(self.build)
                            Checkbox("Export Simulated Mill Fillets", self.state("export_mill_fillets"))

                        with HBox():
                            Label("Cut Method")
                            RadioButton("V-Cuts or Mousebites", "vc_or_mb", self.state("cut_method")).click(self.build)
                            RadioButton("V-Cuts and Mousebites", "vc_and_mb", self.state("cut_method")).click(self.build)
                            RadioButton("Mousebites", "mb", self.state("cut_method")).click(self.build)
                            # RadioButton("V-Cut", "vc", self.state("cut_method")).click(self.build)
                            RadioButton("None", "none", self.state("cut_method")).click(self.build)


                        with HBox():
                            Label("V-Cut Layer")
                            with ComboBox(editable=False, text_model=self.state("vc_layer")):
                                ComboBoxItem("Cmts.User")
                                ComboBoxItem("User.1")
                                ComboBoxItem("Edge.Cuts")

                            Checkbox("Merge V-Cuts within", self.state("merge_vcuts")).click(self.build)
                            TextField(self.state("merge_vcuts_threshold")).change(self.build)

                            Spacer()
                        with HBox():
                            Label("Mousebites")
                            Label("Spacing")
                            TextField(self.state("mb_spacing")).change(self.build)
                            Label("Diameter")
                            TextField(self.state("mb_diameter")).change(self.build)
                            Label("Offset")
                            TextField(self.state("mb_offset")).change(self.build)
                            Spacer()

                        if self.state.use_frame:
                            with HBox():
                                Label("Frame Size")
                                Label("Width")
                                TextField(self.state("frame_width")).change(self.build)
                                Label("Height")
                                TextField(self.state("frame_height")).change(self.build)

                            with HBox():
                                Label("Frame Width")
                                Label("Top")
                                TextField(self.state("frame_top")).change(self.build)
                                Label("Bottom")
                                TextField(self.state("frame_bottom")).change(self.build)
                                Label("Left")
                                TextField(self.state("frame_left")).change(self.build)
                                Label("Right")
                                TextField(self.state("frame_right")).change(self.build)

                        with HBox():
                            Label("Rename")
                            Label("Net")
                            TextField(self.state("netRenamePattern")).change(self.build)
                            Label("Ref")
                            TextField(self.state("refRenamePattern")).change(self.build)

                        with HBox():
                            Label("Align")
                            Button("⤒").click(self.align_top)
                            Button("⤓").click(self.align_bottom)
                            Button("⇤").click(self.align_left)
                            Button("⇥").click(self.align_right)

                        if self.state.pcb:
                            with Scroll().layout(weight=1):
                                with VBox():
                                    if isinstance(self.state.focus, PCB):
                                        with HBox():
                                            Label(f"Selected PCB: {self.state.pcb.index(self.state.focus)}. {self.state.focus.ident}")

                                            Spacer()

                                            Button("Duplicate").click(self.duplicate, self.state.focus)
                                            Button("Remove").click(self.remove, self.state.focus)

                                        with Grid():
                                            r = 0

                                            Label("Rotate").grid(row=r, column=0)
                                            with HBox().grid(row=r, column=1):
                                                Button("↺ (r)").click(self.rotateBy, 90)
                                                Button("↺ 15°").click(self.rotateBy, 15)
                                                TextField(self.state.focus("rotate")).change(self.build)
                                                Button("↻ 15°").click(self.rotateBy, -15)
                                                Button("↻ (R)").click(self.rotateBy, -90)
                                                Spacer()
                                            r += 1

                                            Label("Align").grid(row=r, column=0)
                                            with HBox().grid(row=r, column=1):
                                                Button("⤒").click(self.align_top, pcb=self.state.focus)
                                                Button("⤓").click(self.align_bottom, pcb=self.state.focus)
                                                Button("⇤").click(self.align_left, pcb=self.state.focus)
                                                Button("⇥").click(self.align_right, pcb=self.state.focus)
                                                Spacer()
                                            r += 1

                                            Label("Tabs").grid(row=r, column=0)
                                            with HBox().grid(row=r, column=1):
                                                Button("Add").click(self.add_tab)
                                                if not self.state.focus.tabs():
                                                    Checkbox("Disable auto tab", self.state.focus("disable_auto_tab")).click(self.build)
                                                Spacer()
                                            r += 1

                                            for i, tab in enumerate(self.state.focus.tabs()):
                                                Label(f"Tab {i+1}").grid(row=r, column=0)
                                                with HBox().grid(row=r, column=1):
                                                    Button("Highlight").click(self.highlight_tab, i)
                                                    Button("Remove").click(self.remove_tab, i)
                                                    Spacer()
                                                r += 1

                                    elif self.state.focus:
                                        with HBox():
                                            Label("Selected Hole")

                                            Spacer()

                                            Button("Remove").click(self.remove, self.state.focus)

                                    Spacer()
                        else:
                            Spacer()

                        Label(f"Conflicts: {len(self.state.conflicts)}")

wx_app = wx.App()

ui = UI()

inputs = sys.argv[1:]
if inputs:
    if inputs[0].endswith(PNL_SUFFIX):
        ui.load(None, inputs[0])
        if len(inputs) > 1:
            ui.build(export=inputs[1])
            sys.exit(0)
        else:
            ui.autoScale()
            ui.build()
    else:
        for boardfile in inputs:
            if boardfile.endswith(PCB_SUFFIX):
                ui._addPCB(PCB(boardfile))

        ui.autoScale()
        ui.build()
ui.run()
