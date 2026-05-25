#!/usr/bin/env python3
"""
03_load_to_neo4j.py — Stream JSONL files into Neo4j, setting Article.pub_type
in batches of (default) 5000 PMIDs per transaction.

Idempotent: re-running a completed batch just re-SETs the same value.
Resumable: writes ./checkpoint.json after every successful batch and skips
already-processed batches on restart.

Usage:
    python 03_load_to_neo4j.py --jsonl-dir ./data/jsonl
    python 03_load_to_neo4j.py --jsonl-dir ./data/jsonl --batch-size 5000
    python 03_load_to_neo4j.py --jsonl-dir ./data/jsonl --incremental
    python 03_load_to_neo4j.py --jsonl-dir ./data/jsonl --restart   # ignore checkpoint
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterator, List

try:
    from neo4j import GraphDatabase
except ImportError:
    sys.stderr.write("ERROR: neo4j driver required. pip install neo4j\n")
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    sys.stderr.write("ERROR: python-dotenv required. pip install python-dotenv\n")
    sys.exit(1)


# Load agent's .env so we share NEO4J_* credentials.
_AGENT_ENV = Path(__file__).resolve().parent.parent.parent / "my_agent" / ".env"
if _AGENT_ENV.exists():
    load_dotenv(_AGENT_ENV)

CHECKPOINT_FILE = Path(__file__).resolve().parent / "checkpoint.json"

CYPHER_BATCH_UPDATE = """
UNWIND $batch AS row
MATCH (a:Article {pubmedid: row.pmid})
SET a.pub_type = row.pub_types
"""


# ----------------------------------------------------------------------------
# Checkpoint helpers.
# ----------------------------------------------------------------------------

def load_checkpoint() -> dict:
    if not CHECKPOINT_FILE.exists():
        return {"completed_files": [], "current_file": None, "current_batch_id": 0}
    try:
        with open(CHECKPOINT_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {"completed_files": [], "current_file": None, "current_batch_id": 0}


def save_checkpoint(state: dict) -> None:
    """Atomic write: tmp + rename."""
    tmp = CHECKPOINT_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, CHECKPOINT_FILE)


# ----------------------------------------------------------------------------
# Batch iterator.
# ----------------------------------------------------------------------------

def iter_batches(path: Path, batch_size: int) -> Iterator[List[dict]]:
    """Read a JSONL file and yield batches of `batch_size` rows."""
    batch: List[dict] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                batch.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if batch:
        yield batch


# ----------------------------------------------------------------------------
# Driver.
# ----------------------------------------------------------------------------

def run_migration(
    jsonl_dir: Path,
    batch_size: int,
    incremental: bool,
    restart: bool,
) -> None:
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    database = os.getenv("NEO4J_DATABASE")
    if not all([uri, user, password, database]):
        sys.stderr.write(
            "ERROR: NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD / NEO4J_DATABASE "
            "must be set in env or my_agent/.env\n"
        )
        sys.exit(1)

    if restart and CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        print(f"--restart: removed {CHECKPOINT_FILE}")

    state = load_checkpoint()
    completed = set(state.get("completed_files", []))

    files = sorted(jsonl_dir.glob("*.jsonl"))
    if incremental:
        files = [p for p in files if p.name not in completed]

    if not files:
        print(f"No JSONL files to load in {jsonl_dir} (incremental={incremental}).")
        return

    print(f"Loading {len(files)} JSONL files → Neo4j {database}@{uri}")
    print(f"  batch_size={batch_size}, incremental={incremental}")
    if completed:
        print(f"  skipping {len(completed)} already-completed files")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    t0 = time.time()
    grand_total = 0
    try:
        for fi, jsonl_path in enumerate(files, start=1):
            file_total = 0
            batch_id = 0
            state["current_file"] = jsonl_path.name
            print(f"\n[{fi}/{len(files)}] {jsonl_path.name}")
            try:
                with driver.session(database=database) as session:
                    for batch in iter_batches(jsonl_path, batch_size):
                        batch_id += 1
                        session.run(CYPHER_BATCH_UPDATE, batch=batch)
                        file_total += len(batch)
                        state["current_batch_id"] = batch_id
                        # Atomic checkpoint after every batch.
                        save_checkpoint(state)
                        if batch_id % 20 == 0:
                            elapsed = time.time() - t0
                            rate = (grand_total + file_total) / max(elapsed, 1)
                            print(
                                f"  batch {batch_id:>5}  +{len(batch):>5}  "
                                f"file_total={file_total:>7,}  "
                                f"rate={rate:,.0f}/s"
                            )
                # Mark file completed only after every batch in it commits.
                completed.add(jsonl_path.name)
                state["completed_files"] = sorted(completed)
                state["current_file"] = None
                state["current_batch_id"] = 0
                save_checkpoint(state)
                grand_total += file_total
                print(f"  done. file_total={file_total:,}  grand_total={grand_total:,}")
            except Exception as exc:
                print(f"  ERROR loading {jsonl_path.name}: {exc}", file=sys.stderr)
                print(f"  partial progress saved in {CHECKPOINT_FILE}", file=sys.stderr)
                raise
    finally:
        driver.close()

    elapsed = time.time() - t0
    print("\n=== load summary ===")
    print(f"  files loaded:    {len(files):,}")
    print(f"  pmids updated:   {grand_total:,}")
    print(f"  elapsed:         {elapsed/60:.1f} min")
    print(f"  effective rate:  {grand_total/max(elapsed,1):,.0f} pmids/s")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl-dir", required=True, type=Path)
    ap.add_argument("--batch-size", type=int, default=5000)
    ap.add_argument(
        "--incremental", action="store_true",
        help="Skip files already in checkpoint.json's completed_files.",
    )
    ap.add_argument(
        "--restart", action="store_true",
        help="Delete checkpoint.json and start fresh.",
    )
    args = ap.parse_args()
    run_migration(args.jsonl_dir, args.batch_size, args.incremental, args.restart)


if __name__ == "__main__":
    main()
