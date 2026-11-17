"""Convert the Hebrew (RTL) literature-review Markdown chapter to a Word document.

    python -I md_to_docx.py <in.md> <out.docx>
"""
import re
import sys

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

sys.stdout.reconfigure(encoding="utf-8")
SRC, OUT = sys.argv[1], sys.argv[2]
HEB = re.compile(r"[֐-׿]")
LATIN_FONT, HEB_FONT = "Times New Roman", "David"
# link text may contain escaped brackets ("\[CARE\]"); a URL may contain one level of balanced
# parentheses, as Elsevier DOIs do ("10.1016/S0140-6736(20)30183-5")
INLINE = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|\[((?:\\.|[^\]\\])+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)"
                    r"|(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")       # _italic_ (not snake_case words)
UNESCAPE = re.compile(r"\\([\\\[\]|])")

doc = Document()
sec = doc.sections[0]
sec.page_width, sec.page_height = Cm(21), Cm(29.7)
for side in ("left_margin", "right_margin"):
    setattr(sec, side, Cm(2.5))
sec.top_margin = sec.bottom_margin = Cm(2.5)


def set_fonts(rpr_owner, size=None, bold=None, color=None):
    """Set Latin + Hebrew (complex-script) fonts and sizes on a style or run."""
    f = rpr_owner.font
    f.name = LATIN_FONT
    if size:
        f.size = Pt(size)
    if bold is not None:
        f.bold = bold
    if color:
        f.color.rgb = color
    rpr = rpr_owner.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for k, v in (("w:ascii", LATIN_FONT), ("w:hAnsi", LATIN_FONT), ("w:cs", HEB_FONT), ("w:eastAsia", LATIN_FONT)):
        fonts.set(qn(k), v)
    for a in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
        fonts.attrib.pop(qn(a), None)
    if size:
        szcs = rpr.find(qn("w:szCs"))
        if szcs is None:
            szcs = OxmlElement("w:szCs")
            rpr.append(szcs)
        szcs.set(qn("w:val"), str(int(size * 2)))
    if bold:
        rpr.append(OxmlElement("w:bCs"))


styles = doc.styles
set_fonts(styles["Normal"], 12)
styles["Normal"].paragraph_format.space_after = Pt(6)
styles["Normal"].paragraph_format.line_spacing = 1.5
DARK = RGBColor(0x1F, 0x2A, 0x44)
for name, size in (("Title", 18), ("Heading 1", 16), ("Heading 2", 14), ("Heading 3", 12.5)):
    st = styles[name]
    set_fonts(st, size, True, DARK)
    pf = st.paragraph_format
    pf.space_before, pf.space_after, pf.keep_with_next = Pt(18 if name != "Heading 3" else 12), Pt(6), True
for name in ("List Bullet", "List Number"):
    set_fonts(styles[name], 12)


def rtl_paragraph(p, rtl=True):
    ppr = p._p.get_or_add_pPr()
    if rtl:
        ppr.append(OxmlElement("w:bidi"))
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    else:
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT


def mark_run(run, text, bold=False, italic=False):
    run.bold, run.italic = bold or None, italic or None
    rpr = run._r.get_or_add_rPr()
    if bold:
        rpr.append(OxmlElement("w:bCs"))
    if italic:
        rpr.append(OxmlElement("w:iCs"))
    if HEB.search(text):
        rpr.append(OxmlElement("w:rtl"))


def add_link(p, text, url):
    if not url.startswith("http"):
        url = url.split("#")[0]  # Word can't open a local file with a #page fragment
    rid = p.part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                           is_external=True)
    h = OxmlElement("w:hyperlink")
    h.set(qn("r:id"), rid)
    r = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    for tag, val in (("w:color", "1F5FAD"), ("w:u", "single")):
        e = OxmlElement(tag)
        e.set(qn("w:val"), val)
        rpr.append(e)
    r.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    r.append(t)
    h.append(r)
    p._p.append(h)


def add_inline(p, text, bold=False, italic=False):
    pos = 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            seg = text[pos:m.start()]
            mark_run(p.add_run(seg), seg, bold, italic)
        if m.group(1) is not None:
            add_inline(p, m.group(1), True, italic)
        elif m.group(2) is not None or m.group(5) is not None:
            add_inline(p, m.group(2) if m.group(2) is not None else m.group(5), bold, True)
        else:
            add_link(p, UNESCAPE.sub(r"\1", m.group(3)), m.group(4))
        pos = m.end()
    if pos < len(text):
        seg = text[pos:]
        mark_run(p.add_run(seg), seg, bold, italic)


def para(text, style=None, rtl=None):
    p = doc.add_paragraph(style=style)
    rtl_paragraph(p, bool(HEB.search(text)) if rtl is None else rtl)
    add_inline(p, text)
    return p


def shade(cell, hex_fill):
    tcpr = cell._tc.get_or_add_tcPr()
    sh = OxmlElement("w:shd")
    sh.set(qn("w:val"), "clear")
    sh.set(qn("w:color"), "auto")
    sh.set(qn("w:fill"), hex_fill)
    tcpr.append(sh)


def add_table(rows):
    cols = len(rows[0])
    t = doc.add_table(rows=len(rows), cols=cols)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t._tbl.tblPr.append(OxmlElement("w:bidiVisual"))
    widths = [Cm(3), Cm(7.5), Cm(5.5)] if cols == 3 else [Cm(16 / cols)] * cols
    for i, row in enumerate(rows):
        for j, val in enumerate(row):
            c = t.cell(i, j)
            c.width = widths[j]
            p = c.paragraphs[0]
            rtl_paragraph(p, True)
            p.alignment = None  # start of line = right in an RTL paragraph
            p.paragraph_format.line_spacing = 1.15
            add_inline(p, val, bold=(i == 0))
            if i == 0:
                shade(c, "DCE3EE")
        trpr = t.rows[i]._tr.get_or_add_trPr()
        trpr.append(OxmlElement("w:cantSplit"))
        if i == 0:
            trpr.append(OxmlElement("w:tblHeader"))
    doc.add_paragraph()


def add_page_numbers():
    p = sec.footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run()
    for tag, extra in (("w:fldChar", {"w:fldCharType": "begin"}), ("w:instrText", None),
                       ("w:fldChar", {"w:fldCharType": "end"})):
        e = OxmlElement(tag)
        if extra:
            for k, v in extra.items():
                e.set(qn(k), v)
        else:
            e.set(qn("xml:space"), "preserve")
            e.text = "PAGE"
        r._r.append(e)


text = open(SRC, encoding="utf-8").read().replace("\\u200F", "‏")
lines = text.split("\n")
in_refs = False
i = 0
while i < len(lines):
    ln = lines[i].rstrip()
    if not ln or ln in ("---", ">"):
        i += 1
        continue
    if ln.startswith("|"):
        rows = []
        while i < len(lines) and lines[i].startswith("|"):
            cells = [c.strip().replace("\\|", "|")       # "\|" is a literal pipe inside a cell
                     for c in re.split(r"(?<!\\)\|", lines[i].strip().removeprefix("|").removesuffix("|"))]
            if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                rows.append(cells)
            i += 1
        add_table(rows)
        continue
    m = re.match(r"(#{1,3}) (.*)", ln)
    if m:
        level = len(m.group(1))
        p = para(m.group(2), "Title" if level == 1 else f"Heading {level - 1}", rtl=True)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER if level == 1 else None  # None = start (right in RTL)
        in_refs = "מקורות" in m.group(2)
    elif ln.startswith("> "):
        p = para(ln[2:])
        pf = p.paragraph_format
        pf.left_indent = pf.right_indent = Cm(1.2)
        pf.line_spacing = 1.15
        for r in p.runs:
            r.font.size = Pt(11)
    elif ln.startswith("- "):
        para(ln[2:], "List Bullet", rtl=True)
    elif re.match(r"\d+\. ", ln):
        para(re.sub(r"^\d+\. ", "", ln), "List Number", rtl=True)
    elif in_refs:
        p = para(ln, rtl=False)
        pf = p.paragraph_format
        pf.left_indent, pf.first_line_indent, pf.line_spacing = Cm(1.27), Cm(-1.27), 1.15
    elif re.match(r"\*\*[^*]+:\*\*", ln):  # metadata line ("**Label:** value"): no justification
        p = para(ln)
        if p.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY:
            p.alignment = None
        p.paragraph_format.line_spacing = 1.15
        p.paragraph_format.space_after = Pt(3)
    else:
        para(ln)
    i += 1

add_page_numbers()
doc.core_properties.title = next((l[2:] for l in lines if l.startswith("# ")), "")
doc.save(OUT)
print("saved", OUT)
