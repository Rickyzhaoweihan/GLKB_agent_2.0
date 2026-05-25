#!/usr/bin/env python3
"""
04_verify.py — Run coverage + distribution checks on the migrated `pub_type`
property. Prints a summary table and exits non-zero if the acceptance
thresholds aren't met.

Acceptance thresholds (derived from the empirical analysis in
docs/plans/2026-05-22-search-mode-design.md):
    with_pub_type / total       > 0.99    (≤1% Unknown/missing OK)
    journal_article / total     > 0.93    (sanity: should be ~95%)
    reviews / total             ≈ 0.035 ± 0.01

Usage:
    python 04_verify.py
    python 04_verify.py --strict       # exit non-zero on threshold miss
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from neo4j import GraphDatabase
    from dotenv import load_dotenv
except ImportError:
    sys.stderr.write("ERROR: neo4j and python-dotenv required.\n")
    sys.exit(1)


_AGENT_ENV = Path(__file__).resolve().parent.parent.parent / "my_agent" / ".env"
if _AGENT_ENV.exists():
    load_dotenv(_AGENT_ENV)


VERIFY_CYPHER = """
MATCH (a:Article)
RETURN
  count(a) AS total,
  sum(CASE WHEN a.pub_type IS NOT NULL THEN 1 ELSE 0 END) AS with_pub_type,
  sum(CASE WHEN 'Review' IN coalesce(a.pub_type, []) THEN 1 ELSE 0 END) AS reviews,
  sum(CASE WHEN 'Systematic Review' IN coalesce(a.pub_type, []) THEN 1 ELSE 0 END) AS sys_reviews,
  sum(CASE WHEN 'Meta-Analysis' IN coalesce(a.pub_type, []) THEN 1 ELSE 0 END) AS meta_analyses,
  sum(CASE WHEN 'Journal Article' IN coalesce(a.pub_type, []) THEN 1 ELSE 0 END) AS journal_article,
  sum(CASE WHEN 'Case Reports' IN coalesce(a.pub_type, []) THEN 1 ELSE 0 END) AS case_reports,
  sum(CASE WHEN 'Editorial' IN coalesce(a.pub_type, []) THEN 1 ELSE 0 END) AS editorials,
  sum(CASE WHEN 'Letter' IN coalesce(a.pub_type, []) THEN 1 ELSE 0 END) AS letters,
  sum(CASE WHEN 'Unknown' IN coalesce(a.pub_type, []) THEN 1 ELSE 0 END) AS unknown_tag
"""


THRESHOLDS = {
    "with_pub_type_rate": 0.99,
    "journal_article_rate": 0.93,
    "review_rate_min": 0.020,
    "review_rate_max": 0.050,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero if any acceptance threshold is missed.",
    )
    args = ap.parse_args()

    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    database = os.getenv("NEO4J_DATABASE")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            row = session.run(VERIFY_CYPHER).single()
    finally:
        driver.close()

    if row is None:
        sys.stderr.write("Verify query returned no rows. Is the GLKB Article corpus populated?\n")
        sys.exit(2)

    stats = dict(row)
    total = max(stats["total"], 1)
    with_rate = stats["with_pub_type"] / total
    review_rate = stats["reviews"] / total
    journal_rate = stats["journal_article"] / total

    print("=== GLKB pub_type migration verification ===\n")
    print(f"  total articles:           {stats['total']:>12,}")
    print(f"  with pub_type populated:  {stats['with_pub_type']:>12,}  ({with_rate:6.2%})")
    print(f"  tagged Journal Article:   {stats['journal_article']:>12,}  ({journal_rate:6.2%})")
    print(f"  tagged Review:            {stats['reviews']:>12,}  ({review_rate:6.2%})")
    print(f"  tagged Systematic Review: {stats['sys_reviews']:>12,}")
    print(f"  tagged Meta-Analysis:     {stats['meta_analyses']:>12,}")
    print(f"  tagged Case Reports:      {stats['case_reports']:>12,}")
    print(f"  tagged Editorial:         {stats['editorials']:>12,}")
    print(f"  tagged Letter:            {stats['letters']:>12,}")
    print(f"  tagged Unknown:           {stats['unknown_tag']:>12,}")

    failures = []
    if with_rate < THRESHOLDS["with_pub_type_rate"]:
        failures.append(
            f"  - coverage {with_rate:.2%} < {THRESHOLDS['with_pub_type_rate']:.2%}"
        )
    if journal_rate < THRESHOLDS["journal_article_rate"]:
        failures.append(
            f"  - Journal Article rate {journal_rate:.2%} < {THRESHOLDS['journal_article_rate']:.2%}"
        )
    if not (THRESHOLDS["review_rate_min"] <= review_rate <= THRESHOLDS["review_rate_max"]):
        failures.append(
            f"  - Review rate {review_rate:.2%} outside "
            f"[{THRESHOLDS['review_rate_min']:.2%}, {THRESHOLDS['review_rate_max']:.2%}]"
        )

    if failures:
        print("\nAcceptance threshold check: FAIL")
        for f in failures:
            print(f)
        if args.strict:
            sys.exit(3)
    else:
        print("\nAcceptance threshold check: PASS")


if __name__ == "__main__":
    main()
