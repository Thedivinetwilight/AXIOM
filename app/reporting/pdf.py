"""Minimal dependency-free PDF writer (Helvetica, plain text pages).

Enough for weekly-report export on any machine (Termux included). Produces a
valid single-font PDF: pages of text lines, no images or styling. Latin-1
text only; characters outside Latin-1 are transliterated/ignored honestly.
"""

from __future__ import annotations

PAGE_W, PAGE_H = 595, 842  # A4 points
MARGIN = 54
LINE_H = 13.5
FONT_SIZE = 10
LINES_PER_PAGE = int((PAGE_H - 2 * MARGIN) / LINE_H)


def _escape(text: str) -> str:
    cleaned = (text.replace("\\", "\\\\").replace("(", "\\[").replace(")", "\\]")
               .replace("[", "\\[").replace("]", "\\]"))
    return cleaned.encode("latin-1", "replace").decode("latin-1")


def _page_stream(lines: list[str]) -> str:
    parts = ["BT", f"/F1 {FONT_SIZE} Tf", f"1 0 0 1 {MARGIN} {PAGE_H - MARGIN} Tm",
             f"{LINE_H} TL"]
    for line in lines:
        parts.append(f"({_escape(line)}) Tj T*")
    parts.append("ET")
    return "\n".join(parts)


def simple_pdf(title: str, lines: list[str]) -> bytes:
    """Render title + wrapped lines into a compact valid PDF."""
    import textwrap

    flow: list[str] = [title, "=" * min(len(title), 72), ""]
    for line in lines:
        flow.extend(textwrap.wrap(line, 92) or [""])
    pages = [flow[i:i + LINES_PER_PAGE] for i in range(0, len(flow), LINES_PER_PAGE)] or [[""]]
    objects: list[str] = []
    page_ids = []
    content_ids = []
    next_id = 3  # 1=catalog, 2=pages
    for _ in pages:
        content_ids.append(next_id)
        next_id += 1
        page_ids.append(next_id)
        next_id += 1
    for index, page_lines in enumerate(pages):
        stream = _page_stream(page_lines)
        objects.append((content_ids[index],
                        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"))
        objects.append((page_ids[index],
                        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                        f"/Contents {content_ids[index]} 0 R "
                        f"/Resources << /Font << /F1 3 0 R >> >> >>"))
    objects.insert(2, (3, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    objects.sort(key=lambda item: item[0])
    body = ["%PDF-1.4"]
    offsets = {}
    position = len(body[0]) + 1
    for obj_id, text in objects:
        offsets[obj_id] = position
        chunk = f"{obj_id} 0 obj\n{text}\nendobj"
        body.append(chunk)
        position += len(chunk) + 1
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects_meta = [
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (2, f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"),
    ]
    for obj_id, text in objects_meta:
        offsets[obj_id] = position
        chunk = f"{obj_id} 0 obj\n{text}\nendobj"
        body.append(chunk)
        position += len(chunk) + 1
    total = max(offsets) + 1
    xref_pos = position
    xref = [f"xref\n0 {total}", "0000000000 65535 f "]
    for obj_id in range(1, total):
        xref.append(f"{offsets.get(obj_id, 0):010d} 00000 n ")
    body.append("\n".join(xref))
    body.append(f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF")
    return "\n".join(body).encode("latin-1", "replace")
