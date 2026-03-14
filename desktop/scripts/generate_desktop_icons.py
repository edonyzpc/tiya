from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = ROOT / "assets"
ICONS_DIR = ASSETS_DIR / "icons"

MASTER_SIZE = 1024
PNG_SIZES = [16, 32, 48, 64, 128, 256, 512, 1024]
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
ICNS_SIZES = [(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)]


def rounded_card(size: int) -> Image.Image:
    card = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    inset = int(size * 0.075)
    radius = int(size * 0.235)
    box = (inset, inset, size - inset, size - inset)

    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    base_draw = ImageDraw.Draw(base)
    base_draw.rounded_rectangle(box, radius=radius, fill=(246, 246, 243, 255))

    edge = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    edge_draw = ImageDraw.Draw(edge)
    edge_draw.rounded_rectangle(
        box,
        radius=radius,
        outline=(222, 223, 217, 255),
        width=max(2, int(size * 0.003)),
    )
    base.alpha_composite(edge)

    card.alpha_composite(base)
    return card


def _glyph_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)

    draw.rounded_rectangle(
        [
            int(size * 0.56),
            int(size * 0.27),
            int(size * 0.685),
            int(size * 0.385),
        ],
        radius=int(size * 0.05),
        fill=255,
    )
    draw.rounded_rectangle(
        [
            int(size * 0.18),
            int(size * 0.46),
            int(size * 0.78),
            int(size * 0.70),
        ],
        radius=int(size * 0.115),
        fill=255,
    )
    draw.polygon(
        [
            (int(size * 0.27), int(size * 0.68)),
            (int(size * 0.37), int(size * 0.68)),
            (int(size * 0.30), int(size * 0.81)),
        ],
        fill=255,
    )
    draw.rounded_rectangle(
        [
            int(size * 0.29),
            int(size * 0.54),
            int(size * 0.375),
            int(size * 0.665),
        ],
        radius=int(size * 0.035),
        fill=0,
    )
    draw.rounded_rectangle(
        [
            int(size * 0.235),
            int(size * 0.515),
            int(size * 0.445),
            int(size * 0.585),
        ],
        radius=int(size * 0.03),
        fill=0,
    )
    draw.rounded_rectangle(
        [
            int(size * 0.57),
            int(size * 0.555),
            int(size * 0.655),
            int(size * 0.665),
        ],
        radius=int(size * 0.03),
        fill=0,
    )
    return mask


def add_glyph(card: Image.Image) -> Image.Image:
    size = card.size[0]
    mask = _glyph_mask(size)

    body = Image.new("RGBA", (size, size), (57, 214, 116, 255))
    body.putalpha(mask)

    card.alpha_composite(body)
    return card


def generate_master_icon() -> Image.Image:
    icon = rounded_card(MASTER_SIZE)
    return add_glyph(icon)


def save_pngs(master: Image.Image) -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    for size in PNG_SIZES:
        resized = master.resize((size, size), Image.Resampling.LANCZOS)
        resized.save(ICONS_DIR / f"{size}x{size}.png")
        if size == 512:
            resized.save(ASSETS_DIR / "icon.png")


def save_ico(master: Image.Image) -> None:
    master.save(ASSETS_DIR / "icon.ico", format="ICO", sizes=ICO_SIZES)


def save_icns(master: Image.Image) -> None:
    master.save(ASSETS_DIR / "icon.icns", format="ICNS", sizes=ICNS_SIZES)


def main() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    master = generate_master_icon()
    master.save(ASSETS_DIR / "icon-master.png")
    save_pngs(master)
    save_ico(master)
    save_icns(master)


if __name__ == "__main__":
    main()
