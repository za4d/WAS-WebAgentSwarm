import tempfile
import time
import unittest
from pathlib import Path

from web_agent_swarm.state import DurableStore
from web_agent_swarm.swarm import SwarmRuntime, conversation_id


class FakeBridge:
    def __init__(self):
        self.tabs_data = []
        self.next_id = 10
        self.sent = []

    def health(self):
        return {"ok": True, "extensionClients": 1}

    def tabs(self):
        return {"ok": True, "tabs": list(self.tabs_data)}

    def open_tab(self, url="https://chatgpt.com/", active=False):
        self.next_id += 1
        tab = {
            "id": self.next_id,
            "windowId": 1,
            "index": len(self.tabs_data),
            "title": "ChatGPT",
            "url": url,
            "active": active,
        }
        self.tabs_data.append(tab)
        return {"ok": True, "tab": tab}

    def inspect(self, tab_id, tail=6):
        return {
            "ok": True,
            "snapshot": {
                "generating": False,
                "uiState": "idle",
                "messages": [],
            },
        }

    def send(self, tab_id, text, command_id="", expected_url=""):
        self.sent.append((tab_id, text))
        for idx, tab in enumerate(self.tabs_data):
            if tab["id"] == tab_id and "/c/" not in tab["url"]:
                self.tabs_data[idx] = dict(
                    tab,
                    url="https://chatgpt.com/c/conversation-{0}".format(tab_id),
                )
        return {"ok": True, "tabId": tab_id, "commandId": command_id}

    def close_tab(self, tab_id):
        before = len(self.tabs_data)
        self.tabs_data = [
            tab for tab in self.tabs_data
            if int(tab.get("id", -1)) != int(tab_id)
        ]
        return {
            "ok": len(self.tabs_data) < before,
            "tabId": int(tab_id),
        }

    def refresh_tab(self, tab_id):
        target = next(tab for tab in self.tabs_data if int(tab["id"]) == int(tab_id))
        opened = self.open_tab(target["url"], active=bool(target.get("active")))
        self.close_tab(tab_id)
        return {
            "ok": True,
            "oldTabId": int(tab_id),
            "tabId": int(opened["tab"]["id"]),
            "tab": opened["tab"],
            "recycled": True,
        }


class SwarmRuntimeTest(unittest.TestCase):
    def test_transient_web_conversation_id_is_not_stable(self):
        self.assertEqual(
            conversation_id("https://chatgpt.com/c/WEB:temporary-id"),
            "",
        )
        self.assertEqual(
            conversation_id("https://chatgpt.com/c/6a92d549-4410-83e9-a368-af961df580c1"),
            "6a92d549-4410-83e9-a368-af961df580c1",
        )

    def test_unresponsive_binding_refreshes_same_conversation_and_preserves_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            bridge.tabs_data = [{
                "id": 202, "windowId": 1, "index": 0, "title": "Worker",
                "url": "https://chatgpt.com/c/worker-conv", "active": False,
            }]
            store.bind("implementer", {
                "kind": "worker", "role": "implementer",
                "conversation_id": "worker-conv",
                "url_at_bind": "https://chatgpt.com/c/worker-conv",
            })
            calls = {"inspect": 0}
            original_inspect = bridge.inspect

            def inspect(tab_id, tail=6):
                calls["inspect"] += 1
                if calls["inspect"] == 1:
                    return {
                        "ok": False,
                        "error": "Timed out waiting for extension response.",
                        "browserState": "unresponsive",
                    }
                return original_inspect(tab_id, tail=tail)

            bridge.inspect = inspect
            result = swarm.recover_unresponsive_binding("implementer")
            self.assertTrue(result["ok"])
            self.assertTrue(result["reloaded"])
            self.assertTrue(result["recovered"])
            self.assertFalse(result["durable_ownership_mutated"])
            self.assertEqual(result["conversation_id"], "worker-conv")
            binding = store.bindings()["implementer"]
            self.assertEqual(binding["conversation_id"], "worker-conv")
            self.assertEqual(binding["reload_recovery_count"], 1)
            self.assertTrue(binding["last_reload_recovery_ok"])
            self.assertEqual(len(bridge.tabs_data), 1)
            self.assertEqual(conversation_id(bridge.tabs_data[0]["url"]), "worker-conv")

    def test_unresponsive_recovery_does_not_refresh_after_transient_probe_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            bridge.tabs_data = [{
                "id": 202, "windowId": 1, "index": 0, "title": "Worker",
                "url": "https://chatgpt.com/c/worker-conv", "active": False,
            }]
            store.bind("implementer", {
                "kind": "worker", "role": "implementer",
                "conversation_id": "worker-conv",
                "url_at_bind": "https://chatgpt.com/c/worker-conv",
            })
            result = swarm.recover_unresponsive_binding("implementer")
            self.assertTrue(result["ok"])
            self.assertFalse(result["reloaded"])
            self.assertEqual(result["reason"], "confirm_probe_recovered")
            self.assertEqual(bridge.tabs_data[0]["id"], 202)

    def test_exhausted_main_rolls_over_without_mutating_durable_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            bridge.tabs_data = [
                {
                    "id": 101,
                    "windowId": 1,
                    "index": 0,
                    "title": "Old main",
                    "url": "https://chatgpt.com/c/main-conv",
                    "active": False,
                },
                {
                    "id": 202,
                    "windowId": 1,
                    "index": 1,
                    "title": "Worker",
                    "url": "https://chatgpt.com/c/worker-conv",
                    "active": False,
                },
            ]
            store.bind("main", {
                "kind": "main",
                "role": "",
                "conversation_id": "main-conv",
                "url_at_bind": "https://chatgpt.com/c/main-conv",
            })
            store.bind("implementer", {
                "kind": "worker",
                "role": "implementer",
                "conversation_id": "worker-conv",
                "url_at_bind": "https://chatgpt.com/c/worker-conv",
            })
            original_inspect = bridge.inspect

            def inspect(tab_id, tail=6):
                result = original_inspect(tab_id, tail=tail)
                if int(tab_id) == 101:
                    result["snapshot"] = dict(
                        result["snapshot"],
                        lastAssistantText=(
                            "你已达到此对话的长度上限，你可以开始新聊天以继续对话。"
                        ),
                    )
                return result

            bridge.inspect = inspect
            store.status_set("mission", "preserve-me")

            result = swarm.rollover_main_if_exhausted()

            self.assertTrue(result["ok"])
            self.assertTrue(result["rolled_over"])
            self.assertEqual(result["old_tab_id"], 101)
            self.assertFalse(any(int(tab["id"]) == 101 for tab in bridge.tabs_data))
            binding = store.bindings()["main"]
            self.assertEqual(binding["rollover_count"], 1)
            self.assertEqual(binding["rollover_from_conversation_id"], "main-conv")
            self.assertNotEqual(binding["conversation_id"], "main-conv")
            self.assertEqual(store.status()["mission"]["value"], "preserve-me")
            self.assertIn("durable WebAgentSwarm store/graph", bridge.sent[-1][1])

    def test_exhausted_worker_rolls_over_same_identity_and_preserves_durable_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            bridge.tabs_data = [
                {"id": 101, "windowId": 1, "index": 0, "title": "Main", "url": "https://chatgpt.com/c/main-conv", "active": False},
                {"id": 202, "windowId": 1, "index": 1, "title": "Worker", "url": "https://chatgpt.com/c/worker-conv", "active": False},
            ]
            store.bind("main", {"kind": "main", "role": "", "conversation_id": "main-conv", "url_at_bind": "https://chatgpt.com/c/main-conv"})
            store.bind("implementer", {"kind": "worker", "role": "implementer", "conversation_id": "worker-conv", "url_at_bind": "https://chatgpt.com/c/worker-conv"})
            store.status_set("mission", "preserve-worker-state")
            original_inspect = bridge.inspect

            def inspect(tab_id, tail=6):
                result = original_inspect(tab_id, tail=tail)
                if int(tab_id) == 202:
                    result["snapshot"] = dict(
                        result["snapshot"],
                        lastAssistantText=(
                            "You have reached the maximum length for this conversation. "
                            "Start a new chat to continue."
                        ),
                    )
                return result

            bridge.inspect = inspect
            result = swarm.rollover_worker_if_exhausted("implementer")
            self.assertTrue(result["ok"])
            self.assertTrue(result["rolled_over"])
            self.assertEqual(result["name"], "implementer")
            self.assertEqual(result["role"], "implementer")
            self.assertFalse(result["durable_ownership_mutated"])
            self.assertFalse(any(int(tab["id"]) == 202 for tab in bridge.tabs_data))
            binding = store.bindings()["implementer"]
            self.assertEqual(binding["rollover_count"], 1)
            self.assertEqual(binding["rollover_from_conversation_id"], "worker-conv")
            self.assertTrue(binding["transport_replacement_only"])
            self.assertEqual(store.status()["mission"]["value"], "preserve-worker-state")
            self.assertIn("transport replacement only", bridge.sent[-1][1])
            self.assertIn("claimed_by implementer", bridge.sent[-1][1])

    def test_failed_old_tab_close_becomes_retryable_rollover_debt(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            bridge.tabs_data = [
                {"id": 202, "windowId": 1, "index": 0, "title": "Worker", "url": "https://chatgpt.com/c/worker-conv", "active": False}
            ]
            store.bind("implementer", {"kind": "worker", "role": "implementer", "conversation_id": "worker-conv", "url_at_bind": "https://chatgpt.com/c/worker-conv"})
            original_inspect = bridge.inspect
            original_close = bridge.close_tab
            bridge.inspect = lambda tab_id, tail=6: dict(
                original_inspect(tab_id, tail=tail),
                snapshot={
                    "generating": False,
                    "lastAssistantText": "You have reached the maximum length for this conversation.",
                },
            )
            first = {"done": False}

            def close(tab_id):
                if int(tab_id) == 202 and not first["done"]:
                    first["done"] = True
                    return {"ok": False, "tabId": 202, "error": "transient close failure"}
                return original_close(tab_id)

            bridge.close_tab = close
            result = swarm.rollover_worker_if_exhausted("implementer")
            self.assertTrue(result["rolled_over"])
            self.assertFalse(result["old_tab_closed"])
            self.assertEqual(store.bindings()["implementer"]["rollover_close_pending_tab_id"], 202)
            cleanup = swarm.cleanup_rollover_close_debt()
            self.assertTrue(cleanup["ok"])
            self.assertFalse(any(int(tab["id"]) == 202 for tab in bridge.tabs_data))
            self.assertNotIn("rollover_close_pending_tab_id", store.bindings()["implementer"])

    def test_plan_does_not_touch_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            swarm = SwarmRuntime(bridge=bridge, store=DurableStore(Path(tmp)))
            plan = swarm.plan(worker_count=2)
            self.assertTrue(plan["requires_confirmation"])
            self.assertEqual(bridge.tabs_data, [])
            self.assertEqual(bridge.sent, [])

    def test_explicit_worker_count_can_exceed_role_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            swarm = SwarmRuntime(bridge=bridge, store=DurableStore(Path(tmp)))
            plan = swarm.plan(worker_count=8)
            self.assertTrue(plan["ok"])
            self.assertEqual(plan["worker_count"], 8)
            self.assertEqual(
                sum(item["count"] for item in plan["instance_plan"]),
                8,
            )
            self.assertEqual(bridge.tabs_data, [])
            self.assertEqual(bridge.sent, [])

    def test_main_only_bootstrap_starts_without_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)

            started = swarm.start(start_watch=False)
            self.assertTrue(started["ok"])
            self.assertEqual(started["bootstrap_mode"], "main_only")
            self.assertEqual(started["worker_count"], 0)
            self.assertEqual(started["total_web_agents"], 1)
            self.assertEqual(set(store.bindings()), {"main"})
            self.assertIn("main-only bootstrap", bridge.sent[0][1])

    def test_global_worker_budget_caps_parallel_pressure(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            store.task_fanout(
                items=["i1", "i2", "i3", "i4"],
                owner="implementer",
                group_id="impl-budget",
            )
            store.task_fanout(
                items=["t1", "t2", "t3", "t4"],
                owner="tester",
                group_id="test-budget",
            )

            plan = swarm.coordination_plan(
                active_roles=[],
                max_new_roles=6,
                max_instances_per_role=4,
                max_total_workers=3,
            )
            recommended = plan["scale_recommendation"]["recommended_instances"]
            self.assertEqual(sum(recommended.values()), 3)
            self.assertGreater(recommended["implementer"], 0)
            self.assertGreater(recommended["tester"], 0)
            self.assertEqual(plan["scale_recommendation"]["max_total_workers"], 3)

    def test_automatic_scale_in_uses_grace_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            started = swarm.start(["implementer"], start_watch=False)
            self.assertTrue(started["ok"])

            first = swarm.scale(
                scale_in_grace_seconds=300,
                start_watch=False,
            )
            self.assertTrue(first["ok"])
            self.assertFalse(first["scaled"])
            self.assertEqual(first["removed_workers"], [])
            self.assertEqual(len(first["scale_in_deferred"]), 1)
            binding = store.bindings()["implementer"]
            self.assertGreater(float(binding.get("excess_since") or 0.0), 0.0)

            aged = dict(binding)
            aged["excess_since"] = time.time() - 301.0
            store.bind("implementer", aged)
            second = swarm.scale(
                scale_in_grace_seconds=300,
                start_watch=False,
            )
            self.assertTrue(second["ok"])
            self.assertTrue(second["scaled"])
            self.assertEqual(second["removed_workers"], ["implementer"])
            self.assertEqual(second["worker_count"], 0)

    def test_start_subset(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            swarm = SwarmRuntime(bridge=bridge, store=DurableStore(Path(tmp)))
            result = swarm.start(["implementer", "tester"], start_watch=False)
            self.assertTrue(result["ok"])
            self.assertEqual(result["worker_count"], 2)
            self.assertEqual(result["total_web_agents"], 3)
            self.assertEqual(
                set(swarm.store.bindings()),
                {"main", "implementer", "tester"},
            )

    def test_initial_plan_and_start_support_same_role_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            store.task_fanout(
                items=["a", "b", "c", "d"],
                owner="implementer",
                group_id="initial-map",
            )

            plan = swarm.plan()
            self.assertEqual(plan["worker_count"], 4)
            self.assertEqual(
                plan["instance_plan"],
                [{"role": "implementer", "count": 4}],
            )
            self.assertEqual(bridge.tabs_data, [])

            started = swarm.start(
                ["implementer"],
                instances={"implementer": 4},
                start_watch=False,
            )
            self.assertTrue(started["ok"])
            self.assertEqual(started["worker_count"], 4)
            self.assertEqual(started["worker_instances"], {"implementer": 4})
            self.assertEqual(
                set(store.bindings()),
                {
                    "main",
                    "implementer",
                    "implementer-2",
                    "implementer-3",
                    "implementer-4",
                },
            )

    def test_scale_adds_workers_without_removing_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            swarm = SwarmRuntime(bridge=bridge, store=DurableStore(Path(tmp)))
            started = swarm.start(["implementer", "tester"], start_watch=False)
            self.assertTrue(started["ok"])
            scaled = swarm.scale(
                requested_roles=["reviewer"],
                start_watch=False,
            )
            self.assertTrue(scaled["ok"])
            self.assertTrue(scaled["scaled"])
            self.assertEqual(
                set(swarm.store.bindings()),
                {"main", "implementer", "tester", "reviewer"},
            )
            self.assertEqual(scaled["added_roles"], ["reviewer"])

    def test_capability_routing_reuses_online_worker_and_waits_on_write_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)

            store.task_create(
                "Generic analysis",
                owner="researcher",
                preferred_role="researcher",
                required_capabilities=["analysis"],
                task_id="analysis-flex",
            )
            plan = swarm.coordination_plan(
                active_roles=["tester"],
                max_new_roles=2,
            )
            node = next(
                item for item in plan["task_nodes"]
                if item["task_id"] == "analysis-flex"
            )
            self.assertEqual(node["route"], "dispatch")
            self.assertEqual(node["assigned_role"], "tester")
            self.assertIn("researcher", node["compatible_roles"])
            self.assertEqual(plan["scale_recommendation"]["add_roles"], [])

            store.task_update("analysis-flex", status="done", summary="complete")
            first = store.task_create(
                "Primary writer",
                task_id="writer-a",
                required_capabilities=["implementation"],
                write_resources=["src/shared.py"],
                canonical_root=tmp,
            )
            second = store.task_create(
                "Secondary writer",
                task_id="writer-b",
                required_capabilities=["implementation"],
                write_resources=["src/shared.py"],
                canonical_root=tmp,
            )
            store.task_claim(first["task_id"], "implementer", lease_seconds=60)
            conflict_plan = swarm.coordination_plan(
                active_roles=["implementer", "implementer"],
                max_new_roles=2,
            )
            conflict = next(
                item for item in conflict_plan["task_nodes"]
                if item["task_id"] == second["task_id"]
            )
            self.assertEqual(conflict["route"], "wait_resource")
            self.assertTrue(conflict["resource_conflicts"])
            self.assertNotIn(
                second["task_id"],
                {item["task_id"] for item in conflict_plan["parallel_frontier"]},
            )

    def test_parallelizable_false_suppresses_auto_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(store=store)
            store.task_create(
                "Serial critical section",
                owner="implementer",
                task_id="serial-primary",
                parallelizable=False,
            )
            plan = swarm.coordination_plan(active_roles=[])
            self.assertFalse(any(
                item.get("parent_task_id") == "serial-primary"
                for item in plan["sidecar_opportunities"]
            ))
            ensured = swarm.ensure_parallelism()
            self.assertEqual(ensured["count"], 0)

    def test_coordination_plan_routes_dependencies_and_blocked_interrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            store.task_create(
                "Research first",
                owner="researcher",
                task_id="research",
            )
            store.task_create(
                "Implement after research",
                owner="implementer",
                task_id="implement",
                dependencies=["research"],
            )

            waiting = swarm.coordination_plan(
                active_roles=["implementer"],
                max_new_roles=2,
            )
            implement = next(
                item for item in waiting["task_nodes"]
                if item["task_id"] == "implement"
            )
            self.assertEqual(implement["route"], "wait_dependencies")
            barrier = next(
                item for item in waiting["fan_in_barriers"]
                if item["task_id"] == "implement"
            )
            self.assertEqual(barrier["unresolved"], ["research"])
            research = next(
                item for item in waiting["task_nodes"]
                if item["task_id"] == "research"
            )
            self.assertEqual(research["assigned_role"], "implementer")
            self.assertIn("researcher", research["compatible_roles"])
            self.assertEqual(
                waiting["scale_recommendation"]["add_roles"],
                [],
            )

            store.task_update("research", status="done", summary="evidence ready")
            ready = swarm.coordination_plan(
                active_roles=["implementer"],
                max_new_roles=2,
            )
            implement = next(
                item for item in ready["task_nodes"]
                if item["task_id"] == "implement"
            )
            self.assertEqual(implement["route"], "dispatch")
            self.assertEqual(ready["scale_recommendation"]["add_roles"], [])

            store.task_update(
                "implement",
                status="blocked",
                summary="needs debugging",
            )
            blocked = swarm.coordination_plan(
                active_roles=["implementer"],
                max_new_roles=2,
            )
            interrupt = next(
                item for item in blocked["interrupts"]
                if item["task_id"] == "implement"
            )
            self.assertEqual(interrupt["role"], "debugger")
            # Blocked work remains an interrupt for main; it is not executable
            # browser-worker capacity until main materializes a ready recovery task.
            self.assertEqual(blocked["scale_recommendation"]["add_roles"], [])

    def test_elastic_scale_uses_graph_recommendation(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            started = swarm.start(["implementer"], start_watch=False)
            self.assertTrue(started["ok"])
            store.task_create(
                "Implementation task",
                owner="implementer",
                task_id="work",
            )
            store.task_update("work", status="blocked", summary="debug needed")

            elastic = swarm.elastic_plan(max_new_roles=1)
            self.assertEqual(elastic["add_roles"], [])
            self.assertIn("implementer", elastic["remove_roles"])
            scaled = swarm.scale(
                max_new_roles=1,
                scale_in_grace_seconds=0.0,
                start_watch=False,
            )
            self.assertTrue(scaled["ok"])
            self.assertTrue(scaled["scaled"])
            self.assertEqual(scaled["added_roles"], [])
            self.assertIn("implementer", scaled["removed_workers"])
            binding = store.bindings()["implementer"]
            self.assertTrue(binding.get("suspended"))

    def test_external_wait_does_not_spawn_debugger_pressure(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            store.task_create(
                "Wait for extension",
                owner="implementer",
                task_id="wait-extension",
            )
            store.task_update(
                "wait-extension",
                status="blocked",
                summary="external_wait: waiting for browser extension",
            )

            plan = swarm.coordination_plan(
                active_roles=["implementer"],
                max_new_roles=2,
            )
            node = next(
                item for item in plan["task_nodes"]
                if item["task_id"] == "wait-extension"
            )
            self.assertEqual(node["route"], "wait_external")
            self.assertEqual(node["blocker_kind"], "external_wait")
            self.assertNotIn("debugger", plan["scale_recommendation"]["add_roles"])

    def test_elastic_scale_in_suspends_and_closes_idle_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            started = swarm.start(["implementer", "tester"], start_watch=False)
            self.assertTrue(started["ok"])

            plan = swarm.elastic_plan()
            self.assertTrue(plan["needs_scale_in"])
            scaled = swarm.scale(scale_in_grace_seconds=0, start_watch=False)
            self.assertTrue(scaled["ok"])
            self.assertTrue(scaled["scaled"])
            self.assertEqual(set(scaled["removed_workers"]), {"implementer", "tester"})
            self.assertEqual(scaled["worker_count"], 0)
            bindings = store.bindings()
            self.assertTrue(bindings["implementer"]["suspended"])
            self.assertTrue(bindings["tester"]["suspended"])
            self.assertEqual(
                [tab for tab in bridge.tabs_data if "/c/" in str(tab.get("url") or "")],
                [next(tab for tab in bridge.tabs_data if tab["id"] == 11)],
            )

    def test_scale_in_refuses_worker_with_live_task_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            started = swarm.start(["implementer"], start_watch=False)
            self.assertTrue(started["ok"])
            store.task_create(
                "leased work",
                owner="implementer",
                task_id="leased-work",
            )
            store.task_claim("leased-work", "implementer", lease_seconds=600)

            scaled = swarm.scale(
                requested_instances={"implementer": 0},
                start_watch=False,
            )
            self.assertFalse(scaled["ok"])
            self.assertEqual(scaled["removed_workers"], [])
            self.assertTrue(scaled["scale_in_blocked"])
            self.assertEqual(scaled["worker_instances"]["implementer"], 1)
            self.assertFalse(store.bindings()["implementer"].get("suspended", False))

    def test_send_style_fanout_scales_multiple_instances_of_same_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            started = swarm.start(["implementer"], start_watch=False)
            self.assertTrue(started["ok"])
            fanout = store.task_fanout(
                items=["a", "b", "c", "d"],
                owner="implementer",
                title_prefix="Shard",
                group_id="same-role-map",
            )
            self.assertEqual(len(fanout["task_ids"]), 4)

            elastic = swarm.elastic_plan(
                max_new_roles=3,
                max_instances_per_role=4,
            )
            implementer_add = next(
                item for item in elastic["add_instances"]
                if item["role"] == "implementer"
            )
            self.assertEqual(implementer_add["count"], 3)

            scaled = swarm.scale(
                max_new_roles=3,
                max_instances_per_role=4,
                start_watch=False,
            )
            self.assertTrue(scaled["ok"])
            self.assertEqual(
                scaled["added_workers"],
                ["implementer-2", "implementer-3", "implementer-4"],
            )
            self.assertEqual(scaled["worker_instances"]["implementer"], 4)
            self.assertEqual(
                set(store.bindings()),
                {
                    "main",
                    "implementer",
                    "implementer-2",
                    "implementer-3",
                    "implementer-4",
                },
            )

    def test_verification_failure_is_wait_not_worker_pressure(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            store.task_create(
                "Wait for fresh verifier candidate",
                owner="tester",
                task_id="verify-wait",
            )
            store.task_update(
                "verify-wait",
                status="blocked",
                summary="verification_failure: waiting for fresh upstream candidate",
            )

            plan = swarm.coordination_plan(
                active_roles=["tester"],
                max_new_roles=3,
            )
            node = next(
                item for item in plan["task_nodes"]
                if item["task_id"] == "verify-wait"
            )
            self.assertEqual(node["route"], "wait_verification")
            self.assertEqual(node["blocker_kind"], "verification_failure")
            self.assertEqual(plan["scale_recommendation"]["add_roles"], [])
            tester_pressure = next(
                item for item in plan["role_pressure"]
                if item["role"] == "tester"
            )
            self.assertEqual(tester_pressure["desired_instances"], 0)

    def test_wait_only_role_bypasses_scale_in_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            started = swarm.start(["tester"], start_watch=False)
            self.assertTrue(started["ok"])
            store.task_create(
                "Wait for fresh candidate",
                owner="tester",
                task_id="verify-wait-close",
            )
            store.task_update(
                "verify-wait-close",
                status="blocked",
                summary="verification_failure: waiting for fresh upstream candidate",
            )

            scaled = swarm.scale(
                scale_in_grace_seconds=300,
                start_watch=False,
            )
            self.assertTrue(scaled["ok"])
            self.assertIn("tester", scaled["immediate_wait_scale_in_roles"])
            self.assertEqual(scaled["removed_workers"], ["tester"])
            self.assertEqual(scaled["scale_in_deferred"], [])
            self.assertEqual(scaled["worker_count"], 0)

    def test_ensure_parallelism_materializes_bounded_safe_sidecars_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            store.task_create(
                "Primary implementation",
                owner="implementer",
                task_id="primary",
                resource_hints=["src/core.py"],
                canonical_root=".",
            )

            first = swarm.ensure_parallelism(
                max_sidecars_per_primary=2,
                max_new_sidecars=4,
            )
            self.assertTrue(first["ok"])
            self.assertEqual(first["count"], 2)
            assists = store.task_assists("primary", include_terminal=True)
            self.assertEqual(len(assists), 2)
            self.assertEqual(
                {task["assist_write_policy"] for task in assists},
                {"experiment_only", "proposal_only"},
            )
            self.assertTrue(all(
                task["assist_write_policy"] in {
                    "readonly", "proposal_only", "experiment_only"
                }
                for task in assists
            ))
            self.assertTrue(all(
                bool((task.get("payload") or {}).get("auto_sidecar"))
                for task in assists
            ))

            second = swarm.ensure_parallelism(
                max_sidecars_per_primary=2,
                max_new_sidecars=4,
            )
            self.assertTrue(second["ok"])
            self.assertEqual(second["count"], 0)
            self.assertEqual(
                len(store.task_assists("primary", include_terminal=True)),
                2,
            )

            plan = swarm.coordination_plan(
                active_roles=["implementer"],
                max_new_roles=4,
            )
            self.assertEqual(
                set(plan["scale_recommendation"]["add_roles"]),
                {"tester", "reviewer"},
            )

    def test_send_persists_command_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = FakeBridge()
            store = DurableStore(Path(tmp))
            swarm = SwarmRuntime(bridge=bridge, store=store)
            bridge.tabs_data = [{
                "id": 7, "windowId": 1, "index": 0, "title": "Worker",
                "url": "https://chatgpt.com/c/worker", "active": False,
            }]
            store.bind("implementer", {
                "kind": "worker", "role": "implementer",
                "conversation_id": "worker",
                "url_at_bind": "https://chatgpt.com/c/worker",
            })
            result = swarm.send("implementer", "continue")
            self.assertTrue(result["ok"])
            tx = result["send_transaction"]
            self.assertEqual(tx["status"], "ok")
            self.assertEqual(tx["command_id"], result["command_id"])
            self.assertEqual(store.send_transaction_get(result["command_id"])["status"], "ok")


if __name__ == "__main__":
    unittest.main()
