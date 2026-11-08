#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Render the Meldra mark (broken ring, teal and orange ribbons merging into one stem)
as truecolor half-block art for fastfetch (`"type": "file-raw"`).

    render_fastfetch_logo.py [--radius 13] [--output assets/fastfetch/meldra-logo.ansi] [--plain]

Geometry follows assets/wallpapers/meldra-mark.png. Each terminal cell holds two
square sub-pixels (upper and lower half block), so the ring stays round.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

TEAL = ((72, 156, 160), (134, 220, 214))      # tail -> stem
ORANGE = ((226, 134, 96), (248, 186, 138))
RING = (156, 152, 204)
SS = 4  # supersampling per sub-pixel axis


def bezier(p, t):
    u = 1 - t
    return tuple(u**3*p[0][i] + 3*u*u*t*p[1][i] + 3*u*t*t*p[2][i] + t**3*p[3][i] for i in range(2))


def ribbon_curve(sign):
    return [(sign*0.075, -0.25), (sign*0.075, 0.15), (sign*0.62, 0.30), (sign*1.38, 0.74)]


def ring_gap(angle):
    a = angle % 360
    return 255 <= a <= 285 or 127 <= a <= 165 or 15 <= a <= 53


def sample(x, y, curves):
    """Return (kind, t) for the strongest thing under point (x, y), in units of the ring radius."""
    half = 0.088  # ribbon half-width
    for sign, name in ((-1, "teal"), (1, "orange")):
        # vertical stem
        if abs(x - sign*0.075) <= half and -0.88 <= y <= -0.25:
            return name, 1.0
        best = None
        for t, (cx, cy) in curves[sign]:
            if (x-cx)**2 + (y-cy)**2 <= half*half and (best is None or t > best):
                best = t
        if best is not None:
            return name, 1.0 - best
    r = math.hypot(x, y)
    if abs(r - 1.0) <= 0.035 and not ring_gap(math.degrees(math.atan2(y, x))):
        return "ring", 0.0
    return None, 0.0


def mix(a, b, f):
    return tuple(round(a[i] + (b[i]-a[i])*f) for i in range(3))


def render(radius: int):
    curves = {s: [(i/240, bezier(ribbon_curve(s), i/240)) for i in range(241)] for s in (-1, 1)}
    cols = round(3.1 * radius) + 2
    rows_sub = round(1.95 * radius) + 2
    rows_sub += rows_sub % 2
    cx, cy0 = cols / 2, 0.97 * radius + 1
    cells = []
    for sy in range(rows_sub):
        row = []
        for sx in range(cols):
            hits = {}
            for oy in range(SS):
                for ox in range(SS):
                    px = (sx + (ox + .5)/SS - cx) / radius
                    py = (sy + (oy + .5)/SS - cy0) / radius
                    kind, t = sample(px, py, curves)
                    if kind:
                        k = hits.setdefault(kind, [0, 0.0])
                        k[0] += 1
                        k[1] += t
            total = sum(v[0] for v in hits.values())
            if total < SS*SS*0.34:
                row.append(None)
                continue
            kind = max(hits, key=lambda k: hits[k][0])
            t = hits[kind][1] / hits[kind][0]
            if kind == "ring":
                color = RING
            else:
                lo, hi = TEAL if kind == "teal" else ORANGE
                color = mix(lo, hi, max(0.0, min(1.0, t)))
            row.append(color)
        cells.append(row)
    return cols, rows_sub // 2, cells


def render_square(size: int):
    """A solid rounded square with the mark carved out of it: the stem enters at the top and
    splits into two arms that leave through the lower corners. Left piece teal, right piece
    orange, the piece between the arms lavender."""
    cols, rows_sub = size, size          # square sub-pixels: size columns x size half-rows
    rad, gap = 0.20, 0.085
    arm = []   # (y, x) points along the right arm's centre line, for "inside the V" tests
    for i in range(241):
        t = i / 240
        arm.append((2*(1-t)*t*0.30 + (1-t)**2*-0.16 + t*t*1.15, t*t*1.25))

    def arm_x(v):
        return min(arm, key=lambda a: abs(a[0] - v))[1]

    cells = []
    for sy in range(rows_sub):
        row = []
        for sx in range(cols):
            hit = 0
            colr = (0, 0, 0)
            n = 0
            for oy in range(SS):
                for ox in range(SS):
                    u = (sx + (ox + .5)/SS) / cols * 2 - 1
                    v = (sy + (oy + .5)/SS) / rows_sub * 2 - 1
                    # rounded square
                    qx, qy = max(abs(u) - (1 - rad), 0), max(abs(v) - (1 - rad), 0)
                    if qx*qx + qy*qy > rad*rad:
                        continue
                    # carve: stem from the top edge, then two arms (quadratic curves)
                    carved = abs(u) <= gap and v <= -0.12
                    if not carved and v > -0.2:
                        for sign in (-1, 1):
                            for i in range(61):
                                t = i / 60
                                # control point keeps the arm vertical at first, then sweeping out
                                px = (1-t)**2*0 + 2*(1-t)*t*0 + t*t*sign*1.25
                                py = (1-t)**2*-0.16 + 2*(1-t)*t*0.30 + t*t*1.15
                                if (u-px)**2 + (v-py)**2 <= gap*gap*1.2:
                                    carved = True
                                    break
                            if carved:
                                break
                    if carved:
                        continue
                    # which piece, and a soft vertical shade
                    arm_u = 0.0
                    below = v > -0.1 and abs(u) < arm_x(v)
                    shade = (v + 1) / 2
                    if below:
                        base = mix((132, 128, 190), (176, 172, 224), 1 - shade)
                    elif u < 0:
                        base = mix(TEAL[1], TEAL[0], shade)
                    else:
                        base = mix(ORANGE[1], ORANGE[0], shade)
                    colr = tuple(colr[i] + base[i] for i in range(3))
                    n += 1
            if n < SS*SS*0.45:
                row.append(None)
            else:
                row.append(tuple(round(c / n) for c in colr))
        cells.append(row)
    return cols, rows_sub // 2, cells


def to_ansi(cols, rows, cells, plain=False):
    lines = []
    for r in range(rows):
        out = []
        for c in range(cols):
            up, dn = cells[2*r][c], cells[2*r+1][c]
            if plain:
                out.append({(0, 0): " ", (1, 0): "▀", (0, 1): "▄", (1, 1): "█"}[(up is not None, dn is not None)])
                continue
            if up is None and dn is None:
                out.append(" ")
            elif up is not None and dn is not None:
                if up == dn:
                    out.append("\x1b[38;2;%d;%d;%dm█" % up)
                else:  # two colours in one cell: upper half block, lower as background
                    out.append("\x1b[38;2;%d;%d;%d;48;2;%d;%d;%dm▀\x1b[49m" % (*up, *dn))
            elif up is not None:
                out.append("\x1b[38;2;%d;%d;%dm▀" % up)
            else:
                out.append("\x1b[38;2;%d;%d;%dm▄" % dn)
        lines.append("".join(out).rstrip() + ("" if plain else "\x1b[0m"))
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--style", choices=("square", "mark"), default="square")
    ap.add_argument("--size", type=int, default=30, help="square style: width in columns")
    ap.add_argument("--radius", type=int, default=13)
    ap.add_argument("--output", type=Path, default=Path("assets/fastfetch/meldra-logo.ansi"))
    ap.add_argument("--plain", action="store_true", help="print the shape without colour and exit")
    args = ap.parse_args()
    cols, rows, cells = render_square(args.size) if args.style == "square" else render(args.radius)
    if args.plain:
        print(to_ansi(cols, rows, cells, plain=True), end="")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(to_ansi(cols, rows, cells), encoding="utf-8")
    print(f"{args.output}: {cols} columns x {rows} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
