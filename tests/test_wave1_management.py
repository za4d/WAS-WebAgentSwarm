"""Wave 1 management-contract tests on WAS's real DurableStore.

Generic disposable-state tests. No browser, ChatGPT, network, third-party CLI,
provider account, repository mutation or persistent state is required.
Run from a checked-out fork with:
    PYTHONPATH=src python -m unittest discover -s tests -p test_wave1_management.py -v
Provenance: executable contract checks; not a live agent-team acceptance test.
"""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from web_agent_swarm.state import DurableStore


class Wave1ManagementContractTest(unittest.TestCase):
    def test_dependency_gate_survives_store_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = DurableStore(root)
            store.task_create("Decode input", task_id="decode", owner="implementer")
            store.task_create(
                "Build manifest",
                task_id="manifest",
                owner="implementer",
                dependencies=["decode"],
            )
            with self.assertRaisesRegex(RuntimeError, "dependencies"):
                store.task_claim("manifest", "implementer", lease_seconds=60)
            claimed = store.task_claim("decode", "implementer", lease_seconds=60)
            self.assertEqual(claimed["claimed_by"], "implementer")
            store.task_release("decode", "implementer", status="done", result={"bytes": 2048})
            reopened = DurableStore(root)
            tasks = {item["task_id"]: item for item in reopened.tasks()}
            self.assertEqual(tasks["decode"]["status"], "done")
            self.assertEqual(tasks["manifest"]["status"], "ready")
            downstream = reopened.task_claim("manifest", "implementer", lease_seconds=60)
            self.assertEqual(downstream["status"], "active")
            reopened.task_release("manifest", "implementer", status="done", result={"entries": 1})
            final = {item["task_id"]: item for item in DurableStore(root).tasks()}
            self.assertEqual(final["decode"]["result"], {"bytes": 2048})
            self.assertEqual(final["manifest"]["result"], {"entries": 1})

    def test_competing_writers_block_but_disjoint_lane_progresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = DurableStore(root)
            for task_id, resource in [
                ("writer_a", "src/shared.py"),
                ("writer_b", "src/shared.py"),
                ("writer_independent", "src/other.py"),
            ]:
                store.task_create(
                    "Write " + task_id,
                    task_id=task_id,
                    required_capabilities=["implementation"],
                    write_resources=[resource],
                    canonical_root=str(root),
                )
            store.task_claim("writer_a", "implementer", lease_seconds=60)
            independent = store.task_claim(
                "writer_independent", "implementer-3", lease_seconds=60
            )
            self.assertEqual(independent["claimed_by"], "implementer-3")
            with self.assertRaises(RuntimeError):
                store.task_claim("writer_b", "implementer-2", lease_seconds=60)
            reopened = DurableStore(root)
            active = [
                r for r in reopened.load()["task_resource_reservations"].values()
                if r.get("status") == "active"
            ]
            self.assertEqual(len(active), 2)
            reopened.task_release("writer_a", "implementer", status="done")
            allowed = reopened.task_claim("writer_b", "implementer-2", lease_seconds=60)
            self.assertEqual(allowed["claimed_by"], "implementer-2")
            self.assertEqual(
                next(x for x in reopened.tasks() if x["task_id"] == "writer_independent")["status"],
                "active",
            )

    def test_retry_lease_continuity_and_no_duplicate_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = DurableStore(root)
            store.task_create("Review fixture", task_id="review_job", max_attempts=2)
            first = store.task_claim("review_job", "reviewer", lease_seconds=60)
            self.assertEqual(first["attempts"], 1)
            repeat = store.task_claim("review_job", "reviewer", lease_seconds=60)
            self.assertEqual(repeat["attempts"], 1)
            with self.assertRaises(RuntimeError):
                store.task_claim("review_job", "reviewer-2", lease_seconds=60)
            result = store.task_release(
                "review_job", "reviewer", status="failed",
                retryable=True, error="simulated pre-effect failure"
            )
            self.assertEqual(result["status"], "ready")
            reopened = DurableStore(root)
            second = reopened.task_claim("review_job", "reviewer-2", lease_seconds=60)
            self.assertEqual(second["attempts"], 2)
            reopened.task_release("review_job", "reviewer-2", status="done")
            with self.assertRaises(RuntimeError):
                reopened.task_claim("review_job", "reviewer", lease_seconds=60)

    def test_simultaneous_claim_for_same_write_resource_has_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = DurableStore(root)
            for task_id in ("race_a", "race_b"):
                seed.task_create(
                    "Race " + task_id, task_id=task_id,
                    required_capabilities=["implementation"],
                    write_resources=["src/race.py"], canonical_root=str(root)
                )
            barrier = Barrier(2)

            def claim(task_id, worker):
                candidate = DurableStore(root)
                barrier.wait(timeout=5)
                try:
                    response = candidate.task_claim(task_id, worker, lease_seconds=60)
                    return ("claimed", response["task_id"], worker)
                except RuntimeError:
                    return ("blocked", task_id, worker)

            with ThreadPoolExecutor(max_workers=2) as executor:
                a = executor.submit(claim, "race_a", "implementer")
                b = executor.submit(claim, "race_b", "implementer-2")
                results = [a.result(timeout=15), b.result(timeout=15)]
            self.assertCountEqual([item[0] for item in results], ["claimed", "blocked"])
            winners = [x for x in results if x[0] == "claimed"]
            active = [
                r for r in DurableStore(root).load()["task_resource_reservations"].values()
                if r.get("status") == "active"
            ]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["task_id"], winners[0][1])


if __name__ == "__main__":
    unittest.main()
