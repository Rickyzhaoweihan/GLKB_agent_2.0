#!/usr/bin/env python3
"""
05_reconcile_missing.py — eFetch fallback for PMIDs that GLKB has but the
baseline didn't cover (typically newer PMIDs added after the baseline cut).

Streams `MATCH (a:Article) WHERE a.pub_type IS NULL RETURN a.pubmedid` from
Neo4j in chunks, fetches PublicationType via NCBI eFetch (batches of 200),
parses the returned XML, and SETs the property the same way Step 3 does.

Throughput:
    - without NCBI_API_KEY: ~3 req/s × 200 PMIDs/req = ~600 PMIDs/s
    - with NCBI_API_KEY:    ~10 req/s × 200 PMIDs/req = ~2000 PMIDs/s

Usage:
    python 05_reconcile_missing.py
    python 05_reconcile_missing.py --max-pmids 50000   # cap for safety
    python 05_reconcile_missing.py --dry-run           # list missing, don't write
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import List

try:
    import aiohttp
    from neo4j import GraphDatabase
    from dotenv import load_dotenv
    from lxml import etree
except ImportError as e:
    sys.stderr.write(
        f"ERROR: missing dep ({e}). pip install aiohttp neo4j python-dotenv lxml\n"
    )
    sys.exit(1)


_AGENT_ENV = Path(__file__).resolve().parent.parent.parent / "my_agent" / ".env"
if _AGENT_ENV.exists():
    load_dotenv(_AGENT_ENV)


EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
EFETCH_BATCH = 200  # NCBI's documented max per request


def _build_efetch_params(pmids: List[str]) -> dict:
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "rettype": "xml",
        "retmode": "xml",
        "tool": "glkb-migration",
        "email": os.getenv("NCBI_EMAIL", "glkb@umich.edu"),
    }
    api_key = os.getenv("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    return params


async def fetch_one_batch(session, pmids: List[str], sem: asyncio.Semaphore):
    """eFetch one batch of PMIDs → list of {pmid, pub_types}."""
    async with sem:
        try:
            async with session.post(EFETCH_URL, data=_build_efetch_params(pmids), timeout=60) as resp:
                resp.raise_for_status()
                xml_bytes = await resp.read()
        except Exception as exc:
            print(f"  eFetch batch failed ({len(pmids)} PMIDs): {exc}", file=sys.stderr)
            return []

    out: List[dict] = []
    try:
        root = etree.fromstring(xml_bytes)
        for art in root.findall(".//PubmedArticle"):
            pmid_elem = art.find(".//MedlineCitation/PMID")
            if pmid_elem is None or pmid_elem.text is None:
                continue
            pmid = pmid_elem.text.strip()
            types = [pt.text.strip() for pt in art.findall(".//PublicationType") if pt.text]
            out.append({"pmid": pmid, "pub_types": types or ["Unknown"]})
    except Exception as exc:
        print(f"  XML parse failed: {exc}", file=sys.stderr)
    return out


def get_missing_pmids(driver, database: str, limit: int) -> List[str]:
    """Query GLKB for Article nodes whose pub_type is still NULL."""
    with driver.session(database=database) as session:
        rows = session.run(
            "MATCH (a:Article) WHERE a.pub_type IS NULL "
            "RETURN a.pubmedid AS pmid LIMIT $limit",
            limit=limit,
        )
        return [r["pmid"] for r in rows if r["pmid"]]


def write_batch_to_neo4j(driver, database: str, batch: List[dict]) -> int:
    """Apply a batch of {pmid, pub_types} to Article nodes."""
    if not batch:
        return 0
    with driver.session(database=database) as session:
        session.run(
            """
            UNWIND $batch AS row
            MATCH (a:Article {pubmedid: row.pmid})
            SET a.pub_type = row.pub_types
            """,
            batch=batch,
        )
    return len(batch)


async def run(args):
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    database = os.getenv("NEO4J_DATABASE")
    driver = GraphDatabase.driver(uri, auth=(user, password))

    try:
        print(f"Querying GLKB for missing PMIDs (cap {args.max_pmids:,})…")
        missing = get_missing_pmids(driver, database, args.max_pmids)
        print(f"  found {len(missing):,} PMIDs without pub_type")
        if not missing:
            return
        if args.dry_run:
            print("--dry-run: not writing to Neo4j.")
            return

        # Rate-limit concurrent eFetch requests.
        max_concurrency = 10 if os.getenv("NCBI_API_KEY") else 3
        sem = asyncio.Semaphore(max_concurrency)

        batches = [
            missing[i:i + EFETCH_BATCH]
            for i in range(0, len(missing), EFETCH_BATCH)
        ]
        print(f"  scheduling {len(batches)} eFetch batches (concurrency={max_concurrency})")

        t0 = time.time()
        total_updated = 0
        async with aiohttp.ClientSession() as http:
            tasks = [fetch_one_batch(http, b, sem) for b in batches]
            for i, coro in enumerate(asyncio.as_completed(tasks), start=1):
                results = await coro
                n_written = write_batch_to_neo4j(driver, database, results)
                total_updated += n_written
                if i % 20 == 0 or i == len(tasks):
                    elapsed = time.time() - t0
                    rate = total_updated / max(elapsed, 1)
                    print(
                        f"  [{i}/{len(tasks)}] batches done; "
                        f"updated={total_updated:,} rate={rate:,.0f}/s"
                    )
        print(f"\nReconciliation complete. Updated {total_updated:,} of {len(missing):,} PMIDs.")
        if total_updated < len(missing):
            print(
                f"  ({len(missing) - total_updated:,} PMIDs not returned by eFetch — "
                "may be retracted, withdrawn, or NCBI-side anomalies.)"
            )
    finally:
        driver.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--max-pmids", type=int, default=500_000,
        help="Cap on number of missing PMIDs to process in one run.",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Query missing PMIDs but don't eFetch or write.",
    )
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
