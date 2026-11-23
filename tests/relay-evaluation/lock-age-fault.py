"""Deterministic negative probe for an aged but LIVE WAS writer lock.

Runs against actual web_agent_swarm.state.DurableStore in an isolated temporary
directory. Does not start ChatGPT, Chrome, a daemon, an MCP server or a CLI.
Exit 0: the contender is blocked; exit 1: unsafe concurrent lock or harness issue.
"""
import json
import os
import platform
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from web_agent_swarm.state import DurableStore  # noqa: E402


def main():
    outcome = {"test": "WAS_LOCK_AGE_01", "environment": platform.system(),
               "provenance": "OFFLINE REAL DURABLESTORE / TEMPORARY FILES",
               "fault": "mtime aged by >30 seconds while the first writer lock remains held"}
    with tempfile.TemporaryDirectory() as dirname:
        root = Path(dirname)
        first = DurableStore(root)
        with first._write_lock(timeout=1.0):
            aged = time.time() - 40.0
            os.utime(first.lock_path, (aged, aged))
            outcome["lock_aged"] = time.time() - first.lock_path.stat().st_mtime > 30.0

            def contend():
                try:
                    with DurableStore(root)._write_lock(timeout=0.3):
                        return "UNSAFE_SECOND_WRITER_ENTERED"
                except TimeoutError:
                    return "SECOND_WRITER_BLOCKED"
                except OSError as error:
                    return "OS_ERROR_" + type(error).__name__

            with ThreadPoolExecutor(max_workers=1) as executor:
                observed = executor.submit(contend).result(timeout=3)
            outcome["contender_result"] = observed
            outcome["original_writer_still_holding"] = True
            outcome["safe"] = outcome["lock_aged"] and observed == "SECOND_WRITER_BLOCKED"
    outcome["verdict"] = "PASS" if outcome["safe"] else (
        "FAIL" if observed == "UNSAFE_SECOND_WRITER_ENTERED" else "INCONCLUSIVE"
    )
    print(json.dumps(outcome, indent=2))
    return 0 if outcome["safe"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({
            "test": "WAS_LOCK_AGE_01", "verdict": "HARNESS_ERROR",
            "error_class": type(error).__name__, "message": str(error)[:200]
        }))
        sys.exit(1)
