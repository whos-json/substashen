# Builds substashen.ico: a one-line-diagram glyph (yellow busbar feeding two lines, each with a red breaker),
# drawn separately at every size so the small ones stay crisp instead of being shrunk from 256 px.
import os
from PIL import Image, ImageDraw

BG, EDGE, BUS, BRK, LOAD = (11, 12, 14, 255), (51, 117, 140, 255), (227, 207, 62, 255), (209, 64, 56, 255), (82, 207, 245, 255)


def glyph(n):
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    u = n / 16  # design grid: 16 x 16
    px = lambda v: round(v * u)
    d.rounded_rectangle([0, 0, n - 1, n - 1], radius=max(1, px(2.5)), fill=BG, outline=EDGE if n >= 24 else None)
    lw = max(1, px(1.2))
    d.rectangle([px(3.5), px(3), px(3.5) + max(1, px(1.6)), px(13)], fill=BUS)          # vertical busbar
    for y in (5.5, 10.5):                                                              # two feeders
        d.rectangle([px(4.5), px(y) - lw // 2, px(13), px(y) - lw // 2 + lw - 1], fill=BUS)
        s = max(2, px(2.6))                                                            # breaker
        bx, by = px(7.2), px(y) - s // 2
        d.rectangle([bx, by, bx + s - 1, by + s - 1], fill=BRK)
        if n >= 32:                                                                    # load marker at the end
            d.rectangle([px(12.2), px(y) - px(1.2), px(13.6), px(y) + px(1.2)], outline=LOAD, width=max(1, px(0.4)))
    return im


out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "substashen.ico")
sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
imgs = [glyph(s) for s in sizes]
imgs[-1].save(out, format="ICO", sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
glyph(256).save(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon_preview.png"))
print("wrote", out)
