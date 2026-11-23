"""Extraction du texte, de la structure (titres) et des tableaux, par format.

Chaque parseur renvoie un ParsedDoc : une suite de blocs (texte + chemin de
section + page) et une liste de tableaux. Les tableaux (SoA, registre des
risques...) sont gardés en lignes/colonnes : ne pas les découper comme du texte.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SUPPORTED = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".txt", ".md", ".csv"}


@dataclass
class Block:
    text: str
    section: str = ""
    page: int | None = None


@dataclass
class Table:
    name: str
    header: list[str]
    rows: list[list[str]]


@dataclass
class ParsedDoc:
    blocks: list[Block] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    props: dict = field(default_factory=dict)   # métadonnées natives (docx core properties)
    pages: int | None = None
    ocr_used: bool = False
    ocr_pending: int = 0      # pages sans texte exploitable, non encore passées à l'OCR


def _cell(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return re.sub(r"\s+", " ", str(v)).strip()


def _table_from_grid(name: str, grid: list[list[str]]) -> Table | None:
    """Repère la ligne d'en-tête (première ligne avec ≥2 cellules non vides) et garde le reste."""
    grid = [r for r in grid if any(c for c in r)]
    if len(grid) < 2:
        return None
    # Tableau clé / valeur (ex. cartouche « Version | 2.0 ») : pas de ligne d'en-tête
    if all(len(r) == 2 for r in grid) and len(grid) <= 12 and all(len(r[0]) <= 40 for r in grid):
        return Table(name=name, header=["Champ", "Valeur"], rows=[list(r) for r in grid])
    for i, row in enumerate(grid[:10]):
        if sum(1 for c in row if c) >= 2:
            header = row
            body = [r + [""] * (len(header) - len(r)) for r in grid[i + 1:]]
            return Table(name=name, header=header, rows=[r[: len(header)] for r in body])
    return None


def _table_text(t: Table, chunk_rows: int = 12) -> list[str]:
    """Rend un tableau lisible pour l'index plein texte (« Colonne : valeur ; ... »)."""
    out = []
    for start in range(0, len(t.rows), chunk_rows):
        lines = []
        for r in t.rows[start:start + chunk_rows]:
            cells = [f"{h}: {v}" for h, v in zip(t.header, r) if v]
            if cells:
                lines.append(" ; ".join(cells))
        if lines:
            out.append("\n".join(lines))
    return out


# ---------------------------------------------------------------- PDF
def parse_pdf(path: Path, ocr: bool = True) -> ParsedDoc:
    import pymupdf

    doc = ParsedDoc()
    pdf = pymupdf.open(path)
    doc.pages = pdf.page_count
    sizes = []
    pages_spans = []
    for page in pdf:
        spans_lines = []
        for b in page.get_text("dict").get("blocks", []):
            for line in b.get("lines", []):
                spans = line.get("spans", [])
                text = "".join(s["text"] for s in spans).strip()
                if not text:
                    continue
                size = max(s["size"] for s in spans)
                bold = any("bold" in s.get("font", "").lower() or (s.get("flags", 0) & 16) for s in spans)
                spans_lines.append((text, size, bold))
                sizes.append(size)
        pages_spans.append(spans_lines)

    body = sorted(sizes)[len(sizes) // 2] if sizes else 11
    section = ""
    for pno, lines in enumerate(pages_spans, start=1):
        # Page scannée OU texte non extractible (polices sans table Unicode) : il ne reste
        # que l'en-tête ou le pied de page. Seuil : moins de 120 caractères utiles.
        if sum(len(t) for t, _, _ in lines) < 120:
            if not ocr:
                doc.ocr_pending += 1
                continue
            text = _ocr_page(pdf[pno - 1])
            if text:
                doc.ocr_used = True
                doc.blocks.append(Block(text=text, section=section, page=pno))
            continue
        buf: list[str] = []
        for text, size, bold in lines:
            is_heading = len(text) < 120 and (size >= body * 1.15 or (bold and len(text) < 80)) \
                and not text.endswith((".", ",", ";"))
            if is_heading:
                if buf:
                    doc.blocks.append(Block(" ".join(buf), section, pno))
                    buf = []
                section = text
            else:
                buf.append(text)
        if buf:
            doc.blocks.append(Block(" ".join(buf), section, pno))
    return doc


def _ocr_page(page) -> str:
    """OCR local d'une page scannée (Tesseract, français + anglais), si disponible."""
    try:
        import io

        import pytesseract
        from PIL import Image

        pix = page.get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(img, lang="fra+eng").strip()
    except Exception as e:  # Tesseract absent ou langue manquante
        log.warning("OCR indisponible pour une page scannée : %s", e)
        return ""


# ---------------------------------------------------------------- DOCX
def parse_docx(path: Path) -> ParsedDoc:
    import docx
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph

    d = docx.Document(path)
    doc = ParsedDoc()
    cp = d.core_properties
    doc.props = {
        "title": cp.title or "",
        "author": cp.author or "",
        "revision": str(cp.revision or ""),
        "modified": cp.modified.strftime("%Y-%m-%d") if cp.modified else "",
    }
    headings: list[str] = []
    buf: list[str] = []
    t_index = 0

    def flush():
        if buf:
            doc.blocks.append(Block("\n".join(buf), " > ".join(headings)))
            buf.clear()

    # Parcours dans l'ordre du document (paragraphes et tableaux mêlés).
    for child in d.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name or "").lower() if p.style is not None else ""
            m = re.match(r"(heading|titre)\s*(\d)", style)
            if m or style == "title":
                flush()
                level = int(m.group(2)) if m else 1
                headings[:] = headings[: level - 1] + [text]
            else:
                buf.append(text)
        elif tag == "tbl":
            flush()
            t_index += 1
            tbl = DocxTable(child, d)
            grid = [[_cell(c.text) for c in row.cells] for row in tbl.rows]
            t = _table_from_grid(f"Tableau {t_index}", grid)
            if t:
                doc.tables.append(t)
                for txt in _table_text(t):
                    doc.blocks.append(Block(txt, " > ".join(headings + [t.name])))
    flush()
    return doc


# ---------------------------------------------------------------- PPTX
def parse_pptx(path: Path) -> ParsedDoc:
    from pptx import Presentation

    doc = ParsedDoc()
    prs = Presentation(path)
    for i, slide in enumerate(prs.slides, start=1):
        title = slide.shapes.title.text.strip() if slide.shapes.title is not None else f"Diapositive {i}"
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape != slide.shapes.title:
                t = shape.text_frame.text.strip()
                if t:
                    texts.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                grid = [[_cell(c.text) for c in r.cells] for r in shape.table.rows]
                t = _table_from_grid(f"Diapo {i}", grid)
                if t:
                    doc.tables.append(t)
                    texts.extend(_table_text(t))
        if texts:
            doc.blocks.append(Block("\n".join(texts), title, i))
    doc.pages = len(prs.slides)
    return doc


# ---------------------------------------------------------------- XLSX
def parse_xlsx(path: Path) -> ParsedDoc:
    import openpyxl

    doc = ParsedDoc()
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    for ws in wb.worksheets:
        grid = [[_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
        grid = [r for r in grid if any(r)]
        # Lignes de titre au-dessus du tableau (souvent : référence, version, approbation)
        preamble = []
        for r in grid[:10]:
            if sum(1 for c in r if c) >= 2:
                break
            preamble.append(" ".join(c for c in r if c))
        if preamble:
            doc.blocks.append(Block("\n".join(preamble), f"Feuille {ws.title}"))
        t = _table_from_grid(ws.title, grid)
        if t:
            doc.tables.append(t)
            for txt in _table_text(t):
                doc.blocks.append(Block(txt, f"Feuille {ws.title}"))
    return doc


def parse_csv(path: Path) -> ParsedDoc:
    import csv

    doc = ParsedDoc()
    raw = path.read_text(encoding="utf-8", errors="replace")
    dialect = csv.Sniffer().sniff(raw[:2000], delimiters=",;\t") if raw else csv.excel
    grid = [[_cell(c) for c in r] for r in csv.reader(raw.splitlines(), dialect)]
    t = _table_from_grid(path.stem, grid)
    if t:
        doc.tables.append(t)
        doc.blocks.extend(Block(x, path.stem) for x in _table_text(t))
    return doc


# ---------------------------------------------------------------- TXT / MD
def parse_text(path: Path) -> ParsedDoc:
    doc = ParsedDoc()
    headings: list[str] = []
    buf: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            if buf:
                doc.blocks.append(Block("\n".join(buf), " > ".join(headings)))
                buf = []
            level = len(m.group(1))
            headings[:] = headings[: level - 1] + [m.group(2).strip()]
        elif line.strip():
            buf.append(line.strip())
    if buf:
        doc.blocks.append(Block("\n".join(buf), " > ".join(headings)))
    return doc


def parse(path: Path, ocr: bool = True) -> ParsedDoc:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return parse_pdf(path, ocr=ocr)
    if ext == ".docx":
        return parse_docx(path)
    if ext == ".pptx":
        return parse_pptx(path)
    if ext in (".xlsx", ".xlsm"):
        return parse_xlsx(path)
    if ext == ".csv":
        return parse_csv(path)
    if ext in (".txt", ".md"):
        return parse_text(path)
    raise ValueError(f"Format non pris en charge : {ext}")


def chunk_blocks(blocks: list[Block], max_chars: int = 1400, min_chars: int = 200) -> list[Block]:
    """Regroupe les blocs d'une même section jusqu'à max_chars ; coupe aux phrases si besoin."""
    out: list[Block] = []
    cur: Block | None = None
    for b in blocks:
        pieces = _split_long(b.text, max_chars)
        for piece in pieces:
            if cur and cur.section == b.section and len(cur.text) + len(piece) + 1 <= max_chars:
                cur.text += "\n" + piece
            elif cur and len(cur.text) < min_chars and len(cur.text) + len(piece) + 1 <= max_chars:
                cur.text += "\n" + piece   # fusionne un bout trop court avec la suite
                cur.section = cur.section or b.section
            else:
                if cur:
                    out.append(cur)
                cur = Block(piece, b.section, b.page)
    if cur:
        out.append(cur)
    return out


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?;])\s+|\n", text):
        if len(cur) + len(sent) + 1 > max_chars and cur:
            parts.append(cur)
            cur = ""
        while len(sent) > max_chars:      # phrase interminable (tableau aplati)
            parts.append(sent[:max_chars])
            sent = sent[max_chars:]
        cur = (cur + " " + sent).strip()
    if cur:
        parts.append(cur)
    return parts
