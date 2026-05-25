# Design: Search Modes (REVIEW + NON_REVIEW) for GLKBAgent

**Date**: 2026-05-22
**Goal**: Let users constrain retrieval to either review/synthesis literature
(REVIEW) or to everything except reviews (NON_REVIEW), with an AUTO fallback
that preserves current behavior when no mode is specified.
**Approach**: Single-layer query-side filter (PubMed query expansion + GLKB
Cypher WHERE clause), driven by a per-session/per-message `SearchMode` enum
that flows from HTTP request → `service/runner.py` → user-message prefix →
explicit `mode` parameter the agent passes to `search_pubmed` / `article_search`.

## Problem

Currently the agent treats all PubMed/GLKB articles uniformly. Two common user
intents are unserved:

- **Review intent**: "Summarize what's known about X." → user wants narrative
  reviews, systematic reviews, meta-analyses — high signal-to-noise, broad
  coverage, high-quality literature.
- **Non-review intent**: "What did the original RFX6 knockout paper find?" →
  user wants primary research, not literature syntheses.

### Why pub_type tags alone aren't enough for REVIEW recall

PubMed's `<PublicationType>` field is DTD-enforced (100% coverage), but ~46%
of articles carry only the generic `"Journal Article"` tag — including reviews
published recently. NLM publication-type indexing has a 6–12 month lag, so
filtering on `Review[Publication Type]` alone gives ~99% precision but only
~85% recall.

The wrapper compensates by **expanding the query** to match EITHER an NLM
pub_type tag OR a title-based heuristic (`review|systematic review|
meta-analysis|overview` in Title). Combined: precision ~95%, recall ~95%.

## Mode semantics

### REVIEW

- **PubMed query**: `(<query>) AND ("Review"[Publication Type] OR "Systematic Review"[Publication Type] OR "Meta-Analysis"[Publication Type] OR review[Title] OR "systematic review"[Title] OR "meta-analysis"[Title] OR overview[Title])`
- **GLKB Cypher**: `WHERE any(t IN coalesce(a.pub_type, []) WHERE t IN ['Review','Systematic Review','Meta-Analysis']) OR a.title =~ '(?i).*\b(review|systematic\s+review|meta.?analys[ie]s|overview)\b.*'`
- **Quality**: `article_search`'s existing impact-weighted scorer
  (`log(1+5*score) + log(1+n_citation) + 0.5*impact_factor*…`) surfaces
  high-IF, high-citation reviews first — no extra ranking needed.
- **Pre-migration limitation**: GLKB Article nodes don't yet have `pub_type`
  populated, so the REVIEW Cypher's `OR` clause falls through to title-regex
  matching only. Skill instructs the agent to prefer `search_pubmed` for
  REVIEW until the migration completes.

### NON_REVIEW

Symmetric inverse, narrow (only the 3 review-related tags are excluded —
case reports, editorials, letters, guidelines are kept):

- **PubMed query**: `(<query>) NOT "Review"[Publication Type] NOT "Systematic Review"[Publication Type] NOT "Meta-Analysis"[Publication Type] NOT review[Title] NOT "systematic review"[Title] NOT "meta-analysis"[Title]`
- **GLKB Cypher**: `WHERE NOT any(t IN coalesce(a.pub_type, []) WHERE t IN ['Review','Systematic Review','Meta-Analysis']) AND NOT (a.title =~ '(?i).*\b(review|systematic\s+review|meta.?analys[ie]s|overview)\b.*')`

### AUTO

No filter. Identical to pre-feature behavior. Used when the HTTP request omits
`mode` and the session has no default.

## Mode propagation

```
HTTP request
  POST /stream { question, mode? }              ← user explicitly selects mode
  POST .../chat { message, mode? }
  PATCH .../mode { mode }                       ← session-default mode
   │
   ▼
service/api.py
   resolve effective mode (per-request → session.state["search_mode"] → AUTO)
   passes `mode` into runner.run_stream / runner.run
   │
   ▼
service/runner.py
   build_mode_context(SearchMode) → ModeContext
   prepend mode_ctx.user_message_prefix to the LLM-facing message:
     "[search-mode: review]\n
      Prioritize narrative reviews ...\n
      \n
      <original question>"
   yield synthetic ModeContext event up front (so api.py can surface it
   in the SSE Complete payload without recomputing)
   │
   ▼
GLKBAgent (LlmAgent)
   sees the prefix; per skill instruction, calls
     search_pubmed(query=..., mode="review")
     article_search(keywords=..., mode="review")
   │
   ▼
my_agent/tools.py
   _mode_pubmed_clause(mode)   → appends AND/NOT clauses to PubMed query
   _mode_cypher_where(mode)    → injects WHERE fragment into article_search Cypher
   │
   ▼
service/api.py /stream
   emits SSE Complete event with search_mode + mode_applied_constraints
   appends search_mode to transcript JSONL
```

## Implementation summary

The HTTP invocation surface is unchanged from typical session/chat patterns —
just a `mode` field. The actual filter logic is **one query-side layer** in
`my_agent/tools.py`. No post-retrieval classifier, no journal prefix list, no
"Layer 4 active filter" skill text.

| File | Status | Purpose |
|------|--------|---------|
| `service/search_mode.py` | NEW (~110 lines) | `SearchMode` enum + `ModeContext` + `build_mode_context` |
| `service/models.py` | MOD (~15 lines added) | `ChatRequest.mode` field, new `UpdateModeRequest` |
| `service/runner.py` | MOD (~50 lines added) | `_resolve_mode_context` helper + `mode` param on `run`/`run_stream` + prefix prepend + ModeContext event emission |
| `service/api.py` | MOD (~100 lines added) | `PATCH .../mode` endpoint, `StreamRequest.mode` field, forward `mode` to runner, intercept ModeContext, add `search_mode` + `mode_applied_constraints` to SSE Complete and transcript |
| `my_agent/tools.py` | MOD (~90 lines added) | Mode query fragments, `_mode_pubmed_clause` / `_mode_cypher_where` helpers, `mode` param on `search_pubmed` and `article_search`, RETURN `pub_type` from Cypher |
| `my_agent/skills/pubmed_reader/SKILL.md` | MOD (~30 lines added) | `Mode Awareness` section |
| `my_agent/skills/glkb_knowledge_graph/SKILL.md` | MOD (~10 lines added) | Short search-mode-awareness paragraph |
| `CLAUDE.md` | MOD (~30 lines added) | Active Design Work summary |

## GLKB pub_type migration (parallel workstream)

`scripts/migrate_pub_types/` is an independent set of 6 files that populates
`Article.pub_type: LIST<STRING>` on every Article node by streaming NLM Annual
Baseline XML files into Neo4j. See `scripts/migrate_pub_types/README.md` for
the run book.

The agent feature **does not block on migration** — the
`coalesce(a.pub_type, [])` Cypher idiom makes the REVIEW filter a title-only
filter pre-migration (lower recall, same precision) and a pub_type+title
filter post-migration. The skill instructs the agent to fall back to
`search_pubmed` for REVIEW until migration completes.

After migration: `pubmed_reader/SKILL.md` needs a 5-line text update to flip
REVIEW preference to `article_search`. No code change.

## PR plan

| PR | Scope |
|----|-------|
| 1 | Agent feature: `service/search_mode.py` + models + runner + api + tools.py + skills + CLAUDE.md |
| 2 | Migration scripts: `scripts/migrate_pub_types/` (independent) |

## Verification

```bash
# A — syntax sanity
python -m py_compile service/search_mode.py service/runner.py service/api.py \
    service/models.py my_agent/tools.py

# B — AUTO regression (no mode field)
curl -X POST http://localhost:8000/stream -H "Content-Type: application/json" \
    -d '{"question":"What is TP53?"}'
# Expect: identical to pre-feature behavior. SSE Complete: search_mode="auto".

# C — REVIEW mode
curl -N -X POST http://localhost:8000/stream \
    -d '{"question":"Summarize what is known about RFX6 and diabetes","mode":"review"}'
# Expect: every cited article either has Review/SystReview/MetaAnalysis pub_type
# OR a review-y title. High-IF reviews surface first.

# D — NON_REVIEW mode
curl -N -X POST http://localhost:8000/stream \
    -d '{"question":"Original RFX6 knockout studies","mode":"non_review"}'
# Expect: no cited article tagged Review and no review-y title patterns.

# E — sticky session default
curl -X PATCH http://localhost:8000/apps/glkb/users/u1/sessions/s1/mode \
    -d '{"mode":"review"}'
# Subsequent no-mode messages on s1 should resolve to REVIEW.

# F — Pre-migration GLKB smoke
# Article.pub_type is null on all GLKB nodes; REVIEW article_search should
# still return results via the title regex OR branch; NON_REVIEW article_search
# still returns results (pub_type NOT clause is a no-op on coalesced []).
```

## Edge cases

| Case | Behavior |
|------|----------|
| `mode="invalid"` | `SearchMode.parse()` silently falls back to AUTO. No 400 raised. |
| Legacy client sending `mode="research"` | Coerces to AUTO (the old RESEARCH mode is gone). |
| User question conflicts with mode header | Skill instructs agent to follow the explicit question and note the mismatch. |
| Multi-turn: mode set in turn 1, follow-up in turn 5 | Session default persists. Per-message overrides affect that turn only. |
| Pre-migration REVIEW article_search returns sparse | Skill instructs agent to fall back to search_pubmed. |
