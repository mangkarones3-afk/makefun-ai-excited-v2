#!/usr/bin/env python3
"""PHOENIX IN THE GALLERY — make.fun ai-excited-01 entry.

A golden phoenix bursts out of a cracked, gilt-framed oil painting
hanging in a dark museum gallery. Its wings tear past the frame on
three sides, painted plumage becomes real feathers, torn canvas flaps
hang from the holes it ripped, and embers drift in the spotlight.

The whole picture is drawn by this program: pure Python standard
library, no image assets, no external files. Rendering happens on a
float framebuffer at double resolution and is averaged down at the
end, so every edge is anti-aliased. All randomness flows from the
seeded generator below (never the `random` module, never the clock),
so `python3 main.py` produces a byte-identical entry.png every run.
"""

import math
import os
import struct
import sys
import zlib
from array import array

SIDE = 1024                 # final image is SIDE x SIDE
SS = 2                      # supersample factor
RW = SIDE * SS              # working (render) width
RN = RW * RW                # working pixel count
MASK64 = 0xFFFFFFFFFFFFFFFF


# ---------------------------------------------------------------------------
# deterministic randomness
# ---------------------------------------------------------------------------

class Rng:
    """SplitMix64: a small, fast, fully deterministic PRNG."""

    def __init__(self, seed):
        self.state = (seed + 0x9E3779B97F4A7C15) & MASK64

    def _next(self):
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return z ^ (z >> 31)

    def f(self, lo=0.0, hi=1.0):
        """Uniform float in [lo, hi)."""
        return lo + (hi - lo) * (self._next() / 18446744073709551616.0)

    def pick(self, seq):
        return seq[self._next() % len(seq)]


def hash2(a, b):
    """Integer lattice hash -> [0, 1). Stable across runs and versions."""
    h = (int(a) * 0xA24BAED4963EE407) ^ (int(b) * 0x9FB21C651E98DF25)
    h &= MASK64
    h ^= h >> 29
    h = (h * 0xBF58476D1CE4E5B9) & MASK64
    h ^= h >> 32
    return h / 18446744073709551616.0


# ---------------------------------------------------------------------------
# colours: float triples in 0..1
# ---------------------------------------------------------------------------

def rgb(r, g, b):
    return (r / 255.0, g / 255.0, b / 255.0)


def lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t)


def shade(c, f):
    return (c[0] * f, c[1] * f, c[2] * f)


def ramp(stops):
    """t -> colour through [(t, colour), ...] stops."""
    def fn(t):
        if t <= stops[0][0]:
            return stops[0][1]
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t <= t1:
                return lerp(c0, c1, (t - t0) / (t1 - t0))
        return stops[-1][1]
    return fn


def vshader(c_top, c_bot, y0, y1):
    """A fill shader colouring by vertical position between y0 and y1."""
    span = max(1.0, y1 - y0)

    def fn(x, y):
        t = (y - y0) / span
        return lerp(c_top, c_bot, 0.0 if t < 0.0 else (1.0 if t > 1.0 else t))
    return fn


# ---------------------------------------------------------------------------
# the canvas: three float planes at working resolution.
# All drawing primitives take design coordinates (final-image units);
# the SS scaling happens inside.
# ---------------------------------------------------------------------------

class Canvas:
    def __init__(self):
        self.r = array("f", bytes(4 * RN))
        self.g = array("f", bytes(4 * RN))
        self.b = array("f", bytes(4 * RN))

    # -- row spans in working pixels, x1 inclusive ---------------------
    def _span(self, y, x0, x1, c):
        if y < 0 or y >= RW:
            return
        x0 = max(0, x0)
        x1 = min(RW - 1, x1)
        if x1 < x0:
            return
        o = y * RW + x0
        n = x1 - x0 + 1
        self.r[o:o + n] = array("f", [c[0]]) * n
        self.g[o:o + n] = array("f", [c[1]]) * n
        self.b[o:o + n] = array("f", [c[2]]) * n

    def _span_fx(self, y, x0, x1, c, f, add):
        """Blend toward c with weight f, or add c*f, over a span."""
        if y < 0 or y >= RW or f <= 0.0:
            return
        x0 = max(0, x0)
        x1 = min(RW - 1, x1)
        if x1 < x0:
            return
        o = y * RW
        R, G, B = self.r, self.g, self.b
        if add:
            ar, ag, ab = c[0] * f, c[1] * f, c[2] * f
            for x in range(x0, x1 + 1):
                i = o + x
                R[i] += ar
                G[i] += ag
                B[i] += ab
        else:
            ia = 1.0 - f
            ar, ag, ab = c[0] * f, c[1] * f, c[2] * f
            for x in range(x0, x1 + 1):
                i = o + x
                R[i] = R[i] * ia + ar
                G[i] = G[i] * ia + ag
                B[i] = B[i] * ia + ab

    def _px(self, x, y, c, f, add):
        i = y * RW + x
        if add:
            self.r[i] += c[0] * f
            self.g[i] += c[1] * f
            self.b[i] += c[2] * f
        else:
            ia = 1.0 - f
            self.r[i] = self.r[i] * ia + c[0] * f
            self.g[i] = self.g[i] * ia + c[1] * f
            self.b[i] = self.b[i] * ia + c[2] * f

    # -- design-space primitives ---------------------------------------
    def vrect(self, x0, y0, x1, y1, fn):
        """Fill a rectangle row by row; fn(t) colours each row,
        t running 0 at y0 to 1 at y1."""
        ya = max(0, int(y0 * SS))
        yb = min(RW - 1, int(y1 * SS))
        xa, xb = int(x0 * SS), int(x1 * SS)
        span = max(1.0, (y1 - y0) * SS)
        for y in range(ya, yb + 1):
            self._span(y, xa, xb, fn((y - y0 * SS) / span))

    def disc(self, cx, cy, r, c):
        cx, cy, r = cx * SS, cy * SS, r * SS
        r2 = r * r
        for y in range(max(0, int(cy - r)), min(RW - 1, int(cy + r)) + 1):
            dy = y - cy
            rem = r2 - dy * dy
            if rem < 0:
                continue
            dx = int(math.sqrt(rem))
            self._span(y, int(cx - dx), int(cx + dx), c)

    def disc_a(self, cx, cy, r, c, a):
        if a <= 0.0:
            return
        cx, cy, r = cx * SS, cy * SS, r * SS
        r2 = r * r
        for y in range(max(0, int(cy - r)), min(RW - 1, int(cy + r)) + 1):
            dy = y - cy
            for x in range(max(0, int(cx - r)), min(RW - 1, int(cx + r)) + 1):
                dx = x - cx
                if dx * dx + dy * dy <= r2:
                    self._px(x, y, c, a, False)

    def glow(self, cx, cy, r, c, peak, mode="add"):
        """Soft radial light/shade with squared falloff, evaluated in
        short row chunks so big radii stay cheap. mode 'add' adds
        light; mode 'mix' blends toward the colour."""
        if peak <= 0.0:
            return
        add = (mode == "add")
        cx, cy, r = cx * SS, cy * SS, r * SS
        r2 = r * r
        inv = 1.0 / r2
        chunk = 14
        for y in range(max(0, int(cy - r)), min(RW - 1, int(cy + r)) + 1):
            dy = y - cy
            dy2 = dy * dy
            if dy2 >= r2:
                continue
            half = math.sqrt(r2 - dy2)
            xa, xb = int(cx - half), int(cx + half)
            x = xa
            while x <= xb:
                xe = min(xb, x + chunk - 1)
                mx = (x + xe) * 0.5 - cx
                d2 = mx * mx + dy2
                if d2 < r2:
                    f = 1.0 - d2 * inv
                    self._span_fx(y, x, xe, c, f * f * peak, add)
                x = xe + 1

    def line(self, x0, y0, x1, y1, w, c, a=1.0):
        """Round-capped stroke: small discs stamped along the segment."""
        dist = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(dist / max(0.3, w * 0.35)))
        r = w * 0.5
        for i in range(n + 1):
            t = i / n
            x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            if a >= 1.0:
                self.disc(x, y, r, c)
            else:
                self.disc_a(x, y, r, c, a)

    def pline(self, pts, w, c, a=1.0):
        for p, q in zip(pts, pts[1:]):
            self.line(p[0], p[1], q[0], q[1], w, c, a)

    def _edges(self, pts):
        """Yield (y, xa, xb) working-pixel spans of a design polygon."""
        P = [(x * SS, y * SS) for (x, y) in pts]
        ys = [p[1] for p in P]
        y0 = max(0, int(math.floor(min(ys))))
        y1 = min(RW - 1, int(math.ceil(max(ys))))
        n = len(P)
        for y in range(y0, y1 + 1):
            yc = y + 0.5
            nodes = []
            for i in range(n):
                ax, ay = P[i]
                bx, by = P[(i + 1) % n]
                if (ay <= yc < by) or (by <= yc < ay):
                    nodes.append(ax + (yc - ay) * (bx - ax) / (by - ay))
            if len(nodes) < 2:
                continue
            nodes.sort()
            for i in range(0, len(nodes) - 1, 2):
                xa = int(math.ceil(nodes[i] - 0.5))
                xb = int(math.floor(nodes[i + 1] - 0.5))
                if xb >= xa:
                    yield y, xa, xb

    def poly(self, pts, shader):
        """Polygon fill; shader(x, y) in design coords -> colour.
        Spans are coloured in chunks so 2D gradients stay smooth."""
        for (y, xa, xb) in self._edges(pts):
            x = xa
            while x <= xb:
                xe = min(xb, x + 23)
                c = shader((x + xe) * 0.5 / SS, y / SS)
                self._span(y, x, xe, c)
                x = xe + 1

    def poly_a(self, pts, c, a):
        """Polygon blended at constant alpha (shadows, light shapes)."""
        for (y, xa, xb) in self._edges(pts):
            self._span_fx(y, xa, xb, c, a, False)


CV = Canvas()


# ---------------------------------------------------------------------------
# small geometry helpers
# ---------------------------------------------------------------------------

def qbez(p0, p1, p2, t):
    mt = 1.0 - t
    return (mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
            mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1])


def catmull_closed(pts, subdiv=4):
    """Smooth a closed outline through its control points."""
    n = len(pts)
    out = []
    for i in range(n):
        p0 = pts[(i - 1) % n]
        p1 = pts[i]
        p2 = pts[(i + 1) % n]
        p3 = pts[(i + 2) % n]
        for j in range(subdiv):
            t = j / subdiv
            t2, t3 = t * t, t * t * t
            out.append((
                0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t
                       + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                       + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3),
                0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t
                       + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                       + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)))
    return out


def clip_seg(x0, y0, x1, y1, box):
    """Clip a segment to an axis-aligned box (Liang-Barsky).
    Returns the clipped endpoints, or None if fully outside."""
    xmin, ymin, xmax, ymax = box
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for (p, q) in ((-dx, x0 - xmin), (dx, xmax - x0),
                   (-dy, y0 - ymin), (dy, ymax - y0)):
        if abs(p) < 1e-9:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    return (x0 + dx * t0, y0 + dy * t0, x0 + dx * t1, y0 + dy * t1)


# ---------------------------------------------------------------------------
# palette and layout
# ---------------------------------------------------------------------------

WALL_FN = ramp([
    (0.00, rgb(27, 27, 24)), (0.45, rgb(35, 33, 28)),
    (0.80, rgb(24, 23, 20)), (1.00, rgb(13, 12, 12)),
])
FLOOR_Y = 884.0

FRAME = (204.0, 106.0, 820.0, 802.0)        # outer gilt frame, x0 y0 x1 y1
CANVAS_R = (252.0, 154.0, 772.0, 754.0)     # the painting itself
GILT_D = rgb(96, 66, 20)
GILT_M = rgb(172, 126, 46)
GILT_B = rgb(238, 190, 96)
GILT_H = rgb(255, 228, 156)

PAINT_FN = ramp([
    (0.00, rgb(25, 43, 51)), (0.38, rgb(43, 65, 65)),
    (0.62, rgb(86, 94, 72)), (0.80, rgb(138, 114, 68)),
    (1.00, rgb(56, 50, 36)),
])

AMBER_D = rgb(118, 54, 14)
AMBER = rgb(198, 106, 22)
GOLD = rgb(242, 170, 48)
GOLD_L = rgb(255, 206, 94)
PALE = rgb(255, 238, 180)
EMBER = rgb(255, 122, 34)
OLD_TIP = rgb(128, 102, 60)     # the painted, tarnished gold of the old tips


# ---------------------------------------------------------------------------
# the feather builder.
#
# A feather is a ribbon swept along a spine that is grown step by step:
# the heading starts at `deg` and rotates by `bend` radians over the
# length, so the vane curves like a real plume. Each station carries a
# half-width from a growth-and-taper profile. The vane is painted as a
# stack of passes — a darker rim underneath, the main gradient, a
# cross-vane tone, herringbone barb strokes, a sheen line, the shaft —
# so the silhouette stays smooth at any zoom.
# ---------------------------------------------------------------------------

def feather(ox, oy, deg, length, hw, c_root, c_tip, bend=0.0, *,
            barbs=0, barb_gain=1.0, rim=0.0, tone=False, sheen=False,
            shaft=True, flat=False):
    n = max(10, min(30, int(length / 13)))
    a0 = math.radians(deg)
    step = length / n
    # grow the spine: position, tangent, normal, half-width per station
    st = []
    x, y = ox, oy
    for i in range(n + 1):
        t = i / n
        ang = a0 + bend * t
        tx, ty = math.cos(ang), math.sin(ang)
        grow = math.sin(min(1.0, t * 1.15) * math.pi * 0.5)
        w = hw * (0.30 + 0.70 * grow) * (1.0 - 0.88 * t * t * t)
        st.append((x, y, tx, ty, -ty, tx, max(0.7, w)))
        x += tx * step
        y += ty * step
    spine = [(s[0], s[1]) for s in st]
    seed = int(ox * 3.3) * 131 + int(oy * 3.9)

    def base_col(t):
        s = t * t * (3.0 - 2.0 * t)
        c = lerp(c_root, c_tip, 0.5 * t + 0.5 * s)
        if not flat:
            c = lerp(shade(c_root, 0.55), c, min(1.0, t * 3.2))
        return c

    def strip(scale_w, off, shader, alpha=None):
        for i in range(n):
            t = (i + 0.5) / n
            quad = []
            for j in (i, i + 1):
                px, py, tx, ty, nx, ny, w = st[j]
                cxp, cyp = px + nx * off * w, py + ny * off * w
                h = w * scale_w
                if j == i:
                    quad.append((cxp + nx * h, cyp + ny * h))
                    quad.append((cxp - nx * h, cyp - ny * h))
                else:
                    quad.append((cxp - nx * h, cyp - ny * h))
                    quad.append((cxp + nx * h, cyp + ny * h))
            c = shader(t, i)
            if alpha is None:
                CV.poly(quad, lambda qx, qy, cc=c: cc)
            else:
                CV.poly_a(quad, c, alpha)

    if rim > 0.0 and not flat:
        strip(1.14, 0.0, lambda t, i: shade(base_col(t), rim))
    strip(1.0, 0.0,
          lambda t, i: shade(base_col(t),
                             1.0 + 0.05 * (hash2(seed + i, 7) - 0.5))
          if not flat else base_col(t))
    if tone and not flat:
        ny_mid = st[n // 2][5]
        up = 1.0 if ny_mid < 0 else -1.0
        strip(0.60, -0.46 * up, lambda t, i: shade(base_col(t), 0.76),
              alpha=0.30)
        strip(0.52, 0.44 * up,
              lambda t, i: lerp(base_col(t), PALE, 0.32), alpha=0.10)
    if barbs > 0 and not flat:
        # herringbone fibres: short strokes from near the shaft running
        # outward and tipward, alternating dark and light, hashed in
        # length and spacing so no stripe pattern can form
        nstat = max(6, int(length / 24))
        for s_i in range(nstat):
            t = 0.14 + 0.80 * (s_i / max(1, nstat - 1))
            idx = min(n, int(t * n))
            px, py, tx, ty, nx, ny, w = st[idx]
            hv = hash2(seed + s_i * 3, 21)
            if hv < 0.18:
                continue
            reach = w * (0.60 + 0.40 * hv)
            dark = (s_i % 2 == 0)
            c = shade(base_col(t), 0.64) if dark \
                else lerp(base_col(t), PALE, 0.55)
            al = (0.27 if dark else 0.15) * barb_gain
            for sgn in (-1, 1):
                sx = px + nx * sgn * w * 0.06
                sy = py + ny * sgn * w * 0.06
                ex = px + tx * reach * 0.58 + nx * sgn * reach
                ey = py + ty * reach * 0.58 + ny * sgn * reach
                CV.line(sx, sy, ex, ey, 0.75, c, al)
    if sheen and not flat:
        ny_mid = st[n // 2][5]
        up = 1.0 if ny_mid < 0 else -1.0
        pts = []
        for i, (px, py, tx, ty, nx, ny, w) in enumerate(st):
            t = i / n
            off = 0.30 * w * (1.0 - t * 0.5) * up
            pts.append((px + nx * off, py + ny * off))
        for i in range(n):
            CV.line(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1],
                    max(0.5, st[i][6] * 0.33), rgb(255, 232, 170), 0.12)
    if shaft and not flat:
        i0 = max(1, int(n * 0.04))
        i1 = min(n, int(n * 0.92))
        CV.pline([spine[i] for i in range(i0, i1 + 1)], 1.15,
                 shade(c_root, 0.55), 0.5)
    return spine


# ---------------------------------------------------------------------------
# the scale-plate builder: one shingled feather-scale. The outline is
# sampled from quadratic curves — a tucked top edge, swelling sides, a
# rounded free tip — then painted with a shadow copy beneath, a graded
# face, a lit rim along the free edge and a soft crown highlight.
# ---------------------------------------------------------------------------

def _plate_outline():
    pts = []

    def add(p0, p1, p2, k):
        for i in range(k):
            pts.append(qbez(p0, p1, p2, i / k))
    add((-0.46, -0.38), (0.0, -0.52), (0.46, -0.38), 4)     # tucked top
    add((0.46, -0.38), (0.62, 0.05), (0.30, 0.38), 4)       # right side
    add((0.30, 0.38), (0.16, 0.55), (0.0, 0.60), 3)        # tip, right
    add((0.0, 0.60), (-0.16, 0.55), (-0.30, 0.38), 3)      # tip, left
    add((-0.30, 0.38), (-0.62, 0.05), (-0.46, -0.38), 4)   # left side
    return pts


_PLATE = _plate_outline()


def scale(cx, cy, w, h, rot, c, rim_a=0.28):
    ca, sa = math.cos(rot), math.sin(rot)

    def place(dx, dy):
        return (cx + dx * w * ca - dy * h * sa,
                cy + dx * w * sa + dy * h * ca)

    pts = [place(dx, dy) for (dx, dy) in _PLATE]
    under = [place(dx, dy + 0.09) for (dx, dy) in _PLATE]
    CV.poly(under, lambda qx, qy: shade(c, 0.30))

    def grad(qx, qy):
        # v: position along the plate's own axis, 0 tucked -> 1 tip
        dx, dy = qx - cx, qy - cy
        v = (-dx * sa + dy * ca) / h + 0.44
        if v < 0.30:
            return lerp(shade(c, 0.52), shade(c, 0.88), v / 0.30)
        if v < 0.76:
            return lerp(shade(c, 0.88), c, (v - 0.30) / 0.46)
        return lerp(c, lerp(c, PALE, 0.36), (v - 0.76) / 0.24)
    CV.poly(pts, grad)
    # lit rim along the free (lower) edge: outline points 7..14
    rim = [pts[i] for i in range(7, 15)]
    CV.pline(rim, 1.05, lerp(c, PALE, 0.52), rim_a)
    hx, hy = place(-0.13, -0.02)
    CV.disc_a(hx, hy, w * 0.12, lerp(c, PALE, 0.30), 0.12)


# ---------------------------------------------------------------------------
# the gallery room: wall, floor, spotlight beam, plaque
# ---------------------------------------------------------------------------

def paint_wall():
    CV.vrect(0, 0, SIDE, FLOOR_Y, WALL_FN)
    # panel seams: a dark cut with a lit lip beside it
    for x in (128, 896):
        CV.vrect(x, 0, x + 2.5, FLOOR_Y, lambda t: rgb(22, 21, 19))
        CV.vrect(x + 2.5, 0, x + 4, FLOOR_Y, lambda t: rgb(52, 48, 42))
    # picture rail near the top
    CV.vrect(0, 66, SIDE, 70, lambda t: rgb(20, 19, 18))
    CV.vrect(0, 70, SIDE, 72.5, lambda t: rgb(54, 50, 44))
    # skirting board
    CV.vrect(0, FLOOR_Y - 16, SIDE, FLOOR_Y - 13,
             lambda t: rgb(58, 53, 46))
    CV.vrect(0, FLOOR_Y - 13, SIDE, FLOOR_Y, lambda t: rgb(26, 24, 22))


def paint_floor():
    top, bot = rgb(31, 27, 24), rgb(9, 8, 8)
    CV.vrect(0, FLOOR_Y, SIDE, SIDE, lambda t: lerp(top, bot, t))
    # warm pool of spilled light under the painting
    CV.glow(512, 958, 300, rgb(255, 176, 92), 0.055)
    CV.glow(512, 940, 170, rgb(255, 196, 110), 0.05)
    # the frame's glow smeared down the polished floor: an irregular
    # walk of streaks — uneven gaps, widths, lengths and strengths,
    # each trailed by two fainter side smears — so it reads as a
    # reflection and never as a pattern
    dice = Rng(4451)
    x = 336.0
    while x < 694:
        wdt = dice.f(5.0, 15.0)
        al = dice.f(0.022, 0.055)
        y1 = dice.f(952, 1012)
        drift = dice.f(-10, 10)
        col = rgb(232, 178, 100)
        CV.line(x, 894, x + drift, y1, wdt * 0.45, col, al)
        CV.line(x - wdt * 0.5, 896, x - wdt * 0.5 + drift, y1 - 6,
                wdt * 0.30, col, al * 0.45)
        CV.line(x + wdt * 0.5, 896, x + wdt * 0.5 + drift, y1 - 6,
                wdt * 0.30, col, al * 0.45)
        x += dice.f(23, 57)


def paint_beam():
    """The spotlight cone from the upper-left lamp. It is painted row
    by row: across each row the strength follows a sine profile that
    feathers to nothing at the edges, and down its length it fades
    away — no hard polygon edge can show anywhere."""
    def beam(xt0, xt1, xb0, xb1, peak, col):
        y_top, y_bot = -10.0, 800.0
        rows = int((y_bot - y_top) * SS)
        for j in range(rows + 1):
            y = y_top + j / SS
            v = j / rows
            sf = max(0.0, (v - 0.40) / 0.52)
            fv = 1.0 - 0.985 * min(1.0, sf) ** 1.5
            if fv <= 0.02:
                continue
            lx = xt0 + (xb0 - xt0) * v
            rx = xt1 + (xb1 - xt1) * v
            yi = int(y * SS)
            xa, xb = int(lx * SS), int(rx * SS)
            chunks = 26
            for i in range(chunks):
                u0, u1 = i / chunks, (i + 1) / chunks
                um = (u0 + u1) / 2
                a = peak * (math.sin(math.pi * um) ** 1.35) * fv
                if a < 0.004:
                    continue
                CV._span_fx(yi, int(xa + (xb - xa) * u0),
                            int(xa + (xb - xa) * u1), col, a, False)
    beam(196, 330, 120, 700, 0.115, rgb(255, 226, 170))
    beam(226, 324, 208, 622, 0.10, rgb(255, 236, 190))
    # dust motes drifting inside the beam
    dice = Rng(902)
    for _ in range(120):
        t = dice.f()
        x = 260 + 170 * t + dice.f(-120, 190) * t
        y = t * 800 + dice.f(-14, 14)
        if 0 <= x < SIDE and 0 <= y < FLOOR_Y:
            CV.disc_a(x, y, dice.f(0.6, 1.7), rgb(255, 240, 205),
                      dice.f(0.10, 0.34))


def paint_plaque():
    # brass title plaque centred under the frame
    CV.vrect(464, 828, 560, 860,
             lambda t: lerp(rgb(150, 118, 58), rgb(104, 80, 34), t))
    CV.vrect(464, 828, 560, 831, lambda t: rgb(226, 190, 110))
    CV.vrect(464, 857, 560, 860, lambda t: rgb(70, 54, 22))
    # engraved lettering: three fine stamped lines of varied length,
    # each a dark cut with a hairline of light beneath it
    for (lx0, lx1, ly) in [(482, 542, 837.5), (488, 536, 843.0),
                           (497, 527, 848.5)]:
        CV.vrect(lx0, ly, lx1, ly + 1.7, lambda t: rgb(74, 56, 24))
        CV.vrect(lx0, ly + 1.7, lx1, ly + 2.4, lambda t: rgb(196, 158, 84))


# ---------------------------------------------------------------------------
# the gilt frame
# ---------------------------------------------------------------------------

def _band(x0, y0, x1, y1, c_top, c_bot):
    CV.vrect(x0, y0, x1, y1, lambda t: lerp(c_top, c_bot, t))


def cast_frame_shadow():
    fx0, fy0, fx1, fy1 = FRAME
    for (dx, dy, a) in [(30, 40, 0.20), (22, 30, 0.24), (14, 20, 0.28)]:
        CV.poly_a([(fx0 + dx, fy0 + dy), (fx1 + dx, fy0 + dy),
                   (fx1 + dx, fy1 + dy), (fx0 + dx, fy1 + dy)],
                  (0.0, 0.0, 0.0), a)


def paint_frame():
    fx0, fy0, fx1, fy1 = FRAME
    cx0, cy0, cx1, cy1 = CANVAS_R
    _band(fx0, fy0, fx1, fy1, shade(GILT_D, 0.8), shade(GILT_D, 0.55))
    _band(fx0 + 7, fy0 + 7, fx1 - 7, fy1 - 7,
          lerp(GILT_M, GILT_B, 0.35), shade(GILT_M, 0.72))
    _band(fx0 + 20, fy0 + 20, fx1 - 20, fy1 - 20,
          GILT_B, lerp(GILT_M, GILT_B, 0.4))
    _band(fx0 + 30, fy0 + 30, fx1 - 30, fy1 - 30,
          shade(GILT_D, 1.05), shade(GILT_M, 0.8))
    _band(fx0 + 40, fy0 + 40, fx1 - 40, fy1 - 40,
          lerp(GILT_B, GILT_H, 0.45), GILT_M)
    # crisp light on the top and left, shadow on bottom and right
    CV.vrect(fx0 + 7, fy0 + 7, fx1 - 7, fy0 + 10.5, lambda t: GILT_H)
    CV.vrect(fx0 + 7, fy0 + 7, fx0 + 10.5, fy1 - 7,
             lambda t: lerp(GILT_H, GILT_B, 0.4))
    CV.vrect(fx0 + 20, fy0 + 20, fx1 - 20, fy0 + 22.5,
             lambda t: rgb(255, 240, 190))
    CV.vrect(fx0 + 7, fy1 - 10.5, fx1 - 7, fy1 - 7,
             lambda t: shade(GILT_D, 0.5))
    CV.vrect(fx1 - 10.5, fy0 + 7, fx1 - 7, fy1 - 7,
             lambda t: shade(GILT_D, 0.62))

    # egg-and-dart beading along the raised ridge
    def bead(cx, cy):
        CV.disc(cx, cy, 4.6, shade(GILT_D, 0.85))
        CV.disc(cx - 0.8, cy - 0.8, 3.6, GILT_B)
        CV.disc(cx - 1.4, cy - 1.4, 1.5, GILT_H)
    x = fx0 + 38
    while x < fx1 - 34:
        bead(x, fy0 + 25)
        bead(x, fy1 - 25)
        x += 21
    y = fy0 + 38
    while y < fy1 - 34:
        bead(fx0 + 25, y)
        bead(fx1 - 25, y)
        y += 21

    # corner rosettes: eight petals around a bright boss
    for (cx, cy) in [(fx0 + 25, fy0 + 25), (fx1 - 25, fy0 + 25),
                     (fx0 + 25, fy1 - 25), (fx1 - 25, fy1 - 25)]:
        for k in range(8):
            a = k * math.pi / 4
            px, py = cx + 10.5 * math.cos(a), cy + 10.5 * math.sin(a)
            CV.disc(px, py, 5.4, GILT_M)
            CV.disc(px - 1.2, py - 1.2, 3.4, GILT_B)
        CV.disc(cx, cy, 7.6, shade(GILT_D, 0.9))
        CV.disc(cx, cy, 6.0, GILT_B)
        CV.disc(cx - 1.6, cy - 1.6, 2.6, GILT_H)

    # mid-side cartouches: a small shell fan at each side's centre
    for (cx, cy, sideways) in [(fx0 + 25, 454, True), (fx1 - 25, 454, True),
                               (512, fy0 + 25, False), (512, fy1 - 25, False)]:
        for k in range(-2, 3):
            if sideways:
                px, py = cx, cy + k * 7.5
            else:
                px, py = cx + k * 7.5, cy
            CV.disc(px, py, 4.4, GILT_M)
            CV.disc(px - 1.2, py - 1.0, 2.8, GILT_B)
        CV.disc(cx, cy, 5.6, GILT_B)
        CV.disc(cx - 1.4, cy - 1.4, 2.4, GILT_H)

    # inner lip where frame meets canvas
    CV.vrect(cx0 - 4, cy0 - 4, cx1 + 4, cy0 - 1.5, lambda t: GILT_H)
    CV.vrect(cx0 - 4, cy0 - 4, cx0 - 1.5, cy1 + 4,
             lambda t: lerp(GILT_H, GILT_B, 0.35))
    CV.vrect(cx0 - 4, cy1 + 1.5, cx1 + 4, cy1 + 4,
             lambda t: shade(GILT_D, 0.7))
    CV.vrect(cx1 + 1.5, cy0 - 4, cx1 + 4, cy1 + 4,
             lambda t: shade(GILT_D, 0.85))


# ---------------------------------------------------------------------------
# the painting inside the frame
# ---------------------------------------------------------------------------

def paint_picture():
    cx0, cy0, cx1, cy1 = CANVAS_R
    CV.vrect(cx0, cy0, cx1, cy1, PAINT_FN)
    dice = Rng(31337)
    # painted clouds: soft, muted, unmistakably flat brushwork
    for _ in range(26):
        CV.glow(dice.f(cx0 + 20, cx1 - 20), dice.f(cy0 + 26, cy0 + 210),
                dice.f(26, 74), rgb(196, 186, 150), 0.05)
    for _ in range(14):
        CV.glow(dice.f(cx0 + 20, cx1 - 20), dice.f(cy0 + 90, cy0 + 260),
                dice.f(20, 52), rgb(16, 26, 30), 0.06)
    # banded painted clouds, warm-lit from above, shadowed beneath
    dice2 = Rng(90210)
    for _ in range(5):
        bx = dice2.f(cx0 + 60, cx1 - 60)
        by = dice2.f(cy0 + 30, cy0 + 140)
        for _ in range(6):
            CV.glow(bx + dice2.f(-52, 52), by + dice2.f(-7, 7),
                    dice2.f(20, 44), rgb(214, 192, 148), 0.05)
        CV.glow(bx, by + 13, 40, rgb(28, 42, 46), 0.045)
    # warm horizon glow pooled behind the ridge lines
    for (gx, gr) in [(360, 120), (512, 150), (664, 120)]:
        CV.glow(gx, 528, gr, rgb(236, 186, 104), 0.055)
    # far ridge: a pale blue-teal silhouette against the glow
    ridge = [(cx0, 588)]
    x = int(cx0)
    while x <= int(cx1):
        ridge.append((x, 520 - 34 * math.sin(x * 0.011 + 1.9)
                      - 14 * math.sin(x * 0.027 + 0.4)))
        x += 22
    ridge.append((cx1, 588))
    CV.poly(ridge, vshader(rgb(50, 66, 62), rgb(38, 52, 50), 470, 588))
    # near ridge: darker, lower, its crest catching a whisper of light
    ridge2 = [(cx0, 626)]
    x = int(cx0)
    while x <= int(cx1):
        ridge2.append((x, 566 - 22 * math.sin(x * 0.016 + 4.2)
                      - 10 * math.sin(x * 0.041 + 1.1)))
        x += 22
    ridge2.append((cx1, 626))
    CV.poly(ridge2, vshader(rgb(40, 52, 42), rgb(29, 39, 31), 520, 626))
    for p, q in zip(ridge2, ridge2[1:]):
        CV.line(p[0], p[1], q[0], q[1], 1.3, rgb(120, 116, 78), 0.20)
    # painted hills along the canvas foot
    hills = [(cx0, cy1), (cx0, 668)]
    x = int(cx0)
    while x <= int(cx1):
        hills.append((x, 664 - 20 * math.sin(x * 0.013 + 0.7)
                      - 9 * math.sin(x * 0.031)))
        x += 24
    hills.append((cx1, cy1))
    CV.poly(hills, vshader(rgb(52, 58, 42), rgb(30, 32, 24), 640, cy1))
    # dry horizontal brushstrokes worrying the painted ground
    dice3 = Rng(5150)
    for _ in range(46):
        x = dice3.f(cx0 + 16, cx1 - 70)
        y = dice3.f(652, 736)
        c = dice3.pick([rgb(150, 124, 78), rgb(120, 100, 60),
                        rgb(96, 86, 52)])
        CV.line(x, y, x + dice3.f(18, 60), y + dice3.f(-1.5, 1.5),
                dice3.f(1.6, 2.6), c, dice3.f(0.08, 0.15))
    # a painted moon, small and dim, set well OFF the head's axis in
    # the far left sky — a quiet disc, no rays
    CV.disc_a(424, 204, 27, rgb(176, 138, 74), 0.46)
    CV.disc_a(424, 204, 20, rgb(196, 160, 90), 0.40)
    # two tiny painted birds, far away in the painted sky
    for (bx, by, s) in [(352, 248, 10), (672, 216, 8)]:
        CV.line(bx - s, by - s * 0.4, bx, by, 1.8, rgb(24, 32, 34), 0.7)
        CV.line(bx, by, bx + s, by - s * 0.4, 1.8, rgb(24, 32, 34), 0.7)
    # painted light pooling around where the breach will tear open
    CV.glow(512, 450, 130, rgb(232, 192, 112), 0.10)
    # a painted road of light on the ground, leading to the breach
    CV.poly_a([(468, cy1), (556, cy1), (530, 518), (496, 518)],
              rgb(240, 205, 130), 0.10)


def paint_ghost():
    """The flat painted phoenix the real one tore itself out of:
    muted silhouettes still trapped on the canvas, and the dull
    gold-leaf halo ring behind where the head broke through."""
    ghost = rgb(38, 38, 30)
    ghost_hi = rgb(62, 56, 40)
    for k in range(28):
        a = k * math.pi / 14
        CV.disc_a(512 + 118 * math.cos(a), 328 + 118 * math.sin(a),
                  7.5, rgb(122, 100, 52), 0.55)
    # ghost wings drooping low into the canvas corners
    feather(442, 478, 197, 112, 12, ghost, ghost_hi, bend=0.30, flat=True)
    feather(448, 504, 205, 104, 11, ghost, ghost_hi, bend=0.26, flat=True)
    feather(582, 478, -17, 112, 12, ghost, ghost_hi, bend=-0.30, flat=True)
    feather(576, 504, -25, 104, 11, ghost, ghost_hi, bend=-0.26, flat=True)
    # ghost tail ribbons painted down the canvas
    for (dx, ln, bd) in [(-52, 196, 0.30), (-18, 224, 0.12),
                         (18, 224, -0.12), (52, 196, -0.30)]:
        feather(512 + dx, 518, 90, ln, 13, ghost, ghost_hi, bend=bd,
                flat=True)


def paint_cracks():
    """Craquelure over the canvas plus the great radial crack web
    bursting from the breach point. Every segment is clipped to the
    canvas (inset 4px): no crack may touch frame, wall or bird."""
    cx0, cy0, cx1, cy1 = CANVAS_R
    box = (cx0 + 4, cy0 + 4, cx1 - 4, cy1 - 4)
    bx, by = 512, 450
    dice = Rng(2718)

    def crack(x0, y0, x1, y1, w, c, a):
        seg = clip_seg(x0, y0, x1, y1, box)
        if seg:
            CV.line(seg[0], seg[1], seg[2], seg[3], w, c, a)

    # fine network: short wandering segments
    for _ in range(300):
        x = dice.f(cx0 + 8, cx1 - 8)
        y = dice.f(cy0 + 8, cy1 - 8)
        a = dice.f(0, math.pi * 2)
        for _seg in range(int(dice.f(2, 4.999))):
            nx = x + math.cos(a) * dice.f(7, 15)
            ny = y + math.sin(a) * dice.f(7, 15)
            crack(x, y, nx, ny, 0.75, rgb(18, 14, 10), 0.20)
            x, y = nx, ny
            a += dice.f(-0.7, 0.7)
    # radial great cracks from the breach
    for k in range(13):
        a = k * (math.pi * 2 / 13) + dice.f(-0.16, 0.16)
        x, y = bx, by
        reach = dice.f(190, 330)
        travelled = 0.0
        while travelled < reach:
            step = dice.f(16, 34)
            a += dice.f(-0.22, 0.22)
            nx = x + math.cos(a) * step
            ny = y + math.sin(a) * step
            wdt = max(0.6, 1.95 * (1.0 - travelled / reach))
            crack(x, y, nx, ny, wdt, rgb(16, 12, 8), 0.48)
            crack(x + 1.6, y + 1.8, nx + 1.6, ny + 1.8, wdt * 0.7,
                  rgb(255, 240, 205), 0.09)
            if dice.f() < 0.4:
                ba = a + (1 if dice.f() < 0.5 else -1) * dice.f(0.5, 0.9)
                crack(nx, ny, nx + math.cos(ba) * dice.f(14, 40),
                      ny + math.sin(ba) * dice.f(14, 40),
                      0.8, rgb(16, 12, 8), 0.4)
            x, y = nx, ny
            travelled += step
    # concentric stress arcs around the breach
    for (rr, a0, a1) in [(64, 0.3, 2.4), (96, 2.8, 5.1), (132, 5.4, 7.6),
                         (78, 3.6, 5.9), (150, 0.9, 2.9)]:
        prev = None
        for i in range(23):
            a = a0 + (a1 - a0) * (i / 22)
            p = (bx + rr * math.cos(a), by + rr * math.sin(a))
            if prev:
                crack(prev[0], prev[1], p[0], p[1], 1.0, rgb(16, 12, 8),
                      0.30)
            prev = p


def paint_weave():
    """Woven canvas texture: speckle plus faint thread lines."""
    cx0, cy0, cx1, cy1 = CANVAS_R
    dice = Rng(161)
    for _ in range(5200):
        x = dice.f(cx0, cx1)
        y = dice.f(cy0, cy1)
        if dice.f() < 0.5:
            CV.disc_a(x, y, dice.f(0.5, 1.1), rgb(255, 246, 220),
                      dice.f(0.03, 0.07))
        else:
            CV.disc_a(x, y, dice.f(0.5, 1.1), rgb(8, 8, 6),
                      dice.f(0.03, 0.07))
    x = int(cx0) + 6
    while x < int(cx1):
        CV.line(x, cy0 + 4, x, cy1 - 4, 0.6, (0.0, 0.0, 0.0), 0.028)
        x += 9
    y = int(cy0) + 6
    while y < int(cy1):
        CV.line(cx0 + 4, y, cx1 - 4, y, 0.6, rgb(255, 250, 230), 0.022)
        y += 9


# ---------------------------------------------------------------------------
# the phoenix
# ---------------------------------------------------------------------------

def paint_breach_light():
    # radiant light spilling from the hole torn in the canvas
    CV.glow(512, 450, 168, rgb(255, 178, 80), 0.30)
    CV.glow(512, 450, 84, rgb(255, 224, 156), 0.34)
    CV.glow(512, 450, 36, rgb(255, 246, 214), 0.30)


def paint_bird_shadow():
    # soft cast shadow thrown down-right onto canvas and wall, so the
    # bird sits physically in front of its own painting
    sh = rgb(10, 8, 5)
    CV.glow(548, 506, 128, sh, 0.34, mode="mix")
    CV.glow(318, 450, 116, sh, 0.20, mode="mix")
    CV.glow(742, 450, 116, sh, 0.20, mode="mix")
    CV.glow(392, 468, 92, sh, 0.13, mode="mix")
    CV.glow(668, 468, 92, sh, 0.13, mode="mix")
    CV.glow(472, 418, 84, sh, 0.12, mode="mix")
    CV.glow(608, 328, 50, sh, 0.22, mode="mix")
    CV.glow(520, 638, 90, sh, 0.18, mode="mix")


def paint_tail():
    # covert skirt: a layered fan of short wide feathers rooted at
    # (505, 522) — tucked under the breast once the body is drawn —
    # so every ribbon root below lands on plumage, never bare canvas
    for (deg, ln, semi, bend) in [(56, 130, 29, -0.16), (72, 146, 30, -0.08),
                                  (90, 152, 31, 0.0), (108, 146, 30, 0.08),
                                  (124, 130, 29, 0.16)]:
        feather(505, 522, deg, ln, semi, AMBER,
                lerp(GOLD_L, OLD_TIP, 0.35), bend=bend, sheen=True,
                barbs=5, barb_gain=1.1, rim=0.50)
    # two long central feathers running with the fan, near-vertical,
    # their roots tucked under the ribbons
    feather(497, 532, 95, 302, 12, AMBER, lerp(GOLD_L, OLD_TIP, 0.4),
            bend=0.10, barbs=5, rim=0.50, shaft=False)
    feather(515, 532, 85, 302, 12, AMBER, lerp(GOLD_L, OLD_TIP, 0.4),
            bend=-0.10, barbs=5, rim=0.50, shaft=False)
    # the five great ribbons of the fan
    ribbons = [(-48, 108, 204, 18, 0.44), (-24, 100, 254, 21, 0.19),
               (0, 90, 286, 23, 0.0), (24, 80, 254, 21, -0.19),
               (48, 72, 204, 18, -0.44)]
    tip_c = lerp(GOLD_L, OLD_TIP, 0.5)
    for ri, (dx, deg, ln, semi, bend) in enumerate(ribbons):
        spine = feather(505 + dx * 0.5, 532, deg, ln, semi, GOLD, tip_c,
                        bend=bend, barbs=6, barb_gain=1.2, rim=0.50)
        # a jewel eyespot grown INTO the vane near the tip: soft teal
        # blends in a feather-toned halo, elongated along the spine,
        # with fibre strokes running through it so the vane's own
        # barbs continue across the spot — no ring, no eyelet
        i0 = int(len(spine) * 0.78)
        ex, ey = spine[i0]
        gx = spine[min(i0 + 2, len(spine) - 1)][0] - spine[max(i0 - 2, 0)][0]
        gy = spine[min(i0 + 2, len(spine) - 1)][1] - spine[max(i0 - 2, 0)][1]
        gl = math.hypot(gx, gy) or 1.0
        tx, ty = gx / gl, gy / gl
        es = 0.92 + 0.16 * ((ri * 37) % 3) / 2.0
        CV.glow(ex, ey, 13.5 * es, lerp(tip_c, PALE, 0.25), 0.32,
                mode="mix")
        for off in (-4.2, -2.1, 0.0, 2.1, 4.2):
            CV.glow(ex + tx * off * es, ey + ty * off * es, 4.6 * es,
                    rgb(24, 62, 64), 0.50, mode="mix")
        CV.glow(ex, ey, 5.2 * es, rgb(14, 40, 46), 0.48, mode="mix")
        bdx, bdy = tx * 0.72 - ty * 0.69, ty * 0.72 + tx * 0.69
        for k in (-2, -1, 0, 1, 2):
            bx, by = ex + tx * k * 3.4 * es, ey + ty * k * 3.4 * es
            CV.line(bx - bdx * 9.5 * es, by - bdy * 9.5 * es,
                    bx + bdx * 9.5 * es, by + bdy * 9.5 * es,
                    0.8, shade(tip_c, 0.62), 0.26)
        for k in (-1, 1):
            bx = ex + tx * k * 3.4 * es - ty * 2.2 * es
            by = ey + ty * k * 3.4 * es + tx * 2.2 * es
            CV.line(bx - bdx * 8.0 * es, by - bdy * 8.0 * es,
                    bx + bdx * 8.0 * es, by + bdy * 8.0 * es,
                    0.7, lerp(tip_c, PALE, 0.45), 0.16)
        CV.disc_a(ex - tx * 1.4 - 1.1, ey - ty * 1.4 - 1.1, 1.0,
                  rgb(255, 236, 178), 0.80)


def paint_wing(mirror):
    """One wing. The screen-right wing is the mirror image, but it
    rides a few degrees higher and more open, and every blade carries
    its own length/angle/bend drift — the fan must not read as a
    symmetric emblem."""
    def W(x, y, deg, ln, semi, cb, ct, **kw):
        if mirror:
            return feather(1024 - x, y, 180 - deg, ln, semi, cb, ct,
                           bend=-kw.pop("bend", 0.0), **kw)
        return feather(x, y, deg, ln, semi, cb, ct, **kw)

    S = (464, 430)
    lift = 4.0 if mirror else -1.5
    # the wing's arm: one great covert mass underneath everything
    W(S[0], S[1], -126, 252, 48, AMBER_D, AMBER, bend=0.10, rim=0.50,
      barbs=5, barb_gain=1.0)
    # secondaries: the inner fan, darker and shorter than the primaries
    secs = [(-147, 196), (-135, 226), (-123, 242), (-110, 236),
            (-97, 212), (-84, 182)]
    if mirror:
        sec_mul = [0.95, 1.05, 0.93, 1.04, 0.97, 1.01]
    else:
        sec_mul = [1.03, 0.96, 1.02, 0.97, 1.01, 0.95]
    for i, (deg, ln) in enumerate(secs):
        W(S[0] - i * 9, S[1] + i * 8, deg + lift * 0.7, ln * sec_mul[i],
          23, shade(AMBER, 0.8), shade(GOLD, 0.9),
          bend=0.24 + 0.03 * math.sin(i * 2.7 + (1.3 if mirror else 0.0)),
          sheen=(i % 2 == 0), barbs=6, barb_gain=1.1, tone=True, rim=0.44)
    # intermediate feathers tucked into uneven gaps between primaries
    if mirror:
        inters = [(-151, 292, 0.27), (-121, 322, 0.25), (-90, 216, 0.22)]
    else:
        inters = [(-165, 256, 0.26), (-137, 306, 0.30), (-106, 266, 0.24)]
    inter_tips = [lerp(GOLD_L, EMBER, 0.10), GOLD_L, PALE]
    for i, (deg, ln, bend) in enumerate(inters):
        W(S[0], S[1], deg + lift, ln, 20, shade(rgb(162, 82, 16), 0.94),
          inter_tips[i], bend=bend, sheen=True, barbs=7, barb_gain=1.2,
          tone=True, rim=0.42)
    # primaries: six great blades sweeping wider than they are tall
    prims = [(-171, 322), (-158, 382), (-144, 428), (-129, 442),
             (-113, 406), (-97, 346)]
    prim_bend = [0.24, 0.30, 0.33, 0.27, 0.30, 0.24]
    prim_tips = [GOLD_L, lerp(GOLD_L, EMBER, 0.16), PALE,
                 lerp(PALE, GOLD_L, 0.35), lerp(GOLD_L, EMBER, 0.09),
                 GOLD_L]
    if mirror:
        len_mul = [1.03, 0.93, 1.06, 0.95, 1.0, 0.90]
        ang_off = [2.5, -2.0, 3.0, -1.5, 2.0, -2.5]
        bend_off = [-0.03, 0.05, -0.05, 0.04, -0.03, 0.05]
    else:
        len_mul = [0.95, 1.04, 0.98, 1.05, 0.94, 1.01]
        ang_off = [-2.5, 1.5, -1.5, 2.5, -2.0, 1.0]
        bend_off = [0.04, -0.05, 0.03, -0.04, 0.05, -0.03]
    for i, (deg, ln) in enumerate(prims):
        base = rgb(163, 79, 15) if i % 2 == 0 else rgb(143, 65, 12)
        base = shade(base, 0.95 + 0.05 * math.sin(i * 2.3 + 0.7))
        W(S[0], S[1], deg + lift + ang_off[i], ln * len_mul[i], 27,
          base, prim_tips[i], bend=prim_bend[i] + bend_off[i],
          sheen=True, barbs=9, barb_gain=1.35, tone=True, rim=0.36)
    # shoulder tuft: a dense diagonal band of coverts across the
    # primary roots, so the wing base reads as layered plumage
    tuft = [(456, 422, -121, 63, 20), (444, 410, -129, 59, 19),
            (432, 400, -137, 53, 17)]
    for i, (tx, ty, deg, ln, semi) in enumerate(tuft):
        W(tx, ty, deg, ln, semi, shade(AMBER, 0.92),
          GOLD_L if i % 2 else GOLD, bend=0.26, sheen=True, shaft=False,
          barbs=5, barb_gain=1.1, rim=0.50)
    # covert rows clothing the shoulder: shorter, wider blades in
    # four close rows, fibre-textured, lit from lighter bases
    rows = [(0, 7, 52, 17), (26, 7, 44, 15), (50, 6, 37, 13),
            (72, 5, 30, 11.5)]
    for (dist, n, ln, semi) in rows:
        for i in range(n):
            t = i / (n - 1)
            deg = -160 + 76 * t
            bx = S[0] + math.cos(math.radians(-122)) * dist - i * 6.5
            by = S[1] + math.sin(math.radians(-122)) * dist + i * 5
            c = GOLD_L if (i + int(dist)) % 3 == 0 else GOLD
            W(bx, by, deg, ln, semi, shade(c, 0.98),
              PALE if i % 3 == 0 else GOLD_L, bend=0.30, sheen=True,
              shaft=False, barbs=6, barb_gain=1.15, rim=0.50)


def _foot(cx, cy, s):
    """One small tucked foot: a leg stub growing out of the belly
    plumage, a foot pad, three two-segment toes with knuckle bends,
    each ending in a curved, pointed dark claw."""
    CV.glow(cx, cy + 5, 15, shade(AMBER, 0.55), 0.50, mode="mix")
    stub = [(cx - 7 * s, cy - 16), (cx + 7 * s, cy - 16),
            (cx + 4.5 * s, cy + 1), (cx - 4.5 * s, cy + 1)]
    CV.poly(stub, vshader(lerp(AMBER, GOLD, 0.35), rgb(158, 90, 28),
                          cy - 16, cy + 1))
    feather(cx, cy - 12, 90 + 10 * s, 17, 9, GOLD, GOLD_L,
            bend=0.10 * s, shaft=False, barbs=3, barb_gain=1.0, rim=0.0)
    CV.disc_a(cx, cy + 2, 7.5, rgb(166, 96, 30), 0.98)
    CV.disc_a(cx - 2 * s, cy, 3.4, rgb(192, 120, 44), 0.55)
    toe_c = rgb(160, 92, 30)
    claw_c = rgb(43, 23, 7)
    for (bx, heading, curl) in [(-6.5, 118, -18), (0.0, 95, -6),
                                (6.5, 70, 10)]:
        if s < 0:
            heading, curl = 180 - heading, -curl
        a1 = math.radians(heading)
        a2 = math.radians(heading + curl)
        x0, y0 = cx + bx * s, cy + 3 - abs(bx) * 0.18
        x1, y1 = x0 + 10.5 * math.cos(a1), y0 + 10.5 * math.sin(a1)
        x2, y2 = x1 + 9.0 * math.cos(a2), y1 + 9.0 * math.sin(a2)
        CV.line(x0, y0, x1, y1, 3.8, toe_c, 0.97)
        CV.disc_a(x1, y1, 2.2, rgb(180, 108, 38), 0.95)
        CV.line(x1, y1, x2, y2, 2.7, toe_c, 0.97)
        dxu, dyu = math.cos(a2), math.sin(a2)
        pxu, pyu = -dyu, dxu
        claw = [(x2 + pxu * 1.7, y2 + pyu * 1.7),
                (x2 + dxu * 5.0 + pxu * 1.3, y2 + dyu * 5.0 + pyu * 1.3 + 1.5),
                (x2 + dxu * 9.5, y2 + dyu * 9.5 + 3.5),
                (x2 + dxu * 5.0 - pxu * 1.2, y2 + dyu * 5.0 - pyu * 1.2 + 1.2),
                (x2 - pxu * 1.7, y2 - pyu * 1.7)]
        CV.poly(claw, lambda qx, qy: claw_c)
        CV.line(x2 + pxu * 1.1, y2 + pyu * 1.1,
                x2 + dxu * 8.6, y2 + dyu * 8.6 + 3.0, 0.7, PALE, 0.20)


def paint_body():
    # torso: a proud breast tapering to the tail root
    torso = [(516, 362), (556, 388), (574, 448), (564, 514),
             (532, 560), (492, 566), (460, 524), (450, 452),
             (458, 394), (486, 364)]
    CV.poly(torso, vshader(rgb(228, 140, 42), rgb(146, 70, 18), 358, 568))
    # the breast is feather-scale armour: fan-shaped plates with
    # hashed size, rotation, offset and shading, laid bottom-first
    # so each row shingles over the one below
    rows = [(398, 46), (413, 51), (428, 53), (443, 52), (458, 48),
            (473, 42), (488, 36), (503, 30), (518, 23), (533, 17),
            (548, 11), (557, 11)]
    for j in range(len(rows) - 1, -1, -1):
        yy, hw = rows[j]
        full = hw * 2
        n = max(3, int(full / 17))
        step = full / (n - 1) if n > 1 else 0.0
        for i in range(n):
            h1 = hash2(i * 3 + 1, j * 5 + 2)
            h2 = hash2(i * 7 + 4, j * 3 + 1)
            h3 = hash2(i * 11 + 2, j * 13 + 5)
            x = 512 - hw + step * i + (h1 - 0.5) * 5.0
            y = yy + (h2 - 0.5) * 3.4
            # plates near the midline get a second dose of jitter so
            # no findable row structure survives in the centre column
            cw = 0.0
            if step > 0:
                cw = max(0.0, 1.0 - abs(x - 512) / (step * 1.2))
            if cw > 0.0:
                x += (h3 - 0.5) * 6.5 * cw
                y += (h1 - 0.5) * 3.2 * cw
            w = step * 1.30 * (0.86 + 0.32 * h3)
            if cw > 0.0:
                w *= 1.0 + (h2 - 0.5) * 0.20 * cw
            h = w * (0.78 + 0.16 * h1)
            c = GOLD_L if h2 > 0.72 else GOLD
            if hash2(i * 3 + 9, j * 7 + 1) > 0.88:
                c = lerp(c, EMBER, 0.13)
            c = shade(c, (1.0 - j * 0.014) * (0.90 + 0.17 * h3))
            if cw > 0.0:
                c = shade(c, 1.0 + (h1 - 0.5) * 0.15 * cw)
            scale(x, y, w, h, (h1 - 0.5) * 0.13, c)
    # belly modelling: a low shadow under the breast, soft light above
    CV.glow(512, 551, 34, shade(AMBER, 0.72), 0.22, mode="mix")
    CV.glow(508, 533, 20, GOLD_L, 0.16, mode="mix")
    # the neck: a tapered ribbon rising in an S-curve to the head
    spine = [qbez((514, 398), (496, 330), (556, 312), i / 14)
             for i in range(15)]

    def neck_r(t):
        return 30 - 14 * t ** 1.35

    left, right = [], []
    for i, (x, y) in enumerate(spine):
        t = i / 14
        q_prev = spine[max(0, i - 1)]
        q_next = spine[min(14, i + 1)]
        gx, gy = q_next[0] - q_prev[0], q_next[1] - q_prev[1]
        gl = math.hypot(gx, gy) or 1.0
        nx, ny = -gy / gl, gx / gl
        r = neck_r(t)
        left.append((x + nx * r, y + ny * r))
        right.append((x - nx * r, y - ny * r))
    CV.poly(left + right[::-1],
            lambda qx, qy: lerp(AMBER, GOLD,
                                max(0.0, min(1.0, (400 - qy) / 90.0))))
    # a lit chain riding the neck's upper-left curve
    for i in range(14):
        t = i / 14
        x0, y0 = spine[i]
        x1, y1 = spine[i + 1]
        CV.line(x0 - 10 + 3 * t, y0 - 4, x1 - 10 + 3 * (i + 1) / 14,
                y1 - 4, 28 - 10 * t, GOLD_L, 0.5)
    # neck plumage: miniature shingles in gold tones, tips pointing
    # down-neck, in seven columns wrapping around the sides — outer
    # columns smaller and darker, falling into the neck's own shadow
    for si in range(2, 14):
        s = si / 14
        x, y = spine[si]
        gx = spine[si + 1][0] - spine[si - 1][0]
        gy = spine[si + 1][1] - spine[si - 1][1]
        gl = math.hypot(gx, gy) or 1.0
        tx, ty = gx / gl, gy / gl
        nx, ny = -ty, tx
        r_s = neck_r(s)
        base = lerp(AMBER, GOLD, s)
        rot = math.atan2(tx, -ty)
        lats = [-0.80, -0.53, -0.27, 0.0, 0.27, 0.53, 0.80]
        if si % 2:
            lats = [max(-0.84, min(0.84, lat + 0.13)) for lat in lats]
        for k, lat in enumerate(lats):
            side = abs(lat) / 0.84
            hv = hash2(si * 5 + k * 7 + 1, 91)
            px = x + nx * lat * r_s * 0.90 + (hv - 0.5) * 2.4
            py = y + ny * lat * r_s * 0.90 + (hv - 0.5) * 2.0
            w = r_s * 0.47 * (1.0 - 0.16 * side) * (0.90 + 0.20 * hv)
            c_p = lerp(GOLD, GOLD_L, 0.15 + 0.45 * hv)
            c_p = lerp(c_p, base, 0.25)
            c_p = shade(c_p, (0.94 + 0.10 * s) * (1.0 - 0.30 * side))
            scale(px, py, w, w * 1.05, rot + (hv - 0.5) * 0.14, c_p)
    # a ruff where the neck meets the breast
    for (deg, ln, semi, bend) in [(54, 32, 14, 0.14), (72, 38, 15, 0.08),
                                  (90, 40, 15, 0.0), (108, 36, 14, -0.08),
                                  (124, 30, 13, -0.14)]:
        feather(516, 394, deg, ln, semi, lerp(AMBER, GOLD, 0.25), GOLD_L,
                bend=bend, sheen=True, shaft=False, barbs=5,
                barb_gain=1.1, rim=0.50)
    # the tucked feet (drawn here, over the belly's lower edge)
    _foot(470, 549, 1)
    _foot(541, 553, -1)


def paint_head():
    hx, hy = 584, 290
    # the whole head is drawn through a 6-degree tilt, beak dipping
    # with intent; every local helper rotates around (hx, hy)
    TILT = math.radians(6.0)
    ca_t, sa_t = math.cos(TILT), math.sin(TILT)
    TILT_DEG = 6.0

    def R(x, y):
        dx, dy = x - hx, y - hy
        return (hx + dx * ca_t - dy * sa_t, hy + dx * sa_t + dy * ca_t)

    def L(dx, dy):
        """Local head coords -> world."""
        return R(hx + dx, hy + dy)

    def HM(dx, dy, r, c, peak, mode="mix"):
        px, py = L(dx, dy)
        CV.glow(px, py, r, c, peak, mode=mode)

    def DM(dx, dy, r, c, a):
        px, py = L(dx, dy)
        CV.disc_a(px, py, r, c, a)

    def LN(p, q, w, c, a=1.0):
        ax, ay = L(*p)
        bx, by = L(*q)
        CV.line(ax, ay, bx, by, w, c, a)

    def PL(pts, shader):
        CV.poly([L(*p) for p in pts], shader)

    def FT(dx, dy, deg, ln, semi, cb, ct, **kw):
        px, py = L(dx, dy)
        return feather(px, py, deg + TILT_DEG, ln, semi, cb, ct, **kw)

    HEAD_GOLD = lerp(AMBER, GOLD, 0.45)
    # skull: a smoothed profile blob — elongated cranium, sloped
    # forehead running down into the beak base, a defined jaw curve.
    # No circle anywhere in the silhouette.
    outline = [(-58, 2), (-52, -16), (-38, -29), (-14, -35), (14, -35),
               (34, -28), (46, -15), (51, -1), (47, 11), (37, 21),
               (22, 28), (2, 32), (-18, 31), (-39, 26), (-52, 17)]
    blob = [L(*p) for p in catmull_closed(outline)]
    CV.poly(blob, vshader(lerp(HEAD_GOLD, GOLD, 0.18),
                          shade(HEAD_GOLD, 0.94), 250, 324))
    # modelled light: broad gold blend, crown highlight, lit cheek
    # (soft blends only — a hard disc on the cheek reads as a sticker)
    HM(-4, -9, 33, GOLD, 0.80)
    HM(-12, -21, 12, lerp(GOLD_L, PALE, 0.5), 0.38)
    HM(-18, -14, 20, GOLD_L, 0.40)
    HM(-26, 8, 11, GOLD, 0.50)
    HM(-30, 0, 9, GOLD_L, 0.45)
    HM(-22, 17, 9, shade(GOLD, 0.95), 0.50)
    # jaw shadow falloff where the head turns down to the neck
    HM(8, 25, 17, lerp(AMBER_D, AMBER, 0.30), 0.30)
    # a dark under-band along the jaw, so the tight flat shingles
    # laid over it read as one continuous plumage surface flowing
    # from the jaw down into the neck — gaps show as shadow, never
    # as holes, and no plate hangs off the surface like a petal
    band = [(-50, 6), (-26, 18), (2, 23), (26, 19), (38, 8),
            (24, 24), (-2, 28), (-26, 25), (-46, 16)]
    PL(band, lambda qx, qy: shade(HEAD_GOLD, 0.82))
    jaw_rows = [
        (0.97, 12.0, [(-36, 12, 0.50), (-22, 18, 0.40), (-8, 22, 0.30),
                      (6, 23, 0.20), (19, 20, 0.10), (29, 15, 0.02)]),
        (0.93, 11.5, [(-43, 18, 0.55), (-30, 23, 0.46), (-16, 26, 0.36),
                      (-2, 27, 0.26), (11, 25, 0.17), (23, 21, 0.07)]),
        (0.89, 11.0, [(-36, 23, 0.50), (-23, 26, 0.41), (-9, 27, 0.31),
                      (5, 27, 0.21), (16, 24, 0.12)]),
    ]
    for ri, (rshade, pw, plates) in enumerate(jaw_rows):
        for pi, (dx, dy, rot) in enumerate(plates):
            hv = hash2(pi * 3 + ri * 7 + 1, ri * 5 + pi + 2)
            c_p = lerp(GOLD, GOLD_L, 0.18 + 0.40 * hv)
            c_p = lerp(c_p, HEAD_GOLD, 0.30)
            c_p = shade(c_p, rshade * (0.93 + 0.12 * hv))
            cxw, cyw = L(dx + (hv - 0.5) * 2.2, dy + (hv - 0.5) * 1.8)
            scale(cxw, cyw, pw * (0.90 + 0.20 * hv), pw * 0.46,
                  rot + TILT, c_p, rim_a=0.14)
    # the beak: a raptor's — deep at the base, slim, the upper
    # mandible tapering steadily along a near-straight top edge to a
    # needle point that overhangs the mouth line; the lower mandible
    # thinner, shorter, its own point tucked under the hook. Clean
    # surface, no nostril mark.
    upper = [(22, -20), (42, -16), (60, -10), (76, -3), (98, 7),
             (80, 3), (58, 4), (38, 3), (22, 0)]
    PL(upper, vshader(rgb(255, 222, 138), rgb(202, 134, 50), 268, 308))
    HM(34, -13, 8, rgb(226, 138, 44), 0.55)
    HM(34, -13, 4.5, rgb(242, 172, 84), 0.45)
    lower = [(23, 3), (46, 6), (68, 8), (50, 12), (30, 11)]
    PL(lower, vshader(rgb(172, 108, 38), rgb(128, 78, 26), 292, 308))
    HM(44, 27, 9, rgb(122, 60, 18), 0.26)
    LN((24, 2), (48, 4.5), 1.4, rgb(104, 62, 20), 0.75)
    LN((48, 4.5), (70, 6.5), 1.4, rgb(104, 62, 20), 0.75)
    # a fine lit ridge along the upper mandible, running all the
    # way out to the point so the eye follows the taper
    LN((26, -17), (44, -13.5), 1.2, lerp(GOLD_L, PALE, 0.4), 0.30)
    LN((44, -13.5), (62, -8), 1.2, lerp(GOLD_L, PALE, 0.4), 0.22)
    LN((62, -8), (78, -1.5), 1.0, lerp(GOLD_L, PALE, 0.4), 0.15)
    LN((78, -1.5), (95, 5.5), 0.8, lerp(GOLD_L, PALE, 0.4), 0.12)
    # brow ridge: a short tapered arc hugging the skull's curve,
    # fading into the head shading at its rear end — not a plank
    brow = [(26, -26), (16, -28.5), (6, -26.5), (-2, -22.5)]
    brow_w = [4.8, 3.8, 2.7]
    brow_a = [0.50, 0.44, 0.33]
    for i in range(3):
        LN(brow[i], brow[i + 1], brow_w[i], shade(AMBER_D, 0.85),
           brow_a[i])
    LN((26, -28.2), (16, -30.2), 1.5, lerp(GOLD_L, PALE, 0.4), 0.38)
    LN((16, -30.2), (7, -28.8), 1.3, lerp(GOLD_L, PALE, 0.4), 0.24)

    # the eye: an almond of deep amber around a dark pupil, a dark
    # eye-line trailing from its rear corner, one small catchlight
    def almond(cx, cy, w, h, tilt, c):
        ca, sa = math.cos(tilt), math.sin(tilt)
        pts = []
        for i in range(13):
            t = i / 12
            lx = (t - 0.5) * w
            ly = -math.sin(math.pi * t) * h * 0.60
            pts.append((cx + lx * ca - ly * sa, cy + lx * sa + ly * ca))
        for i in range(12, -1, -1):
            t = i / 12
            lx = (t - 0.5) * w
            ly = math.sin(math.pi * t) * h * 0.40
            pts.append((cx + lx * ca - ly * sa, cy + lx * sa + ly * ca))
        PL(pts, lambda qx, qy: c)

    HM(9, -8, 20, EMBER, 0.12, mode="add")
    LN((18, -9.5), (-5, -12.5), 1.8, rgb(120, 60, 16), 0.50)
    almond(9, -8, 20, 13, -0.16, rgb(236, 120, 26))
    almond(10, -8, 15, 9.5, -0.16, rgb(16, 8, 2))
    DM(5.5, -11.3, 1.3, (1.0, 1.0, 1.0), 0.95)
    DM(13.5, -5.2, 0.8, (1.0, 1.0, 1.0), 0.5)
    # crest: real feathers — tapered barbed vanes whose base points
    # are staggered along the crown arc, streaming back like flame
    HM(-38, -40, 34, rgb(255, 180, 80), 0.10, mode="add")
    for (dx, dy, deg, ln, semi, bend) in [
            (-30, -15, -170, 88, 8.5, -0.44),
            (-24, -23, -160, 96, 9, -0.38),
            (-16, -30, -148, 84, 8.5, -0.32)]:
        FT(dx, dy, deg, ln, semi, shade(lerp(AMBER, GOLD, 0.30), 0.92),
           GOLD_L, bend=bend, sheen=True, shaft=False, barbs=6,
           barb_gain=1.15, rim=0.50)
    for (dx, dy, deg, ln, semi, bend, ct) in [
            (0, -36, -118, 74, 10, -0.14, PALE),
            (-8, -35, -130, 92, 11, -0.20, GOLD_L),
            (-16, -33, -142, 104, 11, -0.27, lerp(GOLD_L, EMBER, 0.10)),
            (-24, -28, -154, 98, 10, -0.34, GOLD_L),
            (-30, -22, -165, 84, 9.5, -0.41, PALE),
            (-35, -15, -174, 66, 8.5, -0.46, GOLD_L)]:
        FT(dx, dy, deg, ln, semi, lerp(AMBER, GOLD, 0.32), ct,
           bend=bend, sheen=True, shaft=False, barbs=6, barb_gain=1.2,
           rim=0.50)


def paint_phoenix():
    paint_breach_light()
    paint_bird_shadow()
    paint_tail()
    paint_wing(False)
    paint_wing(True)
    paint_body()
    paint_head()
    # a last breath of light at the breast
    CV.glow(512, 450, 96, rgb(255, 172, 72), 0.15)


# ---------------------------------------------------------------------------
# torn canvas: holes, hinged flaps, one fallen flap, flying shards.
# HARD RULE: every torn element — hole, flap, shred, shard, and every
# cast shadow — keeps at least ~25px of clean painting between itself
# and any part of the bird. The cluster positions below were placed
# and measured against that rule.
# ---------------------------------------------------------------------------

def _torn(points, amp, dice, vertex_jitter=0.0):
    """Tear a polygon outline: optionally jitter the vertices, then
    subdivide every edge with jittered midpoints."""
    pts = points
    if vertex_jitter > 0.0:
        pts = [(x + dice.f(-vertex_jitter, vertex_jitter),
                y + dice.f(-vertex_jitter, vertex_jitter))
               for (x, y) in pts]
    out = []
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        out.append((ax, ay))
        ex, ey = bx - ax, by - ay
        el = math.hypot(ex, ey) or 1.0
        nx, ny = -ey / el, ex / el
        for tt in (0.34, 0.67):
            off = dice.f(-amp, amp)
            out.append((ax + ex * tt + nx * off, ay + ey * tt + ny * off))
    return out


def _xform(points, dx, dy, ang, center):
    cx, cy = center
    ca, sa = math.cos(ang), math.sin(ang)
    return [(cx + (x - cx) * ca - (y - cy) * sa + dx,
             cy + (x - cx) * sa + (y - cy) * ca + dy)
            for (x, y) in points]


def _flap_face(pts, free_edge, fragments, strokes, shadow_off=(7, 10),
               shadow_a=0.44, c_top=rgb(178, 144, 94),
               c_bot=rgb(118, 90, 52)):
    """Paint one torn flap: cast shadow, linen face with fragments of
    the painting still on it, a lit curled free edge, frayed fibres."""
    sh = [(x + shadow_off[0], y + shadow_off[1]) for (x, y) in pts]
    CV.poly_a(sh, (0.0, 0.0, 0.0), shadow_a)
    ys = [p[1] for p in pts]
    CV.poly(pts, vshader(c_top, c_bot, min(ys), max(ys)))
    for (fx, fy, fr, fc, fa) in fragments:
        CV.glow(fx, fy, fr, fc, fa, mode="mix")
    for ((sx0, sy0), (sx1, sy1), sc, sa) in strokes:
        CV.line(sx0, sy0, sx1, sy1, 2.6, sc, sa)
    (ax, ay), (bx, by) = free_edge
    emx, emy = (ax + bx) / 2, (ay + by) / 2
    att = max(pts, key=lambda p: (p[0] - emx) ** 2 + (p[1] - emy) ** 2)
    CV.glow(att[0], att[1], 27, rgb(40, 30, 16), 0.20, mode="mix")
    CV.glow(emx, emy, 24, rgb(255, 222, 164), 0.16)
    CV.line(ax, ay, bx, by, 2.6, rgb(255, 240, 205), 0.22)
    CV.line(ax, ay, bx, by, 1.2, rgb(255, 250, 228), 0.30)
    cy_mean = sum(p[1] for p in pts) / len(pts)
    for k in range(3):
        t = 0.25 + 0.25 * k
        mx, my = ax + (bx - ax) * t, ay + (by - ay) * t
        dx = (mx - 512) * 0.06 + (1.5 if mx > 512 else -1.5)
        dy = -4.5 if cy_mean > my else 4.5
        CV.line(mx, my, mx + dx, my + dy, 0.8, rgb(92, 74, 46), 0.5)
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        CV.line(x0, y0, x1, y1, 1.1, rgb(84, 66, 40), 0.45)


def _hinged_flap(hole_pts, flap_pts, hinge, free_edge, fragments,
                 strokes, seed):
    """A flap still hinged to the hole it tore from: dark hole, the
    flap folded out of it, a crease-and-fold hinge line between."""
    dice = Rng(seed)
    hole = _torn(hole_pts, 5.6, dice, vertex_jitter=7.0)
    hcx = sum(p[0] for p in hole) / len(hole)
    hcy = sum(p[1] for p in hole) / len(hole)
    hole_in = [(hcx + (x - hcx) * 0.94, hcy + (y - hcy) * 0.94)
               for (x, y) in hole]
    ys = [p[1] for p in hole_in]
    CV.poly(hole_in, vshader(rgb(36, 29, 19), rgb(20, 16, 11),
                             min(ys), max(ys)))
    CV.glow(hcx, hcy, 30, (0.0, 0.0, 0.0), 0.22, mode="mix")
    _flap_face(_torn(flap_pts, 3.4, Rng(seed + 5)), free_edge, fragments,
               strokes, shadow_off=(9, 13), shadow_a=0.46)
    (ax, ay), (bx, by) = hinge
    CV.line(ax, ay - 2.5, bx, by - 2.5, 1.8, rgb(14, 11, 7), 0.55)
    CV.line(ax, ay, bx, by, 2.2, rgb(255, 240, 205), 0.38)
    CV.line(ax, ay, bx, by, 1.0, rgb(255, 250, 228), 0.45)


def paint_debris():
    # the fallen flap: it tore clean off, leaving a dark hole with
    # two shreds clinging to the rim; the flap itself falls away
    # down-left with air beneath it
    flap1 = [(322, 472), (366, 494), (350, 542), (314, 530), (312, 498)]
    hole = _torn(flap1, 5.2, Rng(11), vertex_jitter=7.0)
    hcx = sum(p[0] for p in hole) / len(hole)
    hcy = sum(p[1] for p in hole) / len(hole)
    hole = [(hcx + (x - hcx) * 0.92, hcy + (y - hcy) * 0.92)
            for (x, y) in hole]
    ys = [p[1] for p in hole]
    CV.poly(hole, vshader(rgb(37, 30, 20), rgb(22, 18, 12),
                          min(ys), max(ys)))
    CV.glow(hcx, hcy, 30, (0.0, 0.0, 0.0), 0.22, mode="mix")
    for shred in [[(314, 494), (331, 487), (323, 509)],
                  [(357, 533), (369, 519), (373, 541)]]:
        CV.poly(_torn(shred, 1.6, Rng(5)),
                vshader(rgb(192, 162, 116), rgb(150, 120, 76), 487, 543))
    f1c = (sum(p[0] for p in flap1) / len(flap1),
           sum(p[1] for p in flap1) / len(flap1))
    fell = _xform(_torn(flap1, 3.4, Rng(23), vertex_jitter=5.0),
                  -24, 36, -0.30, f1c)
    fell_edge = _xform([(366, 494), (350, 542)], -24, 36, -0.30, f1c)
    _flap_face(fell, (fell_edge[0], fell_edge[1]),
               [(310, 540, 13, rgb(52, 74, 74), 0.24),
                (326, 564, 8, rgb(46, 54, 38), 0.27)],
               [((304, 544), (324, 536), rgb(52, 74, 74), 0.35),
                ((312, 560), (330, 554), rgb(90, 70, 40), 0.30)],
               shadow_off=(11, 15), shadow_a=0.46,
               c_top=rgb(160, 126, 80), c_bot=rgb(106, 82, 48))
    # right side: a flap folded down out of its hole in the open
    # canvas right of the tail, below the wing's reach; its face
    # carries the full painting language — sky and hill fragments
    # plus a horizon stroke
    _hinged_flap([(686, 528), (722, 516), (756, 536), (752, 576),
                  (718, 590), (686, 566)],
                 [(688, 562), (720, 584), (730, 626), (704, 648),
                  (674, 620), (672, 586)],
                 ((688, 564), (720, 586)), ((730, 626), (704, 648)),
                 [(704, 610, 12, rgb(52, 74, 74), 0.26),
                  (722, 590, 8, rgb(140, 116, 70), 0.24),
                  (686, 624, 7, rgb(52, 58, 42), 0.26),
                  (698, 584, 10, rgb(52, 74, 74), 0.30),
                  (720, 616, 9, rgb(52, 58, 42), 0.30),
                  (704, 600, 7, rgb(168, 142, 92), 0.22)],
                 [((686, 608), (714, 600), rgb(52, 74, 74), 0.34),
                  ((692, 626), (720, 620), rgb(150, 120, 66), 0.28),
                  ((684, 602), (728, 594), rgb(52, 74, 74), 0.36),
                  ((688, 630), (722, 622), rgb(52, 58, 42), 0.30)],
                 seed=47)
    # lower left: a smaller torn piece hinged out of its own hole,
    # clear of the tail ribbons and of the fallen flap above it
    _hinged_flap([(288, 637), (322, 627), (342, 651), (334, 681),
                  (302, 687), (284, 663)],
                 [(300, 679), (332, 673), (340, 707), (318, 727),
                  (290, 709), (286, 685)],
                 ((301, 681), (333, 675)), ((340, 707), (318, 727)),
                 [(312, 697, 9, rgb(52, 74, 74), 0.25),
                  (326, 713, 6, rgb(52, 58, 42), 0.26)],
                 [((298, 697), (324, 691), rgb(52, 74, 74), 0.32),
                  ((302, 713), (326, 709), rgb(110, 88, 50), 0.26)],
                 seed=71)
    # shards of canvas and gilt flying outward — irregular torn
    # fragments (never clean rectangles), rejection-sampled against
    # proxy volumes of the bird so the >=25px rule holds for them too
    def near_bird(x, y):
        if ((x - 512) / 112.0) ** 2 + ((y - 462) / 158.0) ** 2 < 1.0:
            return True
        if ((x - 584) / 124.0) ** 2 + ((y - 282) / 120.0) ** 2 < 1.0:
            return True
        if y < 508 and 282 < x < 742:
            return True
        if abs(x - 507) < 155 and y >= 472:
            return True
        return False

    dice = Rng(777)
    placed, tries = 0, 0
    while placed < 16 and tries < 300:
        tries += 1
        a = dice.f(0, math.pi * 2)
        rr = dice.f(96, 215)
        x = 512 + math.cos(a) * rr
        y = 450 + math.sin(a) * rr * 0.9
        if near_bird(x, y):
            continue
        c = dice.pick([rgb(150, 128, 88), rgb(196, 158, 84),
                       rgb(90, 84, 60), rgb(232, 190, 96)])
        w = dice.f(5, 15)
        h = dice.f(3, 7)
        rot = dice.f(0, 3.1)
        al = dice.f(0.5, 0.95)
        rect_pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2),
                    (-w / 2, h / 2)]
        pts = _xform(_torn(rect_pts, 1.8, Rng(1000 + placed)),
                     x, y, rot, (0, 0))
        CV.poly_a(pts, shade(c, 0.92), al)
        CV.line(pts[0][0], pts[0][1], pts[1][0], pts[1][1], 1.0,
                shade(c, 0.55), al * 0.8)
        placed += 1


# ---------------------------------------------------------------------------
# the air in front of the painting: embers and gold-leaf flakes
# ---------------------------------------------------------------------------

def _off_face(x, y):
    """No ember or flake may land on the face — flat spark dots on
    the head read as spots. Positions inside the head's ellipse are
    pushed radially well clear of it."""
    ex = (x - 588) / 94.0
    ey = (y - 284) / 80.0
    d2 = ex * ex + ey * ey
    if d2 < 1.0:
        s = 1.22 / max(0.25, math.sqrt(d2))
        return 588 + (x - 588) * s, 284 + (y - 284) * s
    return x, y


def paint_air():
    dice = Rng(60606)
    # embers drifting through the gallery air: one per cell of a
    # jittered 10x10 stratification, so no rows or columns can form
    cells = [(i, j) for j in range(10) for i in range(10)]
    for (ci, cj) in cells[:95]:
        x = 82 + ci * 86 + dice.f(8, 78)
        y = 130 + cj * 73 + dice.f(8, 65)
        x, y = _off_face(x, y)
        r = dice.f(0.8, 2.6)
        CV.disc_a(x, y, r * 2.1, EMBER, 0.30)
        CV.disc_a(x, y, r, rgb(255, 196, 100), 0.9)
    # soft ember glows hanging near the breach
    for _ in range(18):
        x = 512 + dice.f(-380, 380)
        y = 450 + dice.f(-280, 300)
        x, y = _off_face(x, y)
        CV.glow(x, y, dice.f(7, 15), rgb(255, 150, 50), 0.16)
    # gold-leaf flakes torn from the frame, still falling — only in
    # front of the canvas, each with a soft shadow on the painting
    # behind it, so they float instead of adhering to any surface
    cx0, cy0, cx1, cy1 = CANVAS_R
    for _ in range(48):
        x = dice.f(cx0 + 6, cx1 - 6)
        y = dice.f(cy0 + 6, cy1 - 6)
        x, y = _off_face(x, y)
        c = dice.pick([GILT_H, GILT_B, rgb(255, 214, 120)])
        w = dice.f(3, 8)
        h = dice.f(2, 4.5)
        CV.glow(x + 3, y + 5, w * 0.9, (0.0, 0.0, 0.0), 0.16,
                mode="mix")
        base = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2),
                (-w / 2, h / 2)]
        pts = _xform(_torn(base, 1.1, Rng(int(x * 7 + y))), x, y,
                     dice.f(0, 3.1), (0, 0))
        al = dice.f(0.45, 0.95)
        CV.poly_a(pts, shade(c, 0.94), al)
        CV.line(pts[0][0], pts[0][1], pts[1][0], pts[1][1], 0.8,
                shade(c, 0.6), al * 0.8)
    # two big blurred embers close to the viewer, bottom corners
    CV.glow(138, 828, 30, rgb(255, 130, 40), 0.10)
    CV.glow(892, 872, 36, rgb(255, 130, 40), 0.09)


# ---------------------------------------------------------------------------
# finishing: downsample, bloom, light grade, PNG
# ---------------------------------------------------------------------------

def downsample():
    """Average each 2x2 block of the float planes into byte planes."""
    out = [bytearray(SIDE * SIDE) for _ in range(3)]
    planes = (CV.r, CV.g, CV.b)
    for ch in range(3):
        src, dst = planes[ch], out[ch]
        for y in range(SIDE):
            r0 = (2 * y) * RW
            r1 = r0 + RW
            o = y * SIDE
            for x in range(SIDE):
                i0 = r0 + 2 * x
                i1 = r1 + 2 * x
                v = (src[i0] + src[i0 + 1] + src[i1] + src[i1 + 1]) * 63.75
                dst[o + x] = 0 if v < 0 else (255 if v > 255 else int(v))
    return out


def make_bloom(img):
    """Glow map: the brightest golds, captured at quarter resolution
    and blurred until they bleed. Returned as three 256x256 planes."""
    qw = SIDE // 4
    src = [bytearray(qw * qw) for _ in range(3)]
    for y in range(qw):
        for x in range(qw):
            best, bl = 0, -1
            for dy in range(4):
                base = (4 * y + dy) * SIDE + 4 * x
                for dx in range(4):
                    i = base + dx
                    lum = img[0][i] + img[1][i] + img[2][i]
                    if lum > bl:
                        bl, best = lum, i
            over = bl - 585
            if over > 0:
                f = min(1.0, over / 175.0)
                o = y * qw + x
                for ch in range(3):
                    src[ch][o] = int(img[ch][best] * f)
    kern = (1, 2, 3, 2, 1)

    def blur(plane, horizontal):
        out = bytearray(qw * qw)
        for y in range(qw):
            for x in range(qw):
                acc = 0
                for k in range(-2, 3):
                    xx = min(qw - 1, max(0, x + k)) if horizontal else x
                    yy = y if horizontal else min(qw - 1, max(0, y + k))
                    acc += plane[yy * qw + xx] * kern[k + 2]
                out[y * qw + x] = acc // 9
        return out

    for _ in range(3):
        src = [blur(p, True) for p in src]
        src = [blur(p, False) for p in src]
    return src


_grain_rng = Rng(777)
GRAIN = [int(_grain_rng.f(-5, 5.999)) for _ in range(256 * 256)]


def light_pass(img, bloom):
    """Spotlight from the upper left, the phoenix's own halo, bloom
    off the brightest gold, a sheen on the floor, a vignette, and a
    whisper of grain over everything. img is three byte planes;
    returns them."""
    qw = SIDE // 4
    spot_x = []
    halo_x = []
    vig_x = []
    for x in range(SIDE):
        sdx = (x - 400) / 580.0
        spot_x.append(sdx * sdx)
        hdx = (x - 512) / 380.0
        halo_x.append(hdx * hdx)
        vx = (x - 512) / 512.0
        vig_x.append(vx * vx)
    for y in range(SIDE):
        sdy = (y - 330) / 540.0
        spot_y = sdy * sdy
        hdy = (y - 430) / 370.0
        halo_y = hdy * hdy
        vy = (y - 512) / 512.0
        vig_y = vy * vy
        grow = (y & 255) * 256
        brow = (y >> 2) * qw
        sheen_y = 0.0
        if y > FLOOR_Y:
            sheen_y = 1.0 - (y - FLOOR_Y) / 140.0
        o = y * SIDE
        for x in range(SIDE):
            spot = 1.0 - (spot_x[x] + spot_y)
            if spot < 0.0:
                spot = 0.0
            spot = spot * spot * 46.0
            halo = 1.0 - (halo_x[x] + halo_y)
            if halo < 0.0:
                halo = 0.0
            halo = halo * halo * 34.0
            vig = 1.0 - 0.40 * max(0.0, vig_x[x] + vig_y - 0.26)
            sheen = 0.0
            if sheen_y > 0.0:
                fx = 1.0 - abs(x - 512) / 430.0
                if fx > 0.0:
                    sheen = fx * fx * sheen_y * 15.0
            i = o + x
            bi = brow + (x >> 2)
            warm = spot + sheen
            r = img[0][i] * vig + warm + halo + bloom[0][bi] * 0.50
            g = img[1][i] * vig + warm * 0.78 + halo * 0.66 \
                + bloom[1][bi] * 0.50
            b = img[2][i] * vig + warm * 0.44 + halo * 0.30 \
                + bloom[2][bi] * 0.50
            lum = (r + g + b) / 3.0
            gr = GRAIN[grow + (x & 255)] * (1.15 - 0.006 * min(255.0, lum))
            img[0][i] = 0 if r + gr < 0 else (255 if r + gr > 255
                                              else int(r + gr))
            img[1][i] = 0 if g + gr < 0 else (255 if g + gr > 255
                                              else int(g + gr))
            img[2][i] = 0 if b + gr < 0 else (255 if b + gr > 255
                                              else int(b + gr))
    return img


def write_png(path, img):
    """PNG with a per-row filter chosen by sampled SAD over four
    candidate filters (none/sub/up/average)."""
    W3 = SIDE * 3
    raw = bytearray()
    prev = bytes(W3)
    for y in range(SIDE):
        line = bytearray(W3)
        line[0::3] = bytes(img[0][y * SIDE:(y + 1) * SIDE])
        line[1::3] = bytes(img[1][y * SIDE:(y + 1) * SIDE])
        line[2::3] = bytes(img[2][y * SIDE:(y + 1) * SIDE])
        line = bytes(line)
        cands = [(0, line)]
        sub = bytes((line[i] - (line[i - 3] if i >= 3 else 0)) & 0xFF
                    for i in range(W3))
        cands.append((1, sub))
        up = bytes((line[i] - prev[i]) & 0xFF for i in range(W3))
        cands.append((2, up))
        avg = bytes((line[i] - (((line[i - 3] if i >= 3 else 0)
                                  + prev[i]) >> 1)) & 0xFF
                    for i in range(W3))
        cands.append((3, avg))

        def sad(data):
            return sum(abs(v - 256 if v > 127 else v)
                       for v in data[::37])
        best = min(cands, key=lambda c: sad(c[1]))
        raw.append(best[0])
        raw += best[1]
        prev = line
    comp = zlib.compress(bytes(raw), 9)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", SIDE, SIDE, 8, 2,
                                        0, 0, 0))
           + chunk(b"IDAT", comp)
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def main():
    paint_wall()
    paint_floor()
    cast_frame_shadow()
    paint_beam()
    paint_frame()
    paint_picture()
    paint_ghost()
    paint_cracks()
    paint_weave()
    paint_phoenix()
    paint_debris()
    paint_air()
    paint_plaque()
    img = downsample()
    img = light_pass(img, make_bloom(img))
    if len(sys.argv) > 1:
        out = sys.argv[1]
    else:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "entry.png")
    write_png(out, img)


if __name__ == "__main__":
    main()
