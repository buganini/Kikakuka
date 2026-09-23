#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
plugin_dir="$repo_dir/kicad-plugin/plugins"
overlay_dir="$repo_dir/tools/kicad-plugin-icons"

command -v magick >/dev/null 2>&1 || {
    echo "ImageMagick (magick) is required" >&2
    exit 1
}
command -v rsvg-convert >/dev/null 2>&1 || {
    echo "librsvg (rsvg-convert) is required" >&2
    exit 1
}

render_icon() {
    name=$1
    overlay=$2
    size=$3
    temporary="${TMPDIR:-/tmp}/kikakuka-${name}-${size}-overlay.png"

    rsvg-convert --width "$size" --height "$size" \
        "$overlay_dir/$overlay.svg" > "$temporary"
    magick "$plugin_dir/kikakuka-${size}.png" "$temporary" \
        -compose Over -composite "$plugin_dir/${name}-${size}.png"
    rm -f "$temporary"
}

for size in 24 48; do
    render_icon placeholder cube "$size"
    render_icon coupler-3d-viewer eye "$size"
    render_icon disable-coupler-helpers eye-off "$size"
done
