"""Pixel-art app icon: a pair of untied red sneakers resting on a wooden floor.

Writes crisp PNGs with no dependencies (art is a 32x32 grid, scaled by whole numbers).
Usage: python3 tools/make_icons.py runapp/static/icons
"""
import struct
import sys
import zlib

N = 32
WALL = (27, 34, 48)          # app navy
WALL_LOW = (33, 41, 58)      # skirting just above the floor
FLOOR = (112, 76, 52)        # wood
FLOOR_DARK = (92, 61, 41)    # plank seams
FLOOR_LIGHT = (128, 89, 61)  # wood grain highlight
SHADOW = (66, 43, 29)

# Sneaker facing right (heel left, toe right), untied: the tongue flops up out of the
# opening and the eyelets are empty. "." is transparent.
SPRITE = [
    ".........TT.........",
    "........TTTK........",
    "....KKKKTTTK........",
    "...KRRRRKTTK........",
    "...KRLRRRKTKKK......",
    "..KRRRRRRREKEKKK....",
    "..KRRRRRRRRRRRRRKK..",
    ".KRRRRRRRRRRRRRRRRK.",
    ".KRWWWWRRRRRRRRRRRRK",
    ".KDRRRWWWWRRRRRRRLRK",
    "KWWWWWWWWWWWWWWWWWWK",
    ".KGGKGGKGGKGGKGGKGK.",
    "..KKKKKKKKKKKKKKKK..",
]
FRONT = {"K": (42, 10, 16), "R": (226, 52, 52), "D": (160, 28, 38), "L": (255, 132, 112),
         "W": (246, 246, 246), "G": (178, 186, 200), "T": (250, 104, 92), "E": (120, 22, 30)}
BACK = {"K": (30, 8, 12), "R": (186, 38, 46), "D": (128, 22, 32), "L": (220, 98, 88),
        "W": (212, 214, 220), "G": (146, 152, 166), "T": (214, 82, 74), "E": (96, 18, 24)}
LACE = (246, 246, 246)
LACE_BACK = (212, 214, 220)

img = [[WALL] * N for _ in range(N)]


def put(x, y, c):
    if 0 <= x < N and 0 <= y < N:
        img[y][x] = c


# Wall meets floor at y=16; floorboards with staggered seams below.
FLOOR_TOP = 14
for x in range(N):
    put(x, FLOOR_TOP - 1, WALL_LOW)
for y in range(FLOOR_TOP, N):
    for x in range(N):
        put(x, y, FLOOR)
for y in range(FLOOR_TOP, N, 4):
    for x in range(N):
        put(x, y, FLOOR_DARK)
for i, y in enumerate(range(FLOOR_TOP + 1, N, 4)):
    for seam in ((7, 23) if i % 2 == 0 else (15, 30)):
        for dy in range(3):
            put(seam, y + dy, FLOOR_DARK)
for x, y in ((3, 16), (20, 20), (27, 24), (10, 29), (28, 16)):
    for dx in range(3):
        put(x + dx, y, FLOOR_LIGHT)


def shadow(x0, x1, y):
    for x in range(x0, x1):
        put(x, y, SHADOW)


def sprite(ox, oy, pal):
    for dy, row in enumerate(SPRITE):
        for dx, ch in enumerate(row):
            if ch != ".":
                put(ox + dx, oy + dy, pal[ch])


def lace(points, color):
    for x, y in points:
        put(x, y, color)


# Back shoe: further away, so higher up the floor. Sole bottom at y=19.
BX, BY = 9, 7
shadow(BX + 1, BX + 20, BY + 13)
sprite(BX, BY, BACK)
# its laces spill forward over the toe and onto the floor
lace([(BX + 13, BY + 5), (BX + 14, BY + 4), (BX + 15, BY + 4), (BX + 16, BY + 5), (BX + 17, BY + 6),
      (BX + 18, BY + 7), (BX + 19, BY + 8), (BX + 20, BY + 9), (BX + 20, BY + 10), (BX + 20, BY + 11),
      (BX + 19, BY + 12), (BX + 18, BY + 13)], LACE_BACK)

# Front shoe: closer, lower. Sole bottom at y=25.
FX, FY = 2, 13
shadow(FX + 1, FX + 21, FY + 13)
sprite(FX, FY, FRONT)
# two loose laces: one over the side down to the floor, one trailing out in front
lace([(FX + 10, FY + 5), (FX + 9, FY + 6), (FX + 8, FY + 7), (FX + 7, FY + 6), (FX + 6, FY + 5)], LACE)
lace([(FX + 12, FY + 5), (FX + 13, FY + 4), (FX + 14, FY + 4), (FX + 15, FY + 5), (FX + 16, FY + 6),
      (FX + 17, FY + 7), (FX + 18, FY + 8), (FX + 19, FY + 9), (FX + 20, FY + 10), (FX + 21, FY + 11),
      (FX + 21, FY + 12), (FX + 22, FY + 13), (FX + 23, FY + 13), (FX + 24, FY + 13), (FX + 25, FY + 12)], LACE)


def write_png(path, scale, pad=0, pad_color=WALL):
    size = N * scale + 2 * pad
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            ax, ay = (x - pad) // scale, (y - pad) // scale
            inside = x >= pad and y >= pad and ax < N and ay < N
            row += bytes(img[ay][ax] if inside else (FLOOR if y >= pad + FLOOR_TOP * scale else pad_color))
        rows.append(bytes(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))
    open(path, "wb").write(png)


if __name__ == "__main__":
    out = sys.argv[1]
    write_png(f"{out}/icon-512.png", 16)
    write_png(f"{out}/icon-192.png", 6)
    write_png(f"{out}/apple-touch-icon.png", 5, pad=10)
    # Android crops maskable icons to a circle: keep the art inside the central safe zone.
    write_png(f"{out}/icon-maskable.png", 12, pad=64)
    print("written")
