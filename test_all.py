"""
Full test runner for GLKB memory changes.

Usage:
    python test_all.py                      # SQLite unit tests only (no external deps)
    python test_all.py --service            # + session management tests (service must be running)
    python test_all.py --service --chat     # + chat + memory isolation (needs Neo4j + LLM)

Start the service first for --service / --chat:
    uvicorn service.api:app --host 0.0.0.0 --port 5001 --reload
"""

import sys
import os
import json
import sqlite3
import subprocess
import urllib.request
import urllib.error
import time

PORT = 5001
BASE_URL = f"http://localhost:{PORT}"
CHAT_TIMEOUT = 120   # seconds — LLM + Neo4j calls can be slow

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"
INFO = "\033[94mINFO\033[0m"


def check(label: str, condition: bool) -> bool:
    print(f"  {'✓' if condition else '✗'}  {label}: {PASS if condition else FAIL}")
    return condition


def info(msg: str):
    print(f"  ℹ  {msg}")


# =============================================================================
# HTTP helper
# =============================================================================

def _http(method: str, path: str, body: dict = None, timeout: int = 10):
    url = BASE_URL + path
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()), e.code
        except Exception:
            return {"error": str(e)}, e.code
    except urllib.error.URLError as e:
        return None, 0


def service_is_running() -> bool:
    _, status = _http("GET", "/health")
    return status == 200


# =============================================================================
# Step 1: SQLite isolation unit tests
# =============================================================================

def run_unit_tests() -> bool:
    print("\n" + "=" * 60)
    print("STEP 1: SQLite isolation unit tests")
    print("=" * 60)
    result = subprocess.run(
        [sys.executable, "test_memory_isolation.py"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    return result.returncode == 0


# =============================================================================
# Step 2: Service session tests
# =============================================================================

def run_service_tests() -> tuple:
    """Returns (passed, alice_session_id, bob_session_id) for reuse in chat tests."""
    print("\n" + "=" * 60)
    print("STEP 2: Session management tests")
    print("=" * 60)

    if not service_is_running():
        print(f"  {SKIP}  Service not running on port {PORT}.")
        print("         Start it with: uvicorn service.api:app --host 0.0.0.0 --port 5001 --reload")
        return True, None, None

    all_passed = True

    print("\n  [Session creation]")
    alice_resp, status = _http("POST", "/apps/glkb/users/alice_test/sessions")
    all_passed &= check("alice session created", status in (200, 201))
    alice_session_id = (alice_resp or {}).get("id")

    bob_resp, status = _http("POST", "/apps/glkb/users/bob_test/sessions")
    all_passed &= check("bob session created", status in (200, 201))
    bob_session_id = (bob_resp or {}).get("id")

    all_passed &= check("alice and bob have different session IDs",
                        bool(alice_session_id and bob_session_id and
                             alice_session_id != bob_session_id))

    print("\n  [Session list isolation]")
    alice_sessions, _ = _http("GET", "/apps/glkb/users/alice_test/sessions")
    bob_sessions,   _ = _http("GET", "/apps/glkb/users/bob_test/sessions")

    alice_ids = {s["id"] for s in (alice_sessions or {}).get("sessions", [])}
    bob_ids   = {s["id"] for s in (bob_sessions   or {}).get("sessions", [])}

    all_passed &= check("alice sees her own session",      alice_session_id in alice_ids)
    all_passed &= check("alice cannot see bob's session",  bob_session_id   not in alice_ids)
    all_passed &= check("bob cannot see alice's session",  alice_session_id not in bob_ids)

    return all_passed, alice_session_id, bob_session_id


# =============================================================================
# Step 3: Chat + memory isolation tests
# =============================================================================

def _chat(user_id: str, session_id: str, message: str) -> str | None:
    """Send one message and return the agent's response text, or None on error."""
    info(f"  [{user_id}] → {message[:80]}")
    resp, status = _http(
        "POST",
        f"/apps/glkb/users/{user_id}/sessions/{session_id}/chat",
        body={"message": message},
        timeout=CHAT_TIMEOUT,
    )
    if status != 200 or resp is None:
        print(f"    {FAIL} (HTTP {status}: {resp})")
        return None
    response = resp.get("response", "")
    info(f"  [{user_id}] ← {response[:120]}{'...' if len(response) > 120 else ''}")
    return response


def _db_users() -> list:
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_logs", "layermem.db")
    if not os.path.exists(db_path):
        return []
    con = sqlite3.connect(db_path)
    users = [row[0] for row in con.execute("SELECT DISTINCT user_id FROM metadata")]
    con.close()
    return users


def _db_trajectories(user_id: str) -> list:
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_logs", "layermem.db")
    if not os.path.exists(db_path):
        return []
    con = sqlite3.connect(db_path)
    rows = [row[0] for row in con.execute(
        "SELECT chunk_text FROM trajectories WHERE user_id=?", (user_id,)
    )]
    con.close()
    return rows


def run_chat_tests(alice_session_id: str, bob_session_id: str) -> bool:
    print("\n" + "=" * 60)
    print("STEP 3: Chat + memory isolation tests  (needs Neo4j + LLM)")
    print("=" * 60)

    if not alice_session_id or not bob_session_id:
        print(f"  {SKIP}  No sessions available — run --service first.")
        return True

    all_passed = True

    # --- Alice asks about TP53 ---
    print("\n  [Alice: ask about TP53]")
    r = _chat("alice_test", alice_session_id, "What is TP53 and what diseases is it associated with?")
    all_passed &= check("alice got a response", bool(r))

    # --- Bob asks about BRCA1 ---
    print("\n  [Bob: ask about BRCA1]")
    r = _chat("bob_test", bob_session_id, "What is BRCA1 and what diseases is it associated with?")
    all_passed &= check("bob got a response", bool(r))

    # --- Ask both to save memory ---
    print("\n  [Saving memory]")
    _chat("alice_test", alice_session_id, "Please save our conversation to memory now.")
    _chat("bob_test",   bob_session_id,   "Please save our conversation to memory now.")

    # --- Verify both users in DB ---
    print("\n  [DB isolation check]")
    db_users = _db_users()
    info(f"users in layermem.db: {db_users}")
    all_passed &= check("alice_test in DB", "alice_test" in db_users)
    all_passed &= check("bob_test in DB",   "bob_test"   in db_users)

    alice_trajs = _db_trajectories("alice_test")
    bob_trajs   = _db_trajectories("bob_test")
    info(f"alice trajectories: {len(alice_trajs)}, bob trajectories: {len(bob_trajs)}")

    alice_text = " ".join(alice_trajs).lower()
    bob_text   = " ".join(bob_trajs).lower()

    all_passed &= check("alice's trajectories mention TP53",   "tp53"  in alice_text)
    all_passed &= check("alice's trajectories don't mention BRCA1", "brca1" not in alice_text)
    all_passed &= check("bob's trajectories mention BRCA1",    "brca1" in bob_text)
    all_passed &= check("bob's trajectories don't mention TP53",    "tp53"  not in bob_text)

    return all_passed


# =============================================================================
# Cleanup
# =============================================================================

def cleanup(alice_session_id: str, bob_session_id: str):
    print("\n  [Cleanup]")
    for user_id, session_id in [("alice_test", alice_session_id), ("bob_test", bob_session_id)]:
        if not session_id:
            continue
        try:
            _, status = _http("DELETE", f"/apps/glkb/users/{user_id}/sessions/{session_id}", timeout=30)
            check(f"{user_id} session deleted", status == 200)
        except Exception as e:
            print(f"  ℹ  {user_id} session delete timed out or failed ({e}) — continuing")

    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_logs", "layermem.db")
    if os.path.exists(db_path):
        con = sqlite3.connect(db_path)
        tables = ["concepts", "reflections", "reflection_item_embeddings",
                  "traj_sums", "trajectories", "persona_entries",
                  "rubrics", "connections", "metadata"]
        for table in tables:
            con.execute(f"DELETE FROM {table} WHERE user_id IN ('alice_test', 'bob_test')")
        con.commit()
        con.close()
        check("test users removed from layermem.db", True)


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    run_service = "--service" in sys.argv or "--chat" in sys.argv
    run_chat    = "--chat"    in sys.argv

    results = []
    alice_session_id = bob_session_id = None

    results.append(run_unit_tests())

    if run_service:
        passed, alice_session_id, bob_session_id = run_service_tests()
        results.append(passed)

    if run_chat:
        results.append(run_chat_tests(alice_session_id, bob_session_id))

    if run_service or run_chat:
        cleanup(alice_session_id, bob_session_id)

    print("\n" + "=" * 60)
    if all(results):
        print(f"\033[92mAll tests passed.\033[0m")
    else:
        print(f"\033[91mSome tests failed.\033[0m")
        sys.exit(1)
