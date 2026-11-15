# -*- coding: utf-8 -*-
"""Command line: python -m verified_lit_review "research question" --n 3 [--lang he|en]"""
import argparse
import os
import sys

from .agent import MODEL, Agent
from .db import Store

ICONS = {"search": "🔍", "fetch": "⬇ ", "retrieve": "📄", "verify": "✅"}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Agentic RAG literature review with verified quotes")
    ap.add_argument("question")
    ap.add_argument("--n", type=int, default=3, help="number of papers (default 3)")
    ap.add_argument("--lang", default="he", help="report language: he or en (default he)")
    ap.add_argument("--out", default="output", help="output folder (default ./output)")
    ap.add_argument("--db", default="medical_cache.db")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Create a key at console.anthropic.com and set it as an "
                 "environment variable (never put it in the code).")
    os.makedirs(args.out, exist_ok=True)
    agent = Agent(Store(args.db), os.path.join(args.out, "pdfs"), model=args.model, effort=args.effort,
                  on_event=lambda kind, msg: print(f"{ICONS.get(kind, '•')} {msg}", flush=True))
    print(f"🤖 {args.model} · question: {args.question}\n", flush=True)
    r = agent.run(args.question, n_papers=args.n, language=args.lang,
                  out_path=os.path.join(args.out, "Research_Summary_Report.md"))
    total = r["verified"] + len(r["rejected"])
    print(f"\n📝 Report: {r['report']}")
    print(f"✅ Quotes verified: {r['verified']}/{total} · papers: {r['papers']} · run #{r['run_id']}")
    for q in r["rejected"]:
        where = f"actually on page(s) {q['found_on_pages']}" if q["found_on_pages"] else q.get("reason") or "not found"
        print(f"❌ REJECTED p.{q['claimed_page']} ({where}): {q['text'][:80]}...")
    u = r["usage"]
    cached = u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    print(f"💲 {u['input_tokens'] + cached:,} input ({u.get('cache_read_input_tokens', 0):,} from cache) + "
          f"{u['output_tokens']:,} output tokens ≈ ${r['cost_usd']} · {r['seconds']}s")


if __name__ == "__main__":
    main()
