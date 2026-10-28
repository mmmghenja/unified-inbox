"""Ф11-3a B (docs/architecture.md), тест 6: `hub_media` отдаёт PDF текстом (pypdf) в маркерах
данных — до 200 КиБ символов с пометкой «показано N из M страниц / обрезано»; скан, битый, зашифрованный PDF и
таймаут разбора — описание с причиной. PDF собираются в тесте."""

import io
import time

import httpx
from mcp import Client
from pypdf import PdfReader, PdfWriter

from hub.mcp_server import format as fmt
from hub.mcp_server import server as server_mod

from .conftest import text_of
from .test_mcp_media_tool import _Core, _server

OCTET = {"content-type": "application/octet-stream"}


def make_pdf(pages: list[str]) -> bytes:
    """Минимальный PDF: по странице на строку, текст шрифтом Helvetica (извлекается pypdf)."""
    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {5 + 2 * i} 0 R >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for k, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{k} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def _data_block(out: str) -> list[str]:
    lines = out.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith("----- ДАННЫЕ "))
    nonce = lines[start].split()[2]
    end = lines.index(fmt.data_end(nonce))
    return lines[start + 1 : end]


async def _call(db, config_dir, make_server, body: bytes, *, seen: dict | None = None, headers=None):
    core = _Core({7: httpx.Response(200, content=body, headers=headers or OCTET)})
    server, hub = _server(db, config_dir, make_server, core)
    if seen is not None:
        hub.media_seen[7] = {"channel_id": 3, **seen}
    async with Client(server) as client:
        result = await client.call_tool("hub_media", {"media_id": 7})
    assert not result.is_error
    return text_of(result)


async def test_pdf_with_text_by_detected(db, config_dir, make_server):
    pdf = make_pdf(["Invoice 42 total 1000", "Second page text"])
    out = await _call(db, config_dir, make_server, pdf, seen={"detected": "pdf", "name": "счет.pdf"})
    head = out.split("\n")[0]
    assert "PDF из беседы #3 (media_id=7)" in head and "показано 2 из 2 страниц" in head
    assert "обрезано" not in head
    data = "\n".join(_data_block(out))
    assert "Invoice 42 total 1000" in data and "Second page text" in data and "«счет.pdf»" in data


async def test_pdf_by_content_type_without_seen(db, config_dir, make_server):
    pdf = make_pdf(["Only page"])
    out = await _call(db, config_dir, make_server, pdf, headers={"content-type": "application/pdf"})
    assert "показано 1 из 1 страниц" in out and "Only page" in "\n".join(_data_block(out))


async def test_pdf_by_source_mime(db, config_dir, make_server):
    pdf = make_pdf(["By mime"])
    out = await _call(db, config_dir, make_server, pdf, seen={"mime": "application/pdf"})
    assert "By mime" in "\n".join(_data_block(out))


async def test_pdf_text_cut_at_200_kib(db, config_dir, make_server):
    page = "A" * 1000
    pdf = make_pdf([page] * 300)
    out = await _call(db, config_dir, make_server, pdf, seen={"detected": "pdf"})
    head = out.split("\n")[0]
    assert "из 300 страниц, обрезано" in head
    shown = int(head.split("показано ")[1].split(" ")[0])
    assert shown < 300
    data = "".join(_data_block(out))
    assert data.count("A") == server_mod.MEDIA_PDF_TEXT_MAX


async def test_scan_without_text_is_description(db, config_dir, make_server):
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    buf = io.BytesIO()
    writer.write(buf)
    out = await _call(db, config_dir, make_server, buf.getvalue(), seen={"detected": "pdf"})
    assert "посмотреть можно только в хабе (текста в PDF нет — это скан)." in out
    assert "----- ДАННЫЕ" not in out


async def test_broken_pdf_is_description(db, config_dir, make_server):
    out = await _call(db, config_dir, make_server, b"%PDF-1.4\ngarbage \x00\xff", seen={"detected": "pdf"})
    assert "(PDF не разобран — файл повреждён)" in out


async def test_encrypted_pdf_is_description(db, config_dir, make_server):
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf(["secret"]))))
    writer.encrypt("pw", algorithm="RC4-128")
    buf = io.BytesIO()
    writer.write(buf)
    out = await _call(db, config_dir, make_server, buf.getvalue(), seen={"detected": "pdf"})
    assert "(PDF зашифрован — текст не извлекается)" in out
    assert "secret" not in out


async def test_parse_timeout_is_description(db, config_dir, make_server, monkeypatch):
    def slow(_data):
        time.sleep(3.0)
        return "не должно дойти"

    monkeypatch.setattr(server_mod, "_pdf_text", slow)
    monkeypatch.setattr(server_mod, "MEDIA_PDF_TIMEOUT_S", 0.05)
    started = time.monotonic()
    out = await _call(db, config_dir, make_server, make_pdf(["x"]), seen={"detected": "pdf"})
    assert time.monotonic() - started < 2.5  # медленный раннер CI: запас, разбор всё равно 3 с
    assert "(разбор PDF не уложился в 20 с)" in out


async def test_pdf_over_20_mib_is_description(db, config_dir, make_server):
    size = server_mod.MEDIA_PDF_MAX + 1
    response = httpx.Response(
        200, headers={**OCTET, "content-length": str(size)}, content=b"%PDF-" + b"x" * (size - 5)
    )
    core = _Core({7: response})
    server, hub = _server(db, config_dir, make_server, core)
    hub.media_seen[7] = {"channel_id": 3, "detected": "pdf"}
    async with Client(server) as client:
        out = text_of(await client.call_tool("hub_media", {"media_id": 7}))
    assert "(PDF больше 20 МиБ — текст не извлекается)" in out
