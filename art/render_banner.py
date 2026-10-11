"""Render a pixel-art banner from Voidrunner's launch splash.

Reuses the splash's own planet shader, wordmark font, gradient and palette, but
composes them on a real pixel canvas (320x160, scaled 4x to 1280x640, GitHub's
social-preview size) instead of half-block terminal cells.

usage: python art/render_banner.py <path to voidrunner.py> art/banner.png [scale]

No dependencies beyond the standard library; it writes the PNG itself.
"""
import importlib.util
import math
import struct
import sys
import types
import zlib

src, out_path = sys.argv[1], sys.argv[2]
SCALE = int(sys.argv[3]) if len(sys.argv) > 3 else 4
spec = importlib.util.spec_from_file_location("voidrunner", src)
vr = importlib.util.module_from_spec(spec)
sys.modules["voidrunner"] = vr
spec.loader.exec_module(vr)
mix, scale = vr._mix, vr._scale

W, H = 320, 160
BG_TOP, BG_BOTTOM = (6, 6, 20), (16, 10, 34)
img = [[mix(BG_TOP, BG_BOTTOM, y / (H - 1)) for _ in range(W)] for y in range(H)]


def put(x, y, c):
    if 0 <= x < W and 0 <= y < H and c is not None:
        img[y][x] = c


def blend(x, y, c, a):
    if 0 <= x < W and 0 <= y < H:
        img[y][x] = mix(img[y][x], c, a)


# --- a faint nebula behind everything, ordered-dithered so it stays pixel art
BAYER = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))
for y in range(H):
    for x in range(W):
        n = (math.sin(x * 0.031 + y * 0.012) + math.sin(x * 0.013 - y * 0.041 + 1.7)
             + 0.6 * math.sin(x * 0.07 + y * 0.05)) / 2.6
        d = math.hypot((x - 90) / 120, (y - 50) / 70)
        level = max(0.0, n * 0.5 + 0.35 - d * 0.35)
        steps = level * 3
        if steps - int(steps) > (BAYER[y % 4][x % 4] + 0.5) / 16:
            steps = int(steps) + 1
        else:
            steps = int(steps)
        if steps:
            img[y][x] = mix(img[y][x], (60, 24, 92), min(1.0, steps * 0.22))

# --- stars: deterministic, three layers, the splash's own colours
seed = zlib.crc32(b"Voidrunner") or 1
for i in range(260):
    seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
    x = seed % W
    seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
    y = seed % (H - 2)
    layer = i % 9
    if 94 <= y <= 146 and 40 <= x <= 280:
        continue  # keep the lettering clean
    if layer == 0:  # bright cross-shaped star
        c = (200, 214, 255)
        put(x, y, (255, 255, 255))
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            blend(x + dx, y + dy, c, 0.55)
    elif layer < 4:
        put(x, y, (120, 132, 170))
    else:
        blend(x, y, (82, 94, 128), 0.8)

# --- the ringed gas giant, using the splash's shader directly
planet = types.SimpleNamespace(radius=34.0, planet_x=236.0, planet_y=58.0, rings=True)
front = {}
for y in range(H):
    for x in range(W):
        c, is_front = vr._planet_pixel(planet, x, y)
        if c is None:
            continue
        if is_front:
            front[(x, y)] = c
        put(x, y, c)

# --- the ship, a larger sprite in the splash's ship palette, on a plasma trail
SHIP = (
    "......ff..............",
    ".....fff..............",
    "....lllllll...........",
    "...llllllllllcc.......",
    "..lllllllllllcccl.....",
    "hhhhhhhhhhhhhhhhhhhhn.",
    "hhhhhhhhhhhhhhhhhhhhhn",
    ".dddddddddddddddddd...",
    "..ddddddddddddd.......",
    "....ddddddd...........",
    ".....fff..............",
    "......ff..............",
)
COLORS = dict(vr._SPLASH_SHIP_COLORS)
ship_x, ship_y = 118, 68  # top-left of the sprite
mid = ship_y + 5
trail_len = 112
for d in range(1, trail_len + 1):
    x = ship_x - d
    heat = 1.0 - d / trail_len
    level = math.ceil(heat * 6) / 6
    core = mix((70, 30, 110), (255, 140, 225), level)
    for dy, strength in ((0, 1.0), (1, 1.0), (-1, 0.55), (2, 0.55), (-2, 0.25), (3, 0.25)):
        if strength < 1.0 and d > trail_len * (0.5 if strength > 0.3 else 0.25):
            continue
        blend(x, mid + dy, core, strength * (0.35 + 0.65 * heat))
for ry, row in enumerate(SHIP):
    for rx, cell in enumerate(row):
        if cell != ".":
            c = COLORS[cell]
            if cell in "hd" and ry in (6, 7):
                c = scale(c, 0.9)
            put(ship_x + rx, ship_y + ry, c)
# engine glow at the tail
for dy in (0, 1):
    put(ship_x - 1, mid + dy, (255, 236, 250))
    blend(ship_x - 2, mid + dy, (255, 160, 230), 0.8)

# rings that pass in front of the planet stay in front of it
for (x, y), c in front.items():
    put(x, y, c)

# --- the wordmark: the splash's large face, doubled, gradient and shadow
font = vr._SPLASH_FONT_LARGE
gw, gh = len(font["V"][0]), len(font["V"])
logo_w = len(vr._SPLASH_WORD) * (gw + 1) - 1
layout = types.SimpleNamespace(font=font, logo_w=logo_w, logo_px_h=gh)
PX = 3
lx0 = (W - logo_w * PX) // 2
ly0 = 99
for py in range(gh + 1):
    for px in range(logo_w + 1):
        c = vr._logo_pixel(layout, px, py)
        if c is None:
            continue
        for sy in range(PX):
            for sx in range(PX):
                put(lx0 + px * PX + sx, ly0 + py * PX + sy, c)

# --- subtitle in a 3x5 pixel face, the splash's steel `hull` colour
F3 = {k: "".join(v.split()) for k, v in {
    "A": ".#. #.# ### #.# #.#", "C": "### #.. #.. #.. ###", "D": "##. #.# #.# #.# ##.",
    "E": "### #.. ##. #.. ###", "G": "### #.. #.# #.# ###", "I": "### .#. .#. .#. ###",
    "L": "#.. #.. #.. #.. ###", "N": "##. #.# #.# #.# #.#", "O": "### #.# #.# #.# ###",
    "P": "### #.# ### #.. #..", "R": "##. #.# ##. #.# #.#", "S": "### #.. ### ..# ###",
    "T": "### .#. .#. .#. .#.", "X": "#.# #.# .#. #.# #.#", "&": ".#. #.# .#. #.# .##",
    "-": "... ... ### ... ...", " ": "... ... ... ... ...",
}.items()}
subtitle = "TACTICAL DEEP-SPACE TRADING AND EXPLORATION"
sw = len(subtitle) * 4 - 1
sx0, sy0 = (W - sw) // 2, 134
for i, ch in enumerate(subtitle):
    g = F3[ch]
    for r in range(5):
        for c in range(3):
            if g[r * 3 + c] == "#":
                put(sx0 + i * 4 + c, sy0 + r, (127, 163, 191))

# a thin chrome rule under the subtitle, like the deck's frames
for x in range(sx0, sx0 + sw):
    t = abs((x - W / 2) / (sw / 2))
    blend(x, sy0 + 9, (127, 163, 191), max(0.0, 0.6 - t * 0.6))

# --- write a PNG with nothing but the standard library
rows = []
for y in range(H * SCALE):
    src_row = img[y // SCALE]
    line = bytearray(b"\x00")
    for x in range(W * SCALE):
        line.extend(bytes(src_row[x // SCALE]))
    rows.append(bytes(line))


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W * SCALE, H * SCALE, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")
with open(out_path, "wb") as f:
    f.write(png)
print(out_path, W * SCALE, H * SCALE)
