#!/usr/bin/env python3
"""
02_parse_xml.py — Stream NLM PubMed XML.gz files and emit one JSONL line
per article: {"pmid": "...", "pub_types": ["...", ...]}.

Streaming with lxml.etree.iterparse keeps memory bounded regardless of
input file size (~30 MB gzipped, ~300 MB unzipped per file).

Usage:
    python 02_parse_xml.py --input-dir ./data/baseline --output-dir ./data/jsonl
    python 02_parse_xml.py --input-dir ./data/updatefiles --output-dir ./data/jsonl --incremental
    python 02_parse_xml.py --input-dir ./data/baseline --output-dir ./data/jsonl --workers 8

Time budget: ~2-3 hours for the full baseline on a single CPU. Parallelizable
across files via the `--workers` flag (default = os.cpu_count() // 2).
"""

from __future__ import annotations

import argparse
import gzip
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional

try:
    from lxml import etree
except ImportError:
    sys.stderr.write(
        "ERROR: lxml is required. Install with: pip install lxml\n"
    )
    sys.exit(1)


# ----------------------------------------------------------------------------
# Per-article extraction.
# ----------------------------------------------------------------------------

def extract_pub_types(article_elem) -> List[str]:
    """Pull every <PublicationType> text inside a <PubmedArticle>.

    Returns ['Unknown'] if no PublicationType elements are found (the DTD
    requires at least one, but defensive coding never hurts).
    """
    types: List[str] = []
    for pt in article_elem.findall(".//PublicationType"):
        if pt.text:
            types.append(pt.text.strip())
    return types or ["Unknown"]


def extract_pmid(article_elem) -> Optional[str]:
    """Get the PMID from a <PubmedArticle>. NLM puts PMIDs in several spots;
    the canonical one is `<MedlineCitation><PMID>...</PMID></MedlineCitation>`."""
    pmid_elem = article_elem.find(".//MedlineCitation/PMID")
    if pmid_elem is None:
        pmid_elem = article_elem.find(".//PMID")
    if pmid_elem is None or pmid_elem.text is None:
        return None
    return pmid_elem.text.strip()


def parse_file(input_path: Path, output_path: Path) -> dict:
    """Stream one .xml.gz, write one JSONL line per article."""
    n_in, n_ok, n_no_pmid = 0, 0, 0
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with gzip.open(input_path, "rb") as gz_fh, open(output_path, "w") as out_fh:
        # `iterparse` events:
        #   - start: tag opens (don't consume yet)
        #   - end:   tag closes (process and clear)
        context = etree.iterparse(gz_fh, events=("end",), tag="PubmedArticle")
        for _event, elem in context:
            n_in += 1
            pmid = extract_pmid(elem)
            if pmid:
                pub_types = extract_pub_types(elem)
                out_fh.write(json.dumps({"pmid": pmid, "pub_types": pub_types}) + "\n")
                n_ok += 1
            else:
                n_no_pmid += 1
            # Free the entire subtree — keep memory bounded.
            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]

    return {
        "file": input_path.name,
        "articles_total": n_in,
        "articles_with_pmid": n_ok,
        "articles_missing_pmid": n_no_pmid,
    }


# ----------------------------------------------------------------------------
# Driver.
# ----------------------------------------------------------------------------

def discover_inputs(input_dir: Path, incremental: bool, output_dir: Path) -> List[Path]:
    """List .xml.gz files in input_dir. With `--incremental`, skip files that
    already have a matching .jsonl in output_dir."""
    candidates = sorted(input_dir.glob("pubmed*.xml.gz"))
    if not incremental:
        return candidates
    todo: List[Path] = []
    for p in candidates:
        jsonl_name = p.name.replace(".xml.gz", ".jsonl")
        if not (output_dir / jsonl_name).exists():
            todo.append(p)
    return todo


def _worker(args):
    """multiprocessing entry point — unpacks to parse_file."""
    in_path, out_path = args
    try:
        return parse_file(in_path, out_path)
    except Exception as exc:
        return {"file": in_path.name, "error": str(exc)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input-dir", required=True, type=Path)
    ap.add_argument("--output-dir", required=True, type=Path)
    ap.add_argument(
        "--incremental", action="store_true",
        help="Skip files that already have a matching .jsonl in output-dir.",
    )
    ap.add_argument(
        "--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2),
        help="Number of parallel worker processes.",
    )
    args = ap.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    inputs = discover_inputs(args.input_dir, args.incremental, args.output_dir)
    if not inputs:
        print(f"No .xml.gz files to process in {args.input_dir}.")
        return

    print(f"Parsing {len(inputs)} files with {args.workers} workers → {args.output_dir}")
    work_items = [
        (p, args.output_dir / p.name.replace(".xml.gz", ".jsonl"))
        for p in inputs
    ]
    t0 = time.time()
    totals = {"articles_total": 0, "articles_with_pmid": 0, "articles_missing_pmid": 0, "errors": 0}
    with mp.Pool(args.workers) as pool:
        for i, result in enumerate(pool.imap_unordered(_worker, work_items), start=1):
            if "error" in result:
                totals["errors"] += 1
                print(f"  [{i}/{len(work_items)}] ERROR {result['file']}: {result['error']}", file=sys.stderr)
            else:
                totals["articles_total"] += result["articles_total"]
                totals["articles_with_pmid"] += result["articles_with_pmid"]
                totals["articles_missing_pmid"] += result["articles_missing_pmid"]
                if i % 50 == 0 or i == len(work_items):
                    elapsed = time.time() - t0
                    rate = totals["articles_total"] / max(elapsed, 1)
                    print(
                        f"  [{i}/{len(work_items)}] {result['file']} "
                        f"+{result['articles_with_pmid']:>6} "
                        f"(cumulative {totals['articles_with_pmid']:,} @ {rate:,.0f}/s)"
                    )

    elapsed = time.time() - t0
    print("\n=== parse summary ===")
    print(f"  files processed:        {len(inputs) - totals['errors']:,}")
    print(f"  files with errors:      {totals['errors']:,}")
    print(f"  articles total:         {totals['articles_total']:,}")
    print(f"  articles with PMID:     {totals['articles_with_pmid']:,}")
    print(f"  articles missing PMID:  {totals['articles_missing_pmid']:,}")
    print(f"  elapsed:                {elapsed/60:.1f} min")


if __name__ == "__main__":
    main()
