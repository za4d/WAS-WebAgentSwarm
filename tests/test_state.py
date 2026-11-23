import tempfile
import unittest
from pathlib import Path

from web_agent_swarm.state import DurableStore


class DurableStoreTest(unittest.TestCase):
    def test_task_and_status_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            task = store.task_create("Build feature", owner="implementer")
            updated = store.task_update(task["task_id"], status="active", summary="started")
            self.assertEqual(updated["status"], "active")
            self.assertEqual(store.tasks()[0]["summary"], "started")

            store.status_set("build", "running")
            self.assertEqual(store.status()["build"]["value"], "running")

    def test_task_dependencies_are_durable_and_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            first = store.task_create(
                "Research",
                owner="researcher",
                task_id="research",
            )
            second = store.task_create(
                "Implement",
                owner="implementer",
                task_id="implement",
                dependencies=[first["task_id"]],
            )
            self.assertEqual(second["dependencies"], ["research"])
            self.assertEqual(
                next(item for item in store.tasks() if item["task_id"] == "implement")[
                    "dependencies"
                ],
                ["research"],
            )
            with self.assertRaises(ValueError):
                store.task_create(
                    "Invalid",
                    task_id="invalid",
                    dependencies=["missing"],
                )

    def test_capability_claim_and_resource_intent_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            verify = store.task_create(
                "Verify candidate",
                task_id="verify-cap",
                preferred_role="tester",
                required_capabilities=["verification"],
            )
            with self.assertRaises(RuntimeError):
                store.task_claim(verify["task_id"], "implementer", lease_seconds=60)
            claimed_verify = store.task_claim(
                verify["task_id"],
                "tester",
                lease_seconds=60,
            )
            self.assertEqual(claimed_verify["claimed_by"], "tester")
            store.task_release(verify["task_id"], "tester", status="done")

            first = store.task_create(
                "Writer one",
                task_id="writer-one",
                required_capabilities=["implementation"],
                write_resources=["src/shared.py"],
                canonical_root=tmp,
            )
            second = store.task_create(
                "Writer two",
                task_id="writer-two",
                required_capabilities=["implementation"],
                write_resources=["src/shared.py"],
                canonical_root=tmp,
            )
            store.task_claim(first["task_id"], "implementer", lease_seconds=60)
            active = [
                item
                for item in store.load()["task_resource_reservations"].values()
                if item.get("status") == "active"
            ]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["resource"], "src/shared.py")
            with self.assertRaises(RuntimeError):
                store.task_claim(second["task_id"], "implementer-2", lease_seconds=60)

            store.task_release(first["task_id"], "implementer", status="done")
            self.assertFalse(any(
                item.get("status") == "active"
                and item.get("task_id") == first["task_id"]
                for item in store.load()["task_resource_reservations"].values()
            ))
            reclaimed = store.task_claim(
                second["task_id"],
                "implementer-2",
                lease_seconds=60,
            )
            self.assertEqual(reclaimed["claimed_by"], "implementer-2")

    def test_claim_lease_retry_and_reduce(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            fanout = store.task_fanout(
                items=[{"value": 1}, {"value": 2}, {"value": 3}],
                owner="implementer",
                title_prefix="Map",
                group_id="map-group",
                max_attempts=2,
            )
            self.assertEqual(len(fanout["task_ids"]), 3)

            first = fanout["task_ids"][0]
            claimed = store.task_claim(first, "implementer", lease_seconds=60)
            self.assertEqual(claimed["claimed_by"], "implementer")
            self.assertEqual(claimed["attempts"], 1)
            with self.assertRaises(RuntimeError):
                store.task_claim(first, "implementer-2", lease_seconds=60)

            retried = store.task_release(
                first,
                "implementer",
                status="failed",
                error="transient",
                retryable=True,
            )
            self.assertEqual(retried["status"], "ready")
            reclaimed = store.task_claim(first, "implementer-2", lease_seconds=60)
            self.assertEqual(reclaimed["attempts"], 2)
            store.task_release(
                first,
                "implementer-2",
                status="done",
                result={"mapped": 10},
            )

            for index, task_id in enumerate(fanout["task_ids"][1:], start=2):
                worker = "implementer-{0}".format(index + 1)
                store.task_claim(task_id, worker, lease_seconds=60)
                store.task_release(
                    task_id,
                    worker,
                    status="done",
                    result={"mapped": index * 10},
                )

            reduced = store.task_reduce("map-group")
            self.assertTrue(reduced["complete"])
            self.assertFalse(reduced["failed"])
            self.assertEqual(
                reduced["results"],
                [{"mapped": 10}, {"mapped": 20}, {"mapped": 30}],
            )

    def test_task_step_fingerprint_tracks_inputs_and_mvcc_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            store.task_create(
                "Fingerprint task",
                task_id="fingerprint",
                payload={"mode": "strict"},
                canonical_root=tmp,
                resource_hints=["src/a.py"],
            )
            first = store.task_step_fingerprint(
                "fingerprint",
                "regression",
                inputs={"command": "pytest"},
            )
            again = store.task_step_fingerprint(
                "fingerprint",
                "regression",
                inputs={"command": "pytest"},
            )
            self.assertEqual(first["input_fingerprint"], again["input_fingerprint"])

            changed_input = store.task_step_fingerprint(
                "fingerprint",
                "regression",
                inputs={"command": "pytest -q"},
            )
            self.assertNotEqual(
                first["input_fingerprint"],
                changed_input["input_fingerprint"],
            )

            canonical_id = first["basis"]["canonical_id"]
            data = store.load()
            data["canonical_epochs"][canonical_id] = 1
            data["resource_versions"][canonical_id + "::src/a.py"] = {
                "resource": "src/a.py",
                "canonical_id": canonical_id,
                "version": 1,
                "sha256": "deadbeef",
                "exists": True,
            }
            store.save(data)
            changed_source = store.task_step_fingerprint(
                "fingerprint",
                "regression",
                inputs={"command": "pytest"},
            )
            self.assertNotEqual(
                first["input_fingerprint"],
                changed_source["input_fingerprint"],
            )
            self.assertEqual(changed_source["basis"]["canonical_epoch"], 1)
            self.assertEqual(
                changed_source["basis"]["resources"][0]["sha256"],
                "deadbeef",
            )

    def test_task_step_cache_reuses_results_and_serializes_same_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            store.task_create("Expensive task", task_id="expensive")

            first = store.task_step_acquire(
                "expensive",
                "full-regression",
                "implementer",
                input_fingerprint="sha256:abc",
                lease_seconds=60,
            )
            self.assertEqual(first["action"], "execute")
            self.assertEqual(first["record"]["attempts"], 1)

            waiting = store.task_step_acquire(
                "expensive",
                "full-regression",
                "tester",
                input_fingerprint="sha256:abc",
                lease_seconds=60,
            )
            self.assertEqual(waiting["action"], "wait")
            self.assertEqual(waiting["record"]["claimed_by"], "implementer")

            beat = store.task_step_heartbeat(
                "expensive",
                "full-regression",
                "implementer",
                lease_seconds=120,
            )
            self.assertGreater(beat["lease_expires_at"], first["record"]["lease_expires_at"])

            done = store.task_step_complete(
                "expensive",
                "full-regression",
                "implementer",
                status="done",
                result={"passed": 71},
                summary="71/71",
            )
            self.assertEqual(done["status"], "done")

            reused = store.task_step_acquire(
                "expensive",
                "full-regression",
                "tester",
                input_fingerprint="sha256:abc",
            )
            self.assertEqual(reused["action"], "reuse")
            self.assertTrue(reused["reusable"])
            self.assertEqual(reused["record"]["result"], {"passed": 71})

            changed = store.task_step_acquire(
                "expensive",
                "full-regression",
                "tester",
                input_fingerprint="sha256:def",
            )
            self.assertEqual(changed["action"], "execute")
            self.assertEqual(changed["record"]["generation"], 2)
            store.task_step_complete(
                "expensive",
                "full-regression",
                "tester",
                status="failed",
                error="new input failed",
            )
            retry = store.task_step_acquire(
                "expensive",
                "full-regression",
                "reviewer",
                input_fingerprint="sha256:def",
            )
            self.assertEqual(retry["action"], "execute")
            self.assertEqual(retry["record"]["attempts"], 2)

    def test_task_steps_are_checkpointed_and_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            store.task_create("Checkpoint steps", task_id="step-cp")
            store.task_step_acquire(
                "step-cp", "download", "researcher", input_fingerprint="v1"
            )
            store.task_step_complete(
                "step-cp", "download", "researcher",
                result={"artifact": "v1.bin"},
            )
            checkpoint = store.checkpoint_create(label="with step result")

            store.task_step_acquire(
                "step-cp", "download", "researcher", input_fingerprint="v2"
            )
            store.task_step_complete(
                "step-cp", "download", "researcher",
                result={"artifact": "v2.bin"},
            )
            store.checkpoint_restore(checkpoint["checkpoint_id"])
            reused = store.task_step_acquire(
                "step-cp", "download", "tester", input_fingerprint="v1"
            )
            self.assertEqual(reused["action"], "reuse")
            self.assertEqual(reused["record"]["result"], {"artifact": "v1.bin"})


    def test_durable_interrupt_resume_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            task = store.task_create("Need approval", owner="reviewer", task_id="approval")

            interrupt = store.interrupt_create(
                "Approve publishing?",
                task_id=task["task_id"],
                payload={"change": "publish"},
                interrupt_id="approval-int",
            )
            self.assertEqual(interrupt["status"], "open")
            blocked = next(item for item in store.tasks() if item["task_id"] == "approval")
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["interrupt_id"], "approval-int")
            self.assertTrue(blocked["summary"].startswith("human_input:"))
            self.assertEqual(len(store.interrupts(status="open")), 1)

            resumed = store.interrupt_resume("approval-int", value={"approved": True})
            self.assertEqual(resumed["status"], "resumed")
            self.assertEqual(resumed["resume_value"], {"approved": True})
            ready = next(item for item in store.tasks() if item["task_id"] == "approval")
            self.assertEqual(ready["status"], "ready")
            self.assertEqual(ready["interrupt_id"], "")

    def test_checkpoint_restore_is_safe_and_restores_coordination_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            task = store.task_create("Checkpoint me", task_id="cp-task")
            store.status_set("phase", "before")
            checkpoint = store.checkpoint_create(
                label="before mutation",
                metadata={"reason": "test"},
            )

            store.task_claim(task["task_id"], "implementer", lease_seconds=60)
            with self.assertRaises(RuntimeError):
                store.checkpoint_restore(checkpoint["checkpoint_id"])

            store.task_release(
                task["task_id"],
                "implementer",
                status="done",
                result={"changed": True},
            )
            store.status_set("phase", "after")

            restored = store.checkpoint_restore(checkpoint["checkpoint_id"])
            self.assertTrue(restored["restored"])
            restored_task = next(
                item for item in store.tasks() if item["task_id"] == "cp-task"
            )
            self.assertEqual(restored_task["status"], "ready")
            self.assertEqual(restored_task["result"], None)
            self.assertEqual(store.status()["phase"]["value"], "before")
            self.assertEqual(
                store.checkpoints()[0]["checkpoint_id"],
                checkpoint["checkpoint_id"],
            )

    def test_checkpoint_lineage_survives_restore_and_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            first = store.checkpoint_create(label="first")
            second = store.checkpoint_create(label="second")
            third = store.checkpoint_create(label="third")

            self.assertEqual(second["parent_checkpoint_id"], first["checkpoint_id"])
            self.assertEqual(third["parent_checkpoint_id"], second["checkpoint_id"])

            lineage = store.checkpoint_lineage()
            self.assertEqual(
                [item["checkpoint_id"] for item in lineage["lineage"]],
                [
                    third["checkpoint_id"],
                    second["checkpoint_id"],
                    first["checkpoint_id"],
                ],
            )
            self.assertFalse(lineage["truncated"])

            store.checkpoint_restore(first["checkpoint_id"])
            branch = store.checkpoint_create(label="branch-from-first")
            self.assertEqual(branch["parent_checkpoint_id"], first["checkpoint_id"])

            branch_lineage = store.checkpoint_lineage(branch["checkpoint_id"])
            self.assertEqual(
                [item["checkpoint_id"] for item in branch_lineage["lineage"]],
                [branch["checkpoint_id"], first["checkpoint_id"]],
            )
            listed = {
                item["checkpoint_id"]: item
                for item in store.checkpoints()
            }
            self.assertTrue(listed[branch["checkpoint_id"]]["is_head"])
            self.assertFalse(listed[third["checkpoint_id"]]["is_head"])

    def test_checkpoint_restore_invalidates_restored_live_superstep(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            task = store.task_create("Round work", task_id="round-work")
            store.superstep_begin(["implementer"], step_id="checkpoint-step")
            store.task_claim(
                task["task_id"],
                "implementer",
                lease_seconds=60,
                superstep_id="checkpoint-step",
            )
            checkpoint = store.checkpoint_create(label="inside round")

            store.superstep_abort("checkpoint-step", reason="finish current timeline")
            store.task_release(
                task["task_id"],
                "implementer",
                status="done",
                result={"timeline": "new"},
            )

            restored = store.checkpoint_restore(checkpoint["checkpoint_id"])
            self.assertEqual(restored["recovered_supersteps"], ["checkpoint-step"])
            restored_step = store.superstep_get("checkpoint-step")
            self.assertEqual(restored_step["status"], "aborted")
            self.assertIn("checkpoint restore", restored_step["error"])

            restored_task = next(
                item for item in store.tasks() if item["task_id"] == "round-work"
            )
            self.assertEqual(restored_task["status"], "active")
            self.assertEqual(restored_task["superstep_id"], "")
            self.assertEqual(restored_task["lease_expires_at"], 0.0)
            reclaimed = store.task_claim(
                "round-work",
                "implementer-2",
                lease_seconds=60,
            )
            self.assertEqual(reclaimed["claimed_by"], "implementer-2")

    def test_superstep_pending_writes_commit_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            left = store.task_create("Left", owner="implementer", task_id="left")
            right = store.task_create("Right", owner="tester", task_id="right")
            step = store.superstep_begin(
                ["implementer", "tester"],
                step_id="round-1",
                metadata={"purpose": "atomic round"},
            )
            self.assertEqual(step["status"], "open")
            store.task_claim(
                left["task_id"],
                "implementer",
                lease_seconds=60,
                superstep_id="round-1",
            )
            store.task_claim(
                right["task_id"],
                "tester",
                lease_seconds=60,
                superstep_id="round-1",
            )
            with self.assertRaises(RuntimeError):
                store.task_release(
                    left["task_id"],
                    "implementer",
                    status="done",
                    result={"forbidden": True},
                )

            staged_left = store.superstep_stage(
                "round-1",
                "implementer",
                [
                    {
                        "op": "task_release",
                        "task_id": "left",
                        "status": "done",
                        "summary": "left complete",
                        "result": {"value": 1},
                    },
                    {"op": "status_set", "key": "left_output", "value": 1},
                ],
            )
            self.assertFalse(staged_left["ready_to_commit"])
            tasks_before = {item["task_id"]: item for item in store.tasks()}
            self.assertEqual(tasks_before["left"]["status"], "active")
            self.assertEqual(tasks_before["left"]["claimed_by"], "implementer")
            self.assertNotIn("left_output", store.status())

            staged_right = store.superstep_stage(
                "round-1",
                "tester",
                [
                    {
                        "op": "task_release",
                        "task_id": "right",
                        "status": "done",
                        "summary": "right complete",
                        "result": {"value": 2},
                    },
                    {"op": "status_set", "key": "right_output", "value": 2},
                ],
            )
            self.assertTrue(staged_right["ready_to_commit"])
            tasks_ready = {item["task_id"]: item for item in store.tasks()}
            self.assertEqual(tasks_ready["left"]["status"], "active")
            self.assertEqual(tasks_ready["right"]["status"], "active")
            self.assertEqual(store.status(), {})

            committed = store.superstep_commit("round-1")
            self.assertEqual(committed["status"], "committed")
            self.assertEqual(committed["commit_summary"]["participant_count"], 2)
            self.assertEqual(committed["commit_summary"]["write_count"], 4)
            tasks_after = {item["task_id"]: item for item in store.tasks()}
            self.assertEqual(tasks_after["left"]["status"], "done")
            self.assertEqual(tasks_after["right"]["status"], "done")
            self.assertEqual(tasks_after["left"]["claimed_by"], "")
            self.assertEqual(tasks_after["right"]["claimed_by"], "")
            self.assertEqual(store.status()["left_output"]["value"], 1)
            self.assertEqual(store.status()["right_output"]["value"], 2)

    def test_superstep_failure_keeps_pending_writes_uncommitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            first = store.task_create("First", task_id="first")
            second = store.task_create("Second", task_id="second")
            store.superstep_begin(["worker-a", "worker-b"], step_id="round-fail")
            store.task_claim(
                first["task_id"],
                "worker-a",
                lease_seconds=60,
                superstep_id="round-fail",
            )
            store.task_claim(
                second["task_id"],
                "worker-b",
                lease_seconds=60,
                superstep_id="round-fail",
            )
            store.superstep_stage(
                "round-fail",
                "worker-a",
                [
                    {
                        "op": "task_release",
                        "task_id": "first",
                        "status": "done",
                        "result": "should-not-publish",
                    },
                    {"op": "status_set", "key": "partial", "value": True},
                ],
            )

            aborted = store.superstep_fail(
                "round-fail",
                "worker-b",
                "worker crashed",
            )
            self.assertEqual(aborted["status"], "aborted")
            tasks = {item["task_id"]: item for item in store.tasks()}
            self.assertEqual(tasks["first"]["status"], "active")
            self.assertEqual(tasks["first"]["result"], None)
            self.assertEqual(tasks["second"]["status"], "active")
            self.assertNotIn("partial", store.status())
            diagnostic = store.superstep_get("round-fail", include_pending=True)
            self.assertIn("worker-a", diagnostic["pending_writes"])
            with self.assertRaises(RuntimeError):
                store.superstep_commit("round-fail")

    def test_superstep_conflicting_writes_abort_without_partial_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            store.superstep_begin(["worker-a", "worker-b"], step_id="round-conflict")
            store.superstep_stage(
                "round-conflict",
                "worker-a",
                [{"op": "status_set", "key": "shared", "value": "a"}],
            )
            store.superstep_stage(
                "round-conflict",
                "worker-b",
                [{"op": "status_set", "key": "shared", "value": "b"}],
            )

            result = store.superstep_commit("round-conflict")
            self.assertEqual(result["status"], "conflict")
            self.assertIn("conflicting pending writes", result["error"])
            self.assertNotIn("shared", store.status())
    def test_cr01_di01_rejects_reassignment_and_regressive_uncertain_receipt(self):
        # Reviewer S follow-up DI-01: the same R8 command must never have
        # its durable target, recipient-name, uncertainty or evidence replaced.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = DurableStore(root)
            command = "r8_" + "f" * 32
            store.send_transaction_record(
                command, name="reviewer", tab_id=73, status="attempting",
                relay_attempt_id="f" * 32, relay_recipient="R",
                relay_generation=1, body_sha256="a" * 64,
                expected_url="https://chatgpt.com/c/rrrr3333",
            )
            store.send_transaction_record(
                command, status="unknown", result={"error": "lost callback"},
                may_have_executed=True,
            )
            store = DurableStore(root)  # actual on-disk reload
            before = store.send_transaction_get(command)
            for changes in (
                {"name": "another-worker", "status": "unknown"},
                {"tab_id": 74, "status": "unknown"},
                {"status": "failed"},
                {"status": "attempting"},
                {"status": "busy"},
                {"status": "ok"},
                {"status": "unknown", "result": {"ok": True}},
            ):
                with self.subTest(changes=changes):
                    with self.assertRaisesRegex(ValueError, "immutable|regressive|terminal"):
                        store.send_transaction_record(command, **changes)
                    self.assertEqual(store.send_transaction_get(command), before)
            # A no-op repeat with omitted identity/status evidence stays
            # legal, preserving an existing uncertain receipt byte-for-byte.
            same = store.send_transaction_record(command, status="unknown")
            for field in ("name", "tab_id", "status", "result", "may_have_executed",
                          "relay_attempt_id", "body_sha256"):
                self.assertEqual(same[field], before[field])

    def test_cr01_di01_rejects_terminal_success_rewritten_to_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            command = "r8_" + "e" * 32
            store.send_transaction_record(
                command, name="reviewer", tab_id=73, status="attempting",
                relay_attempt_id="e" * 32, relay_recipient="R",
                relay_generation=1, body_sha256="b" * 64,
                expected_url="https://chatgpt.com/c/rrrr3333",
            )
            store.send_transaction_record(
                command, status="ok", result={"ok": True},
                may_have_executed=True,
            )
            with self.assertRaisesRegex(ValueError, "immutable|regressive|terminal"):
                store.send_transaction_record(
                    command, status="failed", result={"ok": False},
                )
            self.assertEqual(store.send_transaction_get(command)["status"], "ok")

    def test_relay_attempt_receipt_is_immutable_and_survives_diagnostic_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            aid = "f" * 32
            cid = "r8_" + aid
            kwargs = {
                "relay_attempt_id": aid, "relay_recipient": "R",
                "relay_generation": 1,
                "body_sha256": "a" * 64,
                "expected_url": "https://chatgpt.com/c/rrrr3333",
            }
            first = store.send_transaction_record(
                cid, name="reviewer", tab_id=73, status="attempting", **kwargs)
            self.assertEqual(first["relay_attempt_id"], aid)
            final = store.send_transaction_record(
                cid, status="unknown", may_have_executed=True)
            self.assertEqual(final["relay_attempt_id"], aid)
            self.assertTrue(final["may_have_executed"])
            with self.assertRaisesRegex(ValueError, "immutable R8"):
                store.send_transaction_record(
                    cid, status="ok", relay_attempt_id="e" * 32)
            self.assertEqual(store.send_transaction_get(cid)["status"], "unknown")
            for n in range(520):
                store.send_transaction_record(
                    "standalone-" + str(n), status="failed")
            reloaded = DurableStore(Path(tmp))
            record = reloaded.send_transaction_get(cid)
            self.assertIsNotNone(record)
            self.assertEqual(record["relay_recipient"], "R")
            self.assertTrue(record["may_have_executed"])
            self.assertEqual(record["body_sha256"], "a" * 64)

    def test_state_schema_migration_corruption_recovery_and_send_ledger(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = DurableStore(root)
            legacy = {"schema_version": 11, "bindings": {}, "tasks": {}}
            store.path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded = store.load()
            self.assertEqual(loaded["schema_version"], 13)
            self.assertEqual(loaded["send_transactions"], {})
            self.assertEqual(loaded["retired_conversations"], [])

            tx = store.send_transaction_record("cmd-1", name="main", tab_id=7, status="attempting")
            self.assertEqual(tx["status"], "attempting")
            final = store.send_transaction_record(
                "cmd-1", name="main", tab_id=7, status="ack_timeout",
                result={"ok": False}, may_have_executed=True,
            )
            self.assertTrue(final["may_have_executed"])
            self.assertEqual(store.send_transaction_get("cmd-1")["status"], "ack_timeout")

            store.save(store.load())
            store.path.write_text("{broken", encoding="utf-8")
            recovered = store.load()
            self.assertEqual(recovered["schema_version"], 13)
            self.assertTrue(any(root.glob("state.corrupt.*.json")))


if __name__ == "__main__":
    unittest.main()
