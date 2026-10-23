#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""Draws the logo: three little boxy agents around one conversation, pixel
art on a dark rounded tile.

    python3 icon/make_logo.py        # writes icon/logo.svg
    inkscape icon/logo.svg --export-type=png --export-filename=icon/logo.png -w 512

Only the standard library.
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent
SIZE = 512
CELL = 12                          # px per pixel-art cell
ORIGIN = (SIZE - 42 * CELL) / 2    # a 42 x 42 grid, centred on the tile

TILE = "#11142B"
INK = "#E8ECF4"
CRITTER = "#D97757"


def rect(x, y, w, h, color):
    return f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" fill="{color}"/>'


def cells(c, r, color, w=1, h=1):
    """w x h cells of the grid, from column c, row r."""
    return rect(ORIGIN + c * CELL, ORIGIN + r * CELL, w * CELL, h * CELL, color)


def critter(gc, gr, look=0):
    """A critter whose 10 x 6 body starts at grid column gc, row gr: side
    nubs, four legs, two eyes looking left (-1), ahead (0) or right (1)."""
    out = [cells(gc, gr, CRITTER, 10, 6),
           cells(gc - 1, gr + 2, CRITTER, 1, 2), cells(gc + 10, gr + 2, CRITTER, 1, 2)]
    out += [cells(gc + c, gr + 6, CRITTER) for c in (1, 3, 6, 8)]
    out += [cells(gc + c + look, gr + 1, TILE, 1, 2) for c in (2, 7)]
    return out


def logo():
    out = [f'<rect width="{SIZE}" height="{SIZE}" rx="112" fill="{TILE}"/>']
    # A speech bubble in the middle, three dots in it, its tail towards the
    # critter at the bottom left, who's talking.
    bc, br, bw, bh = 15, 17, 12, 7
    out += [cells(bc + 1, br, INK, bw - 2, bh), cells(bc, br + 1, INK, bw, bh - 2),
            cells(bc + 1, br + bh, INK, 2, 1), cells(bc, br + bh + 1, INK)]
    out += [cells(bc + 2 + i * 3, br + 3, CRITTER, 2, 1) for i in range(3)]
    # Three critters around it, each looking at it.
    for (gc, gr), look in (((16, 8), 0), ((5, 26), 1), ((27, 26), -1)):
        out += critter(gc, gr, look)
    return out


def main():
    body = "\n".join(logo())
    (HERE / "logo.svg").write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}" '
                                   f'viewBox="0 0 {SIZE} {SIZE}">\n{body}\n</svg>\n')
    print("wrote icon/logo.svg")


if __name__ == "__main__":
    main()
