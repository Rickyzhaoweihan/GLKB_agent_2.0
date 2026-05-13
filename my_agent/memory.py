"""
LayerMem integration for the GLKB agent.

Exposes MemoryToolset and _memory_after_agent_callback for wiring into agent.py.
All settings are read from config.yaml via cfg.
"""

import sys
import os
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools import FunctionTool

from config import cfg

logger = logging.getLogger(__name__)

# -----------------------------------------
# LayerMem setup
# -----------------------------------------

MEMORY_DB_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", cfg.paths.layermem_db)
)
_LAYERMEM_ENABLED = cfg.memory.enabled
CONSOLIDATE_EVERY_N_FLUSHES = cfg.memory.consolidate_every_n_flushes
AGENT_TEXT_LIMIT = cfg.memory.agent_text_limit

if _LAYERMEM_ENABLED:
    _LAYERMEM_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "layerwise_memory"))
    sys.path.insert(0, _LAYERMEM_DIR)

    from agent_memory import ConversationMemory, ModifiedMemory, load_from_sqlite
    from layermem_config import async_client as _mem_async_client, LLM_MODEL as _mem_llm_model  # type: ignore[import]

    logger.info(f"LayerMem enabled (db: {MEMORY_DB_PATH})")
else:
    logger.info("LayerMem disabled (set memory.enabled: true in config.yaml to enable)")

# Per-user ConversationMemory instances, keyed by user_id.
# Loaded lazily on first request; persist for the lifetime of the server process.
_user_mems: dict = {}


def _get_user_mem(user_id: str) -> "ConversationMemory":
    if user_id not in _user_mems:
        inner = load_from_sqlite(MEMORY_DB_PATH, user_id) if os.path.exists(MEMORY_DB_PATH) else ModifiedMemory()
        _user_mems[user_id] = ConversationMemory(inner, MEMORY_DB_PATH, user_id)
        logger.info(f"Loaded memory for user {user_id!r}")
    return _user_mems[user_id]


# -----------------------------------------
# Episode boundary detection helpers
# -----------------------------------------

def _truncate_turn(lines: list) -> str:
    parts = []
    for line in lines:
        if line.startswith("GLKBAgent:") and len(line) > 10 + AGENT_TEXT_LIMIT:
            line = line[:10 + AGENT_TEXT_LIMIT] + "..."
        parts.append(line)
    return "\n".join(parts)


async def _is_boundary(buffer_lines: list) -> bool:
    """Ask the memory LLM if the most recent turn is a topic shift from the buffered episode."""
    prior = "\n".join(_truncate_turn([l]) for l in buffer_lines[:-2])
    latest = _truncate_turn(buffer_lines[-2:])
    try:
        response = await _mem_async_client.chat.completions.create(  # type: ignore[name-defined]
            model=_mem_llm_model,  # type: ignore[name-defined]
            messages=[{
                "role": "user",
                "content": (
                    "You are a conversation segmentation assistant for a biomedical research assistant.\n\n"
                    "Current episode:\n"
                    f"{prior}\n\n"
                    "New turn:\n"
                    f"{latest}\n\n"
                    "Reply YES only if the new turn switches to an unrelated biomedical subject "
                    "(e.g. a different gene, disease, or research area with no connection to the episode above). "
                    "Follow-up questions about the SAME content, clarifications, related entities, or deeper dives into the same subject are NO. "
                    "Reply YES or NO only."
                ),
            }],
            max_tokens=5,
            temperature=0,
        )
        answer = (response.choices[0].message.content or "").strip().upper()
        return answer.startswith("YES")
    except Exception as e:
        logger.warning(f"Episode boundary check failed, keeping current episode: {e}")
        return False


async def _flush_keep_last_turn(mem: "ConversationMemory", session_id: str) -> None:
    """Flush all turns except the most recent into an episode; latest turn seeds the new episode."""
    all_lines = list(mem._turn_buffers.get(session_id, []))
    if len(all_lines) < 4:
        return
    to_flush = all_lines[:-2]
    mem._turn_buffers[session_id] = all_lines[-2:]
    content = "\n".join(to_flush)
    timestamp = datetime.now(timezone.utc).isoformat()
    mem._flush_count += 1
    source_id = f"{session_id}_part{mem._flush_count}"
    # Await ingestion so concepts/embeddings are in ModifiedMemory before mem.save() is called
    await mem._mem.add_content_async(content, source_id, "conversation", timestamp, False)
    if mem._flush_count % CONSOLIDATE_EVERY_N_FLUSHES == 0:
        asyncio.create_task(mem.consolidate())  # consolidation stays background — expensive


async def _trigger_flush(mem: "ConversationMemory", session_id: str, wait: bool = False, consolidate: bool = True) -> None:
    """Pop the turn buffer and ingest. Background by default; await if wait=True.

    consolidate=False skips the periodic sleep_update — use this on shutdown
    to avoid spawning a long-running task that may be killed mid-consolidation.
    """
    lines = mem._turn_buffers.pop(session_id, [])
    if not lines:
        return
    content = "\n".join(lines)
    timestamp = datetime.now(timezone.utc).isoformat()
    mem._flush_count += 1
    source_id = f"{session_id}_part{mem._flush_count}"
    ingestion = mem._mem.add_content_async(content, source_id, "conversation", timestamp, False)
    if wait:
        await ingestion
    else:
        asyncio.create_task(ingestion)
    if consolidate and mem._flush_count % CONSOLIDATE_EVERY_N_FLUSHES == 0:
        asyncio.create_task(mem.consolidate())

# -----------------------------------------
# After-agent callback
# -----------------------------------------

async def _memory_after_agent_callback(callback_context) -> None:
    """Auto-buffer each turn; flush to LayerMem when the memory LLM detects a topic shift."""
    if not _LAYERMEM_ENABLED:
        return None

    user_id = callback_context.session.user_id
    session_id = callback_context.session.id

    mem = _get_user_mem(user_id)

    user_text = ""
    if callback_context.user_content:
        user_text = " ".join(
            p.text for p in (getattr(callback_context.user_content, "parts", None) or [])
            if getattr(p, "text", None)
        )

    agent_text = ""
    for event in reversed(callback_context.session.events):
        if event.author == "GLKBAgent" and event.content:
            texts = [p.text for p in (event.content.parts or []) if getattr(p, "text", None)]
            if texts:
                agent_text = " ".join(texts)
                break

    if not user_text and not agent_text:
        return None

    if user_text:
        mem.add_turn("User", user_text, session_id=session_id)
    if agent_text:
        mem.add_turn("GLKBAgent", agent_text, session_id=session_id)

    logger.debug(f"Memory buffer | user={len(user_text)}chars agent={len(agent_text)}chars")

    buffer_lines = mem._turn_buffers.get(session_id, [])
    if len(buffer_lines) >= 4 and await _is_boundary(buffer_lines):
        logger.info("Episode boundary detected — flushing buffer, keeping latest turn")
        await _flush_keep_last_turn(mem, session_id)
        mem.save()

    return None

# -----------------------------------------
# Agent-callable memory tools
# -----------------------------------------

async def query_memory(question: str, tool_context=None) -> dict:
    """Query long-term memory for relevant context from past sessions."""
    if not _LAYERMEM_ENABLED:
        return {"answer": "Memory is disabled. Set memory.enabled: true in config.yaml to enable."}
    # tool_context is injected by ADK; user_id is a direct attribute on Context
    user_id = tool_context.user_id  # type: ignore[union-attr]
    mem = _get_user_mem(user_id)
    answer = await mem.answer(question)
    return {"answer": answer}


async def save_memory(tool_context=None) -> dict:
    """Flush current buffer, consolidate memory, and persist to disk."""
    if not _LAYERMEM_ENABLED:
        return {"status": "disabled"}
    user_id = tool_context.user_id  # type: ignore[union-attr]
    session_id = tool_context.session.id
    mem = _get_user_mem(user_id)
    await _trigger_flush(mem, session_id, wait=True)
    await mem.consolidate(n_questions_per_chunk=1)
    mem.save()
    return {"status": "ok", "path": MEMORY_DB_PATH}

# -----------------------------------------
# MemoryToolset
# -----------------------------------------

class MemoryToolset(BaseToolset):
    """Exposes memory tools and flushes + saves all users on runner shutdown."""

    async def get_tools(self, readonly_context: ReadonlyContext = None) -> list:
        return [FunctionTool(query_memory), FunctionTool(save_memory)]

    async def close(self) -> None:
        if not _LAYERMEM_ENABLED:
            return
        for user_id, mem in list(_user_mems.items()):
            for session_id in list(mem._turn_buffers.keys()):
                await _trigger_flush(mem, session_id, wait=True, consolidate=False)
            mem.save()
            logger.info(f"Memory flushed and saved for user {user_id!r} on runner close.")
