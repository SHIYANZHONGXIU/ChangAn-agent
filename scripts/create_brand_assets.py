"""从已选 A「启阙」的同一组几何坐标生成矢量、PNG 和 Windows ICO。"""

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
GATE = [(18, 82), (18, 29), (50, 14), (82, 29), (82, 82), (65, 82), (65, 42), (50, 35), (35, 42), (35, 82)]
INK, WHITE, ORANGE = "#202328", "#f0f1f4", "#e66736"
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def mark_svg(color, tile=False):
    path = "M" + "L".join(f"{x} {y}" for x, y in GATE) + "Z"
    mark = f'<path d="{path}" fill="{color}"/><path d="M42 63H58V82H42Z" fill="{color}"/><rect x="77" y="11" width="12" height="12" rx="2" fill="{ORANGE}"/>'
    if tile:
        mark = f'<rect x="2" y="2" width="96" height="96" rx="22" fill="{INK}"/><g transform="translate(8 8) scale(.84)">{mark}</g>'
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 100 100" role="img" aria-label="长安 · 启阙">{mark}</svg>'


def mark_image(color, tile=False):
    image = Image.new("RGBA", (1600, 1600))
    draw = ImageDraw.Draw(image)
    if tile:
        draw.rounded_rectangle((32, 32, 1568, 1568), radius=352, fill=INK)
    def point(x, y):
        return ((8 + x * .84) * 16, (8 + y * .84) * 16) if tile else (x * 16, y * 16)
    draw.polygon([point(x, y) for x, y in GATE], fill=color)
    draw.rectangle((*point(42, 63), *point(58, 82)), fill=color)
    draw.rounded_rectangle((*point(77, 11), *point(89, 23)), radius=2 * 16 * (.84 if tile else 1), fill=ORANGE)
    return image.resize((512, 512), Image.Resampling.LANCZOS)


def main():
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "changan-logo.svg").write_text(mark_svg(INK), encoding="utf-8")
    (assets / "changan-app.svg").write_text(mark_svg(WHITE, tile=True), encoding="utf-8")
    mark_image(INK).save(assets / "changan-logo-light.png")
    mark_image(WHITE).save(assets / "changan-logo-dark.png")
    icon = mark_image(WHITE, tile=True)
    icon.save(assets / "changan-app.png")
    icon.save(assets / "changan.ico", format="ICO", sizes=[(n, n) for n in ICO_SIZES])
    with Image.open(assets / "changan.ico") as saved:
        assert saved.ico.sizes() == {(n, n) for n in ICO_SIZES}
    print(assets)


if __name__ == "__main__":
    main()
