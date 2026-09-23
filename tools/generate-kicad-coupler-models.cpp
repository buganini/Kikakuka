// Generate the prebuilt KiCad coupler helper STEP files.
//
// This is a maintainer utility, not a plugin runtime dependency.  It uses
// Open CASCADE so the shipped plugin and library remain dependency-free.

#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepBuilderAPI_MakeSolid.hxx>
#include <BRepBuilderAPI_Sewing.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <Quantity_Color.hxx>
#include <STEPCAFControl_Writer.hxx>
#include <TDataStd_Name.hxx>
#include <TDocStd_Document.hxx>
#include <XCAFDoc_ColorTool.hxx>
#include <XCAFDoc_DocumentTool.hxx>
#include <XCAFDoc_ShapeTool.hxx>
#include <gp_Pnt.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Solid.hxx>

#include <algorithm>
#include <filesystem>
#include <iostream>
#include <string>
#include <vector>

namespace {

constexpr double BASE_HALF_SLOPE = 0.075;

struct Point2D {
    double x;
    double y;
};

TopoDS_Face polygon_face(std::vector<gp_Pnt> points, bool reverse = false) {
    BRepBuilderAPI_MakePolygon polygon;
    std::vector<gp_Pnt> unique;
    for (const gp_Pnt& point : points) {
        if (unique.empty() || unique.back().Distance(point) > 1e-9)
            unique.push_back(point);
    }
    if (unique.size() > 1 && unique.front().Distance(unique.back()) <= 1e-9)
        unique.pop_back();
    if (reverse)
        std::reverse(unique.begin(), unique.end());
    for (const gp_Pnt& point : unique)
        polygon.Add(point);
    polygon.Close();
    return BRepBuilderAPI_MakeFace(polygon.Wire());
}

TopoDS_Shape faceted_solid(
    const std::vector<gp_Pnt>& bottom,
    const std::vector<gp_Pnt>& top
) {
    BRepBuilderAPI_Sewing sewing(1e-7);
    sewing.Add(polygon_face(bottom, true));
    sewing.Add(polygon_face(top));
    for (std::size_t index = 0; index < bottom.size(); ++index) {
        const std::size_t next = (index + 1) % bottom.size();
        sewing.Add(polygon_face({
            bottom[index], bottom[next], top[next], top[index]
        }));
    }
    sewing.Perform();
    BRepBuilderAPI_MakeSolid solid(TopoDS::Shell(sewing.SewedShape()));
    return solid.Solid();
}

TopoDS_Shape symmetric_wedge_polygon(const std::vector<Point2D>& outline) {
    std::vector<gp_Pnt> bottom;
    std::vector<gp_Pnt> top;
    for (const Point2D& point : outline) {
        const double half_thickness = -point.y * BASE_HALF_SLOPE;
        bottom.emplace_back(point.x, point.y, -half_thickness);
        top.emplace_back(point.x, point.y, half_thickness);
    }
    return faceted_solid(bottom, top);
}

TopoDS_Shape triangular_wedge() {
    return symmetric_wedge_polygon(
        {{-1.0, -1.0}, {1.0, -1.0}, {0.0, 0.0}});
}

void add_colored_shape(
    const Handle(XCAFDoc_ShapeTool)& shape_tool,
    const Handle(XCAFDoc_ColorTool)& color_tool,
    const TopoDS_Shape& shape,
    const char* name,
    const Quantity_Color& color
) {
    TDF_Label label = shape_tool->AddShape(shape, Standard_False);
    TDataStd_Name::Set(label, name);
    color_tool->SetColor(label, color, XCAFDoc_ColorGen);
    color_tool->SetColor(label, color, XCAFDoc_ColorSurf);
}

bool write_model(const std::filesystem::path& filename, bool moving) {
    Handle(TDocStd_Document) document =
        new TDocStd_Document("MDTV-XCAF");
    Handle(XCAFDoc_ShapeTool) shape_tool =
        XCAFDoc_DocumentTool::ShapeTool(document->Main());
    Handle(XCAFDoc_ColorTool) color_tool =
        XCAFDoc_DocumentTool::ColorTool(document->Main());

    const Quantity_Color cyan(0.1, 0.8, 1.0, Quantity_TOC_RGB);
    const Quantity_Color orange(1.0, 0.4, 0.1, Quantity_TOC_RGB);
    add_colored_shape(
        shape_tool, color_tool, triangular_wedge(),
        moving ? "CouplerMoving" : "CouplerFixed",
        moving ? cyan : orange);

    STEPCAFControl_Writer writer;
    writer.SetColorMode(Standard_True);
    writer.SetNameMode(Standard_True);
    if (!writer.Transfer(document, STEPControl_AsIs)) {
        std::cerr << "Unable to transfer " << filename << "\n";
        return false;
    }
    if (writer.Write(filename.string().c_str()) != IFSelect_RetDone) {
        std::cerr << "Unable to write " << filename << "\n";
        return false;
    }
    return true;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "usage: " << argv[0] << " OUTPUT_DIRECTORY\n";
        return 2;
    }
    const std::filesystem::path output(argv[1]);
    std::filesystem::create_directories(output);
    if (!write_model(output / "coupler-fixed.step", false)
        || !write_model(output / "coupler-moving.step", true)) {
        return 1;
    }
    return 0;
}
