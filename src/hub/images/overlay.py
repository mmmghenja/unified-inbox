"""Заголовок поверх картинки (Ф91, дополнение 24.10: скрипт иллюстраций Umbrella больше не нужен — всё через
`hub_image`). Вид — как у обложек Umbrella (`scripts/marketing/dzen/dzen-cover.py`: Roboto, синяя черта
`#2F6BFF` слева от заголовка), но поверх фото:

- нижняя часть картинки затемняется градиентом (от прозрачного к ~80 % чёрного внизу) — чуть выше блока
  текста, не меньше нижней трети;
- заголовок — Roboto Bold, белый, до 2 строк с переносом по словам по ширине (не влезает — шрифт меньше, потом
  многоточие); слово длиннее строки режется по буквам;
- подпись — Roboto Regular мелко под заголовком, одна строка;
- вертикальная черта цвета `ACCENT` — слева от строк заголовка.

Водяной знак — отдельно от заголовка, `watermark` + `watermark_style`:
- `corner` (по умолчанию) — мелко в правом нижнем углу, ~55 %, тонкая тень; при заголовке — справа над его
  блоком, чтобы не налезать на текст;
- `diagonal` — одна крупная надпись по диагонали через всю картинку, ~18 %;
- `tile` — повтор надписи сеткой под углом, шаг по строке — ширина надписи × 1,6, ~12 %.
Непрозрачность — `watermark_opacity` 0.05…0.9. Ничего не рисуется, пока параметр не задан.

Шрифты лежат в пакете (`fonts/`, OFL 1.1 — `fonts/OFL.txt`), шрифты системы не нужны. Размеры — от меньшей
стороны картинки (база 900 пикселей), поэтому вид одинаков на 16:9, 1:1 и 9:16.

`layout` считает строки и места (без рисования) — по нему проверяется, что текст не вылезает за край;
`render` рисует по тому же расчёту.
"""

import io
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS = Path(__file__).with_name("fonts")
BOLD = FONTS / "Roboto-Bold.ttf"
REGULAR = FONTS / "Roboto-Regular.ttf"
ACCENT = (0x2F, 0x6B, 0xFF)
WHITE = (255, 255, 255)
SUB = (0xDD, 0xE3, 0xEE)
TITLE_MAX_LINES = 2
TITLE_MAX = 120
SUBTITLE_MAX = 160
WATERMARK_MAX = 40
WATERMARK_STYLES = ("corner", "diagonal", "tile")
# Непрозрачность по умолчанию: угол ~55 %, диагональ ~18 %, сетка ~12 %.
WATERMARK_OPACITY = {"corner": 0.55, "diagonal": 0.18, "tile": 0.12}
OPACITY_MIN = 0.05
OPACITY_MAX = 0.9
_BASE = 900.0
_SHADE_MAX = 205  # непрозрачность чёрного у нижнего края
_MAX_PIXELS = 4096 * 4096


class OverlayError(ValueError):
    """Картинка не открылась или не годится для наложения."""


@dataclass(frozen=True)
class Line:
    text: str
    x: int
    y: int  # верх строки
    width: int
    size: int  # кегль


@dataclass(frozen=True)
class Layout:
    width: int
    height: int
    margin: int
    title: tuple[Line, ...]
    subtitle: Line | None
    corner: Line | None  # водяной знак `corner`
    bar: tuple[int, int, int, int] | None  # x0, y0, x1, y1
    shade_top: int


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def _clean(text: str | None, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _split_long(word: str, font, limit: float, draw) -> list[str]:
    parts, cur = [], ""
    for ch in word:
        if cur and draw.textlength(cur + ch, font=font) > limit:
            parts.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts


def wrap(text: str, font, limit: float, draw) -> list[str]:
    """Строки по словам не шире `limit` пикселей (длинное слово — по буквам)."""
    lines: list[str] = []
    cur = ""
    for word in text.split():
        pieces = [word] if draw.textlength(word, font=font) <= limit else _split_long(word, font, limit, draw)
        for piece in pieces:
            trial = f"{cur} {piece}".strip()
            if not cur or draw.textlength(trial, font=font) <= limit:
                cur = trial
            else:
                lines.append(cur)
                cur = piece
    if cur:
        lines.append(cur)
    return lines


def _ellipsize(text: str, font, limit: float, draw) -> str:
    if draw.textlength(text, font=font) <= limit:
        return text
    while text and draw.textlength(text + "…", font=font) > limit:
        text = text[:-1].rstrip()
    return text + "…"


def _fit_title(text: str, s: float, limit: float, draw) -> tuple[list[str], int]:
    """Заголовок в 2 строки: кегль 64·s, если не влезает — меньше до 44·s, дальше — многоточие."""
    size = max(int(64 * s), 12)
    floor = max(int(44 * s), 10)
    while True:
        font = _font(BOLD, size)
        lines = wrap(text, font, limit, draw)
        if len(lines) <= TITLE_MAX_LINES or size <= floor:
            break
        size = max(size - max(int(4 * s), 1), floor)
    if len(lines) > TITLE_MAX_LINES:
        rest = " ".join(lines[TITLE_MAX_LINES - 1 :])
        lines = [*lines[: TITLE_MAX_LINES - 1], _ellipsize(rest, font, limit, draw)]
    return lines, size


def _corner_size(s: float) -> int:
    return max(int(28 * s), 9)


def layout(
    width: int,
    height: int,
    *,
    title: str,
    subtitle: str | None = None,
    watermark: str | None = None,
    watermark_style: str = "corner",
) -> Layout:
    """Места строк. Водяной знак здесь считается только для `corner` (две другие раскладки — на всю
    картинку): без заголовка — правый нижний угол, с заголовком — справа над блоком заголовка, чтобы не
    налезать на текст."""
    title = _clean(title, TITLE_MAX)
    subtitle = _clean(subtitle, SUBTITLE_MAX) or None
    watermark = _clean(watermark, WATERMARK_MAX) or None
    s = min(width, height) / _BASE
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    margin = max(int(64 * s), 8)
    bar_w = max(int(10 * s), 3)
    gap = max(int(26 * s), 6)
    text_x = margin + bar_w + gap
    limit = width - text_x - margin
    bottom = height - margin

    sub_line = None
    sub_size = max(int(32 * s), 10)
    sub_h = int(sub_size * 1.25) + max(int(14 * s), 3) if subtitle and title else 0

    title_lines: list[Line] = []
    bar = None
    block_top = height
    if title:
        lines, size = _fit_title(title, s, limit, draw)
        font = _font(BOLD, size)
        line_h = int(size * 1.18)
        top = bottom - sub_h - line_h * len(lines)
        for i, text in enumerate(lines):
            title_lines.append(
                Line(text, text_x, top + i * line_h, int(draw.textlength(text, font=font)), size)
            )
        bar = (margin, top + int(size * 0.12), margin + bar_w, top + line_h * len(lines) - int(size * 0.12))
        block_top = top
        if subtitle:
            font = _font(REGULAR, sub_size)
            text = _ellipsize(subtitle, font, limit, draw)
            sub_top = bottom - int(sub_size * 1.25)
            sub_line = Line(text, text_x, sub_top, int(draw.textlength(text, font=font)), sub_size)

    corner = None
    if watermark and watermark_style == "corner":
        size = _corner_size(s)
        font = _font(REGULAR, size)
        text = _ellipsize(watermark, font, width - 2 * margin, draw)
        w = int(draw.textlength(text, font=font))
        top = height - margin - size if not title else block_top - max(int(30 * s), 6) - size
        corner = Line(text, width - margin - w, max(top, 0), w, size)
    shade_top = max(min(height * 2 // 3, block_top - int(80 * s)), 0) if title else height
    return Layout(width, height, margin, tuple(title_lines), sub_line, corner, bar, shade_top)


def _open(data: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > _MAX_PIXELS:
                raise OverlayError("картинка слишком большая для наложения")
            return source.convert("RGB")
    except OverlayError:
        raise
    except Exception as exc:  # битые данные — Pillow бросает разное
        raise OverlayError("картинка не открылась") from exc


def _alpha(opacity: float) -> int:
    return max(1, min(255, round(opacity * 255)))


def _stroked_text(text: str, size: int, alpha: int) -> Image.Image:
    """Надпись на прозрачном слое: белая с тонкой тёмной обводкой — видна и на светлом, и на тёмном."""
    font = _font(BOLD, size)
    stroke = max(size // 28, 1)
    box = font.getbbox(text, stroke_width=stroke)
    layer = Image.new("RGBA", (box[2] - box[0] + 2, box[3] - box[1] + 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(
        (1 - box[0], 1 - box[1]),
        text,
        font=font,
        fill=(255, 255, 255, alpha),
        stroke_width=stroke,
        stroke_fill=(0, 0, 0, min(255, round(alpha * 1.25))),
    )
    return layer


def _diagonal(image: Image.Image, text: str, alpha: int) -> Image.Image:
    """Одна крупная надпись по диагонали через всю картинку (~80 % диагонали)."""
    width, height = image.size
    diag = math.hypot(width, height)
    probe = _font(BOLD, 100)
    size = max(int(100 * 0.8 * diag / max(probe.getlength(text), 1)), 12)
    size = min(size, int(min(width, height) * 0.5))
    mark = _stroked_text(text, size, alpha).rotate(math.degrees(math.atan2(height, width)), expand=True)
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    # Без маски: вставка с маской в прозрачный слой умножает альфу саму на себя (18 % -> 3 %).
    layer.paste(mark, ((width - mark.width) // 2, (height - mark.height) // 2))
    return Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")


def _tile(image: Image.Image, text: str, alpha: int, s: float) -> Image.Image:
    """Повтор надписи сеткой под 30°: шаг по строке — ширина надписи × 1,6, между строками — высота × 3."""
    width, height = image.size
    side = int(math.hypot(width, height)) + 2
    mark = _stroked_text(text, max(int(40 * s), 10), alpha)
    step_x = max(int(mark.width * 1.6), 1)
    step_y = max(int(mark.height * 3), 1)
    plane = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    for row, y in enumerate(range(0, side, step_y)):
        shift = (step_x // 2) * (row % 2)
        for x in range(-step_x + shift, side, step_x):
            plane.paste(mark, (x, y))  # без маски — см. `_diagonal`; надписи сетки не перекрываются
    plane = plane.rotate(30, center=(side / 2, side / 2))
    left, top = (side - width) // 2, (side - height) // 2
    layer = plane.crop((left, top, left + width, top + height))
    return Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")


def _corner(image: Image.Image, line: Line, alpha: int) -> Image.Image:
    """Мелкий текст в углу с тонкой тенью (читается на светлом)."""
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = _font(REGULAR, line.size)
    off = max(line.size // 14, 1)
    draw.text((line.x + off, line.y + off), line.text, font=font, fill=(0, 0, 0, max(alpha * 2 // 3, 1)))
    draw.text((line.x, line.y), line.text, font=font, fill=(255, 255, 255, alpha))
    return Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")


def corner_box(plan: Layout) -> tuple[int, int, int, int] | None:
    """Прямоугольник, в который рисуется угловой знак (с тенью и запасом под выносные элементы)."""
    if plan.corner is None:
        return None
    line = plan.corner
    pad = max(line.size // 3, 2)
    return (line.x - pad, line.y - pad, line.x + line.width + pad, line.y + line.size + pad)


def render(
    data: bytes,
    *,
    title: str | None = None,
    subtitle: str | None = None,
    watermark: str | None = None,
    watermark_style: str = "corner",
    watermark_opacity: float | None = None,
) -> bytes:
    """PNG/JPEG/WebP -> PNG с заголовком и/или водяным знаком. Ни заголовка, ни знака — исходные байты как
    есть (подпись без заголовка не рисуется)."""
    title = _clean(title, TITLE_MAX)
    watermark = _clean(watermark, WATERMARK_MAX) or None
    if watermark_style not in WATERMARK_STYLES:
        raise OverlayError(f"watermark_style: {', '.join(WATERMARK_STYLES)}")
    if not title and not watermark:
        return data
    image = _open(data)
    plan = layout(
        image.width,
        image.height,
        title=title,
        subtitle=subtitle,
        watermark=watermark,
        watermark_style=watermark_style,
    )
    if plan.title:
        shade = Image.new("L", (1, image.height), 0)
        span = max(image.height - plan.shade_top, 1)
        for y in range(plan.shade_top, image.height):
            t = (y - plan.shade_top) / span
            shade.putpixel((0, y), int(_SHADE_MAX * t**1.3))
        black = Image.new("RGB", image.size, (0, 0, 0))
        image = Image.composite(black, image, shade.resize(image.size))
        draw = ImageDraw.Draw(image)
        if plan.bar is not None:
            draw.rectangle(plan.bar, fill=ACCENT)
        for line in plan.title:
            draw.text((line.x, line.y), line.text, font=_font(BOLD, line.size), fill=WHITE)
        if plan.subtitle is not None:
            line = plan.subtitle
            draw.text((line.x, line.y), line.text, font=_font(REGULAR, line.size), fill=SUB)
    if watermark:
        opacity = WATERMARK_OPACITY[watermark_style] if watermark_opacity is None else watermark_opacity
        alpha = _alpha(opacity)
        if watermark_style == "corner" and plan.corner is not None:
            image = _corner(image, plan.corner, alpha)
        elif watermark_style == "diagonal":
            image = _diagonal(image, watermark, alpha)
        elif watermark_style == "tile":
            image = _tile(image, watermark, alpha, min(image.width, image.height) / _BASE)
    out = io.BytesIO()
    image.save(out, "PNG", optimize=False)
    return out.getvalue()
