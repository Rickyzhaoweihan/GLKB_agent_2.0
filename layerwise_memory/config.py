"""
Configuration for Hierarchical Memory System

Model names, API base, and merge thresholds are read from
my_agent/config.yaml via the cfg object. API keys come from .env.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Locate and import cfg from my_agent/config.py
sys.path.insert(0, str(Path(__file__).parent.parent / "my_agent"))
from config import cfg

API_KEY = (os.getenv('MODEL_API_KEY')
           or os.getenv('OPENROUTER_API_KEY')
           or os.getenv('OPENAI_API_KEY')
           or '')

BASE_URL        = cfg.memory.llm_api_base
LLM_MODEL       = cfg.memory.llm_model
EMBEDDING_MODEL = cfg.memory.embedding_model
EMBEDDING_DIMENSIONS = cfg.memory.embedding_dimensions

DEFAULT_CONCEPT_MERGE_THRESHOLD    = cfg.memory.concept_merge_threshold
DEFAULT_REFLECTION_MERGE_THRESHOLD = cfg.memory.reflection_merge_threshold

OPENAI_AVAILABLE = True  # error surfaces on first use if not installed


class _LazyAsyncClient:
    """AsyncOpenAI proxy — defers the openai import to first use."""

    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._real = None

    def _get(self):
        if self._real is None:
            from openai import AsyncOpenAI
            self._real = AsyncOpenAI(**self._kwargs)
        return self._real

    def __getattr__(self, name):
        return getattr(self._get(), name)


class _LazySyncClient:
    """OpenAI proxy — defers the openai import to first use."""

    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._real = None

    def _get(self):
        if self._real is None:
            from openai import OpenAI
            self._real = OpenAI(**self._kwargs)
        return self._real

    def __getattr__(self, name):
        return getattr(self._get(), name)


# Initialize clients (lazy — openai is imported only on first LLM/embedding call)
client = None
async_client = None
if not API_KEY:
    print("WARNING: No API key found (MODEL_API_KEY / OPENROUTER_API_KEY / OPENAI_API_KEY).", file=sys.stderr)
else:
    client = _LazySyncClient(api_key=API_KEY, base_url=BASE_URL, max_retries=6)
    async_client = _LazyAsyncClient(api_key=API_KEY, base_url=BASE_URL, max_retries=6)

__all__ = [
    'API_KEY',
    'LLM_MODEL',
    'EMBEDDING_MODEL',
    'EMBEDDING_DIMENSIONS',
    'DEFAULT_CONCEPT_MERGE_THRESHOLD',
    'DEFAULT_REFLECTION_MERGE_THRESHOLD',
    'client',
    'async_client',
    'OPENAI_AVAILABLE',
]
