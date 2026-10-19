#!/usr/bin/env python3
"""Regenerate the panel icons from the vendors' own artwork.

Not shipped with the extension — a one-off dev tool so the two PNGs in
icons/ can be rebuilt instead of being mystery binaries. Needs Pillow and
network access (the extension itself needs neither).

    python3 tools/make-icons.py

Naming: the files end in `.symbolic.png` on purpose. GNOME Shell decides
whether to tint an icon by testing the file URI
(`icon_uri_is_symbolic`, src/st/st-icon-theme.c) and `.symbolic.png` is one
of the accepted suffixes. Because of that the shell paints both marks with
the panel's foreground colour, which is white on a dark panel and dark on a
light one — so no colour needs hardcoding here.

Content: black pixels with an alpha mask, *not* white pixels. In
`color_symbolic_pixbuf()` a pixel that is pure black with alpha > 0 is
replaced by the foreground colour, whereas any other colour is blended
against the success/warning/error palette. White artwork would render as
garbage, so `assert_symbolic_safe()` below fails loudly if that regresses.
"""

import io
import os
import urllib.request

from PIL import Image

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
SIZE = 48  # panel icons are 16px; 48 keeps them crisp on HiDPI

# OpenAI's CDN serves the blossom as an .ico: already black on transparency,
# which is exactly the shape a symbolic icon wants.
CODEX_URL = "https://cdn.oaistatic.com/assets/favicon-eex17e9e.ico"
# OpenCode's official mark is a light glyph on a near-black tile, so the tile
# has to go and the glyph becomes the mask.
OPENCODE_URL = "https://opencode.ai/apple-touch-icon.png"

ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                     "harness-usage@dchadwick.local", "icons")


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=30) as response:
        return Image.open(io.BytesIO(response.read()))


def as_black_with_alpha(mask, size):
    """Shape comes from `mask`; colour is forced black so the shell tints it."""
    mask = mask.convert("L")
    out = Image.new("RGBA", mask.size, (0, 0, 0, 255))
    out.putalpha(mask)
    return out.resize((size, size), Image.LANCZOS)


def flatten_to_mask(image, low=30, high=225):
    """Luminance becomes alpha, so a light-on-dark mark keeps just its glyph."""
    rgba = image.convert("RGBA")
    return rgba.convert("L").point(
        lambda v: 0 if v < low
        else (255 if v > high
              else int((v - low) * 255 / (high - low)))
    )


def assert_symbolic_safe(path):
    """Guard the invariant the shell depends on: black pixels + real alpha."""
    with Image.open(path) as im:
        rgba = im.convert("RGBA")
        pixels = rgba.get_flattened_data() if hasattr(rgba, "get_flattened_data") \
            else list(rgba.getdata())
        visible = [p for p in pixels if p[3] > 40]
        assert visible, f"{path}: nothing visible"
        colours = {(r, g, b) for r, g, b, _ in visible}
        assert colours == {(0, 0, 0)}, f"{path}: non-black pixels {colours} — shell will not tint"
        coverage = len(visible) / (rgba.width * rgba.height)
        assert 0.05 < coverage < 0.85, f"{path}: implausible coverage {coverage:.0%}"


def main():
    os.makedirs(ICONS, exist_ok=True)
    written = []

    # Already black-on-transparent upstream; just take its alpha as the mask.
    codex = download(CODEX_URL).convert("RGBA")
    path = os.path.join(ICONS, "codex.symbolic.png")
    as_black_with_alpha(codex.getchannel("A"), SIZE).save(path)
    written.append(path)

    # Light glyph on a dark tile: luminance becomes the mask.
    opencode = download(OPENCODE_URL)
    path = os.path.join(ICONS, "opencode.symbolic.png")
    as_black_with_alpha(flatten_to_mask(opencode), SIZE).save(path)
    written.append(path)

    for path in written:
        assert_symbolic_safe(path)
        with Image.open(path) as check:
            print(f"wrote {os.path.basename(path)} ({check.size[0]}x{check.size[1]}, "
                  f"black+alpha, symbolic)")


if __name__ == "__main__":
    main()
