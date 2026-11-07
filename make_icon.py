"""Draws the Monobridge app icon (1024px PNG). Used by build_app.sh."""
import sys

from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# body
d.rounded_rectangle((90, 90, S - 90, S - 90), radius=190, fill=(28, 28, 32, 255))
d.rounded_rectangle((90, 90, S - 90, S - 90), radius=190, outline=(70, 70, 78, 255), width=10)
# three slider tracks with coloured caps
cols = [(255, 0, 255), (12, 235, 235), (255, 230, 0)]
for i, c in enumerate(cols):
    x = 250 + i * 130
    d.rounded_rectangle((x - 14, 250, x + 14, 774), radius=14, fill=(55, 55, 62, 255))
    y = [560, 380, 650][i]
    d.rounded_rectangle((x - 46, y - 30, x + 46, y + 30), radius=16, fill=c + (255,))
# dial
cx, cy, r = 735, 512, 125
d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(45, 45, 52, 255), outline=(10, 102, 255, 255), width=22)
d.line((cx, cy, cx + 60, cy - 70), fill=(235, 235, 235, 255), width=18)
img.save(sys.argv[1] if len(sys.argv) > 1 else "icon.png")


def menubar_icon(path, alpha=255, scale=2):
    """Monochrome 'template' icon for the menu bar (18x18 pt, drawn at @2x).
    macOS recolours it for light/dark menu bars; alpha < 255 = faded (not connected)."""
    W = 18 * scale
    im = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    g = ImageDraw.Draw(im)
    k = (0, 0, 0, alpha)
    u = W / 36.0
    for i, cy in enumerate((20, 11, 24)):             # three slider tracks + caps
        x = (5 + i * 6) * u
        g.rounded_rectangle((x - 1 * u, 5 * u, x + 1 * u, 31 * u), radius=1 * u, fill=k)
        g.rounded_rectangle((x - 2.6 * u, (cy - 2.4) * u, x + 2.6 * u, (cy + 2.4) * u), radius=1.2 * u, fill=k)
    cx, cy, r = 27.5 * u, 18 * u, 6.5 * u              # dial
    g.ellipse((cx - r, cy - r, cx + r, cy + r), outline=k, width=int(2.4 * u))
    g.line((cx, cy, cx + 3.2 * u, cy - 3.8 * u), fill=k, width=int(2.2 * u))
    im.save(path)


if __name__ == "__main__" and len(sys.argv) > 2:
    menubar_icon(sys.argv[2])                  # connected
    menubar_icon(sys.argv[3], alpha=90)        # waiting
