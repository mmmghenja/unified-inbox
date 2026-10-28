"""Ф91 (дополнения 24.10): заголовок поверх картинки — перенос длинного заголовка, текст не вылезает за край,
не больше 2 строк, затемнение снизу; водяной знак трёх видов (`corner`, `diagonal`, `tile`); без заголовка и
знака — байты как есть; шрифт — из пакета хаба."""

import io

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFont

from hub.images import overlay

GRAY = (200, 200, 200)


def _photo(width: int, height: int, color=GRAY) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "PNG")
    return out.getvalue()


def _img(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def _changed_box(before: bytes, after: bytes):
    return ImageChops.difference(_img(before), _img(after)).getbbox()


def _inside_ok(plan: overlay.Layout) -> None:
    lines = [*plan.title, *(x for x in (plan.subtitle, plan.corner) if x is not None)]
    for line in lines:
        assert line.x >= plan.margin
        assert line.x + line.width <= plan.width - plan.margin, line
        assert line.y >= 0 and line.y + line.size <= plan.height


def test_fonts_bundled_with_cyrillic_and_license():
    assert overlay.BOLD.is_file() and overlay.REGULAR.is_file()
    assert "Open Font License" in (overlay.FONTS / "OFL.txt").read_text(encoding="utf-8")
    font = ImageFont.truetype(str(overlay.BOLD), 40)
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    assert draw.textlength("Ёжик", font=font) > 40  # кириллица есть, не нулевые глифы


@pytest.mark.parametrize("size", [(2688, 1536), (2048, 2048), (1536, 2688), (1024, 768)])
def test_long_title_wraps_within_width(size):
    title = "Как подписать договор с контрагентом онлайн и не потерять юридическую силу документа"
    plan = overlay.layout(
        *size, title=title, subtitle="Пошаговая инструкция для малого бизнеса", watermark="umbrella.example.com"
    )
    assert 1 < len(plan.title) <= overlay.TITLE_MAX_LINES
    _inside_ok(plan)
    assert " ".join(line.text for line in plan.title).split()[0] == "Как"
    assert plan.subtitle.y >= plan.title[-1].y + plan.title[-1].size  # подпись под заголовком
    # угловой знак при заголовке — над блоком заголовка, не налезает на текст
    assert plan.corner.y + plan.corner.size <= plan.title[0].y
    x0, y0, x1, y1 = plan.bar  # черта — слева от заголовка, по его высоте
    assert x1 < plan.title[0].x and y0 >= plan.title[0].y
    assert y1 <= plan.title[-1].y + plan.title[-1].size * 1.2
    assert plan.shade_top <= size[1] * 2 // 3


def test_very_long_title_ellipsized_two_lines():
    plan = overlay.layout(1024, 1024, title="слово " * 60)
    assert len(plan.title) == 2 and plan.title[-1].text.endswith("…")
    _inside_ok(plan)


def test_single_huge_word_split_by_letters():
    plan = overlay.layout(1024, 1024, title="Электронно" * 6)
    assert len(plan.title) == 2
    _inside_ok(plan)


def test_short_title_one_line():
    plan = overlay.layout(2688, 1536, title="Кот")
    assert [line.text for line in plan.title] == ["Кот"]
    assert plan.subtitle is None and plan.corner is None


def test_render_title_draws_and_shades_bottom():
    src = _photo(1600, 900)
    title = "Длинный заголовок статьи про электронную подпись"
    image = _img(overlay.render(src, title=title))
    assert image.size == (1600, 900)
    assert image.getpixel((800, 10)) == GRAY  # верх не тронут
    assert sum(image.getpixel((800, 895))) < sum(GRAY) // 2  # низ затемнён
    plan = overlay.layout(1600, 900, title=title)
    x0, y0, x1, y1 = plan.bar
    assert image.getpixel(((x0 + x1) // 2, (y0 + y1) // 2)) == overlay.ACCENT
    line = plan.title[0]
    row = [image.getpixel((x, line.y + line.size // 2)) for x in range(line.x, line.x + line.width)]
    assert any(min(px) > 240 for px in row)  # белый текст есть


def test_nothing_set_returns_same_bytes():
    src = _photo(800, 600)
    assert overlay.render(src, title="", subtitle="под") is src
    assert overlay.render(src) is src


def test_corner_watermark_only_in_corner():
    src = _photo(1600, 900)
    out = overlay.render(src, watermark="umbrella.example.com")
    box = _changed_box(src, out)
    assert box is not None  # пиксели меняются
    allowed = overlay.corner_box(overlay.layout(1600, 900, title="", watermark="umbrella.example.com"))
    # вне надписи — ничего: изменённая область целиком внутри прямоугольника знака в правом нижнем углу
    assert allowed[0] <= box[0] and allowed[1] <= box[1] and box[2] <= allowed[2] and box[3] <= allowed[3]
    assert box[0] > 800 and box[1] > 450


def test_corner_with_title_above_title_block():
    src = _photo(1600, 900)
    plan = overlay.layout(1600, 900, title="Заголовок статьи", subtitle="Подпись", watermark="umbrella.example.com")
    assert plan.corner.y + plan.corner.size <= plan.title[0].y
    assert plan.corner.x + plan.corner.width <= 1600 - plan.margin
    out = overlay.render(src, title="Заголовок статьи", subtitle="Подпись", watermark="umbrella.example.com")
    assert _changed_box(src, out) is not None


def test_diagonal_watermark_crosses_image():
    src = _photo(1600, 900)
    out = overlay.render(src, watermark="umbrella.example.com", watermark_style="diagonal")
    box = _changed_box(src, out)
    assert box is not None
    # одна крупная надпись через всю картинку: изменения тянутся на большую часть ширины и высоты
    assert box[2] - box[0] > 1600 * 0.6 and box[3] - box[1] > 900 * 0.5
    image = _img(out)
    assert image.getpixel((5, 5)) == GRAY and image.getpixel((1594, 5)) == GRAY  # углы чистые


def test_tile_watermark_repeats_everywhere():
    src = _photo(1600, 900)
    out = _img(overlay.render(src, watermark="umbrella.example.com", watermark_style="tile"))
    diff = ImageChops.difference(_img(src), out).convert("L")
    # во всех четырёх четвертях есть изменённые пиксели — надпись повторена сеткой
    for x0, y0 in ((0, 0), (800, 0), (0, 450), (800, 450)):
        assert diff.crop((x0, y0, x0 + 800, y0 + 450)).getbbox() is not None


@pytest.mark.parametrize("style", ["corner", "diagonal", "tile"])
def test_opacity_changes_strength(style):
    src = _photo(800, 600)

    def strength(opacity):
        out = overlay.render(src, watermark="WWW", watermark_style=style, watermark_opacity=opacity)
        diff = ImageChops.difference(_img(src), _img(out)).convert("L")
        return diff.getextrema()[1]

    assert strength(0.8) > strength(0.1)


@pytest.mark.parametrize(("style", "floor"), [("diagonal", 35), ("tile", 25)])
@pytest.mark.parametrize("shade", [220, 40])
def test_default_watermark_visible_on_light_and_dark(style, floor, shade):
    """Приёмка 24.10: при умолчаниях знак виден глазом и на светлом, и на тёмном фоне (белая заливка + тёмная
    обводка), но не забивает картинку. Было 6 и 4 из 255: вставка с маской умножала альфу саму на себя."""
    src = _photo(1600, 900, (shade, shade, shade))
    out = overlay.render(src, watermark="Umbrella", watermark_style=style)
    top = ImageChops.difference(_img(src), _img(out)).convert("L").getextrema()[1]
    assert floor <= top <= 110, (style, shade, top)


def test_bad_style_and_bad_data():
    with pytest.raises(overlay.OverlayError):
        overlay.render(_photo(100, 100), watermark="x", watermark_style="spiral")
    with pytest.raises(overlay.OverlayError):
        overlay.render(b"not an image", title="x")
