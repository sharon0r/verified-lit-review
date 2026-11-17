"""Word export: links with hard titles and DOIs survive the Markdown -> .docx conversion."""
import os
import subprocess
import sys

import docx

from verified_lit_review.build_report import linked_title, md_cell

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "src", "verified_lit_review", "md_to_docx.py")


def test_brackets_pipes_and_parenthesized_doi_become_a_clean_hyperlink(tmp_path):
    a = {"title": "[The CARE | trial]\nof x", "doi": "10.1016/S0140-6736(20)30183-5"}
    md = tmp_path / "r.md"
    md.write_text("| # | Title |\n|---|---|\n| 1 | " + md_cell(linked_title(a)) + " |\n\n"
                  "**Title:** " + linked_title(a) + "  \n", encoding="utf-8")
    out = tmp_path / "r.docx"
    subprocess.run([sys.executable, "-I", SCRIPT, str(md), str(out)], check=True, capture_output=True)

    d = docx.Document(str(out))
    texts = [d.tables[0].cell(1, 1).text] + [p.text for p in d.paragraphs if "CARE" in p.text]
    urls = [r.target_ref for r in d.part.rels.values() if "hyperlink" in r.reltype]
    assert texts and all("[The CARE | trial] of x" in t and "](" not in t and "\\" not in t for t in texts)
    assert urls == ["https://doi.org/10.1016/S0140-6736(20)30183-5"]    # Word reuses one relationship
    assert d.element.xml.count("<w:hyperlink") == 2                    # for both links


def test_underscore_italics_lose_their_underscores_but_snake_case_stays(tmp_path):
    md = tmp_path / "r.md"
    md.write_text("_מספרים שנבדקו מול המאמר: 2/2 נמצאו_ ✅\n\nfile_name_v2.pdf\n", encoding="utf-8")
    out = tmp_path / "r.docx"
    subprocess.run([sys.executable, "-I", SCRIPT, str(md), str(out)], check=True, capture_output=True)
    paras = [p for p in docx.Document(str(out)).paragraphs if p.text.strip()]
    assert paras[0].text == "מספרים שנבדקו מול המאמר: 2/2 נמצאו ✅" and paras[0].runs[0].italic
    assert paras[1].text == "file_name_v2.pdf"
