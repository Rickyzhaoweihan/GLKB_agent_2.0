"""
Test that per-user memory isolation works at the SQLite layer.

No LLM or API calls needed — manipulates ModifiedMemory objects directly
and verifies save/load round-trips stay isolated by user_id.

Run from the project root:
    python test_memory_isolation.py
"""

import sys
import os
import sqlite3
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "layerwise_memory"))

from agent_memory import (
    ConversationMemory,
    ModifiedMemory,
    save_to_sqlite,
    load_from_sqlite,
)

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

def check(label: str, condition: bool) -> bool:
    print(f"  {'✓' if condition else '✗'}  {label}: {PASS if condition else FAIL}")
    return condition


def test_save_load_isolation():
    print("\n--- Test 1: save/load isolates by user_id ---")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        mem_a = ModifiedMemory()
        mem_a.source_registry = {"doc_alice": ["traj_a1", "traj_a2"]}
        mem_a._docs_since_sleep = ["doc_alice"]

        mem_b = ModifiedMemory()
        mem_b.source_registry = {"doc_bob": ["traj_b1"]}
        mem_b._docs_since_sleep = ["doc_bob"]

        save_to_sqlite(db_path, "alice", mem_a)
        save_to_sqlite(db_path, "bob", mem_b)

        loaded_a = load_from_sqlite(db_path, "alice")
        loaded_b = load_from_sqlite(db_path, "bob")

        all_passed = True
        all_passed &= check("alice sees her own registry",
                            loaded_a.source_registry == {"doc_alice": ["traj_a1", "traj_a2"]})
        all_passed &= check("alice does not see bob's registry",
                            "doc_bob" not in loaded_a.source_registry)
        all_passed &= check("bob sees his own registry",
                            loaded_b.source_registry == {"doc_bob": ["traj_b1"]})
        all_passed &= check("bob does not see alice's registry",
                            "doc_alice" not in loaded_b.source_registry)
        all_passed &= check("alice's docs_since_sleep correct",
                            loaded_a._docs_since_sleep == ["doc_alice"])
        all_passed &= check("bob's docs_since_sleep correct",
                            loaded_b._docs_since_sleep == ["doc_bob"])
        return all_passed
    finally:
        os.unlink(db_path)


def test_overwrite_only_own_user():
    print("\n--- Test 2: re-saving one user does not touch the other ---")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        mem_a = ModifiedMemory()
        mem_a.source_registry = {"doc_alice_v1": []}
        mem_b = ModifiedMemory()
        mem_b.source_registry = {"doc_bob": []}

        save_to_sqlite(db_path, "alice", mem_a)
        save_to_sqlite(db_path, "bob", mem_b)

        # Update alice's memory and re-save
        mem_a2 = ModifiedMemory()
        mem_a2.source_registry = {"doc_alice_v2": []}
        save_to_sqlite(db_path, "alice", mem_a2)

        loaded_a = load_from_sqlite(db_path, "alice")
        loaded_b = load_from_sqlite(db_path, "bob")

        all_passed = True
        all_passed &= check("alice's registry updated to v2",
                            loaded_a.source_registry == {"doc_alice_v2": []})
        all_passed &= check("alice's v1 is gone",
                            "doc_alice_v1" not in loaded_a.source_registry)
        all_passed &= check("bob's registry unchanged after alice re-saved",
                            loaded_b.source_registry == {"doc_bob": []})
        return all_passed
    finally:
        os.unlink(db_path)


def test_unknown_user_returns_empty():
    print("\n--- Test 3: loading an unknown user_id returns empty ModifiedMemory ---")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        mem_a = ModifiedMemory()
        mem_a.source_registry = {"doc_alice": []}
        save_to_sqlite(db_path, "alice", mem_a)

        loaded_unknown = load_from_sqlite(db_path, "nobody")

        all_passed = True
        all_passed &= check("unknown user gets empty source_registry",
                            loaded_unknown.source_registry == {})
        all_passed &= check("alice's data not visible to unknown user",
                            "doc_alice" not in loaded_unknown.source_registry)
        return all_passed
    finally:
        os.unlink(db_path)


def test_conversation_memory_class():
    print("\n--- Test 4: ConversationMemory.save() and reload via load_from_sqlite ---")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        inner_a = ModifiedMemory()
        inner_a.source_registry = {"session_alice_1": ["t1"]}
        cm_a = ConversationMemory(inner_a, db_path, "alice")
        cm_a.save()

        inner_b = ModifiedMemory()
        inner_b.source_registry = {"session_bob_1": ["t2"]}
        cm_b = ConversationMemory(inner_b, db_path, "bob")
        cm_b.save()

        reloaded_a = load_from_sqlite(db_path, "alice")
        reloaded_b = load_from_sqlite(db_path, "bob")

        all_passed = True
        all_passed &= check("ConversationMemory.save() persists alice correctly",
                            reloaded_a.source_registry == {"session_alice_1": ["t1"]})
        all_passed &= check("ConversationMemory.save() persists bob correctly",
                            reloaded_b.source_registry == {"session_bob_1": ["t2"]})
        all_passed &= check("alice's _flush_count initialises to 0", cm_a._flush_count == 0)
        all_passed &= check("bob's _flush_count initialises to 0", cm_b._flush_count == 0)
        all_passed &= check("alice and bob have separate instances", cm_a is not cm_b)
        return all_passed
    finally:
        os.unlink(db_path)


def test_raw_schema():
    print("\n--- Test 5: database schema has user_id columns ---")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        mem = ModifiedMemory()
        save_to_sqlite(db_path, "test_user", mem)

        con = sqlite3.connect(db_path)
        cur = con.cursor()
        expected_tables = [
            "concepts", "reflections", "reflection_item_embeddings",
            "traj_sums", "trajectories", "persona_entries",
            "rubrics", "connections", "metadata",
        ]
        all_passed = True
        for table in expected_tables:
            cols = [row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()]
            has_user_id = "user_id" in cols
            all_passed &= check(f"  {table} has user_id column", has_user_id)
        con.close()
        return all_passed
    finally:
        os.unlink(db_path)


if __name__ == "__main__":
    results = []
    results.append(test_save_load_isolation())
    results.append(test_overwrite_only_own_user())
    results.append(test_unknown_user_returns_empty())
    results.append(test_conversation_memory_class())
    results.append(test_raw_schema())

    print()
    if all(results):
        print(f"\033[92mAll tests passed.\033[0m")
    else:
        failed = results.count(False)
        print(f"\033[91m{failed} test(s) failed.\033[0m")
        sys.exit(1)
