"""Match the two supplied Discord buttons using color runs and a full template check."""

import re
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ASSETS = Path(__file__).resolve().parents[1] / "assets"
SCALES = (1, 0.9, 1.1, 0.8, 1.25, 1.5, 0.67, 0.75, 1.75, 2, 0.6, 2.5, 3)
RUN = re.compile(b"\xff{3,}")


def mask(image, color):
    bands = [
        b.point([255 if abs(x - v) <= 10 else 0 for x in range(256)])
        for b, v in zip(image.split(), color)
    ]
    return ImageChops.darker(ImageChops.darker(bands[0], bands[1]), bands[2])


def compact_template(file):
    im = Image.open(file).convert("RGB")
    bg = im.getpixel((0, 0))
    fg = sorted(im.getcolors(), reverse=True)[1][1]
    box = ImageChops.difference(im, Image.new("RGB", im.size, bg)).getbbox()
    x, y, r, b = box
    return (
        im.crop((max(0, x - 3), max(0, y - 3), min(im.width, r + 3), min(im.height, b + 3))),
        bg,
        fg,
    )


def color_runs(bitmap):
    w, h = bitmap.size
    data = bitmap.tobytes()
    rows = []
    for y in range(h - 1, -1, -1):
        row = data[y * w : (y + 1) * w]
        rows.extend(
            (m.start(), y, m.end() - m.start())
            for m in reversed(list(RUN.finditer(row)))
            if m.end() - m.start() <= 110
        )
        if len(rows) > 25000:
            break
    return rows


def locate(image, dpr=1):
    image = image.convert("RGB")
    best, best_score = None, 0.115
    for file in ("send_idle.png", "send_ready.png"):
        base, bg, fg = compact_template(ASSETS / file)
        candidates = color_runs(mask(image, fg))
        contrast = sum(abs(a - b) for a, b in zip(fg, bg)) / 3
        scales = dict.fromkeys([1, round(dpr, 2), *SCALES])
        for scale in scales:
            w, h = (max(5, round(v * scale)) for v in base.size)
            tile = base.resize((w, h), Image.Resampling.BILINEAR)
            runs = color_runs(mask(tile, fg))
            if not runs:
                continue
            dx, dy, target = max(runs, key=lambda z: z[2])
            for x, y, n in candidates:
                if abs(n - target) > max(2, round(scale)):
                    continue
                left, top = x - dx, y - dy
                if left < 0 or top < 0 or left + w > image.width or top + h > image.height:
                    continue
                if max(abs(a - b) for a, b in zip(bg, image.getpixel((left, top)))) > 14:
                    continue
                crop = image.crop((left, top, left + w, top + h))
                error = sum(ImageStat.Stat(ImageChops.difference(crop, tile)).mean) / (3 * contrast)
                if error < best_score:
                    best_score, best = error, (left + w / 2, top + h / 2, scale)
                    if error < 0.012:
                        return best
    return best


def composer_point(image, arrow):
    """Locate the text area in the same rounded input bar as a matched send button."""
    x, y, scale = arrow
    x, y = round(x), round(y)

    def px(value):
        return round(value * scale)

    if not (px(20) < y < image.height - px(16) and px(16) < x < image.width - px(12)):
        return None
    # Sample above and below letters, emoji and buttons. The margin of both reference
    # templates is the input bar color. Allow for blending at different Discord zooms.
    bg = image.getpixel((x + px(13), y + px(10)))[:3]
    if max(abs(a - b) for a, b in zip(bg, (57, 58, 65))) > 13:
        return None
    pixels = image.load()

    def background(col, row):
        return max(abs(a - b) for a, b in zip(pixels[col, row][:3], bg)) <= 4

    samples = [y + px(n) for n in (-15, -12, 12, 15)]

    def edge(columns, step):
        gap = 0
        for col in columns:
            if sum(background(col, row) for row in samples) >= 2:
                gap = 0
            else:
                gap += 1
                if gap >= max(4, px(5)):
                    return col - gap * step
        return None

    left = edge(range(x - px(16), -1, -1), -1)
    right = edge(range(x + px(14), image.width), 1)
    if left is None:
        return None
    if right is None:
        right = image.width - 1
    if not px(14) <= right - x <= px(85):
        return None
    # Leave room for the attachment button on the left and Discord's tool buttons on
    # the right. Never click the send arrow or a fixed fraction of the window.
    point = min(left + px(108), x - px(320))
    if point < left + px(82):
        return None

    def bottom(col):
        gap = 0
        for row in range(y + px(16), min(image.height, y + px(94))):
            gap = 0 if background(col, row) else gap + 1
            if gap >= max(3, px(4)):
                return row - gap
        return None

    # Chat messages can contain pictures of arrows. Require both ends of this bar to
    # share their lower edge and sit near the bottom of the Discord client.
    edges = bottom(point), bottom(x + px(14))
    if None in edges or abs(edges[0] - edges[1]) > px(5) or image.height - edges[1] > px(100):
        return None
    return point, y
