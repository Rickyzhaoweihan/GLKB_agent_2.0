"""
Configuration for Hierarchical Memory System

Model names, API base, and embedding settings are read from
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

BASE_URL             = cfg.memory.llm_api_base
LLM_MODEL            = cfg.memory.llm_model
EMBEDDING_MODEL      = cfg.memory.embedding_model
EMBEDDING_DIMENSIONS = cfg.memory.embedding_dimensions

OPENAI_AVAILABLE = True  # error surfaces on first use if not installed

client = None
async_client = None
if not API_KEY:
    print("WARNING: No API key found (MODEL_API_KEY / OPENROUTER_API_KEY / OPENAI_API_KEY).", file=sys.stderr)
else:
    from openai import OpenAI, AsyncOpenAI
    client       = OpenAI(api_key=API_KEY, base_url=BASE_URL, max_retries=6)
    async_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, max_retries=6)

__all__ = [
    'API_KEY',
    'LLM_MODEL',
    'EMBEDDING_MODEL',
    'EMBEDDING_DIMENSIONS',
    'client',
    'async_client',
    'OPENAI_AVAILABLE',
]
