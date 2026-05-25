# GLKB `pub_type` Migration

One-time data migration: populate `Article.pub_type: LIST<STRING>` on every
`Article` node in the GLKB Neo4j database, derived from NLM Annual Baseline
XML files.

Independent workstream — does not block the agent search-mode feature (the
agent ships with forward-compatible Cypher and skill text that handles both
pre- and post-migration phases). See
`docs/plans/2026-05-22-search-mode-design.md` → "GLKB pub_type Migration" for
the full design rationale.

## Prerequisites

| Item | Requirement |
|------|-------------|
| Python | 3.10+ |
| Disk | ~45 GB free (downloaded baseline + parsed JSONL) |
| Network | rsync access to `rsync://ftp.ncbi.nlm.nih.gov/pubmed/` |
| Neo4j | read+write access to the GLKB database |
| Optional | `NCBI_API_KEY` env var (10 req/s vs 3 req/s for Step 5 reconciliation) |

Python packages: `lxml`, `neo4j`, `aiohttp`, `python-dotenv`. Install with:

```bash
pip install lxml neo4j aiohttp python-dotenv
```

The migration scripts read the same `.env` as the agent
(`my_agent/.env` — `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`,
`NEO4J_DATABASE`).

## End-to-end run

```bash
cd scripts/migrate_pub_types

# Step 1 — download (~1.5 hours, network-bound)
./01_download_baseline.sh

# Step 2 — parse XML.gz → JSONL (~2-3 hours CPU-bound, parallelizable)
python 02_parse_xml.py --input-dir ./data/baseline --output-dir ./data/jsonl
python 02_parse_xml.py --input-dir ./data/updatefiles --output-dir ./data/jsonl

# Step 3 — bulk load into Neo4j (~3 hours, write-bound)
python 03_load_to_neo4j.py --jsonl-dir ./data/jsonl --batch-size 5000

# Step 4 — verify coverage
python 04_verify.py

# Step 5 — eFetch fallback for un-covered PMIDs (~minutes for <1% of articles)
python 05_reconcile_missing.py

# Step 6 — create the membership index
# Run this Cypher directly via cypher-shell or any Neo4j client:
#   CREATE INDEX article_pub_type IF NOT EXISTS FOR (a:Article) ON (a.pub_type)
```

## Resumability

Step 3 writes `checkpoint.json` after every successful batch. Re-running the
script picks up where it left off. The migration is **idempotent** — re-loading
an already-migrated PMID just re-`SET`s the same value.

To start from scratch, delete `checkpoint.json` (and optionally rollback —
see below).

## Safety

- **Additive only**: every Cypher statement is `SET a.pub_type = ...`. No
  existing properties are deleted or overwritten.
- **Per-batch transactions** (default 5000 PMIDs each): a crash mid-migration
  leaves earlier batches committed, later batches absent. Resume via
  `checkpoint.json`.
- **Test on replica first**: recommended for full corpus runs. Use Step 4's
  acceptance thresholds to spot anomalies before pointing at production.

## Rollback

```cypher
CALL apoc.periodic.iterate(
  "MATCH (a:Article) WHERE a.pub_type IS NOT NULL RETURN a",
  "REMOVE a.pub_type",
  {batchSize: 10000}
)
```

Requires APOC. If APOC isn't available, use a plain `MATCH ... REMOVE` loop
over batched PMID ranges via cypher-shell.

## Monthly incremental refresh

NLM publishes ~30 update files per day. After the initial baseline run, a
monthly cron of Steps 1–3 keeps the GLKB `pub_type` drift under 30 days,
which is well within the NLM indexing lag itself.

```bash
0 3 1 * *  cd /path/to/scripts/migrate_pub_types && \
    ./01_download_baseline.sh && \
    python 02_parse_xml.py --input-dir ./data/updatefiles --output-dir ./data/jsonl --incremental && \
    python 03_load_to_neo4j.py --jsonl-dir ./data/jsonl --incremental
```

## After migration completes

Update the pubmed-reader skill to flip REVIEW-mode preference from
`search_pubmed` to `article_search`. This is a ~5-line edit to
`my_agent/skills/pubmed_reader/SKILL.md`; no code change required.
