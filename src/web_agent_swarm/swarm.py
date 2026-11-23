"""Browser-hosted swarm runtime."""

import copy
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from typing import Dict, List, Optional

from .bridge import BridgeClient
from .graph import compile_graph
from .roles import (
    DEFAULT_PRIORITY,
    DEFAULT_ROLES,
    compatible_roles,
    normalize_capabilities,
    plan_roles,
    role_capabilities,
)
from .state import DurableStore


_CONVERSATION_RE = re.compile(r"/c/([^/?#]+)")


def conversation_id(url: str) -> str:
    match = _CONVERSATION_RE.search(str(url or ""))
    if not match:
        return ""
    cid = match.group(1)
    if ":" in cid:
        return ""
    return cid


class SwarmRuntime:
    def __init__(
        self,
        bridge: Optional[BridgeClient] = None,
        store: Optional[DurableStore] = None,
    ) -> None:
        self.bridge = bridge or BridgeClient()
        self.store = store or DurableStore()
        self.watcher_pid_path = self.store.root / "watcher.pid"
        self.watcher_config_path = self.store.root / "watcher.json"
        self.watcher_log_path = self.store.root / "watcher.log"

    def plan(
        self,
        worker_count: int = 0,
        requested_roles: Optional[List[str]] = None,
    ) -> Dict[str, object]:
        requested_total = int(worker_count or 0)
        if requested_total < 0 or requested_total > 32:
            return {"ok": False, "error": "worker_count must be between 0 and 32"}
        base_count = min(requested_total, len(DEFAULT_ROLES)) if requested_total else 0
        result = plan_roles(
            self.store.tasks(),
            worker_count=base_count,
            requested_roles=requested_roles,
        )
        if not result.get("ok"):
            return result
        roles = list(result["roles"])
        coordination = self.coordination_plan(
            active_roles=[],
            max_new_roles=16,
            max_instances_per_role=4,
        )
        instance_plan = [
            {"role": role, "count": 1}
            for role in roles
        ]

        if requested_total > len(instance_plan) and instance_plan:
            counts = {item["role"]: int(item["count"]) for item in instance_plan}
            if requested_roles:
                candidates = list(roles)
            else:
                pressure_roles = [
                    str(item.get("role") or "")
                    for item in coordination.get("role_pressure", [])
                    if int(item.get("score") or 0) > 0
                ]
                candidates = []
                for role in pressure_roles + roles + list(DEFAULT_PRIORITY):
                    if role in DEFAULT_ROLES and role not in candidates:
                        candidates.append(role)
            remaining = requested_total - sum(counts.values())
            cursor = 0
            while remaining > 0 and candidates:
                role = candidates[cursor % len(candidates)]
                if counts.get(role, 0) < 16:
                    counts[role] = counts.get(role, 0) + 1
                    remaining -= 1
                cursor += 1
                if cursor > 1024:
                    break
            roles = [role for role in candidates if counts.get(role, 0) > 0]
            instance_plan = [
                {"role": role, "count": counts[role]}
                for role in roles
                if counts.get(role, 0) > 0
            ]
            result["basis"] = "user_requested_count_instances"

        if (
            not requested_roles
            and not requested_total
            and coordination.get("ok")
        ):
            graph_instances = list(
                (coordination.get("scale_recommendation") or {}).get(
                    "add_instances"
                )
                or []
            )
            if graph_instances:
                instance_plan = [
                    {
                        "role": str(item["role"]),
                        "count": max(1, int(item["count"])),
                    }
                    for item in graph_instances
                ]
                roles = [item["role"] for item in instance_plan]
                result["basis"] = "durable_task_graph_instances"

        total_workers = sum(int(item["count"]) for item in instance_plan)
        role_text = ", ".join(
            "{0}x{1}".format(item["role"], item["count"])
            if int(item["count"]) > 1
            else str(item["role"])
            for item in instance_plan
        )
        result.update({
            "phase": "proposal",
            "requires_confirmation": True,
            "roles": roles,
            "worker_count": total_workers,
            "total_web_agents": total_workers + 1,
            "includes_main": True,
            "instance_plan": instance_plan,
            "role_plan": [
                {
                    "role": item["role"],
                    "instances": int(item["count"]),
                    "responsibility": DEFAULT_ROLES[item["role"]],
                    "capabilities": role_capabilities(item["role"]),
                }
                for item in instance_plan
            ],
            "confirmation_prompt": (
                "Proposed swarm: {0} worker(s) + 1 main agent: {1}. "
                "Confirm, change the total, roles, or per-role instance counts."
            ).format(total_workers, role_text),
            "coordination": coordination,
        })
        return result

    @staticmethod
    def _blocker_kind(
        task: Dict[str, object],
        unresolved_dependencies: Optional[List[str]] = None,
    ) -> str:
        if unresolved_dependencies:
            return "dependency_wait"

        text = " ".join([
            str(task.get("blocker") or ""),
            str(task.get("summary") or ""),
            str(task.get("last_error") or ""),
        ]).strip().lower()
        explicit = {
            "dependency_wait",
            "external_wait",
            "human_input",
            "verification_failure",
            "recoverable_failure",
            "unknown_blocker",
        }
        for kind in explicit:
            if text.startswith(kind + ":"):
                return kind
        if any(token in text for token in (
            "human input", "user input", "approval", "confirmation",
            "人工", "用户确认", "等待用户",
        )):
            return "human_input"
        if any(token in text for token in (
            "extension", "browser", "network", "connection", "license",
            "schema unavailable", "tool unavailable", "连接", "网络", "许可证",
        )):
            return "external_wait"
        if any(token in text for token in (
            "test failed", "test failure", "verification", "regression",
            "verify", "验证失败", "回归失败",
        )):
            return "verification_failure"
        if any(token in text for token in (
            "timeout", "timed out", "crash", "exception", "stuck", "retry",
            "超时", "崩溃", "异常", "卡死", "重试",
        )):
            return "recoverable_failure"
        return "unknown_blocker"

    @classmethod
    def _task_role(
        cls,
        task: Dict[str, object],
        blocker_kind: str = "",
    ) -> str:
        """Return a soft preferred role; capability/resource matching is authoritative."""
        status = str(task.get("status") or "").strip().lower()
        if status == "review":
            return "reviewer"
        if status == "blocked" and blocker_kind in {
            "recoverable_failure",
            "unknown_blocker",
        }:
            return "debugger"
        preferred = str(
            task.get("preferred_role")
            or task.get("owner")
            or ""
        ).strip().lower()
        if preferred in DEFAULT_ROLES:
            return preferred
        return "implementer"

    @staticmethod
    def _role_from_worker_name(
        worker_name: str,
        bindings: Dict[str, object],
    ) -> str:
        clean = str(worker_name or "").strip()
        binding = bindings.get(clean)
        if isinstance(binding, dict):
            role = str(binding.get("role") or "").strip().lower()
            if role in DEFAULT_ROLES:
                return role
        lowered = clean.lower()
        for role in DEFAULT_ROLES:
            if lowered == role or lowered.startswith(role + "-"):
                return role
        return ""

    @staticmethod
    def _select_compatible_role(
        compatible: List[str],
        preferred: str,
        active_instances: Dict[str, int],
        occupied_instances: Dict[str, int],
        planned_dispatch: Dict[str, int],
    ) -> str:
        if not compatible:
            return ""
        priority = {role: idx for idx, role in enumerate(DEFAULT_PRIORITY)}
        available = [
            role
            for role in compatible
            if int(active_instances.get(role, 0))
            > int(occupied_instances.get(role, 0)) + int(planned_dispatch.get(role, 0))
        ]
        pool = available or list(compatible)
        pool.sort(
            key=lambda role: (
                0 if role == preferred else 1,
                int(planned_dispatch.get(role, 0)),
                priority.get(role, len(priority)),
            )
        )
        return pool[0] if pool else ""

    def _bound_worker_instances(self) -> Dict[str, List[str]]:
        instances = {role: [] for role in DEFAULT_ROLES}
        for name, binding in self.store.bindings().items():
            if name == "main" or not isinstance(binding, dict) or binding.get("suspended"):
                continue
            role = str(binding.get("role") or "").strip().lower()
            if role in DEFAULT_ROLES:
                instances[role].append(str(name))
        for role in instances:
            instances[role].sort()
        return instances

    def _bound_worker_roles(self) -> List[str]:
        instances = self._bound_worker_instances()
        return [role for role in DEFAULT_PRIORITY if instances.get(role)]

    @staticmethod
    def _worker_instance_name(role: str, index: int) -> str:
        return role if int(index) <= 1 else "{0}-{1}".format(role, int(index))

    @staticmethod
    def _next_worker_name(role: str, bindings: Dict[str, object]) -> str:
        if role not in bindings:
            return role
        index = 2
        while "{0}-{1}".format(role, index) in bindings:
            index += 1
        return "{0}-{1}".format(role, index)

    def coordination_plan(
        self,
        active_roles: Optional[List[str]] = None,
        max_new_roles: int = 6,
        max_instances_per_role: int = 4,
        max_total_workers: int = 6,
    ) -> Dict[str, object]:
        """Derive task routing plus same-role instance pressure from durable state."""
        if active_roles is None:
            bound_instances = self._bound_worker_instances()
            active_instances = {
                role: len(bound_instances.get(role, []))
                for role in DEFAULT_ROLES
            }
        else:
            active_instances = {role: 0 for role in DEFAULT_ROLES}
            for role in active_roles:
                clean = str(role or "").strip().lower()
                if clean not in DEFAULT_ROLES:
                    return {"ok": False, "error": "unknown role: {0}".format(clean)}
                active_instances[clean] += 1

        normalized_active = [
            role for role in DEFAULT_PRIORITY if active_instances.get(role, 0) > 0
        ]
        add_limit = max(0, min(int(max_new_roles or 0), 32))
        per_role_limit = max(1, min(int(max_instances_per_role or 1), 16))
        now = time.time()

        tasks = self.store.tasks()
        durable_state = self.store.load()
        bindings = dict(durable_state.get("bindings") or {})
        task_map = {
            str(task.get("task_id") or ""): task
            for task in tasks
            if isinstance(task, dict) and str(task.get("task_id") or "")
        }
        occupied_instances = {role: 0 for role in DEFAULT_ROLES}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if str(task.get("status") or "") != "active":
                continue
            if float(task.get("lease_expires_at") or 0.0) <= now:
                continue
            leased_role = self._role_from_worker_name(
                str(task.get("claimed_by") or ""),
                bindings,
            )
            if leased_role in occupied_instances:
                occupied_instances[leased_role] += 1
        planned_dispatch = {role: 0 for role in DEFAULT_ROLES}
        capability_gaps = []
        shared_status = self.store.status()
        active_graph = self.store.graph_get()
        compiled_graph = compile_graph(
            active_graph if active_graph else None,
            task_map,
            shared_status,
        )
        if compiled_graph.get("active") and not compiled_graph.get("valid", True):
            return {
                "ok": False,
                "error": "active SwarmGraph is invalid",
                "swarm_graph": compiled_graph,
            }
        graph_gates = dict(compiled_graph.get("task_gates") or {})
        graph_task_nodes = dict(compiled_graph.get("task_node_map") or {})
        graph_nodes_by_id = {
            str(item.get("node_id") or ""): item
            for item in compiled_graph.get("nodes", [])
            if isinstance(item, dict)
        }
        nodes = []
        edges = []
        frontier = []
        barriers = []
        waits = []
        interrupts = []
        parallel_sidecars = []
        sidecar_opportunities = []
        live_sidecar_parents = set()
        pressure = {
            role: {
                "role": role,
                "responsibility": DEFAULT_ROLES[role],
                "score": 0,
                "claimable_task_ids": [],
                "leased_task_ids": [],
                "interrupt_task_ids": [],
                "sidecar_task_ids": [],
                "reasons": [],
            }
            for role in DEFAULT_ROLES
        }

        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("task_id") or "")
            status = str(task.get("status") or "").strip().lower()
            assist_parent_task_id = str(task.get("assist_parent_task_id") or "")
            assist_kind = str(task.get("assist_kind") or "")
            assist_write_policy = str(task.get("assist_write_policy") or "")
            if assist_parent_task_id and status not in {"done", "failed", "cancelled"}:
                live_sidecar_parents.add(assist_parent_task_id)
            dependencies = [
                str(item)
                for item in task.get("dependencies", [])
                if str(item)
            ]
            unresolved = [
                dep
                for dep in dependencies
                if task_map.get(dep, {}).get("status") != "done"
            ]
            blocker_kind = (
                self._blocker_kind(task, unresolved)
                if status == "blocked"
                else ""
            )
            graph_gate = graph_gates.get(task_id)
            graph_node_id = str(graph_task_nodes.get(task_id) or "")
            graph_node = graph_nodes_by_id.get(graph_node_id) or {}
            graph_role = str(graph_node.get("role") or "").strip().lower()
            preferred_role = (
                graph_role
                if graph_role in DEFAULT_ROLES
                else self._task_role(task, blocker_kind=blocker_kind)
            )
            required_capabilities = normalize_capabilities(
                task.get("required_capabilities") or []
            )
            if status == "review" and "review" not in required_capabilities:
                required_capabilities.append("review")
            if (
                status == "blocked"
                and blocker_kind in {"recoverable_failure", "unknown_blocker"}
                and "debugging" not in required_capabilities
            ):
                required_capabilities.append("debugging")
            if graph_role in DEFAULT_ROLES:
                compatible = (
                    [graph_role]
                    if set(required_capabilities).issubset(set(role_capabilities(graph_role)))
                    else []
                )
            else:
                compatible = compatible_roles(required_capabilities)
            role = self._select_compatible_role(
                compatible,
                preferred_role,
                active_instances,
                occupied_instances,
                planned_dispatch,
            )
            if not role:
                role = preferred_role if preferred_role in DEFAULT_ROLES else "implementer"
            for dep in dependencies:
                edges.append({"from": dep, "to": task_id, "kind": "dependency"})

            claimed_by = str(task.get("claimed_by") or "")
            lease_expires = float(task.get("lease_expires_at") or 0.0)
            retry_after = float(task.get("retry_after") or 0.0)
            lease_valid = status == "active" and bool(claimed_by) and lease_expires > now
            lease_expired = status == "active" and not lease_valid
            retry_wait = status == "ready" and retry_after > now
            if lease_valid:
                leased_role = self._role_from_worker_name(claimed_by, bindings)
                if leased_role in compatible:
                    role = leased_role
            resource_conflicts = []
            if not lease_valid:
                canonical_id = self.store._task_canonical_id(task)
                for candidate in self.store._task_resource_intents(task):
                    for reservation in (durable_state.get("task_resource_reservations") or {}).values():
                        if not isinstance(reservation, dict) or reservation.get("status") != "active":
                            continue
                        if float(reservation.get("lease_expires_at") or 0.0) <= now:
                            continue
                        if str(reservation.get("task_id") or "") == task_id:
                            continue
                        held_canonical = str(reservation.get("canonical_id") or "")
                        if canonical_id and held_canonical and held_canonical != canonical_id:
                            continue
                        if self.store._intent_conflicts(candidate, reservation):
                            resource_conflicts.append({
                                "requested": candidate["resource"],
                                "held": str(reservation.get("resource") or ""),
                                "held_by_task": str(reservation.get("task_id") or ""),
                            })
                    for claim in (durable_state.get("resource_claims") or {}).values():
                        if not isinstance(claim, dict) or claim.get("status") != "active":
                            continue
                        if float(claim.get("lease_expires_at") or 0.0) <= now:
                            continue
                        held_canonical = str(claim.get("canonical_id") or "")
                        if canonical_id and held_canonical and held_canonical != canonical_id:
                            continue
                        if self.store._intent_conflicts(candidate, claim):
                            resource_conflicts.append({
                                "requested": candidate["resource"],
                                "held": str(claim.get("resource") or ""),
                                "held_by_run": str(claim.get("run_id") or ""),
                            })
            route = "terminal"
            executable = False

            if status == "blocked":
                blocked_record = {
                    "kind": "blocked_task",
                    "blocker_kind": blocker_kind,
                    "task_id": task_id,
                    "role": role,
                    "summary": str(task.get("summary") or ""),
                }
                if blocker_kind == "dependency_wait":
                    route = "wait_dependencies"
                    waits.append(blocked_record)
                elif blocker_kind == "external_wait":
                    route = "wait_external"
                    waits.append(blocked_record)
                elif blocker_kind == "human_input":
                    route = "interrupt_human"
                    interrupts.append(blocked_record)
                elif blocker_kind == "verification_failure":
                    # Blocked verification records durable evidence but contribute
                    # zero worker pressure. Concrete follow-up must exist as a
                    # ready/active repair task or JIT sidecar.
                    route = "wait_verification"
                    waits.append(blocked_record)
                else:
                    route = "interrupt"
                    interrupts.append(blocked_record)
                    pressure[role]["score"] += 40
                    pressure[role]["interrupt_task_ids"].append(task_id)
                    pressure[role]["reasons"].append(
                        "{0} task {1}".format(blocker_kind, task_id)
                    )
            elif status == "ready" and unresolved:
                route = "wait_dependencies"
            elif retry_wait:
                route = "wait_retry"
            elif lease_valid:
                route = "leased"
                pressure[role]["score"] += 80
                pressure[role]["leased_task_ids"].append(task_id)
                pressure[role]["reasons"].append(
                    "leased task {0} by {1}".format(task_id, claimed_by)
                )
            elif status in {"ready", "review"} or lease_expired:
                if not compatible:
                    route = "wait_capability"
                    gap = {
                        "kind": "capability_gap",
                        "task_id": task_id,
                        "preferred_role": preferred_role,
                        "required_capabilities": list(required_capabilities),
                        "graph_role": graph_role,
                    }
                    capability_gaps.append(gap)
                    waits.append(gap)
                elif resource_conflicts:
                    route = "wait_resource"
                    waits.append({
                        "kind": "resource_conflict",
                        "task_id": task_id,
                        "role": role,
                        "conflicts": copy.deepcopy(resource_conflicts),
                    })
                elif isinstance(graph_gate, dict) and graph_gate.get("unselected"):
                    route = "graph_unselected"
                elif isinstance(graph_gate, dict) and not graph_gate.get("ready", False):
                    route = "wait_graph"
                    waits.append({
                        "kind": "graph_gate",
                        "task_id": task_id,
                        "role": role,
                        "graph_id": str(compiled_graph.get("graph_id") or ""),
                        "graph_node_id": graph_node_id,
                        "gate": graph_gate,
                    })
                else:
                    route = "dispatch"
                    executable = True
                    frontier.append({
                        "task_id": task_id,
                        "role": role,
                        "assigned_role": role,
                        "preferred_role": preferred_role,
                        "compatible_roles": list(compatible),
                        "required_capabilities": list(required_capabilities),
                        "status": status,
                        "payload": task.get("payload"),
                        "group_id": str(task.get("group_id") or ""),
                        "recovery": bool(lease_expired),
                        "graph_id": str(compiled_graph.get("graph_id") or ""),
                        "graph_node_id": graph_node_id,
                    })
                    planned_dispatch[role] += 1
                    pressure[role]["score"] += 60 if status == "review" else 50
                    if lease_expired:
                        pressure[role]["score"] += 20
                    pressure[role]["claimable_task_ids"].append(task_id)
                    pressure[role]["reasons"].append(
                        "{0} task {1}".format(
                            "expired-lease" if lease_expired else status,
                            task_id,
                        )
                    )

            if assist_parent_task_id and status not in {"done", "failed", "cancelled"}:
                pressure[role]["sidecar_task_ids"].append(task_id)
                parallel_sidecars.append({
                    "task_id": task_id,
                    "parent_task_id": assist_parent_task_id,
                    "role": role,
                    "assist_kind": assist_kind,
                    "write_policy": assist_write_policy,
                    "resource_hints": list(task.get("resource_hints") or []),
                    "canonical_root": str(task.get("canonical_root") or ""),
                    "status": status,
                    "route": route,
                    "executable": executable,
                    "claimed_by": claimed_by,
                })

            if dependencies:
                barriers.append({
                    "task_id": task_id,
                    "role": role,
                    "dependencies": dependencies,
                    "unresolved": unresolved,
                    "ready": not unresolved,
                })

            nodes.append({
                "task_id": task_id,
                "role": role,
                "assigned_role": role,
                "owner": str(task.get("owner") or ""),
                "preferred_role": preferred_role,
                "compatible_roles": list(compatible),
                "required_capabilities": list(required_capabilities),
                "read_resources": list(task.get("read_resources") or []),
                "write_resources": list(task.get("write_resources") or []),
                "exclusive_resources": list(task.get("exclusive_resources") or []),
                "parallelizable": bool(task.get("parallelizable", True)),
                "shard_strategy": str(task.get("shard_strategy") or ""),
                "resource_conflicts": copy.deepcopy(resource_conflicts),
                "status": status,
                "route": route,
                "executable": executable,
                "blocker_kind": blocker_kind,
                "dependencies": dependencies,
                "unresolved_dependencies": unresolved,
                "claimed_by": claimed_by,
                "lease_expires_at": lease_expires,
                "attempts": int(task.get("attempts") or 0),
                "max_attempts": int(task.get("max_attempts") or 0),
                "group_id": str(task.get("group_id") or ""),
                "assist_parent_task_id": assist_parent_task_id,
                "assist_kind": assist_kind,
                "assist_write_policy": assist_write_policy,
                "resource_hints": list(task.get("resource_hints") or []),
                "canonical_root": str(task.get("canonical_root") or ""),
                "graph_node_id": graph_node_id,
                "graph_gate": copy.deepcopy(graph_gate) if isinstance(graph_gate, dict) else None,
            })

        for node in nodes:
            if str(node.get("assist_parent_task_id") or ""):
                continue
            if not bool(node.get("parallelizable", True)):
                continue
            if str(node.get("route") or "") not in {"dispatch", "leased"}:
                continue
            parent_task_id = str(node.get("task_id") or "")
            if not parent_task_id or parent_task_id in live_sidecar_parents:
                continue
            primary_role = str(node.get("role") or "")
            suggestions = []
            if primary_role == "implementer":
                suggestions = [
                    {
                        "assist_kind": "verification",
                        "role": "tester",
                        "write_policy": "experiment_only",
                        "purpose": "Run independent regression/reproduction while the primary writes code.",
                    },
                    {
                        "assist_kind": "proposal_only",
                        "role": "reviewer",
                        "write_policy": "proposal_only",
                        "purpose": "Review the same code/problem and return a patch/test proposal without competing writes.",
                    },
                ]
            elif primary_role == "debugger":
                suggestions = [
                    {
                        "assist_kind": "readonly_analysis",
                        "role": "researcher",
                        "write_policy": "readonly",
                        "purpose": "Independently mine evidence and alternative root-cause hypotheses.",
                    },
                    {
                        "assist_kind": "verification",
                        "role": "tester",
                        "write_policy": "experiment_only",
                        "purpose": "Build a minimal reproduction or verify the leading hypothesis.",
                    },
                ]
            elif primary_role == "researcher":
                suggestions = [{
                    "assist_kind": "proposal_only",
                    "role": "reviewer",
                    "write_policy": "proposal_only",
                    "purpose": "Challenge assumptions and validate evidence in parallel.",
                }]
            elif primary_role == "tester":
                suggestions = [{
                    "assist_kind": "readonly_analysis",
                    "role": "debugger",
                    "write_policy": "readonly",
                    "purpose": "Analyze failures while the tester runs the verification matrix.",
                }]
            elif primary_role == "reviewer":
                suggestions = [{
                    "assist_kind": "readonly_analysis",
                    "role": "researcher",
                    "write_policy": "readonly",
                    "purpose": "Gather missing evidence while review proceeds.",
                }]
            elif primary_role == "documenter":
                suggestions = [{
                    "assist_kind": "proposal_only",
                    "role": "reviewer",
                    "write_policy": "proposal_only",
                    "purpose": "Fact-check the draft without touching the documenter's write set.",
                }]
            if suggestions:
                task = task_map.get(parent_task_id) or {}
                sidecar_opportunities.append({
                    "parent_task_id": parent_task_id,
                    "title": str(task.get("title") or ""),
                    "primary_role": primary_role,
                    "primary_route": str(node.get("route") or ""),
                    "canonical_root": str(task.get("canonical_root") or ""),
                    "resource_hints": list(task.get("resource_hints") or []),
                    "suggestions": suggestions,
                    "policy": (
                        "Create a sidecar only when the work is genuinely orthogonal. "
                        "Do not create workers merely to fill capacity."
                    ),
                })

        priority_index = {
            role: index
            for index, role in enumerate(DEFAULT_PRIORITY)
        }
        worker_budget = max(1, min(int(max_total_workers or 1), 32))
        raw_desired = {}
        locked_minimum = {}
        for role, row in pressure.items():
            # Blocked/interrupt work belongs to main orchestration until it is
            # converted into a claimable Task/handoff. It must not keep an idle Edge
            # worker renderer alive indefinitely.
            work_units = (
                len(row["claimable_task_ids"])
                + len(row["leased_task_ids"])
            )
            raw_desired[role] = min(per_role_limit, work_units) if work_units else 0
            locked_minimum[role] = min(per_role_limit, len(row["leased_task_ids"]))
            row["current_instances"] = int(active_instances.get(role, 0))
            row["raw_desired_instances"] = int(raw_desired[role])
            row["locked_minimum_instances"] = int(locked_minimum[role])

        effective_budget = max(worker_budget, sum(locked_minimum.values()))
        allocated = dict(locked_minimum)
        budget_order = sorted(
            pressure.values(),
            key=lambda item: (
                -int(item["score"]),
                priority_index.get(str(item["role"]), len(priority_index)),
            ),
        )
        while sum(allocated.values()) < effective_budget:
            advanced = False
            for item in budget_order:
                role = str(item["role"])
                if int(allocated.get(role, 0)) >= int(raw_desired.get(role, 0)):
                    continue
                allocated[role] = int(allocated.get(role, 0)) + 1
                advanced = True
                if sum(allocated.values()) >= effective_budget:
                    break
            if not advanced:
                break

        for role, row in pressure.items():
            row["desired_instances"] = int(allocated.get(role, 0))
            row["missing_instances"] = max(
                0,
                int(row["desired_instances"]) - int(row["current_instances"]),
            )
            row["excess_instances"] = max(
                0,
                int(row["current_instances"]) - int(row["desired_instances"]),
            )

        ranked = sorted(
            pressure.values(),
            key=lambda item: (
                -int(item["missing_instances"]),
                -int(item["score"]),
                priority_index.get(str(item["role"]), len(priority_index)),
            ),
        )

        remaining = add_limit
        add_instances = []
        for item in ranked:
            if remaining <= 0:
                break
            needed = min(int(item["missing_instances"]), remaining)
            if needed <= 0:
                continue
            add_instances.append({
                "role": str(item["role"]),
                "count": needed,
                "current_instances": int(item["current_instances"]),
                "desired_instances": int(item["desired_instances"]),
            })
            remaining -= needed

        add_roles = [
            item["role"]
            for item in add_instances
            if int(item["current_instances"]) == 0
        ]
        remove_instances = [
            {
                "role": str(item["role"]),
                "count": int(item["excess_instances"]),
                "current_instances": int(item["current_instances"]),
                "desired_instances": int(item["desired_instances"]),
            }
            for item in ranked
            if int(item["excess_instances"]) > 0
        ]
        remove_roles = [
            item["role"]
            for item in remove_instances
            if int(item["desired_instances"]) == 0
        ]
        recommended_instances = dict(active_instances)
        for item in add_instances:
            recommended_instances[item["role"]] = (
                int(recommended_instances.get(item["role"], 0)) + int(item["count"])
            )
        for item in remove_instances:
            recommended_instances[item["role"]] = max(
                0,
                int(recommended_instances.get(item["role"], 0)) - int(item["count"]),
            )

        return {
            "ok": True,
            "model": (
                "durable_swarm_graph"
                if compiled_graph.get("active")
                else "durable_task_graph"
            ),
            "graph_semantics": {
                "conditional_edge": (
                    "task state plus explicit SwarmGraph gates select dispatch/wait_graph/"
                    "graph_unselected/wait_dependencies/wait_retry/leased/interrupt/terminal"
                ),
                "explicit_graph": (
                    "an activated durable SwarmGraph can bind tasks to graph nodes, hard-pin "
                    "a role when explicitly requested, and gate dispatch through dependency/conditional edges"
                ),
                "capability_routing": (
                    "ordinary owner/preferred_role is soft; required_capabilities selects compatible role profiles, "
                    "and already-online compatible capacity is preferred before scale-out"
                ),
                "resource_intent": (
                    "declared write/exclusive resources prevent obviously conflicting task dispatch before MVCC lane creation; "
                    "MVCC remains the final publication/conflict boundary"
                ),
                "send_map": (
                    "fan-out shards can target compatible workers across role profiles with different payload state"
                ),
                "reducer": "group results are aggregated after shard completion",
                "fan_in": "dependent tasks wait until every dependency is done",
                "interrupt": "blocked tasks are surfaced to the main orchestrator",
                "lease": "claim/heartbeat prevents duplicate shard execution",
                "retry": "retryable failures return to ready until max_attempts",
                "sidecar": (
                    "JIT assist tasks run beside a current primary task; readonly/proposal/verification "
                    "sidecars do not compete for the primary write-set, while disjoint_write sidecars "
                    "must pass hierarchical MVCC claims"
                ),
            },
            "active_roles": normalized_active,
            "active_instances": active_instances,
            "task_nodes": nodes,
            "dependency_edges": edges,
            "swarm_graph": compiled_graph,
            "parallel_frontier": frontier,
            "parallel_sidecars": parallel_sidecars,
            "sidecar_opportunities": sidecar_opportunities,
            "fan_in_barriers": barriers,
            "waits": waits,
            "interrupts": interrupts,
            "capability_gaps": capability_gaps,
            "role_pressure": ranked,
            "scale_recommendation": {
                "needs_scale": bool(add_instances or remove_instances),
                "needs_scale_out": bool(add_instances),
                "needs_scale_in": bool(remove_instances),
                "add_roles": add_roles,
                "add_instances": add_instances,
                "remove_roles": remove_roles,
                "remove_instances": remove_instances,
                "max_new_workers": add_limit,
                "max_instances_per_role": per_role_limit,
                "max_total_workers": worker_budget,
                "effective_worker_budget": effective_budget,
                "recommended_instances": recommended_instances,
                "basis": (
                    "desired instances only from capability-compatible current claimable/leased/actionable work, including "
                    "explicit JIT sidecars; existing compatible online capacity is reused first and dependency/resource-wait "
                    "future tasks contribute zero worker pressure. Global budget preserves live leased capacity and excess "
                    "instances may scale in safely"
                ),
            },
        }

    def ensure_parallelism(
        self,
        max_sidecars_per_primary: int = 2,
        max_new_sidecars: int = 4,
    ) -> Dict[str, object]:
        """Materialize bounded safe JIT sidecars for runnable primaries.

        This is intentionally conservative: automatic helpers are restricted to
        readonly/proposal_only/experiment_only policies. Canonical or disjoint
        writes are never auto-created, so one primary writer remains authoritative.
        """
        per_primary = max(0, min(int(max_sidecars_per_primary or 0), 2))
        total_limit = max(0, min(int(max_new_sidecars or 0), 8))
        if per_primary <= 0 or total_limit <= 0:
            return {"ok": True, "created": [], "count": 0}

        coordination = self.coordination_plan()
        if not coordination.get("ok"):
            return coordination

        safe_policies = {"readonly", "proposal_only", "experiment_only"}
        created = []
        skipped = []
        opportunities = [
            item
            for item in coordination.get("sidecar_opportunities", [])
            if isinstance(item, dict)
        ]

        # Prefer natural graph/map parallelism over synthetic helpers. Automatic
        # sidecars are a single-primary anti-serialization mechanism, not extra
        # load when multiple primary tasks are already runnable in parallel.
        if len(opportunities) != 1:
            return {
                "ok": True,
                "created": [],
                "count": 0,
                "skipped": [{
                    "reason": (
                        "natural_parallel_frontier"
                        if len(opportunities) > 1
                        else "no_sidecar_opportunity"
                    ),
                    "primary_count": len(opportunities),
                }],
            }

        for opportunity in opportunities:
            if len(created) >= total_limit:
                break
            if not isinstance(opportunity, dict):
                continue
            parent_task_id = str(opportunity.get("parent_task_id") or "").strip()
            if not parent_task_id:
                continue

            # Never keep re-spawning helpers for the same primary. If the primary
            # needs another round later, the orchestrator can create it explicitly.
            if self.store.task_assists(parent_task_id, include_terminal=True):
                skipped.append({
                    "parent_task_id": parent_task_id,
                    "reason": "assist_history_exists",
                })
                continue

            parent_title = str(opportunity.get("title") or parent_task_id)
            canonical_root = str(opportunity.get("canonical_root") or "")
            resource_hints = list(opportunity.get("resource_hints") or [])
            made_for_parent = 0

            for suggestion in opportunity.get("suggestions", []):
                if len(created) >= total_limit or made_for_parent >= per_primary:
                    break
                if not isinstance(suggestion, dict):
                    continue
                write_policy = str(suggestion.get("write_policy") or "").strip().lower()
                if write_policy not in safe_policies:
                    skipped.append({
                        "parent_task_id": parent_task_id,
                        "reason": "unsafe_auto_write_policy",
                        "write_policy": write_policy,
                    })
                    continue

                assist_kind = str(suggestion.get("assist_kind") or "").strip().lower()
                role = str(suggestion.get("role") or "").strip().lower()
                if role not in DEFAULT_ROLES or not assist_kind:
                    continue
                purpose = str(suggestion.get("purpose") or "").strip()
                stable = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "web-agent-swarm:{0}:{1}:{2}".format(
                        parent_task_id,
                        assist_kind,
                        role,
                    ),
                ).hex[:10]
                task_id = "assist_auto_" + stable
                try:
                    task = self.store.task_create(
                        title="Auto {0}: {1}".format(assist_kind, parent_title),
                        description=purpose,
                        owner=role,
                        task_id=task_id,
                        parent_task_id=parent_task_id,
                        assist_parent_task_id=parent_task_id,
                        assist_kind=assist_kind,
                        assist_write_policy=write_policy,
                        resource_hints=resource_hints,
                        canonical_root=canonical_root,
                        payload={
                            "auto_sidecar": True,
                            "purpose": purpose,
                            "primary_task_id": parent_task_id,
                        },
                        max_attempts=2,
                    )
                except ValueError as exc:
                    if "task_id already exists" in str(exc):
                        continue
                    raise
                created.append(task)
                made_for_parent += 1

        return {
            "ok": True,
            "created": created,
            "count": len(created),
            "skipped": skipped,
            "policy": (
                "Auto fan-out is bounded and safe: only readonly/proposal_only/"
                "experiment_only sidecars are materialized; canonical writes remain "
                "single-writer and disjoint_write requires explicit orchestration."
            ),
        }

    def elastic_plan(
        self,
        max_new_roles: int = 6,
        max_instances_per_role: int = 4,
        max_total_workers: int = 6,
    ) -> Dict[str, object]:
        """Recommend missing-role and same-role runtime instances without browser mutation."""
        coordination = self.coordination_plan(
            active_roles=None,
            max_new_roles=max_new_roles,
            max_instances_per_role=max_instances_per_role,
            max_total_workers=max_total_workers,
        )
        if not coordination.get("ok"):
            return coordination
        recommendation = coordination.get("scale_recommendation") or {}
        return {
            "ok": True,
            "phase": "elastic_plan",
            "active_roles": list(coordination.get("active_roles") or []),
            "active_instances": dict(coordination.get("active_instances") or {}),
            "needs_scale": bool(recommendation.get("needs_scale")),
            "needs_scale_out": bool(recommendation.get("needs_scale_out")),
            "needs_scale_in": bool(recommendation.get("needs_scale_in")),
            "add_roles": list(recommendation.get("add_roles") or []),
            "add_instances": list(recommendation.get("add_instances") or []),
            "remove_roles": list(recommendation.get("remove_roles") or []),
            "remove_instances": list(recommendation.get("remove_instances") or []),
            "max_total_workers": int(recommendation.get("max_total_workers") or max_total_workers),
            "effective_worker_budget": int(
                recommendation.get("effective_worker_budget") or max_total_workers
            ),
            "coordination": coordination,
            "parallel_sidecars": list(coordination.get("parallel_sidecars") or []),
            "sidecar_opportunities": list(coordination.get("sidecar_opportunities") or []),
            "policy": (
                "Scale out from durable runnable-shard pressure and leases under the global "
                "worker budget; scale in only durably idle workers after hysteresis plus "
                "lease/superstep/UI safety checks. Browser idle alone is never a scale signal."
            ),
        }

    def _tabs(self) -> List[Dict[str, object]]:
        result = self.bridge.tabs()
        if not result.get("ok"):
            return []
        return [
            item for item in result.get("tabs", [])
            if isinstance(item, dict)
            and str(item.get("url") or "").startswith("https://chatgpt.com/")
        ]

    def _resolve(self, name: str) -> Optional[Dict[str, object]]:
        binding = self.store.bindings().get(name)
        if not isinstance(binding, dict) or binding.get("suspended"):
            return None
        cid = str(binding.get("conversation_id") or "")
        if not cid:
            return None
        matches = [
            tab for tab in self._tabs()
            if conversation_id(str(tab.get("url") or "")) == cid
        ]
        # Multiple open tabs for one registered conversation are ambiguous:
        # selecting the active one is not an authorization to pick a target.
        if len(matches) != 1:
            return None
        return matches[0]

    def _bind(self, name: str, tab: Dict[str, object], kind: str, role: str = "") -> None:
        url = str(tab.get("url") or "")
        cid = conversation_id(url)
        if not cid:
            raise RuntimeError("conversation URL is not stable yet")
        self.store.bind(name, {
            "kind": kind,
            "role": role,
            "capabilities": role_capabilities(role),
            "conversation_id": cid,
            "url_at_bind": url,
            "title_at_bind": str(tab.get("title") or ""),
        })

    @staticmethod
    def _spawn_url(tabs: List[Dict[str, object]]) -> str:
        for tab in tabs:
            url = str(tab.get("url") or "")
            if url.startswith("https://chatgpt.com/g/"):
                return url.split("/c/", 1)[0] if "/c/" in url else url
        return "https://chatgpt.com/"

    @staticmethod
    def _worker_prompt(role: str, worker_name: str = "") -> str:
        name = str(worker_name or role)
        return (
            "You are Web Agent worker instance {0}. Role profile: {1}. Responsibility: {2} "
            "Your role is a capability profile, not exclusive task ownership. A task owner/preferred_role is a scheduling hint; "
            "you may claim any durable frontier task whose compatible_roles includes your profile, while explicit SwarmGraph role pins remain hard. "
            "Use durable task state, not chat memory, as coordination truth. Before doing a "
            "concrete task, claim it with swarm_task_claim(task_id, worker_name=\"{0}\"); "
            "heartbeat long work and normally release it with result/evidence when done. If "
            "the main agent assigns a superstep_id, claim the task with that superstep_id and "
            "do NOT release it directly: keep the lease alive and stage task_release/status_set "
            "operations with swarm_superstep_stage; "
            "on failure call swarm_superstep_fail. The main agent alone commits the ready barrier. "
            "Multiple workers may share your role, so never work an unclaimed task in parallel "
            "with another instance. If this is an assist task, obey assist_write_policy strictly: "
            "readonly/proposal_only/experiment_only sidecars must not take canonical write claims; "
            "same-file assistance returns evidence, tests, or a concrete proposal to the primary writer. "
            "Only disjoint_write sidecars may write project files, and their MVCC write claims must be "
            "non-overlapping with the primary. For any repository mutation or experiment with filesystem "
            "outputs, create an MVCC lane with swarm_lane_start after the task lease, using read/scratch "
            "claims for non-writing sidecars and write claims only when policy permits. Open the returned "
            "lane workspace in CodexPro and mutate/run commands only there; canonical mutation is guarded. "
            "Heartbeat long lanes, call swarm_lane_commit before task_release, and on conflict preserve "
            "the old lane and use swarm_lane_fork/rebase rather than overwriting. For expensive deterministic "
            "sub-steps (large tests, benchmarks, downloads, external API/agent phases), first call "
            "swarm_task_step_fingerprint with stable structured inputs/resource paths, then pass its fingerprint "
            "to swarm_task_step_acquire: reuse means consume the saved result without re-running, wait means "
            "another worker owns the same step, and execute/resume "
            "means heartbeat and finish it with swarm_task_step_complete. If a task carries "
            "payload/group_id, treat it as one map shard and return a compact reducer-friendly result. "
            "Be work-conserving: after releasing one task, immediately re-read the coordination plan and "
            "claim the next capability-compatible eligible task for this worker in the same turn when useful; stop only "
            "when no eligible work remains, a real blocker exists, or the turn budget is nearly exhausted. "
            "If CodexPro-style local development tools are connected, use them for concrete workspace "
            "work and verification. Start by reading the coordination plan and claimable task frontier."
        ).format(name, role, DEFAULT_ROLES[role])

    @staticmethod
    def _main_prompt(roles: List[str]) -> str:
        initial_roles = ", ".join(roles) if roles else "none (main-only bootstrap)"
        return (
            "You are the main Web Agent orchestrator. Initial active worker roles: {0}. "
            "Read web_agent_swarm_snapshot and web_agent_swarm_coordination_plan first. Treat role as a soft capability profile: "
            "dispatch by required_capabilities + resource intent + dependencies, prefer already-online compatible worker slots, and only scale out when compatible free capacity is insufficient. "
            "owner/preferred_role is not exclusive ownership; coordination_plan.assigned_role is the current routing decision. If an "
            "active SwarmGraph exists, treat it as the explicit durable execution topology: obey "
            "its wait_graph/graph_unselected gates, use swarm_graph_* tools for topology/state "
            "changes, and pass expected_revision when coordinating concurrent graph edits. If this "
            "is main-only bootstrap, use web_agent_swarm_elastic_plan and web_agent_swarm_scale "
            "to create only the worker topology justified by the durable parallel frontier. Never open "
            "workers for future dependency-wait stages. Safe JIT sidecars may already have been "
            "materialized automatically for a single runnable primary; inspect parallel_sidecars first and "
            "never duplicate them. If sidecar_opportunities remain and genuinely orthogonal work still exists, "
            "create at most one or two explicit helpers with swarm_task_assist. Prefer readonly_analysis, "
            "verification, or disjoint_write sidecars. If a helper needs to inspect the same files the primary "
            "is writing, make it "
            "proposal_only/readonly: one primary writer owns that write-set and the helper returns evidence, "
            "tests, or a concrete patch proposal rather than competing for the same file. Do not create "
            "sidecars merely to keep tabs busy. Treat task status plus dependency and SwarmGraph truth as graph routing: dispatch independent "
            "parallel_frontier work, respect fan_in_barriers, and treat blocked tasks as "
            "interrupts. For user input, use durable swarm_interrupt_create/resume semantics "
            "rather than chat memory. For divisible work, use swarm_task_fanout to create "
            "Send-style map shards with payloads; multiple instances of the same role may claim "
            "separate shards concurrently, and swarm_task_reduce aggregates completed shard "
            "results. For one coordinated round whose outputs must be all-or-nothing, call "
            "swarm_superstep_begin with exact worker instances, stage task_release/status_set "
            "writes, and commit only when ready_to_commit; any worker failure aborts the round. "
            "Workers must claim/lease tasks before concrete work. Repository-changing work must "
            "also use a resource-scoped MVCC lane: assign canonical_root/resources, require lane-only "
            "CodexPro mutation/experiments, and require swarm_lane_commit before task release. Resource "
            "claims are hierarchical, and commit validates base/read/write versions; conflict must fork "
            "or rebase and never overwrite canonical. Use web_agent_swarm_elastic_plan "
            "to compare durable work pressure with active instance counts, then apply "
            "web_agent_swarm_scale for recommended additions/removals under the global worker "
            "budget. Automatic scale-in uses hysteresis and must preserve bindings and refuse "
            "workers with live leases/supersteps/generation. Treat a bound unresponsive/white-screen tab as transport lifecycle debt: "
            "confirm it once, refresh the exact same conversation before any rollover, preserve durable ownership, and escalate only if refresh fails. "
            "Treat conversation-length exhaustion as a different transport lifecycle debt: replace main or the exact worker instance with a fresh conversation, preserve the same worker name/role and any durable live lease, "
            "bind the fresh conversation before closing the exhausted old tab, and never release or duplicate work merely because chat history ended. "
            "Never scale merely because a browser tab is idle. Use swarm_checkpoint_create before risky task-graph or SwarmGraph "
            "rewrites; checkpoints include graph topology/activation and restore only when no live "
            "lease/superstep exists. Do not interrupt generating workers. "
            "Send one concise instruction per durable next action and avoid duplicate instructions. "
            "If CodexPro-style local development tools are available, use them for concrete "
            "repository work and verification rather than guessing."
        ).format(initial_roles)

    def _send_when_ready(
        self,
        tab_id: int,
        text: str,
        timeout: float = 20.0,
        command_id: str = "",
        expected_url: str = "",
        relay_permit: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        # Retain timeout in the public signature; it must never justify a
        # second potentially effectful /send after a lost callback. The bridge
        # can return ok=False even when the content script has clicked Send.
        active_command_id = str(command_id or "").strip() or uuid.uuid4().hex
        inspected = self.bridge.inspect(tab_id, tail=1)
        if inspected.get("ok"):
            snapshot = inspected.get("snapshot") or {}
            if snapshot.get("generating"):
                return {
                    "ok": False,
                    "busy": True,
                    "stuck_generating": bool(snapshot.get("stuckGenerating")),
                    "command_id": active_command_id,
                    "error": "tab is generating",
                }
        # Exactly one outbound bridge call per permitted runtime invocation.
        # A retry, including one reusing command_id, requires external proof
        # that the prior invocation had no physical Send effect.
        if not expected_url:
            return {
                "ok": False,
                "error": "Pinned conversation URL is required for browser Send.",
                "command_id": active_command_id,
                "attempts": 0,
            }
        bridge_kwargs = {
            "command_id": active_command_id, "expected_url": expected_url,
        }
        if relay_permit is not None:
            bridge_kwargs["relay_permit"] = dict(relay_permit)
        result = self.bridge.send(tab_id, text, **bridge_kwargs)
        result["command_id"] = active_command_id
        result["attempts"] = 1
        return result

    def _wait_conversation(self, tab_id: int, timeout: float = 20.0) -> Optional[Dict[str, object]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for tab in self._tabs():
                if int(tab.get("id", -1)) == int(tab_id):
                    if conversation_id(str(tab.get("url") or "")):
                        return tab
            time.sleep(0.5)
        return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError):
            return False

    def ensure_watcher(
        self,
        interval: float = 300.0,
        max_silence: float = 300.0,
    ) -> Dict[str, object]:
        desired = {
            "interval": max(2.0, float(interval)),
            "max_silence": max(0.0, float(max_silence)),
        }
        pid = 0
        if self.watcher_pid_path.is_file():
            try:
                pid = int(self.watcher_pid_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pid = 0

        restarted = False
        if pid and self._pid_alive(pid):
            current = {}
            if self.watcher_config_path.is_file():
                try:
                    loaded = json.loads(self.watcher_config_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        current = loaded
                except (OSError, ValueError):
                    current = {}
            if current == desired:
                return {
                    "ok": True,
                    "already_running": True,
                    "pid": pid,
                    "config": desired,
                    "log": str(self.watcher_log_path),
                }

            restarted = True
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            deadline = time.time() + 2.0
            while time.time() < deadline and self._pid_alive(pid):
                time.sleep(0.05)
            if self._pid_alive(pid):
                return {
                    "ok": False,
                    "error": "watcher config changed but old watcher could not be stopped",
                    "pid": pid,
                    "config": current,
                }

        self.store.root.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "web_agent_swarm.watcher",
            "--interval",
            str(desired["interval"]),
            "--max-silence",
            str(desired["max_silence"]),
        ]
        kwargs = {
            "cwd": os.getcwd(),
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True

        with self.watcher_log_path.open("ab") as log_handle:
            process = subprocess.Popen(
                command,
                stdout=log_handle,
                stderr=log_handle,
                **kwargs
            )
        self.watcher_pid_path.write_text(str(process.pid) + "\n", encoding="utf-8")
        self.watcher_config_path.write_text(
            json.dumps(desired, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {
            "ok": True,
            "already_running": False,
            "restarted": restarted,
            "pid": process.pid,
            "config": desired,
            "log": str(self.watcher_log_path),
        }

    def _worker_suspend_guard(self, name: str) -> Dict[str, object]:
        clean_name = str(name or "").strip()
        if not clean_name or clean_name == "main":
            return {
                "ok": False,
                "name": clean_name,
                "reasons": ["main_or_invalid_worker"],
            }

        now = time.time()
        tasks = self.store.tasks()
        leased_tasks = [
            str(task.get("task_id") or "")
            for task in tasks
            if isinstance(task, dict)
            and str(task.get("status") or "").lower() == "active"
            and str(task.get("claimed_by") or "") == clean_name
            and float(task.get("lease_expires_at") or 0.0) > now
        ]
        active_steps = [
            str(step.get("step_id") or "")
            for step in self.store.supersteps(include_pending=False)
            if isinstance(step, dict)
            and step.get("status") in {"open", "ready"}
            and clean_name in list(step.get("participants") or [])
        ]
        reasons = []
        if leased_tasks:
            reasons.append("active_task_lease")
        if active_steps:
            reasons.append("active_superstep")
        return {
            "ok": not reasons,
            "name": clean_name,
            "reasons": reasons,
            "leased_task_ids": leased_tasks,
            "active_supersteps": active_steps,
        }

    def suspend(self, name: str, close_tab: bool = True) -> Dict[str, object]:
        clean_name = str(name or "").strip()
        binding = self.store.bindings().get(clean_name)
        if clean_name == "main" or not isinstance(binding, dict):
            return {
                "ok": False,
                "name": clean_name,
                "error": "worker binding not found or main cannot be suspended",
            }
        if binding.get("suspended"):
            return {
                "ok": True,
                "name": clean_name,
                "suspended": True,
                "already_suspended": True,
                "tab_closed": False,
            }

        guard = self._worker_suspend_guard(clean_name)
        if not guard.get("ok"):
            return {
                "ok": False,
                "name": clean_name,
                "safe_to_suspend": False,
                "guard": guard,
                "error": "worker owns an active durable lease/superstep",
            }

        tab = self._resolve(clean_name)
        if tab:
            inspected = self.bridge.inspect(int(tab["id"]), tail=1)
            if not inspected.get("ok"):
                return {
                    "ok": False,
                    "name": clean_name,
                    "safe_to_suspend": False,
                    "error": inspected.get("error") or "worker tab state could not be inspected",
                }
            snapshot = inspected.get("snapshot") or {}
            if snapshot.get("generating"):
                return {
                    "ok": False,
                    "name": clean_name,
                    "safe_to_suspend": False,
                    "stuck_generating": bool(snapshot.get("stuckGenerating")),
                    "error": "worker tab is generating",
                }
            if close_tab:
                closed = self.bridge.close_tab(int(tab["id"]))
                if not closed.get("ok"):
                    return {
                        "ok": False,
                        "name": clean_name,
                        "safe_to_suspend": False,
                        "error": closed.get("error") or "failed to close worker tab",
                    }

        updated = dict(binding)
        updated["suspended"] = True
        updated["suspended_at"] = time.time()
        if tab and str(tab.get("url") or ""):
            updated["url_at_bind"] = str(tab.get("url") or "")
        self.store.bind(clean_name, updated)
        return {
            "ok": True,
            "name": clean_name,
            "role": str(updated.get("role") or ""),
            "suspended": True,
            "tab_closed": bool(tab and close_tab),
            "guard": guard,
        }

    def _spawn_worker_instance(
        self,
        role: str,
        target_url: str,
    ) -> Dict[str, object]:
        clean_role = str(role or "").strip().lower()
        if clean_role not in DEFAULT_ROLES:
            return {"ok": False, "error": "unknown role: {0}".format(clean_role)}
        bindings = self.store.bindings()
        suspended_names = sorted(
            str(name)
            for name, binding in bindings.items()
            if isinstance(binding, dict)
            and binding.get("suspended")
            and str(binding.get("role") or "").strip().lower() == clean_role
        )
        name = (
            suspended_names[0]
            if suspended_names
            else self._next_worker_name(clean_role, bindings)
        )
        existing = bindings.get(name)
        reopen_url = (
            str(existing.get("url_at_bind") or "")
            if isinstance(existing, dict)
            else ""
        )
        opened = self.bridge.open_tab(reopen_url or target_url, active=False)
        if not opened.get("ok") or not opened.get("tab"):
            return {
                "ok": False,
                "name": name,
                "role": clean_role,
                "action": "resume_failed" if reopen_url else "create_failed",
                "error": opened.get("error"),
            }
        tab = opened["tab"]
        sent = self._send_when_ready(
            int(tab["id"]),
            self._worker_prompt(clean_role, name),
            expected_url=str(tab.get("url") or ""),
        )
        if not sent.get("ok"):
            return {
                "ok": False,
                "name": name,
                "role": clean_role,
                "action": "resumed" if reopen_url else "created",
                "error": sent.get("error"),
            }
        stable = self._wait_conversation(int(tab["id"]))
        if not stable:
            return {
                "ok": False,
                "name": name,
                "role": clean_role,
                "action": "resumed" if reopen_url else "created",
                "error": "conversation URL did not stabilize",
            }
        self._bind(name, stable, "worker", clean_role)
        return {
            "ok": True,
            "name": name,
            "role": clean_role,
            "kind": "worker",
            "action": "resumed" if reopen_url else "created",
            "tab": stable,
        }

    def start(
        self,
        roles: Optional[List[str]] = None,
        instances: Optional[Dict[str, int]] = None,
        spawn_url: str = "",
        start_watch: bool = True,
        interval: float = 300.0,
        max_silence: float = 300.0,
    ) -> Dict[str, object]:
        selected = []
        target_counts = {}
        for role in roles or []:
            clean = str(role or "").strip().lower()
            if clean not in DEFAULT_ROLES:
                return {"ok": False, "error": "unknown role: {0}".format(clean)}
            if clean not in selected:
                selected.append(clean)
            target_counts[clean] = max(1, int(target_counts.get(clean, 0)))

        for role, count in (instances or {}).items():
            clean = str(role or "").strip().lower()
            if clean not in DEFAULT_ROLES:
                return {"ok": False, "error": "unknown role: {0}".format(clean)}
            target = max(0, min(int(count or 0), 16))
            if target > 0 and clean not in selected:
                selected.append(clean)
            if target > 0:
                target_counts[clean] = target
            else:
                target_counts.pop(clean, None)
                if clean in selected:
                    selected.remove(clean)

        desired_worker_names = set()
        for role in selected:
            count = max(1, int(target_counts.get(role, 1)))
            target_counts[role] = count
            for index in range(1, count + 1):
                desired_worker_names.add(self._worker_instance_name(role, index))

        current = self.store.bindings()
        deactivated = []
        deactivation_blocked = []
        for stale, binding in current.items():
            if stale == "main" or stale in desired_worker_names:
                continue
            if isinstance(binding, dict) and binding.get("suspended"):
                continue
            suspended = self.suspend(stale, close_tab=True)
            if suspended.get("ok"):
                deactivated.append(stale)
            else:
                deactivation_blocked.append({
                    "name": stale,
                    "error": suspended.get("error"),
                    "guard": suspended.get("guard"),
                    "stuck_generating": suspended.get("stuck_generating", False),
                })

        tabs = self._tabs()
        target = str(spawn_url or "").strip() or self._spawn_url(tabs)
        if not target.startswith("https://chatgpt.com/"):
            return {"ok": False, "error": "spawn_url must be a chatgpt.com URL"}

        desired = [("main", "main", "")]
        for role in selected:
            for index in range(1, int(target_counts[role]) + 1):
                desired.append(
                    (
                        self._worker_instance_name(role, index),
                        "worker",
                        role,
                    )
                )
        records = []

        for name, kind, role in desired:
            binding = self.store.bindings().get(name)
            tab = self._resolve(name)
            action = "reused" if tab else "created"

            if not tab:
                reopen_url = (
                    str(binding.get("url_at_bind") or "")
                    if isinstance(binding, dict)
                    else ""
                )
                opened = self.bridge.open_tab(reopen_url or target, active=False)
                if not opened.get("ok") or not opened.get("tab"):
                    records.append({
                        "name": name,
                        "ok": False,
                        "action": "reopen_failed" if reopen_url else "create_failed",
                        "error": opened.get("error"),
                    })
                    continue
                tab = opened["tab"]
                action = "reopened" if reopen_url else "created"
                prompt = (
                    self._main_prompt(selected)
                    if kind == "main"
                    else self._worker_prompt(role, name)
                )
                sent = self._send_when_ready(
                    int(tab["id"]), prompt, expected_url=str(tab.get("url") or "")
                )
                if not sent.get("ok"):
                    records.append({
                        "name": name,
                        "ok": False,
                        "action": action,
                        "error": sent.get("error"),
                    })
                    continue
                stable = self._wait_conversation(int(tab["id"]))
                if not stable:
                    records.append({
                        "name": name,
                        "ok": False,
                        "action": action,
                        "error": "conversation URL did not stabilize",
                    })
                    continue
                tab = stable
                self._bind(name, tab, kind, role)
            else:
                prompt = self._main_prompt(selected) if kind == "main" else (
                    "Continue as worker instance {0} with role {1}. Re-read the durable "
                    "coordination plan. Claim one eligible task using your exact worker name "
                    "before concrete work; heartbeat/release the lease and do not repeat "
                    "completed or currently leased work."
                ).format(name, role)
                sent = self._send_when_ready(
                    int(tab["id"]), prompt, timeout=3.0,
                    expected_url=str(tab.get("url") or ""),
                )
                if not sent.get("ok") and not sent.get("busy"):
                    records.append({
                        "name": name,
                        "ok": False,
                        "action": action,
                        "error": sent.get("error"),
                    })
                    continue

            records.append({
                "name": name,
                "role": role,
                "kind": kind,
                "ok": True,
                "action": action,
                "tab": tab,
            })

        watcher = (
            self.ensure_watcher(interval=interval, max_silence=max_silence)
            if start_watch
            else {"ok": True, "started": False}
        )
        total_workers = sum(int(target_counts[role]) for role in selected)
        return {
            "ok": (
                all(item.get("ok") for item in records)
                and watcher.get("ok", False)
                and not deactivation_blocked
            ),
            "bootstrap_mode": "main_only" if not selected else "explicit_workers",
            "worker_roles": selected,
            "worker_instances": {
                role: int(target_counts[role])
                for role in selected
            },
            "worker_count": total_workers,
            "total_web_agents": total_workers + 1,
            "deactivated_workers": deactivated,
            "scale_in_blocked": deactivation_blocked,
            "agents": records,
            "watcher": watcher,
        }

    def scale(
        self,
        requested_roles: Optional[List[str]] = None,
        requested_instances: Optional[Dict[str, int]] = None,
        worker_count: int = 0,
        max_new_roles: int = 6,
        max_instances_per_role: int = 4,
        max_total_workers: int = 6,
        scale_in_grace_seconds: float = 300.0,
        spawn_url: str = "",
        start_watch: bool = True,
        interval: float = 300.0,
        max_silence: float = 300.0,
    ) -> Dict[str, object]:
        """Scale worker instances in both directions using durable-pressure targets."""
        bindings = self.store.bindings()
        if "main" not in bindings:
            return {
                "ok": False,
                "error": "main swarm binding is missing; call web_agent_swarm_start first",
            }

        current_map = self._bound_worker_instances()
        current_counts = {
            role: len(current_map.get(role, []))
            for role in DEFAULT_ROLES
        }
        target_counts = dict(current_counts)
        elastic = None
        parallelism = None
        immediate_wait_scale_in_roles = set()
        automatic_scaling = (
            requested_instances is None
            and not requested_roles
            and not int(worker_count or 0)
        )
        grace_seconds = max(0.0, float(scale_in_grace_seconds or 0.0))

        if requested_instances is not None:
            for role, count in requested_instances.items():
                clean = str(role or "").strip().lower()
                if clean not in DEFAULT_ROLES:
                    return {"ok": False, "error": "unknown role: {0}".format(clean)}
                target_counts[clean] = max(0, min(int(count or 0), 16))
        elif requested_roles:
            for role in requested_roles:
                clean = str(role or "").strip().lower()
                if clean not in DEFAULT_ROLES:
                    return {"ok": False, "error": "unknown role: {0}".format(clean)}
                target_counts[clean] = max(1, int(target_counts.get(clean, 0)))
        elif int(worker_count or 0):
            plan = plan_roles(self.store.tasks(), worker_count=worker_count)
            if not plan.get("ok"):
                return plan
            for role in plan["roles"]:
                target_counts[role] = max(1, int(target_counts.get(role, 0)))
        else:
            parallelism = self.ensure_parallelism(
                max_sidecars_per_primary=2,
                max_new_sidecars=max(2, min(int(max_new_roles or 0), 4)),
            )
            if not parallelism.get("ok"):
                return parallelism
            elastic = self.elastic_plan(
                max_new_roles=max_new_roles,
                max_instances_per_role=max_instances_per_role,
                max_total_workers=max_total_workers,
            )
            if not elastic.get("ok"):
                return elastic
            elastic_coordination = elastic.get("coordination") or {}
            recommended = (
                elastic_coordination
                .get("scale_recommendation", {})
                .get("recommended_instances", {})
            )
            wait_routes = {
                "wait_dependencies",
                "wait_external",
                "wait_verification",
                "wait_graph",
            }
            for role in DEFAULT_ROLES:
                role_nodes = [
                    node
                    for node in elastic_coordination.get("task_nodes", [])
                    if isinstance(node, dict)
                    and str(node.get("role") or "") == role
                    and str(node.get("status") or "") not in {
                        "done", "failed", "cancelled"
                    }
                ]
                if role_nodes and all(
                    str(node.get("route") or "") in wait_routes
                    for node in role_nodes
                ):
                    immediate_wait_scale_in_roles.add(role)

            for role in DEFAULT_ROLES:
                target_counts[role] = max(
                    0,
                    min(int(recommended.get(role, current_counts.get(role, 0)) or 0), 16),
                )

        additions = {
            role: max(0, int(target_counts[role]) - int(current_counts[role]))
            for role in DEFAULT_ROLES
        }
        removals = {
            role: max(0, int(current_counts[role]) - int(target_counts[role]))
            for role in DEFAULT_ROLES
        }

        removed_workers = []
        scale_in_blocked = []
        scale_in_deferred = []
        scale_in_skipped = []
        now = time.time()

        for role in DEFAULT_PRIORITY:
            if int(removals.get(role, 0)) > 0:
                continue
            for name in current_map.get(role, []):
                binding = self.store.bindings().get(name)
                if isinstance(binding, dict) and binding.get("excess_since"):
                    updated = dict(binding)
                    updated.pop("excess_since", None)
                    self.store.bind(name, updated)

        for role in DEFAULT_PRIORITY:
            remaining = int(removals.get(role, 0))
            if remaining <= 0:
                continue
            candidates = list(reversed(current_map.get(role, [])))
            for name in candidates:
                if remaining <= 0:
                    break

                guard = self._worker_suspend_guard(name)
                if not guard.get("ok"):
                    scale_in_skipped.append({
                        "name": name,
                        "role": role,
                        "error": "worker has a live durable lease/superstep",
                        "guard": guard,
                    })
                    continue

                if (
                    automatic_scaling
                    and grace_seconds > 0
                    and role not in immediate_wait_scale_in_roles
                ):
                    binding = self.store.bindings().get(name)
                    since = (
                        float(binding.get("excess_since") or 0.0)
                        if isinstance(binding, dict)
                        else 0.0
                    )
                    if since <= 0.0:
                        updated = dict(binding or {})
                        updated["excess_since"] = now
                        self.store.bind(name, updated)
                        since = now
                    elapsed = max(0.0, now - since)
                    if elapsed < grace_seconds:
                        scale_in_deferred.append({
                            "name": name,
                            "role": role,
                            "excess_since": since,
                            "elapsed_seconds": elapsed,
                            "grace_seconds": grace_seconds,
                            "retry_after": since + grace_seconds,
                        })
                        remaining -= 1
                        continue

                result = self.suspend(name, close_tab=True)
                if result.get("ok"):
                    removed_workers.append(name)
                    remaining -= 1
                else:
                    scale_in_skipped.append({
                        "name": name,
                        "role": role,
                        "error": result.get("error"),
                        "guard": result.get("guard"),
                        "stuck_generating": result.get("stuck_generating", False),
                    })
            if remaining > 0:
                scale_in_blocked.append({
                    "role": role,
                    "count_unmet": remaining,
                    "error": "not enough safe worker instances were available for scale-in",
                })

        requested_additions = [
            {"role": role, "count": int(count)}
            for role, count in additions.items()
            if int(count) > 0
        ]
        records = []
        if requested_additions:
            tabs = self._tabs()
            target = str(spawn_url or "").strip() or self._spawn_url(tabs)
            if not target.startswith("https://chatgpt.com/"):
                return {"ok": False, "error": "spawn_url must be a chatgpt.com URL"}
            for item in requested_additions:
                for _ in range(int(item["count"])):
                    records.append(self._spawn_worker_instance(item["role"], target))

        watcher = (
            self.ensure_watcher(interval=interval, max_silence=max_silence)
            if start_watch
            else {"ok": True, "started": False}
        )
        final_map = self._bound_worker_instances()
        final_counts = {
            role: len(final_map.get(role, []))
            for role in DEFAULT_ROLES
        }
        added_workers = [
            str(item.get("name") or "")
            for item in records
            if item.get("ok")
        ]
        actual_by_role = []
        for role in DEFAULT_PRIORITY:
            count = sum(
                1
                for item in records
                if item.get("ok") and item.get("role") == role
            )
            if count:
                actual_by_role.append({"role": role, "count": count})

        return {
            "ok": (
                all(item.get("ok") for item in records)
                and watcher.get("ok", False)
                and not scale_in_blocked
            ),
            "scaled": bool(added_workers or removed_workers),
            "added_roles": [
                item["role"]
                for item in actual_by_role
                if current_counts.get(item["role"], 0) == 0
            ],
            "added_workers": added_workers,
            "added_instances": actual_by_role,
            "removed_workers": removed_workers,
            "removed_instances": [
                {
                    "role": role,
                    "count": max(0, current_counts[role] - final_counts[role]),
                }
                for role in DEFAULT_PRIORITY
                if current_counts[role] > final_counts[role]
            ],
            "scale_in_blocked": scale_in_blocked,
            "scale_in_deferred": scale_in_deferred,
            "scale_in_skipped": scale_in_skipped,
            "scale_in_grace_seconds": grace_seconds,
            "immediate_wait_scale_in_roles": sorted(immediate_wait_scale_in_roles),
            "max_total_workers": max(1, min(int(max_total_workers or 1), 32)),
            "requested_target_instances": target_counts,
            "previous_worker_instances": current_counts,
            "worker_roles": self._bound_worker_roles(),
            "worker_instances": final_counts,
            "worker_count": sum(final_counts.values()),
            "total_web_agents": sum(final_counts.values()) + 1,
            "agents": records,
            "watcher": watcher,
            "parallelism": parallelism,
            "coordination": self.coordination_plan(max_total_workers=max_total_workers),
        }

    def _record_send_health(
        self,
        name: str,
        result: Dict[str, object],
        elapsed_seconds: float,
    ) -> Dict[str, object]:
        clean_name = str(name)
        binding = self.store.bindings().get(clean_name)
        if not isinstance(binding, dict):
            return {}
        updated = dict(binding)
        previous = updated.get("send_health")
        previous = previous if isinstance(previous, dict) else {}
        ok = bool(result.get("ok"))
        error = str(result.get("error") or "")
        http_status = int(result.get("http_status") or 0)
        ack_timeout = (
            http_status == 504
            or "Timed out waiting for extension response" in error
            or "bridge unavailable" in error.lower()
            or "timed out" in error.lower()
        )
        health = {
            "last_attempt_at": time.time(),
            "last_ok": ok,
            "consecutive_failures": (
                0 if ok else int(previous.get("consecutive_failures") or 0) + 1
            ),
            "last_error": "" if ok else error,
            "last_http_status": http_status,
            "last_command_id": str(result.get("command_id") or ""),
            "last_attempts": int(result.get("attempts") or 0),
            "last_latency_ms": int(max(0.0, elapsed_seconds) * 1000),
            "ack_timeout": bool(ack_timeout),
            # A non-timeout HTTP 502 can follow a real content click.
            # The extension explicitly propagates mayHaveExecuted for this
            # case; never downgrade its evidence to a safe pre-effect failure.
            "may_have_executed": bool(
                ok or ack_timeout
                or result.get("mayHaveExecuted") is True
                or result.get("may_have_executed") is True
            ),
        }
        updated["send_health"] = health
        self.store.bind(clean_name, updated)
        return health

    def send(self, name: str, text: str,
             relay_permit: Optional[Dict[str, object]] = None) -> Dict[str, object]:
        clean_name = str(name)
        tab = self._resolve(clean_name)
        if not tab:
            return {"ok": False, "error": "bound conversation is not open"}
        if relay_permit is not None:
            if (not isinstance(relay_permit, dict)
                    or relay_permit.get("commandId") != "r8_" + str(relay_permit.get("attemptId") or "")
                    or relay_permit.get("tabId") != int(tab["id"])
                    or relay_permit.get("expectedUrl") != str(tab.get("url") or "")):
                return {"ok": False, "error": "R8 signed permit does not match resolved tab"}
            command_id = str(relay_permit["commandId"])
        else:
            command_id = uuid.uuid4().hex
        self.store.send_transaction_record(
            command_id,
            name=clean_name,
            tab_id=int(tab["id"]),
            status="attempting",
            relay_attempt_id=str((relay_permit or {}).get("attemptId") or ""),
            relay_recipient=str((relay_permit or {}).get("recipient") or ""),
            relay_generation=int((relay_permit or {}).get("generation") or 0),
            body_sha256=str((relay_permit or {}).get("bodySha256") or ""),
            expected_url=str((relay_permit or {}).get("expectedUrl") or ""),
        )
        started = time.monotonic()
        result = self._send_when_ready(
            int(tab["id"]),
            text,
            timeout=24.0,
            command_id=command_id,
            expected_url=str(tab.get("url") or ""),
            relay_permit=relay_permit,
        )
        result["name"] = clean_name
        result["send_health"] = self._record_send_health(
            clean_name, result, time.monotonic() - started
        )
        health = result["send_health"]
        status = (
            "ok" if result.get("ok")
            else "busy" if result.get("busy")
            else "ack_timeout" if health.get("ack_timeout")
            else "unknown" if health.get("may_have_executed")
            else "failed"
        )
        result["send_transaction"] = self.store.send_transaction_record(
            command_id,
            name=clean_name,
            tab_id=int(tab["id"]),
            status=status,
            result=result,
            may_have_executed=bool(health.get("may_have_executed")),
        )
        return result

    def rollover_main_if_exhausted(self) -> Dict[str, object]:
        """Replace an exhausted main ChatGPT conversation without mutating durable work."""
        from .watchdogs import conversation_limit_reached

        old_binding = self.store.bindings().get("main")
        old_tab = self._resolve("main")
        if not isinstance(old_binding, dict) or not old_tab:
            return {
                "ok": False,
                "rolled_over": False,
                "error": "main binding is unavailable",
            }

        inspected = self.bridge.inspect(int(old_tab["id"]), tail=3)
        if not inspected.get("ok"):
            return {
                "ok": False,
                "rolled_over": False,
                "error": inspected.get("error") or "main inspect failed",
            }
        if not conversation_limit_reached(inspected.get("snapshot") or {}):
            return {
                "ok": True,
                "rolled_over": False,
                "reason": "conversation_not_exhausted",
            }

        active_roles = self._bound_worker_roles()
        old_url = str(old_tab.get("url") or old_binding.get("url_at_bind") or "")
        target = (
            old_url.split("/c/", 1)[0]
            if old_url.startswith("https://chatgpt.com/") and "/c/" in old_url
            else self._spawn_url(self._tabs())
        )
        if not target.startswith("https://chatgpt.com/"):
            target = "https://chatgpt.com/"

        opened = self.bridge.open_tab(target, active=False)
        new_tab = opened.get("tab") if opened.get("ok") else None
        if not new_tab:
            return {
                "ok": False,
                "rolled_over": False,
                "error": opened.get("error") or "failed to open fresh main tab",
            }

        prompt = (
            "The watcher created this fresh main conversation because the previous main "
            "reached ChatGPT's conversation-length limit. Do not depend on old chat memory; "
            "treat the durable WebAgentSwarm store/graph as coordination truth and resume from it. "
            + self._main_prompt(active_roles)
        )
        sent = self._send_when_ready(
            int(new_tab["id"]), prompt, timeout=10.0,
            expected_url=str(new_tab.get("url") or ""),
        )
        if not sent.get("ok"):
            try:
                self.bridge.close_tab(int(new_tab["id"]))
            except Exception:
                pass
            return {
                "ok": False,
                "rolled_over": False,
                "error": sent.get("error") or "failed to bootstrap fresh main conversation",
            }

        stable = self._wait_conversation(int(new_tab["id"]), timeout=60.0)
        if not stable:
            try:
                self.bridge.close_tab(int(new_tab["id"]))
            except Exception:
                pass
            return {
                "ok": False,
                "rolled_over": False,
                "error": "fresh main conversation URL did not stabilize",
            }

        old_conversation_id = str(old_binding.get("conversation_id") or "")
        self._bind("main", stable, "main", "")
        current = self.store.bindings().get("main") or {}
        updated = dict(current)
        updated["rollover_count"] = int(old_binding.get("rollover_count") or 0) + 1
        updated["rollover_from_conversation_id"] = old_conversation_id
        updated["rolled_over_at"] = time.time()
        self.store.bind("main", updated)

        try:
            closed = self.bridge.close_tab(int(old_tab["id"]))
        except Exception as exc:
            closed = {"ok": False, "error": str(exc)}
        self._record_rollover_close_result("main", int(old_tab["id"]), closed)

        return {
            "ok": True,
            "rolled_over": True,
            "old_tab_id": int(old_tab["id"]),
            "new_tab": stable,
            "old_conversation_id": old_conversation_id,
            "new_conversation_id": conversation_id(str(stable.get("url") or "")),
            "active_roles": active_roles,
            "old_tab_closed": bool(closed.get("ok")),
            "close_error": None if closed.get("ok") else closed.get("error"),
        }

    def _record_rollover_close_result(
        self,
        name: str,
        old_tab_id: int,
        closed: Dict[str, object],
    ) -> None:
        binding = self.store.bindings().get(str(name))
        if not isinstance(binding, dict):
            return
        updated = dict(binding)
        if bool((closed or {}).get("ok")):
            updated.pop("rollover_close_pending_tab_id", None)
            updated.pop("rollover_close_error", None)
        else:
            updated["rollover_close_pending_tab_id"] = int(old_tab_id)
            updated["rollover_close_error"] = str((closed or {}).get("error") or "close failed")
        self.store.bind(str(name), updated)

    def cleanup_rollover_close_debt(self) -> Dict[str, object]:
        """Retry closing exhausted tabs left behind by a successful conversation rollover."""
        tab_ids = {int(tab.get("id", -1)) for tab in self._tabs()}
        outcomes = []
        for name, binding in self.store.bindings().items():
            if not isinstance(binding, dict):
                continue
            pending = int(binding.get("rollover_close_pending_tab_id") or 0)
            if pending <= 0:
                continue
            if pending not in tab_ids:
                closed = {"ok": True, "already_closed": True, "tabId": pending}
            else:
                try:
                    closed = self.bridge.close_tab(pending)
                except Exception as exc:
                    closed = {"ok": False, "error": str(exc), "tabId": pending}
            self._record_rollover_close_result(str(name), pending, closed)
            outcomes.append({
                "name": str(name),
                "tab_id": pending,
                "closed": bool(closed.get("ok")),
                "error": None if closed.get("ok") else closed.get("error"),
            })
        return {"ok": all(item["closed"] for item in outcomes), "outcomes": outcomes}

    def cleanup_stale_managed_tabs(self, limit: int = 32) -> Dict[str, object]:
        """Close retired AutoL/WAS conversations that are still open as stale tabs.

        Only durable retirement history is trusted. Bound tabs, active tabs and
        generating/non-idle tabs are protected, so ordinary user chats are never
        inferred to be stale from title/text alone.
        """
        state = self.store.load()
        bindings = self.store.bindings()
        bound_cids = {
            str(binding.get("conversation_id") or "")
            for binding in bindings.values()
            if isinstance(binding, dict) and str(binding.get("conversation_id") or "")
        }
        retired = {
            str(item.get("conversation_id") or ""): item
            for item in state.get("retired_conversations", [])
            if isinstance(item, dict) and str(item.get("conversation_id") or "")
        }
        closed = []
        skipped = []
        tabs = self._tabs()
        candidates = []
        for tab in tabs:
            cid = conversation_id(str(tab.get("url") or ""))
            if (
                not cid
                or cid in bound_cids
                or cid not in retired
                or bool(tab.get("active"))
            ):
                continue
            candidates.append((tab, cid))
        candidates.sort(key=lambda item: (int(item[0].get("windowId", 0)), int(item[0].get("index", 0))))
        for tab, cid in candidates[:max(0, min(int(limit or 0), 64))]:
            tab_id = int(tab.get("id", -1))
            inspected = self.bridge.inspect(tab_id, tail=1)
            if not inspected.get("ok"):
                skipped.append({"tab_id": tab_id, "conversation_id": cid, "reason": "inspect_failed"})
                continue
            ui = inspected.get("snapshot") or {}
            if bool(ui.get("generating")) or str(ui.get("uiState") or "") != "idle":
                skipped.append({"tab_id": tab_id, "conversation_id": cid, "reason": "not_idle"})
                continue
            try:
                result = self.bridge.close_tab(tab_id)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            if result.get("ok"):
                closed.append({
                    "tab_id": tab_id,
                    "conversation_id": cid,
                    "name": str(retired[cid].get("name") or ""),
                    "role": str(retired[cid].get("role") or ""),
                    "reason": "retired_binding_stale_tab_gc",
                    "durable_ownership_mutated": False,
                })
            else:
                skipped.append({
                    "tab_id": tab_id,
                    "conversation_id": cid,
                    "reason": "close_failed",
                    "error": result.get("error"),
                })
        return {
            "ok": True,
            "closed": closed,
            "skipped": skipped,
            "policy": "durable-retired + unbound + inactive + idle + non-generating only",
        }

    def rollover_worker_if_exhausted(self, name: str) -> Dict[str, object]:
        """Replace one exhausted worker conversation without changing its durable worker identity."""
        from .watchdogs import conversation_limit_reached

        clean_name = str(name or "").strip()
        if not clean_name or clean_name == "main":
            return {"ok": False, "rolled_over": False, "error": "worker name is required"}
        old_binding = self.store.bindings().get(clean_name)
        old_tab = self._resolve(clean_name)
        if not isinstance(old_binding, dict) or old_binding.get("suspended") or not old_tab:
            return {
                "ok": False,
                "rolled_over": False,
                "name": clean_name,
                "error": "worker binding is unavailable or suspended",
            }
        role = str(old_binding.get("role") or "").strip().lower()
        if role not in DEFAULT_ROLES:
            return {"ok": False, "rolled_over": False, "name": clean_name, "error": "worker role is invalid"}

        inspected = self.bridge.inspect(int(old_tab["id"]), tail=3)
        if not inspected.get("ok"):
            return {
                "ok": False,
                "rolled_over": False,
                "name": clean_name,
                "role": role,
                "error": inspected.get("error") or "worker inspect failed",
            }
        if not conversation_limit_reached(inspected.get("snapshot") or {}):
            return {
                "ok": True,
                "rolled_over": False,
                "name": clean_name,
                "role": role,
                "reason": "conversation_not_exhausted",
            }

        old_url = str(old_tab.get("url") or old_binding.get("url_at_bind") or "")
        target = (
            old_url.split("/c/", 1)[0]
            if old_url.startswith("https://chatgpt.com/") and "/c/" in old_url
            else self._spawn_url(self._tabs())
        )
        if not target.startswith("https://chatgpt.com/"):
            target = "https://chatgpt.com/"
        opened = self.bridge.open_tab(target, active=False)
        new_tab = opened.get("tab") if opened.get("ok") else None
        if not new_tab:
            return {
                "ok": False,
                "rolled_over": False,
                "name": clean_name,
                "role": role,
                "error": opened.get("error") or "failed to open fresh worker tab",
            }

        prompt = (
            "The watcher created this fresh conversation because worker instance {0} reached "
            "ChatGPT's conversation-length limit. This is transport replacement only: keep the exact "
            "worker identity {0} and role {1}; do not release, duplicate, steal, or recreate durable work solely "
            "because the old conversation ended. Before claiming anything, inspect durable tasks for a live "
            "lease already claimed_by {0}; resume and heartbeat that exact work when present. Do not depend on "
            "old chat memory. "
        ).format(clean_name, role) + self._worker_prompt(role, clean_name)
        sent = self._send_when_ready(
            int(new_tab["id"]), prompt, timeout=10.0,
            expected_url=str(new_tab.get("url") or ""),
        )
        if not sent.get("ok"):
            try:
                self.bridge.close_tab(int(new_tab["id"]))
            except Exception:
                pass
            return {
                "ok": False,
                "rolled_over": False,
                "name": clean_name,
                "role": role,
                "error": sent.get("error") or "failed to bootstrap fresh worker conversation",
            }
        stable = self._wait_conversation(int(new_tab["id"]), timeout=60.0)
        if not stable:
            try:
                self.bridge.close_tab(int(new_tab["id"]))
            except Exception:
                pass
            return {
                "ok": False,
                "rolled_over": False,
                "name": clean_name,
                "role": role,
                "error": "fresh worker conversation URL did not stabilize",
            }

        old_conversation_id = str(old_binding.get("conversation_id") or "")
        self._bind(clean_name, stable, "worker", role)
        current = dict(self.store.bindings().get(clean_name) or {})
        updated = dict(old_binding)
        updated.update(current)
        updated.pop("suspended", None)
        updated.pop("suspended_at", None)
        updated.pop("send_health", None)
        updated["rollover_count"] = int(old_binding.get("rollover_count") or 0) + 1
        updated["rollover_from_conversation_id"] = old_conversation_id
        updated["rolled_over_at"] = time.time()
        updated["transport_replacement_only"] = True
        self.store.bind(clean_name, updated)

        try:
            closed = self.bridge.close_tab(int(old_tab["id"]))
        except Exception as exc:
            closed = {"ok": False, "error": str(exc)}
        self._record_rollover_close_result(clean_name, int(old_tab["id"]), closed)

        return {
            "ok": True,
            "rolled_over": True,
            "name": clean_name,
            "role": role,
            "old_tab_id": int(old_tab["id"]),
            "new_tab": stable,
            "old_conversation_id": old_conversation_id,
            "new_conversation_id": conversation_id(str(stable.get("url") or "")),
            "old_tab_closed": bool(closed.get("ok")),
            "close_error": None if closed.get("ok") else closed.get("error"),
            "durable_ownership_mutated": False,
        }

    def rollover_exhausted(self, name: str) -> Dict[str, object]:
        clean_name = str(name or "").strip()
        if clean_name == "main":
            return self.rollover_main_if_exhausted()
        return self.rollover_worker_if_exhausted(clean_name)

    @staticmethod
    def _inspect_is_unresponsive(result: Dict[str, object]) -> bool:
        error = str((result or {}).get("error") or "")
        state = str((result or {}).get("browserState") or "").strip()
        lowered = error.lower()
        return (
            state == "unresponsive"
            or "Timed out waiting for extension response" in error
            or "receiving end does not exist" in lowered
            or "message port closed" in lowered
        )

    def recover_unresponsive_binding(self, name: str) -> Dict[str, object]:
        """Refresh one confirmed-unresponsive bound tab without changing ownership.

        A stale snapshot alone is never enough: WAS performs a fresh confirmation
        inspect first. Only a second unresponsive result is allowed to recycle the
        exact same conversation URL into a fresh renderer/tab.
        """
        clean_name = str(name or "").strip()
        binding = self.store.bindings().get(clean_name)
        if not clean_name or not isinstance(binding, dict):
            return {"ok": False, "reloaded": False, "error": "binding unavailable"}
        if binding.get("suspended"):
            return {
                "ok": True,
                "reloaded": False,
                "name": clean_name,
                "reason": "binding_suspended",
                "durable_ownership_mutated": False,
            }
        tab = self._resolve(clean_name)
        if not tab:
            return {
                "ok": False,
                "reloaded": False,
                "name": clean_name,
                "reason": "bound_tab_not_open",
                "durable_ownership_mutated": False,
            }
        original_cid = str(binding.get("conversation_id") or "")
        confirm = self.bridge.inspect(int(tab["id"]), tail=1)
        if confirm.get("ok"):
            return {
                "ok": True,
                "reloaded": False,
                "name": clean_name,
                "reason": "confirm_probe_recovered",
                "durable_ownership_mutated": False,
            }
        if not self._inspect_is_unresponsive(confirm):
            return {
                "ok": False,
                "reloaded": False,
                "name": clean_name,
                "reason": "inspect_failed_nonreloadable",
                "error": confirm.get("error"),
                "durable_ownership_mutated": False,
            }

        refreshed = self.bridge.refresh_tab(int(tab["id"]))
        if not refreshed.get("ok"):
            return {
                "ok": False,
                "reloaded": False,
                "name": clean_name,
                "reason": "refresh_failed",
                "error": refreshed.get("error"),
                "durable_ownership_mutated": False,
            }

        after = self._resolve(clean_name)
        after_cid = conversation_id(str((after or {}).get("url") or ""))
        if not after or after_cid != original_cid:
            return {
                "ok": False,
                "reloaded": True,
                "recovered": False,
                "name": clean_name,
                "reason": "conversation_changed_after_refresh",
                "original_conversation_id": original_cid,
                "current_conversation_id": after_cid,
                "durable_ownership_mutated": False,
            }
        verified = self.bridge.inspect(int(after["id"]), tail=1)
        recovered = bool(verified.get("ok"))
        current = dict(self.store.bindings().get(clean_name) or binding)
        current["last_reload_recovery_at"] = time.time()
        current["reload_recovery_count"] = int(current.get("reload_recovery_count") or 0) + 1
        current["last_reload_recovery_ok"] = recovered
        current["last_reload_recovery_error"] = "" if recovered else str(verified.get("error") or "")
        self.store.bind(clean_name, current)
        return {
            "ok": recovered,
            "reloaded": True,
            "recovered": recovered,
            "name": clean_name,
            "tab_id": int(after["id"]),
            "conversation_id": original_cid,
            "error": None if recovered else verified.get("error"),
            "durable_ownership_mutated": False,
        }

    def snapshot(self, tail: int = 4) -> Dict[str, object]:
        bindings = self.store.bindings()
        agents = []
        for name in sorted(bindings):
            tab = self._resolve(name)
            item = {
                "name": name,
                "binding": bindings[name],
                "open": bool(tab),
                "tab": tab,
                "ui": None,
                "error": None,
                "browser_state": (
                    "discarded"
                    if bool((tab or {}).get("discarded"))
                    else (
                        "frozen"
                        if bool((tab or {}).get("frozen"))
                        else "active"
                    )
                ),
            }
            if tab:
                inspected = self.bridge.inspect(int(tab["id"]), tail=tail)
                if inspected.get("ok"):
                    item["ui"] = inspected.get("snapshot")
                    inspected_tab = inspected.get("tab")
                    if isinstance(inspected_tab, dict):
                        item["tab"] = inspected_tab
                        item["browser_state"] = (
                            "discarded"
                            if bool(inspected_tab.get("discarded"))
                            else (
                                "frozen"
                                if bool(inspected_tab.get("frozen"))
                                else "active"
                            )
                        )
                else:
                    item["error"] = inspected.get("error")
                    state_hint = str(inspected.get("browserState") or "").strip()
                    if state_hint in {"discarded", "frozen", "unresponsive"}:
                        item["browser_state"] = state_hint
                    elif "Timed out waiting for extension response" in str(
                        inspected.get("error") or ""
                    ):
                        item["browser_state"] = "unresponsive"
            else:
                item["error"] = "bound conversation is not currently open"
                item["browser_state"] = "closed"
            agents.append(item)

        coordination = self.coordination_plan()
        kind_by_task = {
            str(node.get("task_id") or ""): str(node.get("blocker_kind") or "")
            for node in coordination.get("task_nodes", [])
            if isinstance(node, dict)
        }
        route_by_task = {
            str(node.get("task_id") or ""): str(node.get("route") or "")
            for node in coordination.get("task_nodes", [])
            if isinstance(node, dict)
        }
        tasks = []
        for task in self.store.tasks():
            item = dict(task)
            task_id = str(item.get("task_id") or "")
            if str(item.get("status") or "").lower() == "blocked":
                item["blocker_kind"] = kind_by_task.get(task_id) or self._blocker_kind(item)
            item["route"] = route_by_task.get(task_id) or "terminal"
            tasks.append(item)

        durable = self.store.load()
        from .watchdogs import conversation_limit_reached
        conversation_limit_agents = [
            {
                "name": item.get("name"),
                "role": str((item.get("binding") or {}).get("role") or ""),
                "browser_state": item.get("browser_state"),
            }
            for item in agents
            if conversation_limit_reached(item.get("ui"))
        ]
        return {
            "ok": True,
            "bridge": self.bridge.health(),
            "agents": agents,
            "tasks": tasks,
            "task_steps": [
                copy.deepcopy(record)
                for steps in durable.get("task_steps", {}).values()
                if isinstance(steps, dict)
                for record in steps.values()
                if isinstance(record, dict)
            ],
            "shared_status": self.store.status(),
            "supersteps": self.store.supersteps(include_pending=False),
            "interrupts": self.store.interrupts(),
            "checkpoints": self.store.checkpoints(),
            "graphs": self.store.graphs(),
            "active_graph": self.store.graph_get(),
            "runs": list(durable.get("runs", {}).values()),
            "lanes": list(durable.get("lanes", {}).values()),
            "resource_claims": list(durable.get("resource_claims", {}).values()),
            "commits": list(durable.get("commits", {}).values()),
            "resource_versions": durable.get("resource_versions", {}),
            "canonical_epoch": int(durable.get("canonical_epoch") or 0),
            "canonical_epochs": durable.get("canonical_epochs", {}),
            "coordination": coordination,
            "liveness": {
                "needs_main_rollover": any(
                    item.get("name") == "main" for item in conversation_limit_agents
                ),
                "needs_worker_rollover": any(
                    item.get("name") != "main" for item in conversation_limit_agents
                ),
                "conversation_limit_agents": conversation_limit_agents,
                "rollover_close_debt": [
                    {
                        "name": str(name),
                        "role": str((binding or {}).get("role") or ""),
                        "tab_id": int((binding or {}).get("rollover_close_pending_tab_id") or 0),
                        "error": str((binding or {}).get("rollover_close_error") or ""),
                    }
                    for name, binding in bindings.items()
                    if isinstance(binding, dict)
                    and int(binding.get("rollover_close_pending_tab_id") or 0) > 0
                ],
                "browser_suspended_agents": [
                    {
                        "name": item.get("name"),
                        "role": str((item.get("binding") or {}).get("role") or ""),
                        "browser_state": item.get("browser_state"),
                        "error": item.get("error"),
                    }
                    for item in agents
                    if item.get("browser_state") in {
                        "discarded",
                        "frozen",
                        "unresponsive",
                    }
                ],
                "semantics": (
                    "Browser suspended/unresponsive is a transport-liveness condition, "
                    "not Task completion or safe scale-in evidence."
                ),
            },
            "completion_truth": (
                "Browser idle means only that the page is not generating. Frozen, discarded, "
                "or unresponsive pages are transport faults, not completion. Use durable task "
                "state and real external verification for completion."
            ),
        }
