# Resource maintainer notes

## Icons

The SVG file is the canonical source for each icon in this directory. PNG and
ICO files are generated artifacts and should be regenerated whenever the
matching SVG changes.

Keep icon artwork on a 512 x 512 canvas, preserve transparency and the icon's
intended outer padding, and use paths rather than depending on locally
installed fonts.

Generate the 512 x 512 PNG with Inkscape CLI, replacing `NAME` with the icon's
base filename:

```sh
inkscape resources/NAME.svg \
  --export-filename=resources/NAME.png \
  --export-width=512 \
  --export-height=512 \
  --export-overwrite
```

Generate the Windows icon from that PNG with ImageMagick:

```sh
magick resources/NAME.png \
  -define icon:auto-resize=256,128,64,48,32,16 \
  resources/NAME.ico
```

Inspect both the 512 px PNG and a 48 px rendering after regeneration. Fine
details that work at source size may become unclear at application-icon size.

Only the main Kikakuka application icon needs `icon.icns`; tool-specific icons
use SVG, PNG, and ICO.
