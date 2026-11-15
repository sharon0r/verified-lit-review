"""Tests for the anti-hallucination guardrail in build_report.py.

Each test builds a tiny synthetic PDF (no copyrighted papers needed), writes a spec,
runs the report builder and checks which quotes were accepted or rejected.
"""
import json
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "src" / "verified_lit_review" / "build_report.py"
sys.path.insert(0, str(SCRIPT.parent))
from build_report import norm  # noqa: E402

PAGE1 = "Results\nThe intervention reduced fasting glucose by 0.40 mmol/l (p<0.001).\nGroup sessions were well attended."
PAGE2 = "Discussion\nCommunity-based programs are feasible and cost-saving in the long run."


@pytest.fixture
def workspace(tmp_path):
    doc = pymupdf.open()
    for text in (PAGE1, PAGE2):
        doc.new_page().insert_text((72, 72), text, fontsize=11)
    (tmp_path / "pdfs").mkdir()
    doc.save(tmp_path / "pdfs" / "paper.pdf")
    return tmp_path


def run(workspace, quotes, language="en"):
    spec = {"language": language, "pdf_dir": "pdfs", "articles": [{
        "topic": "test", "file": "paper.pdf", "title": "A test paper", "authors": "Doe J", "year": "2024",
        "journal": "Test Journal", "page_offset": 0, "bullets": ["finding"], "quotes": quotes}]}
    spec_path = workspace / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT), str(spec_path)], capture_output=True, text=True,
                          encoding="utf-8")
    report = (workspace / "Research_Summary_Report.md").read_text(encoding="utf-8")
    rejected_path = workspace / "Research_Summary_Report.rejected.json"
    rejected = json.loads(rejected_path.read_text(encoding="utf-8")) if rejected_path.exists() else []
    return proc.returncode, report, rejected


def test_norm_ignores_case_spacing_punctuation_and_ligatures():
    assert norm("Cost-\nEFFECTIVE,  ﬁnding!") == norm("cost effective finding")


def test_norm_keeps_numbers():
    assert norm("reduced by 65%") != norm("reduced by 75%")


@pytest.mark.parametrize("a, b", [
    ("rose by 1.1%", "rose by 11%"),           # decimal point
    ("difference of -0.40", "difference of 0.40"),  # minus sign
    ("difference of −0.40", "difference of 0.40"),  # unicode minus
    ("significant (p<0.05)", "significant (p>0.05)"),  # comparison sign
    ("ירידה של 65% בתחלואה", "עלייה של 65% בתחלואה"),  # Hebrew letters are kept
])
def test_norm_keeps_meaningful_symbols_and_scripts(a, b):
    assert norm(a) != norm(b)


def test_norm_hyphenation_and_dash_variants_are_layout_noise():
    assert norm("commu-\nnity based") == norm("community-based")
    assert norm("2016–2018") == norm("2016-2018")


def test_hebrew_quote_can_be_verified(tmp_path):
    # PDF base-14 fonts cannot draw Hebrew, so test the matching logic directly
    page = norm("התוכנית הפחיתה את מספר האשפוזים ב-12% בשנה הראשונה.")
    assert norm("הפחיתה את מספר האשפוזים ב-12%") in page
    assert norm("הפחיתה את מספר האשפוזים ב-21%") not in page


def test_too_short_quote_is_rejected(workspace):
    code, report, rejected = run(workspace, [{"page": 1, "text": "0.40"}])
    assert code == 2
    assert rejected[0]["reason"] == "too short to verify"


def test_exact_quote_on_correct_page_is_verified(workspace):
    code, report, rejected = run(workspace, [{"page": 1, "text": "reduced fasting glucose by 0.40 mmol/l"}])
    assert code == 0
    assert rejected == []
    assert "reduced fasting glucose by 0.40 mmol/l" in report


def test_quote_split_across_lines_is_still_verified(workspace):
    code, _, rejected = run(workspace, [{"page": 1, "text": "(p<0.001). Group sessions were well attended."}])
    assert code == 0 and rejected == []


def test_altered_number_is_rejected_and_left_out_of_report(workspace):
    fake = "reduced fasting glucose by 0.80 mmol/l"
    code, report, rejected = run(workspace, [{"page": 1, "text": fake}])
    assert code == 2
    assert fake not in report
    assert rejected[0]["found_on_pages"] == []


def test_real_quote_with_wrong_page_is_rejected_and_real_page_reported(workspace):
    quote = "Community-based programs are feasible"
    code, report, rejected = run(workspace, [{"page": 1, "text": quote}])
    assert code == 2
    assert quote not in report
    assert rejected[0]["found_on_pages"] == [2]


def test_page_out_of_range_is_rejected(workspace):
    code, _, rejected = run(workspace, [{"page": 9, "text": "Group sessions were well attended."}])
    assert code == 2 and rejected[0]["claimed_page"] == 9


def test_mixed_quotes_keep_only_verified_ones(workspace):
    good = "Group sessions were well attended."
    bad = "Group sessions were poorly attended."
    code, report, rejected = run(workspace, [{"page": 1, "text": good}, {"page": 1, "text": bad}])
    assert code == 2
    assert good in report and bad not in report
    assert len(rejected) == 1


def test_missing_pdf_is_an_error(workspace):
    (workspace / "pdfs" / "paper.pdf").unlink()
    spec = {"pdf_dir": "pdfs", "articles": [{"file": "paper.pdf", "title": "t", "quotes": []}]}
    (workspace / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT), str(workspace / "spec.json")],
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 1
    assert "PDF not found" in proc.stdout
    assert not (workspace / "Research_Summary_Report.md").exists()
