# FreekiCAD maintainer notes

## Icon variants

FreekiCAD intentionally has two SVG icon variants. They share the same artwork,
glyph geometry, and colors, but use different canvas padding for different
hosts.

### FreeCAD addon and workbench icon

`freecad/FreekiCAD/resources/icons/FreekiCAD.svg` is the canonical FreeCAD
icon. It follows the FreeCAD artwork guideline:

- a 64 x 64 px SVG document;
- a 60 x 60 px visible area;
- 2 px of transparent padding on every side.

This variant is referenced by `package.xml` for the Addon Manager and by
`freecad/FreekiCAD/init_gui.py` for the workbench selector. Do not reduce its
visible area to match Kikakuka's icons. FreeCAD's official icon guidance is
documented in the
[FreeCAD artwork guidelines](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Artwork_Guidelines.md).

`freecad/FreekiCAD/resources/icons/FreekiCAD.png` is the 128 x 128 raster
rendering of this variant.

### README and KiCad plugin icon

`readme-icon.svg` uses Kikakuka's standard padding. Its artwork occupies the
408 x 408 area from `(52, 52)` to `(460, 460)` on a 512 x 512 canvas. At a
64 px display size, its rasterized visible bounds match the other three icons
at approximately 52 x 52 px.

This variant is used by:

- the repository root `README.md`;
- this directory's `README.md`;
- the KiCad plugin's **Open in FreeCAD** action.

The KiCad plugin images are generated at 24 x 24 and 48 x 48. Their visible
bounds are 20 x 20 and 40 x 40 respectively, matching the base Kikakuka plugin
icon.

## Editing and regeneration

Keep the artwork in both SVG files synchronized. Only the document size and
outer padding transform should differ. In particular, preserve the quadrant
colors, the upper-right `F`, and all glyph sizes and offsets across both files.

After changing the artwork, regenerate the canonical FreeCAD PNG:

```sh
rsvg-convert --width 128 --height 128 \
  FreekiCAD/freecad/FreekiCAD/resources/icons/FreekiCAD.svg \
  --output FreekiCAD/freecad/FreekiCAD/resources/icons/FreekiCAD.png
```

Then regenerate the KiCad plugin icons:

```sh
tools/generate-kicad-plugin-icons.sh
```

The generation script deliberately uses `FreekiCAD/readme-icon.svg`, not the
FreeCAD addon variant.
