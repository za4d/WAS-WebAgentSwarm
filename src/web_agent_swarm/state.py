"""Small durable coordination store. No chat transcript is persisted by default."""

import copy
import hashlib
import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

from .graph import compile_graph, graph_validate, normalize_edge, normalize_node
from .mvcc import normalize_resource, resources_overlap
from .roles import DEFAULT_ROLES, normalize_capabilities, role_capabilities

ASSIST_KINDS = {
    "readonly_analysis": {
        "owner": "researcher",
        "write_policy": "readonly",
        "required_capabilities": ["analysis", "readonly"],
    },
    "verification": {
        "owner": "tester",
        "write_policy": "experiment_only",
        "required_capabilities": ["verification"],
    },
    "disjoint_write": {
        "owner": "implementer",
        "write_policy": "disjoint_write",
        "required_capabilities": ["implementation", "write"],
    },
    "proposal_only": {
        "owner": "reviewer",
        "write_policy": "proposal_only",
        "required_capabilities": ["review"],
    },
}
ASSIST_WRITE_POLICIES = {"readonly", "experiment_only", "disjoint_write", "proposal_only"}

CURRENT_SCHEMA_VERSION = 13


class DurableStateError(RuntimeError):
    """Base class for durable-state load/recovery failures."""


class DurableStateCorruptionError(DurableStateError):
    """Raised when neither primary nor backup durable state can be trusted."""


class DurableStateVersionError(DurableStateError):
    """Raised when state was written by a newer incompatible WAS version."""


def default_state_dir() -> Path:
    configured = os.environ.get("WEB_SWARM_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".web-agent-swarm").resolve()


class DurableStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = (root or default_state_dir()).resolve()
        self.path = self.root / "state.json"
        self.backup_path = self.root / "state.json.bak"
        self.checksum_path = self.root / "state.json.sha256"
        self.backup_checksum_path = self.root / "state.json.bak.sha256"
        self.lock_path = self.root / "state.lock"

    @contextmanager
    def _write_lock(self, timeout: float = 5.0):
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + max(0.1, float(timeout))
        fd = None
        while fd is None:
            try:
                fd = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                    if age > 30.0:
                        self.lock_path.unlink()
                        continue
                except OSError:
                    pass
                if time.time() >= deadline:
                    raise TimeoutError("timed out waiting for durable store write lock")
                time.sleep(0.02)
        try:
            os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            yield
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                self.lock_path.unlink()
            except OSError:
                pass

    def _empty(self) -> Dict[str, object]:
        return {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "bindings": {},
            "tasks": {},
            "task_steps": {},
            "task_resource_reservations": {},
            "checkpoint_head_id": "",
            "shared_status": {},
            "supersteps": {},
            "interrupts": {},
            "checkpoints": {},
            "graphs": {},
            "active_graph_id": "",
            "runs": {},
            "lanes": {},
            "resource_claims": {},
            "resource_versions": {},
            "commits": {},
            "canonical_epoch": 0,
            "canonical_epochs": {},
            "send_transactions": {},
            "retired_conversations": [],
        }

    @staticmethod
    def _sha256_bytes(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _read_checksum(path: Path) -> str:
        try:
            return path.read_text(encoding="ascii").strip().lower()
        except OSError:
            return ""

    def _read_state_file(self, path: Path, checksum_path: Path) -> Dict[str, object]:
        payload = path.read_bytes()
        expected = self._read_checksum(checksum_path)
        if expected and expected != self._sha256_bytes(payload):
            raise DurableStateCorruptionError(
                "checksum mismatch for {0}".format(path.name)
            )
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise DurableStateCorruptionError(
                "invalid JSON in {0}".format(path.name)
            ) from exc
        if not isinstance(data, dict):
            raise DurableStateCorruptionError(
                "durable state root must be an object: {0}".format(path.name)
            )
        return data

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))

    def _write_checksum(self, path: Path, payload: bytes) -> None:
        digest = (self._sha256_bytes(payload) + "\n").encode("ascii")
        self._atomic_write(path, digest)

    def _quarantine_primary(self) -> Optional[Path]:
        if not self.path.exists():
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.root / ("state.corrupt.{0}.json".format(stamp))
        suffix = 1
        while target.exists():
            target = self.root / (
                "state.corrupt.{0}.{1}.json".format(stamp, suffix)
            )
            suffix += 1
        os.replace(str(self.path), str(target))
        if self.checksum_path.exists():
            try:
                os.replace(
                    str(self.checksum_path),
                    str(target.with_suffix(target.suffix + ".sha256")),
                )
            except OSError:
                pass
        return target

    def _restore_backup(self) -> Dict[str, object]:
        data = self._read_state_file(self.backup_path, self.backup_checksum_path)
        payload = self.backup_path.read_bytes()
        self._quarantine_primary()
        self._atomic_write(self.path, payload)
        self._write_checksum(self.checksum_path, payload)
        return data

    @staticmethod
    def _migrate_step(data: Dict[str, object], source_version: int) -> Dict[str, object]:
        migrated = copy.deepcopy(data)
        target_version = source_version + 1
        if target_version == 9:
            migrated.setdefault("graphs", {})
            migrated.setdefault("active_graph_id", "")
        elif target_version == 10:
            migrated.setdefault("task_steps", {})
            migrated.setdefault("checkpoints", {})
            migrated.setdefault("checkpoint_head_id", "")
            migrated.setdefault("interrupts", {})
            migrated.setdefault("supersteps", {})
        elif target_version == 11:
            migrated.setdefault("runs", {})
            migrated.setdefault("lanes", {})
            migrated.setdefault("resource_claims", {})
            migrated.setdefault("resource_versions", {})
            migrated.setdefault("commits", {})
            migrated.setdefault("canonical_epoch", 0)
            migrated.setdefault("canonical_epochs", {})
        elif target_version == 12:
            migrated.setdefault("send_transactions", {})
        elif target_version == 13:
            migrated.setdefault("retired_conversations", [])
        migrated["schema_version"] = target_version
        return migrated

    def _migrate(self, data: Dict[str, object]) -> Dict[str, object]:
        raw_version = data.get("schema_version", 1)
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise DurableStateVersionError("schema_version must be an integer")
        if raw_version < 1:
            raise DurableStateVersionError("schema_version must be positive")
        if raw_version > CURRENT_SCHEMA_VERSION:
            raise DurableStateVersionError(
                "durable state schema {0} is newer than supported schema {1}".format(
                    raw_version, CURRENT_SCHEMA_VERSION
                )
            )
        migrated = copy.deepcopy(data)
        version = raw_version
        while version < CURRENT_SCHEMA_VERSION:
            migrated = self._migrate_step(migrated, version)
            version += 1
        return migrated

    def _normalize(self, data: Dict[str, object]) -> Dict[str, object]:
        base = self._empty()
        for key in base:
            if key == "schema_version":
                continue
            if isinstance(data.get(key), type(base[key])):
                base[key] = data[key]
        for task in base["tasks"].values():
            if isinstance(task, dict):
                deps = task.get("dependencies")
                task["dependencies"] = [
                    str(item) for item in (deps if isinstance(deps, list) else [])
                ]
                task.setdefault("payload", None)
                task.setdefault("group_id", "")
                task.setdefault("group_index", -1)
                task.setdefault("parent_task_id", "")
                task.setdefault("result", None)
                task.setdefault("superstep_id", "")
                task.setdefault("claimed_by", "")
                task.setdefault("lease_expires_at", 0.0)
                task.setdefault("attempts", 0)
                task.setdefault("max_attempts", 3)
                task.setdefault("last_error", "")
                task.setdefault("retry_after", 0.0)
                task.setdefault("interrupt_id", "")
                task.setdefault("assist_parent_task_id", "")
                task.setdefault("assist_kind", "")
                task.setdefault("assist_write_policy", "")
                task.setdefault("resource_hints", [])
                task.setdefault("canonical_root", "")
                task.setdefault(
                    "preferred_role",
                    str(task.get("owner") or "").strip().lower(),
                )
                task["required_capabilities"] = normalize_capabilities(
                    task.get("required_capabilities") or []
                )
                task.setdefault("read_resources", [])
                task.setdefault("write_resources", [])
                task.setdefault("exclusive_resources", [])
                task.setdefault("parallelizable", True)
                task.setdefault("shard_strategy", "")
        for graph_id, graph in list(base["graphs"].items()):
            if not isinstance(graph, dict):
                base["graphs"].pop(graph_id, None)
                continue
            graph.setdefault("graph_id", str(graph_id))
            graph.setdefault("name", str(graph_id))
            graph.setdefault("description", "")
            graph.setdefault("metadata", {})
            graph.setdefault("nodes", {})
            graph.setdefault("edges", {})
            graph.setdefault("revision", 1)
            graph.setdefault("created_at", 0.0)
            graph.setdefault("updated_at", 0.0)
        if base["active_graph_id"] and base["active_graph_id"] not in base["graphs"]:
            base["active_graph_id"] = ""
        base["schema_version"] = CURRENT_SCHEMA_VERSION
        return base

    def load(self) -> Dict[str, object]:
        if not self.path.is_file():
            return self._empty()
        try:
            data = self._read_state_file(self.path, self.checksum_path)
        except (OSError, DurableStateCorruptionError) as primary_error:
            try:
                data = self._restore_backup()
            except (OSError, DurableStateCorruptionError) as backup_error:
                raise DurableStateCorruptionError(
                    "durable state is corrupt and no valid backup is available: "
                    "primary={0}; backup={1}".format(primary_error, backup_error)
                ) from primary_error
        return self._normalize(self._migrate(data))

    def save(self, data: Dict[str, object]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        normalized = copy.deepcopy(data)
        normalized["schema_version"] = CURRENT_SCHEMA_VERSION
        payload = (
            json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

        if self.path.is_file():
            try:
                self._read_state_file(self.path, self.checksum_path)
                previous = self.path.read_bytes()
                self._atomic_write(self.backup_path, previous)
                self._write_checksum(self.backup_checksum_path, previous)
            except (OSError, DurableStateCorruptionError):
                # Never replace a known-good backup with an untrusted primary.
                pass

        self._atomic_write(self.path, payload)
        self._write_checksum(self.checksum_path, payload)

    def diagnostics(self) -> Dict[str, object]:
        result = {
            "ok": True,
            "state_path": str(self.path),
            "schema_version": CURRENT_SCHEMA_VERSION,
            "primary_exists": self.path.is_file(),
            "backup_exists": self.backup_path.is_file(),
            "checksum_present": self.checksum_path.is_file(),
            "error": "",
        }
        try:
            loaded = self.load()
            result["loaded_schema_version"] = int(
                loaded.get("schema_version") or CURRENT_SCHEMA_VERSION
            )
            result["task_count"] = len(loaded.get("tasks") or {})
            result["binding_count"] = len(loaded.get("bindings") or {})
        except DurableStateError as exc:
            result["ok"] = False
            result["error"] = str(exc)
        return result

    def send_transaction_record(
        self,
        command_id: str,
        *,
        name: str = "",
        tab_id: int = 0,
        status: str = "attempting",
        result: Optional[Dict[str, object]] = None,
        may_have_executed: bool = False,
        relay_attempt_id: str = "",
        relay_recipient: str = "",
        relay_generation: int = 0,
        body_sha256: str = "",
        expected_url: str = "",
    ) -> Dict[str, object]:
        clean = str(command_id or "").strip()[:128]
        if not clean:
            raise ValueError("command_id is required")
        clean_status = str(status or "attempting").strip().lower()
        if clean_status not in {"attempting", "ok", "failed", "ack_timeout", "unknown", "busy"}:
            raise ValueError("unsupported send transaction status")
        with self._write_lock():
            data = self.load()
            ledger = data["send_transactions"]
            previous = ledger.get(clean)
            previous = previous if isinstance(previous, dict) else {}
            # R05 immutable link: the diagnostic state cannot silently
            # reassign a previously journal-linked command to another permit.
            proposed = {
                "relay_attempt_id": str(relay_attempt_id or ""),
                "relay_recipient": str(relay_recipient or ""),
                "relay_generation": int(relay_generation or 0),
                "body_sha256": str(body_sha256 or ""),
                "expected_url": str(expected_url or ""),
            }
            for field, offered in proposed.items():
                old = previous.get(field)
                if old not in (None, "", 0) and offered not in (None, "", 0) and old != offered:
                    raise ValueError("immutable R8 Send diagnostic correlation mismatch: " + field)
            # Reviewer S DI-01: a terminal or possibly-effectful R8 receipt is
            # evidence, never a mutable reassignment target. A later separate
            # observation must not rewrite an uncertain original diagnostic.
            protected = bool(previous.get("relay_attempt_id") or
                             previous.get("may_have_executed"))
            if protected and previous:
                for field, offered in (
                    ("name", str(name or "")),
                    ("tab_id", int(tab_id or 0)),
                ):
                    old = previous.get(field)
                    if old not in (None, "", 0) and offered not in ("", 0) and old != offered:
                        raise ValueError("immutable R8 Send diagnostic target mismatch: " + field)
                old_status = str(previous.get("status") or "")
                if old_status and old_status != "attempting" and clean_status != old_status:
                    raise ValueError("regressive terminal R8 Send diagnostic status")
                old_result = previous.get("result")
                if (old_result is not None and isinstance(result, dict)
                        and result != old_result):
                    raise ValueError("immutable terminal R8 Send diagnostic result")
            now = time.time()
            record = {
                "command_id": clean,
                "name": str(name or previous.get("name") or ""),
                "tab_id": int(tab_id or previous.get("tab_id") or 0),
                "status": clean_status,
                "created_at": float(previous.get("created_at") or now),
                "updated_at": now,
                "may_have_executed": bool(may_have_executed or previous.get("may_have_executed")),
                # Diagnostic correlation only; never an independent R8 permit.
                "relay_attempt_id": str(relay_attempt_id or previous.get("relay_attempt_id") or ""),
                "relay_recipient": str(relay_recipient or previous.get("relay_recipient") or ""),
                "relay_generation": int(relay_generation or previous.get("relay_generation") or 0),
                "body_sha256": str(body_sha256 or previous.get("body_sha256") or ""),
                "expected_url": str(expected_url or previous.get("expected_url") or ""),
                "result": copy.deepcopy(result) if isinstance(result, dict) else copy.deepcopy(previous.get("result")),
            }
            ledger[clean] = record
            if len(ledger) > 512:
                ordered = sorted(
                    ledger.items(),
                    key=lambda item: float((item[1] or {}).get("updated_at") or 0.0),
                )
                # Best-effort 512-item limit for disposable, purely
                # pre-effect standalone diagnostics only. Never silently
                # evict a Relay-linked or possibly-effectful Send receipt:
                # those are incident evidence and lack proof of reconciliation.
                overflow = len(ledger) - 512
                for key, value in ordered:
                    if overflow <= 0:
                        break
                    if (isinstance(value, dict)
                            and (value.get("relay_attempt_id")
                                 or value.get("may_have_executed"))):
                        continue
                    ledger.pop(key, None)
                    overflow -= 1
            self.save(data)
            return copy.deepcopy(record)

    def send_transaction_get(self, command_id: str) -> Optional[Dict[str, object]]:
        clean = str(command_id or "").strip()[:128]
        record = self.load()["send_transactions"].get(clean)
        return copy.deepcopy(record) if isinstance(record, dict) else None

    def send_transactions(self, limit: int = 100) -> List[Dict[str, object]]:
        values = [
            copy.deepcopy(item)
            for item in self.load()["send_transactions"].values()
            if isinstance(item, dict)
        ]
        values.sort(key=lambda item: float(item.get("updated_at") or 0.0), reverse=True)
        return values[:max(1, min(int(limit or 100), 512))]

    def bind(self, name: str, binding: Dict[str, object]) -> Dict[str, object]:
        with self._write_lock():
            data = self.load()
            clean_name = str(name)
            updated = dict(binding)
            previous = data["bindings"].get(clean_name)
            if isinstance(previous, dict):
                old_cid = str(previous.get("conversation_id") or "")
                new_cid = str(updated.get("conversation_id") or "")
                if old_cid and new_cid and old_cid != new_cid:
                    retired = [
                        item for item in data.get("retired_conversations", [])
                        if isinstance(item, dict)
                        and str(item.get("conversation_id") or "") != old_cid
                    ]
                    retired.append({
                        "name": clean_name,
                        "role": str(previous.get("role") or ""),
                        "kind": str(previous.get("kind") or "worker"),
                        "conversation_id": old_cid,
                        "url": str(previous.get("url_at_bind") or ""),
                        "retired_at": time.time(),
                        "reason": "binding_replaced",
                    })
                    data["retired_conversations"] = retired[-512:]
            data["bindings"][clean_name] = updated
            self.save(data)
            return data["bindings"][clean_name]

    def unbind(self, name: str) -> bool:
        with self._write_lock():
            data = self.load()
            removed = data["bindings"].pop(str(name), None) is not None
            self.save(data)
            return removed

    def bindings(self) -> Dict[str, object]:
        return dict(self.load()["bindings"])

    @staticmethod
    def _task_resource_list(values: Optional[List[object]]) -> List[str]:
        result = []
        for raw in values or []:
            resource = str(raw.get("resource") or "") if isinstance(raw, dict) else str(raw or "")
            if not resource.strip():
                continue
            normalized = normalize_resource(resource)["resource"]
            if normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def _worker_role(data: Dict[str, object], worker_name: str) -> str:
        clean_worker = str(worker_name or "").strip().lower()
        binding = (data.get("bindings") or {}).get(clean_worker)
        if isinstance(binding, dict):
            role = str(binding.get("role") or "").strip().lower()
            if role in DEFAULT_ROLES:
                return role
        for role in DEFAULT_ROLES:
            if clean_worker == role or clean_worker.startswith(role + "-"):
                return role
        return ""

    @classmethod
    def _worker_capabilities(cls, data: Dict[str, object], worker_name: str) -> List[str]:
        clean_worker = str(worker_name or "").strip()
        binding = (data.get("bindings") or {}).get(clean_worker)
        if isinstance(binding, dict):
            explicit = normalize_capabilities(binding.get("capabilities") or [])
            if explicit:
                return explicit
        return role_capabilities(cls._worker_role(data, clean_worker))

    @staticmethod
    def _task_canonical_id(task: Dict[str, object]) -> str:
        root = str(task.get("canonical_root") or "").strip()
        if not root:
            return ""
        try:
            normalized = os.path.normcase(str(Path(root).expanduser().resolve()))
        except (OSError, RuntimeError, ValueError):
            normalized = root
        return "canon_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _task_resource_intents(cls, task: Dict[str, object]) -> List[Dict[str, str]]:
        intents = []
        seen = set()
        for mode, values in (("write", task.get("write_resources") or []), ("exclusive", task.get("exclusive_resources") or [])):
            for resource in cls._task_resource_list(values):
                normalized = normalize_resource(resource)
                key = (normalized["resource"], mode)
                if key in seen:
                    continue
                seen.add(key)
                normalized["mode"] = mode
                intents.append(normalized)
        return intents

    @staticmethod
    def _intent_conflicts(left: Dict[str, object], right: Dict[str, object]) -> bool:
        if not resources_overlap(left, right):
            return False
        modes = {str(left.get("mode") or "write"), str(right.get("mode") or "write")}
        if "scratch" in modes:
            return False
        if "exclusive" in modes:
            return True
        if modes == {"read"}:
            return False
        if str(left.get("namespace") or "") == "path" and str(right.get("namespace") or "") == "path" and modes == {"read", "write"}:
            return False
        return "write" in modes

    @classmethod
    def _reserve_task_resources(cls, data: Dict[str, object], task: Dict[str, object], worker_name: str, lease_expires_at: float, now: float) -> List[Dict[str, object]]:
        task_id = str(task.get("task_id") or "")
        canonical_id = cls._task_canonical_id(task)
        requested = cls._task_resource_intents(task)
        if not requested:
            return []
        conflicts = []
        for candidate in requested:
            for reservation in (data.get("task_resource_reservations") or {}).values():
                if not isinstance(reservation, dict) or reservation.get("status") != "active":
                    continue
                if float(reservation.get("lease_expires_at") or 0.0) <= now or str(reservation.get("task_id") or "") == task_id:
                    continue
                held_canonical = str(reservation.get("canonical_id") or "")
                if canonical_id and held_canonical and held_canonical != canonical_id:
                    continue
                if cls._intent_conflicts(candidate, reservation):
                    conflicts.append({"requested": candidate["resource"], "requested_mode": candidate["mode"], "held": str(reservation.get("resource") or ""), "held_mode": str(reservation.get("mode") or ""), "held_by_task": str(reservation.get("task_id") or ""), "held_by_worker": str(reservation.get("worker_name") or "")})
            for claim in (data.get("resource_claims") or {}).values():
                if not isinstance(claim, dict) or claim.get("status") != "active":
                    continue
                if float(claim.get("lease_expires_at") or 0.0) <= now:
                    continue
                held_canonical = str(claim.get("canonical_id") or "")
                if canonical_id and held_canonical and held_canonical != canonical_id:
                    continue
                if cls._intent_conflicts(candidate, claim):
                    conflicts.append({"requested": candidate["resource"], "requested_mode": candidate["mode"], "held": str(claim.get("resource") or ""), "held_mode": str(claim.get("mode") or ""), "held_by_run": str(claim.get("run_id") or "")})
        if conflicts:
            raise RuntimeError("task resource intent conflict: " + json.dumps(conflicts, ensure_ascii=False))
        reservations = []
        for candidate in requested:
            reservation_id = "taskres_" + uuid.uuid4().hex[:12]
            record = {"reservation_id": reservation_id, "task_id": task_id, "worker_name": str(worker_name or ""), "canonical_id": canonical_id, "resource": candidate["resource"], "namespace": candidate["namespace"], "path": candidate["path"], "mode": candidate["mode"], "status": "active", "lease_expires_at": float(lease_expires_at), "created_at": now, "updated_at": now}
            data["task_resource_reservations"][reservation_id] = record
            reservations.append(record)
        return reservations

    def task_create(
        self,
        title: str,
        description: str = "",
        owner: str = "",
        task_id: str = "",
        dependencies: Optional[List[str]] = None,
        payload: object = None,
        group_id: str = "",
        group_index: int = -1,
        parent_task_id: str = "",
        max_attempts: int = 3,
        assist_parent_task_id: str = "",
        assist_kind: str = "",
        assist_write_policy: str = "",
        resource_hints: Optional[List[object]] = None,
        canonical_root: str = "",
        preferred_role: str = "",
        required_capabilities: Optional[List[object]] = None,
        read_resources: Optional[List[object]] = None,
        write_resources: Optional[List[object]] = None,
        exclusive_resources: Optional[List[object]] = None,
        parallelizable: bool = True,
        shard_strategy: str = "",
    ) -> Dict[str, object]:
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("title is required")
        with self._write_lock():
            data = self.load()
            active_id = str(task_id or "").strip() or "task_" + uuid.uuid4().hex[:10]
            if active_id in data["tasks"]:
                raise ValueError("task_id already exists")
            deps = []
            for item in dependencies or []:
                dep = str(item or "").strip()
                if dep and dep not in deps:
                    deps.append(dep)
            missing = [dep for dep in deps if dep not in data["tasks"]]
            if missing:
                raise ValueError("unknown dependency task(s): {0}".format(", ".join(missing)))
            if active_id in deps:
                raise ValueError("task cannot depend on itself")
            clean_parent = str(parent_task_id or "").strip()
            if clean_parent and clean_parent not in data["tasks"]:
                raise ValueError("unknown parent_task_id")
            attempts_limit = max(1, int(max_attempts or 1))
            clean_assist_parent = str(assist_parent_task_id or "").strip()
            if clean_assist_parent and clean_assist_parent not in data["tasks"]:
                raise ValueError("unknown assist_parent_task_id")
            clean_assist_kind = str(assist_kind or "").strip().lower()
            if clean_assist_kind and clean_assist_kind not in ASSIST_KINDS:
                raise ValueError("unknown assist_kind: {0}".format(clean_assist_kind))
            clean_write_policy = str(assist_write_policy or "").strip().lower()
            if clean_write_policy and clean_write_policy not in ASSIST_WRITE_POLICIES:
                raise ValueError("unknown assist_write_policy: {0}".format(clean_write_policy))
            clean_owner = str(owner or "").strip().lower()
            clean_preferred = str(preferred_role or clean_owner).strip().lower()
            if clean_preferred and clean_preferred not in DEFAULT_ROLES:
                raise ValueError("unknown preferred_role: {0}".format(clean_preferred))
            clean_capabilities = normalize_capabilities(required_capabilities or [])
            if not clean_capabilities and clean_assist_kind:
                clean_capabilities = normalize_capabilities(
                    ASSIST_KINDS[clean_assist_kind].get("required_capabilities") or []
                )
            clean_read_resources = self._task_resource_list(read_resources)
            clean_write_resources = self._task_resource_list(write_resources)
            clean_exclusive_resources = self._task_resource_list(exclusive_resources)
            if clean_assist_kind and not (clean_read_resources or clean_write_resources):
                if clean_write_policy == "disjoint_write":
                    clean_write_resources = self._task_resource_list(resource_hints)
                elif clean_write_policy in {"readonly", "proposal_only", "experiment_only"}:
                    clean_read_resources = self._task_resource_list(resource_hints)
            task = {
                "task_id": active_id,
                "title": clean_title,
                "description": str(description or "").strip(),
                "owner": clean_owner,
                "preferred_role": clean_preferred,
                "required_capabilities": clean_capabilities,
                "read_resources": clean_read_resources,
                "write_resources": clean_write_resources,
                "exclusive_resources": clean_exclusive_resources,
                "parallelizable": bool(parallelizable),
                "shard_strategy": str(shard_strategy or "").strip().lower(),
                "dependencies": deps,
                "payload": payload,
                "group_id": str(group_id or "").strip(),
                "group_index": int(group_index),
                "parent_task_id": clean_parent,
                "status": "ready",
                "summary": "",
                "result": None,
                "superstep_id": "",
                "claimed_by": "",
                "lease_expires_at": 0.0,
                "attempts": 0,
                "max_attempts": attempts_limit,
                "last_error": "",
                "retry_after": 0.0,
                "interrupt_id": "",
                "assist_parent_task_id": clean_assist_parent,
                "assist_kind": clean_assist_kind,
                "assist_write_policy": clean_write_policy,
                "resource_hints": list(resource_hints or []),
                "canonical_root": str(canonical_root or "").strip(),
                "updated_at": time.time(),
            }
            data["tasks"][active_id] = task
            self.save(data)
            return dict(task)

    def task_assist(
        self,
        parent_task_id: str,
        title: str,
        description: str = "",
        owner: str = "",
        assist_kind: str = "readonly_analysis",
        write_policy: str = "",
        resource_hints: Optional[List[object]] = None,
        canonical_root: str = "",
        payload: object = None,
        max_attempts: int = 2,
    ) -> Dict[str, object]:
        """Create a parallel sidecar task for an already-runnable primary task."""
        clean_parent = str(parent_task_id or "").strip()
        clean_kind = str(assist_kind or "readonly_analysis").strip().lower()
        if clean_kind not in ASSIST_KINDS:
            raise ValueError("unknown assist_kind: {0}".format(clean_kind))
        defaults = ASSIST_KINDS[clean_kind]
        clean_owner = str(owner or defaults["owner"]).strip().lower()
        clean_policy = str(write_policy or defaults["write_policy"]).strip().lower()
        if clean_policy not in ASSIST_WRITE_POLICIES:
            raise ValueError("unknown assist_write_policy: {0}".format(clean_policy))
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("title is required")

        with self._write_lock():
            data = self.load()
            parent = data["tasks"].get(clean_parent)
            if not isinstance(parent, dict):
                raise KeyError("unknown parent_task_id")
            parent_status = str(parent.get("status") or "")
            if parent_status not in {"ready", "active", "review"}:
                raise RuntimeError(
                    "assist parent must be runnable/active, not {0}".format(parent_status)
                )
            unresolved = [
                str(dep)
                for dep in parent.get("dependencies", [])
                if data["tasks"].get(str(dep), {}).get("status") != "done"
            ]
            if unresolved:
                raise RuntimeError(
                    "assist parent still has unresolved dependencies: {0}".format(
                        ", ".join(unresolved)
                    )
                )
            active_id = "assist_" + uuid.uuid4().hex[:10]
            task = {
                "task_id": active_id,
                "title": clean_title,
                "description": str(description or "").strip(),
                "owner": clean_owner,
                "preferred_role": clean_owner,
                "required_capabilities": normalize_capabilities(defaults.get("required_capabilities") or []),
                "read_resources": self._task_resource_list(resource_hints if clean_policy in {"readonly", "proposal_only", "experiment_only"} else []),
                "write_resources": self._task_resource_list(resource_hints if clean_policy == "disjoint_write" else []),
                "exclusive_resources": [],
                "parallelizable": True,
                "shard_strategy": "sidecar",
                "dependencies": [],
                "payload": payload,
                "group_id": "",
                "group_index": -1,
                "parent_task_id": clean_parent,
                "assist_parent_task_id": clean_parent,
                "assist_kind": clean_kind,
                "assist_write_policy": clean_policy,
                "resource_hints": list(resource_hints or []),
                "canonical_root": str(canonical_root or "").strip(),
                "status": "ready",
                "summary": "",
                "result": None,
                "superstep_id": "",
                "claimed_by": "",
                "lease_expires_at": 0.0,
                "attempts": 0,
                "max_attempts": max(1, int(max_attempts or 1)),
                "last_error": "",
                "retry_after": 0.0,
                "interrupt_id": "",
                "updated_at": time.time(),
            }
            data["tasks"][active_id] = task
            self.save(data)
            return dict(task)

    def task_assists(
        self,
        parent_task_id: str,
        include_terminal: bool = True,
    ) -> List[Dict[str, object]]:
        """List sidecar tasks attached to one primary task."""
        clean_parent = str(parent_task_id or "").strip()
        result = []
        for task in self.load()["tasks"].values():
            if not isinstance(task, dict):
                continue
            if str(task.get("assist_parent_task_id") or "") != clean_parent:
                continue
            if not include_terminal and str(task.get("status") or "") in {
                "done", "failed", "cancelled"
            }:
                continue
            result.append(dict(task))
        return sorted(
            result,
            key=lambda item: (
                float(item.get("updated_at") or 0.0),
                str(item.get("task_id") or ""),
            ),
        )

    def task_step_fingerprint(
        self,
        task_id: str,
        step_key: str,
        inputs: object = None,
        resource_paths: Optional[List[str]] = None,
    ) -> Dict[str, object]:
        """Hash deterministic step inputs together with MVCC resource versions."""
        clean_task = str(task_id or "").strip()
        clean_step = str(step_key or "").strip()
        if not clean_task:
            raise ValueError("task_id is required")
        if not clean_step:
            raise ValueError("step_key is required")
        # Fail early if caller passes non-durable/non-JSON input state.
        json.dumps(inputs, ensure_ascii=False, sort_keys=True)

        data = self.load()
        task = data["tasks"].get(clean_task)
        if not isinstance(task, dict):
            raise KeyError("unknown task_id")

        canonical_root = str(task.get("canonical_root") or "").strip()
        canonical_id = ""
        if canonical_root:
            normalized_root = os.path.normcase(
                str(Path(canonical_root).expanduser().resolve())
            )
            canonical_id = (
                "canon_"
                + hashlib.sha256(normalized_root.encode("utf-8")).hexdigest()[:16]
            )
        paths = resource_paths
        if paths is None:
            paths = list(task.get("resource_hints") or [])
        clean_paths = sorted({
            str(path or "").strip().replace("\\", "/").strip("/")
            for path in (paths or [])
            if str(path or "").strip()
        })

        resource_state = []
        for path in clean_paths:
            version = (
                data["resource_versions"].get(canonical_id + "::" + path)
                if canonical_id
                else None
            )
            resource_state.append({
                "resource": path,
                "version": int((version or {}).get("version") or 0),
                "sha256": str((version or {}).get("sha256") or ""),
                "exists": (
                    bool(version.get("exists"))
                    if isinstance(version, dict) and "exists" in version
                    else None
                ),
            })

        basis = {
            "task_id": clean_task,
            "step_key": clean_step,
            "task_payload": copy.deepcopy(task.get("payload")),
            "canonical_id": canonical_id,
            "canonical_epoch": int(
                data.get("canonical_epochs", {}).get(canonical_id) or 0
            ) if canonical_id else 0,
            "resources": resource_state,
            "inputs": inputs,
        }
        raw = json.dumps(
            basis,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = "sha256:" + hashlib.sha256(raw).hexdigest()
        return {
            "task_id": clean_task,
            "step_key": clean_step,
            "input_fingerprint": fingerprint,
            "basis": basis,
        }

    def task_step_acquire(
        self,
        task_id: str,
        step_key: str,
        worker_name: str,
        input_fingerprint: str = "",
        lease_seconds: int = 900,
    ) -> Dict[str, object]:
        """Atomically reuse, wait for, resume, or acquire one durable task step."""
        clean_task = str(task_id or "").strip()
        clean_step = str(step_key or "").strip()
        clean_worker = str(worker_name or "").strip()
        fingerprint = str(input_fingerprint or "").strip()
        if not clean_task:
            raise ValueError("task_id is required")
        if not clean_step:
            raise ValueError("step_key is required")
        if len(clean_step) > 256:
            raise ValueError("step_key is too long")
        if not clean_worker:
            raise ValueError("worker_name is required")
        lease = max(1, min(int(lease_seconds or 1), 86400))

        with self._write_lock():
            data = self.load()
            if clean_task not in data["tasks"]:
                raise KeyError("unknown task_id")
            steps = data["task_steps"].setdefault(clean_task, {})
            current = steps.get(clean_step)
            now = time.time()

            if isinstance(current, dict):
                same_input = str(current.get("input_fingerprint") or "") == fingerprint
                status = str(current.get("status") or "")
                if same_input and status == "done":
                    return {
                        "action": "reuse",
                        "reusable": True,
                        "record": copy.deepcopy(current),
                    }
                if (
                    same_input
                    and status == "active"
                    and float(current.get("lease_expires_at") or 0.0) > now
                ):
                    owner = str(current.get("claimed_by") or "")
                    if owner != clean_worker:
                        return {
                            "action": "wait",
                            "reusable": False,
                            "record": copy.deepcopy(current),
                        }
                    current["lease_expires_at"] = now + lease
                    current["updated_at"] = now
                    self.save(data)
                    return {
                        "action": "resume",
                        "reusable": False,
                        "record": copy.deepcopy(current),
                    }
                generation = int(current.get("generation") or 1)
                attempts = int(current.get("attempts") or 0)
                if not same_input:
                    generation += 1
                    attempts = 0
            else:
                generation = 1
                attempts = 0

            record = {
                "task_id": clean_task,
                "step_key": clean_step,
                "input_fingerprint": fingerprint,
                "generation": generation,
                "status": "active",
                "claimed_by": clean_worker,
                "lease_expires_at": now + lease,
                "attempts": attempts + 1,
                "result": None,
                "summary": "",
                "error": "",
                "started_at": now,
                "updated_at": now,
            }
            steps[clean_step] = record
            self.save(data)
            return {
                "action": "execute",
                "reusable": False,
                "record": copy.deepcopy(record),
            }

    def task_step_heartbeat(
        self,
        task_id: str,
        step_key: str,
        worker_name: str,
        lease_seconds: int = 900,
    ) -> Dict[str, object]:
        clean_task = str(task_id or "").strip()
        clean_step = str(step_key or "").strip()
        clean_worker = str(worker_name or "").strip()
        lease = max(1, min(int(lease_seconds or 1), 86400))
        with self._write_lock():
            data = self.load()
            record = (data["task_steps"].get(clean_task) or {}).get(clean_step)
            if not isinstance(record, dict):
                raise KeyError("unknown task step")
            if str(record.get("status") or "") != "active":
                raise RuntimeError("task step is not active")
            if str(record.get("claimed_by") or "") != clean_worker:
                raise RuntimeError("task step is owned by another worker")
            now = time.time()
            record["lease_expires_at"] = now + lease
            record["updated_at"] = now
            self.save(data)
            return copy.deepcopy(record)

    def task_step_complete(
        self,
        task_id: str,
        step_key: str,
        worker_name: str,
        status: str = "done",
        result: object = None,
        summary: str = "",
        error: str = "",
    ) -> Dict[str, object]:
        clean_task = str(task_id or "").strip()
        clean_step = str(step_key or "").strip()
        clean_worker = str(worker_name or "").strip()
        clean_status = str(status or "done").strip().lower()
        if clean_status not in {"done", "failed"}:
            raise ValueError("task step status must be done or failed")
        with self._write_lock():
            data = self.load()
            record = (data["task_steps"].get(clean_task) or {}).get(clean_step)
            if not isinstance(record, dict):
                raise KeyError("unknown task step")
            if str(record.get("status") or "") != "active":
                raise RuntimeError("task step is not active")
            if str(record.get("claimed_by") or "") != clean_worker:
                raise RuntimeError("task step is owned by another worker")
            now = time.time()
            record["status"] = clean_status
            record["result"] = result
            record["summary"] = str(summary or "").strip()
            record["error"] = str(error or "").strip()
            record["claimed_by"] = ""
            record["lease_expires_at"] = 0.0
            record["completed_at"] = now
            record["updated_at"] = now
            self.save(data)
            return copy.deepcopy(record)

    def task_steps(self, task_id: str) -> List[Dict[str, object]]:
        clean_task = str(task_id or "").strip()
        if not clean_task:
            raise ValueError("task_id is required")
        values = (self.load()["task_steps"].get(clean_task) or {}).values()
        return sorted(
            [
                copy.deepcopy(item)
                for item in values
                if isinstance(item, dict)
            ],
            key=lambda item: (
                str(item.get("step_key") or ""),
                int(item.get("generation") or 0),
            ),
        )

    def task_claim(
        self,
        task_id: str,
        worker_name: str,
        lease_seconds: int = 900,
        superstep_id: str = "",
    ) -> Dict[str, object]:
        clean_worker = str(worker_name or "").strip()
        clean_step = str(superstep_id or "").strip()
        if not clean_worker:
            raise ValueError("worker_name is required")
        with self._write_lock():
            data = self.load()
            if clean_step:
                step = data["supersteps"].get(clean_step)
                if not isinstance(step, dict):
                    raise KeyError("unknown superstep_id")
                if step.get("status") not in {"open", "ready"}:
                    raise RuntimeError("superstep is not open")
                if clean_worker not in step.get("participants", []):
                    raise RuntimeError("worker is not a superstep participant")
            task = data["tasks"].get(str(task_id))
            if not isinstance(task, dict):
                raise KeyError("unknown task_id")
            required_capabilities = set(normalize_capabilities(task.get("required_capabilities") or []))
            worker_capabilities = set(self._worker_capabilities(data, clean_worker))
            if required_capabilities and not required_capabilities.issubset(worker_capabilities):
                missing = sorted(required_capabilities - worker_capabilities)
                raise RuntimeError(
                    "worker lacks required task capabilities: {0}".format(",".join(missing))
                )
            now = time.time()
            live_lanes = [
                lane
                for lane in data["lanes"].values()
                if isinstance(lane, dict)
                and str(lane.get("task_id") or "") == str(task_id)
                and lane.get("status") in {"creating", "active", "committing", "recovery_required"}
            ]
            for lane in live_lanes:
                lane_worker = str(lane.get("worker_name") or "")
                if lane_worker and lane_worker != clean_worker:
                    raise RuntimeError(
                        "task has a live MVCC lane owned by {0}: {1}".format(
                            lane_worker,
                            str(lane.get("lane_id") or ""),
                        )
                    )
            unresolved = [
                dep
                for dep in task.get("dependencies", [])
                if data["tasks"].get(str(dep), {}).get("status") != "done"
            ]
            if unresolved:
                raise RuntimeError(
                    "task dependencies are not done: {0}".format(", ".join(unresolved))
                )
            if float(task.get("retry_after") or 0.0) > now:
                raise RuntimeError("task retry delay has not elapsed")
            status = str(task.get("status") or "")
            if status not in {"ready", "active"}:
                raise RuntimeError("task is not claimable in status {0}".format(status))
            current_worker = str(task.get("claimed_by") or "")
            lease_expires = float(task.get("lease_expires_at") or 0.0)
            existing_valid_owner = (
                status == "active"
                and current_worker == clean_worker
                and lease_expires > now
            )
            if not existing_valid_owner:
                active_graph_id = str(data.get("active_graph_id") or "")
                active_graph = data["graphs"].get(active_graph_id) if active_graph_id else None
                if isinstance(active_graph, dict):
                    compiled = compile_graph(
                        active_graph,
                        {
                            str(item_id): item
                            for item_id, item in data["tasks"].items()
                            if isinstance(item, dict)
                        },
                        data["shared_status"],
                    )
                    if not compiled.get("valid", False):
                        raise RuntimeError("active SwarmGraph is invalid")
                    gate = (compiled.get("task_gates") or {}).get(str(task_id))
                    if isinstance(gate, dict):
                        if gate.get("unselected"):
                            raise RuntimeError("task is on an unselected SwarmGraph branch")
                        if not gate.get("ready", False):
                            raise RuntimeError(
                                "task is blocked by SwarmGraph gate: {0}".format(
                                    ",".join(gate.get("unsatisfied_edge_ids") or gate.get("pending_conditional_edge_ids") or [])
                                    or "pending"
                                )
                            )
                        node_id = str((compiled.get("task_node_map") or {}).get(str(task_id)) or "")
                        graph_node = next(
                            (
                                item
                                for item in compiled.get("nodes", [])
                                if isinstance(item, dict)
                                and str(item.get("node_id") or "") == node_id
                            ),
                            {},
                        )
                        expected_role = str(graph_node.get("role") or "").strip().lower()
                        worker_role = self._worker_role(data, clean_worker)
                        if expected_role and worker_role != expected_role:
                            raise RuntimeError(
                                "task SwarmGraph role requires worker role {0}".format(
                                    expected_role
                                )
                            )
            if current_worker and current_worker != clean_worker and lease_expires > now:
                raise RuntimeError("task is already leased by {0}".format(current_worker))
            attempts = int(task.get("attempts") or 0)
            max_attempts = max(1, int(task.get("max_attempts") or 1))
            if current_worker != clean_worker:
                if attempts >= max_attempts:
                    task["status"] = "failed"
                    task["last_error"] = "max attempts exhausted before claim"
                    task["updated_at"] = now
                    self.save(data)
                    raise RuntimeError("task max attempts exhausted")
                attempts += 1
            existing_step = str(task.get("superstep_id") or "")
            if existing_step and existing_step != clean_step:
                existing = data["supersteps"].get(existing_step)
                if isinstance(existing, dict) and existing.get("status") in {"open", "ready"}:
                    raise RuntimeError("task is already bound to active superstep {0}".format(existing_step))
            new_expiry = now + max(1, int(lease_seconds or 1))
            active_reservations = [
                reservation
                for reservation in data.get("task_resource_reservations", {}).values()
                if isinstance(reservation, dict)
                and reservation.get("status") == "active"
                and str(reservation.get("task_id") or "") == str(task_id)
                and str(reservation.get("worker_name") or "") == clean_worker
                and float(reservation.get("lease_expires_at") or 0.0) > now
            ]
            if existing_valid_owner and active_reservations:
                for reservation in active_reservations:
                    reservation["lease_expires_at"] = new_expiry
                    reservation["updated_at"] = now
            else:
                self._reserve_task_resources(
                    data,
                    task,
                    clean_worker,
                    new_expiry,
                    now,
                )
            task["attempts"] = attempts
            task["status"] = "active"
            task["superstep_id"] = clean_step
            task["claimed_by"] = clean_worker
            task["lease_expires_at"] = new_expiry
            task["updated_at"] = now
            self.save(data)
            return dict(task)

    def task_heartbeat(
        self,
        task_id: str,
        worker_name: str,
        lease_seconds: int = 900,
    ) -> Dict[str, object]:
        clean_worker = str(worker_name or "").strip()
        with self._write_lock():
            data = self.load()
            task = data["tasks"].get(str(task_id))
            if not isinstance(task, dict):
                raise KeyError("unknown task_id")
            if str(task.get("claimed_by") or "") != clean_worker:
                raise RuntimeError("task is not leased by this worker")
            if str(task.get("status") or "") != "active":
                raise RuntimeError("task is not active")
            now = time.time()
            new_expiry = now + max(1, int(lease_seconds or 1))
            task["lease_expires_at"] = new_expiry
            task["updated_at"] = now
            for reservation in data.get("task_resource_reservations", {}).values():
                if (
                    isinstance(reservation, dict)
                    and reservation.get("status") == "active"
                    and str(reservation.get("task_id") or "") == str(task_id)
                    and str(reservation.get("worker_name") or "") == clean_worker
                ):
                    reservation["lease_expires_at"] = new_expiry
                    reservation["updated_at"] = now
            for lane in data["lanes"].values():
                if (
                    isinstance(lane, dict)
                    and str(lane.get("task_id") or "") == str(task_id)
                    and str(lane.get("worker_name") or "") == clean_worker
                    and lane.get("status") in {"creating", "active", "committing", "recovery_required"}
                ):
                    run_id = str(lane.get("run_id") or "")
                    lane["updated_at"] = now
                    for claim in data["resource_claims"].values():
                        if (
                            isinstance(claim, dict)
                            and str(claim.get("run_id") or "") == run_id
                            and claim.get("status") == "active"
                        ):
                            claim["lease_expires_at"] = new_expiry
                            claim["updated_at"] = now
            self.save(data)
            return dict(task)

    def task_release(
        self,
        task_id: str,
        worker_name: str,
        status: str = "done",
        summary: str = "",
        result: object = None,
        error: str = "",
        retryable: bool = True,
        retry_delay_seconds: float = 0.0,
    ) -> Dict[str, object]:
        clean_worker = str(worker_name or "").strip()
        clean_status = str(status or "").strip().lower()
        if clean_status not in {"ready", "blocked", "review", "done", "failed"}:
            raise ValueError("invalid release status")
        with self._write_lock():
            data = self.load()
            task = data["tasks"].get(str(task_id))
            if not isinstance(task, dict):
                raise KeyError("unknown task_id")
            if str(task.get("claimed_by") or "") != clean_worker:
                raise RuntimeError("task is not leased by this worker")
            live_lanes = [
                str(lane.get("lane_id") or "")
                for lane in data["lanes"].values()
                if isinstance(lane, dict)
                and str(lane.get("task_id") or "") == str(task_id)
                and str(lane.get("worker_name") or "") == clean_worker
                and lane.get("status") in {"creating", "active", "committing", "recovery_required"}
            ]
            if live_lanes:
                raise RuntimeError(
                    "task has live MVCC lane(s); commit/abort them before releasing the task lease: {0}".format(
                        ",".join(live_lanes)
                    )
                )
            active_step_id = str(task.get("superstep_id") or "")
            if active_step_id:
                active_step = data["supersteps"].get(active_step_id)
                if isinstance(active_step, dict) and active_step.get("status") in {"open", "ready"}:
                    raise RuntimeError(
                        "task belongs to active superstep {0}; stage pending writes instead".format(
                            active_step_id
                        )
                    )
            now = time.time()
            attempts = int(task.get("attempts") or 0)
            max_attempts = max(1, int(task.get("max_attempts") or 1))
            final_status = clean_status
            last_error = str(error or "").strip()
            retry_after = 0.0
            if clean_status == "failed" and retryable and attempts < max_attempts:
                final_status = "ready"
                retry_after = now + max(0.0, float(retry_delay_seconds or 0.0))
            task["status"] = final_status
            task["summary"] = str(summary or "").strip()
            if result is not None:
                task["result"] = result
            task["last_error"] = last_error
            task["retry_after"] = retry_after
            task["superstep_id"] = ""
            task["claimed_by"] = ""
            task["lease_expires_at"] = 0.0
            task["updated_at"] = now
            for reservation in data.get("task_resource_reservations", {}).values():
                if (
                    isinstance(reservation, dict)
                    and reservation.get("status") == "active"
                    and str(reservation.get("task_id") or "") == str(task_id)
                    and str(reservation.get("worker_name") or "") == clean_worker
                ):
                    reservation["status"] = "released"
                    reservation["lease_expires_at"] = 0.0
                    reservation["updated_at"] = now
            self.save(data)
            return dict(task)

    def task_fanout(
        self,
        items: List[object],
        owner: str = "",
        title_prefix: str = "Shard",
        dependencies: Optional[List[str]] = None,
        group_id: str = "",
        max_attempts: int = 3,
        preferred_role: str = "",
        required_capabilities: Optional[List[object]] = None,
        read_resources: Optional[List[object]] = None,
        write_resources: Optional[List[object]] = None,
        exclusive_resources: Optional[List[object]] = None,
        canonical_root: str = "",
        shard_strategy: str = "map",
    ) -> Dict[str, object]:
        values = list(items or [])
        if not values:
            raise ValueError("items must be non-empty")
        clean_group = str(group_id or "").strip() or "group_" + uuid.uuid4().hex[:10]
        created = []
        for index, item in enumerate(values, start=1):
            created.append(
                self.task_create(
                    title="{0} {1}".format(str(title_prefix or "Shard").strip(), index),
                    owner=owner,
                    dependencies=dependencies,
                    payload=item,
                    group_id=clean_group,
                    group_index=index - 1,
                    max_attempts=max_attempts,
                    preferred_role=preferred_role,
                    required_capabilities=required_capabilities,
                    read_resources=read_resources,
                    write_resources=write_resources,
                    exclusive_resources=exclusive_resources,
                    canonical_root=canonical_root,
                    parallelizable=True,
                    shard_strategy=shard_strategy,
                )
            )
        return {
            "group_id": clean_group,
            "task_ids": [item["task_id"] for item in created],
            "tasks": created,
        }

    def task_reduce(self, group_id: str) -> Dict[str, object]:
        clean_group = str(group_id or "").strip()
        if not clean_group:
            raise ValueError("group_id is required")
        members = [
            task
            for task in self.tasks()
            if str(task.get("group_id") or "") == clean_group
        ]
        members.sort(
            key=lambda item: (
                int(item.get("group_index", -1)),
                str(item.get("task_id") or ""),
            )
        )
        if not members:
            raise KeyError("unknown group_id")
        done = [task for task in members if task.get("status") == "done"]
        failed = [task for task in members if task.get("status") == "failed"]
        return {
            "group_id": clean_group,
            "complete": len(done) == len(members),
            "failed": bool(failed),
            "member_count": len(members),
            "done_count": len(done),
            "failed_task_ids": [task["task_id"] for task in failed],
            "results": [task.get("result") for task in done],
            "tasks": members,
        }

    @staticmethod
    def _normalize_superstep_writes(writes: List[Dict[str, object]]) -> List[Dict[str, object]]:
        normalized = []
        for raw in writes or []:
            if not isinstance(raw, dict):
                raise ValueError("superstep writes must be objects")
            op = str(raw.get("op") or "").strip().lower()
            if op == "task_release":
                task_id = str(raw.get("task_id") or "").strip()
                if not task_id:
                    raise ValueError("task_release requires task_id")
                status = str(raw.get("status") or "done").strip().lower()
                if status not in {"ready", "review", "done"}:
                    raise ValueError(
                        "superstep task_release status must be ready, review, or done; "
                        "use superstep_fail for failed/blocked participants"
                    )
                normalized.append({
                    "op": "task_release",
                    "task_id": task_id,
                    "status": status,
                    "summary": str(raw.get("summary") or "").strip(),
                    "result": raw.get("result"),
                })
            elif op == "status_set":
                key = str(raw.get("key") or "").strip()
                if not key:
                    raise ValueError("status_set requires key")
                normalized.append({
                    "op": "status_set",
                    "key": key,
                    "value": raw.get("value"),
                })
            else:
                raise ValueError("unsupported superstep write op: {0}".format(op or "<empty>"))
        json.dumps(normalized, ensure_ascii=False)
        return normalized

    @staticmethod
    def _superstep_write_target(write: Dict[str, object]) -> str:
        if write.get("op") == "task_release":
            return "task:{0}".format(write.get("task_id"))
        if write.get("op") == "status_set":
            return "status:{0}".format(write.get("key"))
        return "unknown"

    @staticmethod
    def _superstep_summary(step: Dict[str, object]) -> Dict[str, object]:
        participants = list(step.get("participants") or [])
        pending = step.get("pending_writes") or {}
        failures = step.get("failures") or {}
        staged = [name for name in participants if name in pending]
        return {
            "step_id": str(step.get("step_id") or ""),
            "status": str(step.get("status") or ""),
            "participants": participants,
            "staged_participants": staged,
            "missing_participants": [name for name in participants if name not in pending],
            "failures": dict(failures),
            "ready_to_commit": (
                str(step.get("status") or "") == "ready"
                and len(staged) == len(participants)
                and not failures
            ),
            "created_at": float(step.get("created_at") or 0.0),
            "committed_at": float(step.get("committed_at") or 0.0),
            "aborted_at": float(step.get("aborted_at") or 0.0),
            "metadata": step.get("metadata") or {},
            "commit_summary": step.get("commit_summary") or {},
            "error": str(step.get("error") or ""),
        }

    def superstep_begin(
        self,
        participants: List[str],
        step_id: str = "",
        metadata: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        names = []
        for item in participants or []:
            name = str(item or "").strip()
            if name and name not in names:
                names.append(name)
        if not names:
            raise ValueError("participants must be non-empty")
        clean_id = str(step_id or "").strip() or "step_" + uuid.uuid4().hex[:10]
        json.dumps(metadata or {}, ensure_ascii=False)
        with self._write_lock():
            data = self.load()
            if clean_id in data["supersteps"]:
                raise ValueError("step_id already exists")
            now = time.time()
            step = {
                "step_id": clean_id,
                "status": "open",
                "participants": names,
                "pending_writes": {},
                "failures": {},
                "metadata": dict(metadata or {}),
                "created_at": now,
                "committed_at": 0.0,
                "aborted_at": 0.0,
                "commit_summary": {},
                "error": "",
            }
            data["supersteps"][clean_id] = step
            self.save(data)
            return self._superstep_summary(step)

    def superstep_stage(
        self,
        step_id: str,
        worker_name: str,
        writes: List[Dict[str, object]],
    ) -> Dict[str, object]:
        clean_worker = str(worker_name or "").strip()
        if not clean_worker:
            raise ValueError("worker_name is required")
        normalized = self._normalize_superstep_writes(writes)
        with self._write_lock():
            data = self.load()
            step = data["supersteps"].get(str(step_id))
            if not isinstance(step, dict):
                raise KeyError("unknown step_id")
            if step.get("status") not in {"open", "ready"}:
                raise RuntimeError("superstep is not open")
            if clean_worker not in step.get("participants", []):
                raise RuntimeError("worker is not a superstep participant")
            if step.get("failures"):
                raise RuntimeError("superstep already has a participant failure")
            step["pending_writes"][clean_worker] = {
                "writes": normalized,
                "staged_at": time.time(),
            }
            participants = list(step.get("participants") or [])
            step["status"] = (
                "ready"
                if all(name in step["pending_writes"] for name in participants)
                else "open"
            )
            self.save(data)
            summary = self._superstep_summary(step)
            summary["worker_name"] = clean_worker
            summary["pending_write_count"] = len(normalized)
            return summary

    def superstep_fail(self, step_id: str, worker_name: str, error: str) -> Dict[str, object]:
        clean_worker = str(worker_name or "").strip()
        clean_error = str(error or "").strip()
        if not clean_worker:
            raise ValueError("worker_name is required")
        if not clean_error:
            raise ValueError("error is required")
        with self._write_lock():
            data = self.load()
            step = data["supersteps"].get(str(step_id))
            if not isinstance(step, dict):
                raise KeyError("unknown step_id")
            if step.get("status") not in {"open", "ready"}:
                raise RuntimeError("superstep is already terminal")
            if clean_worker not in step.get("participants", []):
                raise RuntimeError("worker is not a superstep participant")
            now = time.time()
            step["failures"][clean_worker] = {
                "error": clean_error,
                "failed_at": now,
            }
            step["status"] = "aborted"
            step["aborted_at"] = now
            step["error"] = "participant failure: {0}".format(clean_worker)
            self.save(data)
            return self._superstep_summary(step)

    def superstep_abort(
        self,
        step_id: str,
        reason: str = "aborted by orchestrator",
    ) -> Dict[str, object]:
        with self._write_lock():
            data = self.load()
            step = data["supersteps"].get(str(step_id))
            if not isinstance(step, dict):
                raise KeyError("unknown step_id")
            if step.get("status") in {"committed", "aborted", "conflict"}:
                return self._superstep_summary(step)
            now = time.time()
            step["status"] = "aborted"
            step["aborted_at"] = now
            step["error"] = str(reason or "aborted by orchestrator").strip()
            self.save(data)
            return self._superstep_summary(step)

    def _apply_superstep_write(
        self,
        data: Dict[str, object],
        step_id: str,
        worker_name: str,
        write: Dict[str, object],
        now: float,
    ) -> None:
        op = write.get("op")
        if op == "status_set":
            data["shared_status"][str(write["key"])] = {
                "value": write.get("value"),
                "updated_at": now,
            }
            return
        if op != "task_release":
            raise RuntimeError("unsupported pending write op")

        task = data["tasks"].get(str(write.get("task_id") or ""))
        if not isinstance(task, dict):
            raise RuntimeError("pending task no longer exists")
        if str(task.get("status") or "") != "active":
            raise RuntimeError("pending task is no longer active")
        if str(task.get("claimed_by") or "") != worker_name:
            raise RuntimeError("pending task lease owner changed")
        if str(task.get("superstep_id") or "") != str(step_id):
            raise RuntimeError("pending task superstep binding changed")
        if float(task.get("lease_expires_at") or 0.0) <= now:
            raise RuntimeError("pending task lease expired before commit")

        task["status"] = str(write.get("status") or "done")
        task["summary"] = str(write.get("summary") or "").strip()
        if "result" in write:
            task["result"] = write.get("result")
        task["last_error"] = ""
        task["retry_after"] = 0.0
        task["superstep_id"] = ""
        task["claimed_by"] = ""
        task["lease_expires_at"] = 0.0
        task["updated_at"] = now
        for reservation in data.get("task_resource_reservations", {}).values():
            if (
                isinstance(reservation, dict)
                and reservation.get("status") == "active"
                and str(reservation.get("task_id") or "") == str(task.get("task_id") or "")
                and str(reservation.get("worker_name") or "") == worker_name
            ):
                reservation["status"] = "released"
                reservation["lease_expires_at"] = 0.0
                reservation["updated_at"] = now

    def superstep_commit(self, step_id: str) -> Dict[str, object]:
        with self._write_lock():
            data = self.load()
            step = data["supersteps"].get(str(step_id))
            if not isinstance(step, dict):
                raise KeyError("unknown step_id")
            if step.get("status") == "committed":
                return self._superstep_summary(step)
            if step.get("status") != "ready":
                raise RuntimeError("superstep is not ready to commit")
            if step.get("failures"):
                raise RuntimeError("superstep has participant failures")
            participants = list(step.get("participants") or [])
            pending = step.get("pending_writes") or {}
            missing = [name for name in participants if name not in pending]
            if missing:
                raise RuntimeError(
                    "superstep is missing staged participants: {0}".format(", ".join(missing))
                )

            seen_targets = {}
            for worker in participants:
                for write in pending[worker].get("writes", []):
                    target = self._superstep_write_target(write)
                    previous = seen_targets.get(target)
                    if previous and previous != worker:
                        now = time.time()
                        step["status"] = "conflict"
                        step["aborted_at"] = now
                        step["error"] = (
                            "conflicting pending writes for {0}: {1}, {2}".format(
                                target, previous, worker
                            )
                        )
                        self.save(data)
                        return self._superstep_summary(step)
                    seen_targets[target] = worker

            candidate = copy.deepcopy(data)
            candidate_step = candidate["supersteps"][str(step_id)]
            now = time.time()
            try:
                for worker in participants:
                    for write in pending[worker].get("writes", []):
                        self._apply_superstep_write(
                            candidate,
                            str(step_id),
                            worker,
                            write,
                            now,
                        )
            except Exception as exc:
                step["status"] = "conflict"
                step["aborted_at"] = now
                step["error"] = str(exc)
                self.save(data)
                return self._superstep_summary(step)

            candidate_step["status"] = "committed"
            candidate_step["committed_at"] = now
            candidate_step["error"] = ""
            candidate_step["commit_summary"] = {
                "participant_count": len(participants),
                "write_count": sum(
                    len(pending[worker].get("writes", [])) for worker in participants
                ),
                "targets": sorted(seen_targets),
            }
            self.save(candidate)
            return self._superstep_summary(candidate_step)

    def superstep_get(self, step_id: str, include_pending: bool = True) -> Dict[str, object]:
        step = self.load()["supersteps"].get(str(step_id))
        if not isinstance(step, dict):
            raise KeyError("unknown step_id")
        result = self._superstep_summary(step)
        if include_pending:
            result["pending_writes"] = copy.deepcopy(step.get("pending_writes") or {})
        return result

    def supersteps(self, include_pending: bool = False) -> List[Dict[str, object]]:
        values = []
        for step in self.load()["supersteps"].values():
            if not isinstance(step, dict):
                continue
            item = self._superstep_summary(step)
            if include_pending:
                item["pending_writes"] = copy.deepcopy(step.get("pending_writes") or {})
            values.append(item)
        return sorted(values, key=lambda item: (item["created_at"], item["step_id"]))

    def task_update(
        self,
        task_id: str,
        status: str = "",
        owner: str = "",
        summary: str = "",
    ) -> Dict[str, object]:
        with self._write_lock():
            data = self.load()
            task = data["tasks"].get(str(task_id))
            if not isinstance(task, dict):
                raise KeyError("unknown task_id")
            if status:
                clean_status = str(status).strip().lower()
                if clean_status not in {"ready", "active", "blocked", "review", "done", "failed"}:
                    raise ValueError("invalid task status")
                task["status"] = clean_status
            if owner:
                task["owner"] = str(owner).strip()
            if summary:
                task["summary"] = str(summary).strip()
            task["updated_at"] = time.time()
            self.save(data)
            return dict(task)

    def tasks(self) -> List[Dict[str, object]]:
        values = list(self.load()["tasks"].values())
        return sorted(
            [dict(item) for item in values if isinstance(item, dict)],
            key=lambda item: (str(item.get("status")), str(item.get("task_id"))),
        )

    def status_set(self, key: str, value: object) -> Dict[str, object]:
        clean = str(key or "").strip()
        if not clean:
            raise ValueError("status key is required")
        with self._write_lock():
            data = self.load()
            data["shared_status"][clean] = {
                "value": value,
                "updated_at": time.time(),
            }
            self.save(data)
            return dict(data["shared_status"][clean])

    def status(self) -> Dict[str, object]:
        return dict(self.load()["shared_status"])

    @staticmethod
    def _graph_revision_guard(graph: Dict[str, object], expected_revision: int) -> None:
        expected = int(expected_revision)
        if expected >= 0 and int(graph.get("revision") or 0) != expected:
            raise RuntimeError(
                "graph revision conflict: expected {0}, current {1}".format(
                    expected,
                    int(graph.get("revision") or 0),
                )
            )

    @staticmethod
    def _graph_summary(graph: Dict[str, object], active: bool = False) -> Dict[str, object]:
        return {
            "graph_id": str(graph.get("graph_id") or ""),
            "name": str(graph.get("name") or ""),
            "description": str(graph.get("description") or ""),
            "revision": int(graph.get("revision") or 0),
            "node_count": len(graph.get("nodes") or {}),
            "edge_count": len(graph.get("edges") or {}),
            "active": bool(active),
            "created_at": float(graph.get("created_at") or 0.0),
            "updated_at": float(graph.get("updated_at") or 0.0),
            "metadata": copy.deepcopy(graph.get("metadata") or {}),
        }

    def graph_create(
        self,
        name: str,
        description: str = "",
        graph_id: str = "",
        metadata: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("graph name is required")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("graph metadata must be an object")
        json.dumps(metadata or {}, ensure_ascii=False)
        clean_id = str(graph_id or "").strip() or "graph_" + uuid.uuid4().hex[:10]
        if any(ch.isspace() for ch in clean_id):
            raise ValueError("graph_id cannot contain whitespace")
        with self._write_lock():
            data = self.load()
            if clean_id in data["graphs"]:
                raise ValueError("graph_id already exists")
            now = time.time()
            graph = {
                "graph_id": clean_id,
                "name": clean_name,
                "description": str(description or "").strip(),
                "metadata": copy.deepcopy(metadata or {}),
                "nodes": {},
                "edges": {},
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            data["graphs"][clean_id] = graph
            self.save(data)
            result = copy.deepcopy(graph)
            result["active"] = False
            return result

    def graph_get(self, graph_id: str = "") -> Dict[str, object]:
        data = self.load()
        clean_id = str(graph_id or "").strip() or str(data.get("active_graph_id") or "")
        if not clean_id:
            return {}
        graph = data["graphs"].get(clean_id)
        if not isinstance(graph, dict):
            raise KeyError("unknown graph_id")
        result = copy.deepcopy(graph)
        result["active"] = clean_id == str(data.get("active_graph_id") or "")
        return result

    def graphs(self) -> List[Dict[str, object]]:
        data = self.load()
        active_id = str(data.get("active_graph_id") or "")
        values = [
            self._graph_summary(graph, active=str(graph_id) == active_id)
            for graph_id, graph in data["graphs"].items()
            if isinstance(graph, dict)
        ]
        return sorted(
            values,
            key=lambda item: (
                not bool(item["active"]),
                float(item["created_at"]),
                str(item["graph_id"]),
            ),
        )

    def graph_activate(
        self,
        graph_id: str,
        expected_revision: int = -1,
    ) -> Dict[str, object]:
        clean_id = str(graph_id or "").strip()
        if not clean_id:
            raise ValueError("graph_id is required")
        with self._write_lock():
            data = self.load()
            graph = data["graphs"].get(clean_id)
            if not isinstance(graph, dict):
                raise KeyError("unknown graph_id")
            self._graph_revision_guard(graph, expected_revision)
            task_map = {
                str(task_id): task
                for task_id, task in data["tasks"].items()
                if isinstance(task, dict)
            }
            validation = graph_validate(graph, tasks=task_map)
            if not validation.get("ok"):
                raise RuntimeError(
                    "cannot activate invalid graph: {0}".format(
                        "; ".join(validation.get("errors") or [])
                    )
                )
            if not graph.get("nodes"):
                raise RuntimeError("cannot activate an empty graph")
            data["active_graph_id"] = clean_id
            self.save(data)
            result = self._graph_summary(graph, active=True)
            result["validation"] = validation
            return result

    def graph_deactivate(self, graph_id: str = "") -> Dict[str, object]:
        with self._write_lock():
            data = self.load()
            active_id = str(data.get("active_graph_id") or "")
            requested = str(graph_id or "").strip()
            if requested and requested != active_id:
                return {
                    "graph_id": requested,
                    "active": False,
                    "changed": False,
                }
            data["active_graph_id"] = ""
            self.save(data)
            return {
                "graph_id": active_id,
                "active": False,
                "changed": bool(active_id),
            }

    def graph_update(
        self,
        graph_id: str,
        patch: Dict[str, object],
        expected_revision: int = -1,
    ) -> Dict[str, object]:
        if not isinstance(patch, dict) or not patch:
            raise ValueError("graph patch must be a non-empty object")
        allowed = {"name", "description", "metadata"}
        unknown = sorted(set(patch) - allowed)
        if unknown:
            raise ValueError("unsupported graph patch field(s): {0}".format(", ".join(unknown)))
        with self._write_lock():
            data = self.load()
            graph = data["graphs"].get(str(graph_id))
            if not isinstance(graph, dict):
                raise KeyError("unknown graph_id")
            self._graph_revision_guard(graph, expected_revision)
            if "name" in patch:
                name = str(patch.get("name") or "").strip()
                if not name:
                    raise ValueError("graph name cannot be empty")
                graph["name"] = name
            if "description" in patch:
                graph["description"] = str(patch.get("description") or "").strip()
            if "metadata" in patch:
                metadata = patch.get("metadata")
                if not isinstance(metadata, dict):
                    raise ValueError("graph metadata must be an object")
                json.dumps(metadata, ensure_ascii=False)
                graph["metadata"] = copy.deepcopy(metadata)
            graph["revision"] = int(graph.get("revision") or 0) + 1
            graph["updated_at"] = time.time()
            self.save(data)
            result = copy.deepcopy(graph)
            result["active"] = str(graph_id) == str(data.get("active_graph_id") or "")
            return result

    def graph_replace(
        self,
        graph_id: str,
        nodes: List[Dict[str, object]],
        edges: List[Dict[str, object]],
        expected_revision: int = -1,
    ) -> Dict[str, object]:
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise ValueError("nodes and edges must be lists")
        with self._write_lock():
            data = self.load()
            graph = data["graphs"].get(str(graph_id))
            if not isinstance(graph, dict):
                raise KeyError("unknown graph_id")
            self._graph_revision_guard(graph, expected_revision)

            normalized_nodes = {}
            for spec in nodes:
                if not isinstance(spec, dict):
                    raise ValueError("every graph node must be an object")
                node = normalize_node(
                    node_id=str(spec.get("node_id") or ""),
                    kind=str(spec.get("kind") or "task"),
                    task_id=str(spec.get("task_id") or ""),
                    role=str(spec.get("role") or ""),
                    label=str(spec.get("label") or ""),
                    status=str(spec.get("status") or "ready"),
                    state=spec.get("state"),
                    metadata=spec.get("metadata") or {},
                )
                node_id = str(node["node_id"])
                if node_id in normalized_nodes:
                    raise ValueError("duplicate node_id: {0}".format(node_id))
                task_id = str(node.get("task_id") or "")
                if task_id and task_id not in data["tasks"]:
                    raise KeyError("unknown task_id: {0}".format(task_id))
                normalized_nodes[node_id] = node

            normalized_edges = {}
            for spec in edges:
                if not isinstance(spec, dict):
                    raise ValueError("every graph edge must be an object")
                edge_id = str(spec.get("edge_id") or "").strip()
                if not edge_id:
                    raise ValueError("bulk graph edge requires edge_id")
                edge = normalize_edge(
                    edge_id=edge_id,
                    source=str(spec.get("source") or ""),
                    target=str(spec.get("target") or ""),
                    kind=str(spec.get("kind") or "dependency"),
                    condition=spec.get("condition"),
                    label=str(spec.get("label") or ""),
                    metadata=spec.get("metadata") or {},
                )
                if edge_id in normalized_edges:
                    raise ValueError("duplicate edge_id: {0}".format(edge_id))
                normalized_edges[edge_id] = edge

            candidate = copy.deepcopy(graph)
            candidate["nodes"] = normalized_nodes
            candidate["edges"] = normalized_edges
            validation = graph_validate(
                candidate,
                tasks={
                    str(task_id): task
                    for task_id, task in data["tasks"].items()
                    if isinstance(task, dict)
                },
            )
            if not validation.get("ok"):
                raise RuntimeError(
                    "replacement topology is invalid: {0}".format(
                        "; ".join(validation.get("errors") or [])
                    )
                )
            if (
                str(data.get("active_graph_id") or "") == str(graph_id)
                and not normalized_nodes
            ):
                raise RuntimeError("cannot replace the active SwarmGraph with an empty topology")

            graph["nodes"] = normalized_nodes
            graph["edges"] = normalized_edges
            graph["revision"] = int(graph.get("revision") or 0) + 1
            graph["updated_at"] = time.time()
            self.save(data)
            return {
                "graph_id": str(graph_id),
                "revision": int(graph["revision"]),
                "node_count": len(normalized_nodes),
                "edge_count": len(normalized_edges),
                "validation": validation,
            }

    def graph_preview(self, graph_id: str = "") -> Dict[str, object]:
        data = self.load()
        clean_id = str(graph_id or "").strip() or str(data.get("active_graph_id") or "")
        if not clean_id:
            return {
                "active": False,
                "preview": True,
                "graph_id": "",
                "revision": 0,
                "nodes": [],
                "edges": [],
                "task_gates": {},
                "task_node_map": {},
            }
        graph = data["graphs"].get(clean_id)
        if not isinstance(graph, dict):
            raise KeyError("unknown graph_id")
        compiled = compile_graph(
            graph,
            {
                str(task_id): task
                for task_id, task in data["tasks"].items()
                if isinstance(task, dict)
            },
            data["shared_status"],
        )
        compiled["active"] = clean_id == str(data.get("active_graph_id") or "")
        compiled["preview"] = True
        return compiled

    def graph_delete(
        self,
        graph_id: str,
        expected_revision: int = -1,
    ) -> Dict[str, object]:
        clean_id = str(graph_id or "").strip()
        if not clean_id:
            raise ValueError("graph_id is required")
        with self._write_lock():
            data = self.load()
            graph = data["graphs"].get(clean_id)
            if not isinstance(graph, dict):
                raise KeyError("unknown graph_id")
            self._graph_revision_guard(graph, expected_revision)
            was_active = str(data.get("active_graph_id") or "") == clean_id
            data["graphs"].pop(clean_id, None)
            if was_active:
                data["active_graph_id"] = ""
            self.save(data)
            return {
                "graph_id": clean_id,
                "deleted": True,
                "was_active": was_active,
            }

    def graph_node_add(
        self,
        graph_id: str,
        node_id: str,
        kind: str = "task",
        task_id: str = "",
        role: str = "",
        label: str = "",
        status: str = "ready",
        state: object = None,
        metadata: Optional[Dict[str, object]] = None,
        expected_revision: int = -1,
    ) -> Dict[str, object]:
        with self._write_lock():
            data = self.load()
            graph = data["graphs"].get(str(graph_id))
            if not isinstance(graph, dict):
                raise KeyError("unknown graph_id")
            self._graph_revision_guard(graph, expected_revision)
            node = normalize_node(
                node_id=node_id,
                kind=kind,
                task_id=task_id,
                role=role,
                label=label,
                status=status,
                state=state,
                metadata=metadata,
            )
            clean_node_id = str(node["node_id"])
            if clean_node_id in graph["nodes"]:
                raise ValueError("node_id already exists")
            clean_task = str(node.get("task_id") or "")
            if clean_task and clean_task not in data["tasks"]:
                raise KeyError("unknown task_id")
            candidate = copy.deepcopy(graph)
            candidate["nodes"][clean_node_id] = node
            validation = graph_validate(
                candidate,
                tasks={
                    str(task_id): task
                    for task_id, task in data["tasks"].items()
                    if isinstance(task, dict)
                },
            )
            if not validation.get("ok"):
                raise RuntimeError(
                    "graph node would make graph invalid: {0}".format(
                        "; ".join(validation.get("errors") or [])
                    )
                )
            graph["nodes"][clean_node_id] = node
            graph["revision"] = int(graph.get("revision") or 0) + 1
            graph["updated_at"] = time.time()
            self.save(data)
            return {
                "graph_id": str(graph_id),
                "revision": int(graph["revision"]),
                "node": copy.deepcopy(node),
                "validation": validation,
            }

    def graph_node_update(
        self,
        graph_id: str,
        node_id: str,
        patch: Dict[str, object],
        expected_revision: int = -1,
    ) -> Dict[str, object]:
        if not isinstance(patch, dict) or not patch:
            raise ValueError("node patch must be a non-empty object")
        allowed = {"kind", "task_id", "role", "label", "status", "state", "metadata"}
        unknown = sorted(set(patch) - allowed)
        if unknown:
            raise ValueError("unsupported node patch field(s): {0}".format(", ".join(unknown)))
        with self._write_lock():
            data = self.load()
            graph = data["graphs"].get(str(graph_id))
            if not isinstance(graph, dict):
                raise KeyError("unknown graph_id")
            self._graph_revision_guard(graph, expected_revision)
            existing = graph["nodes"].get(str(node_id))
            if not isinstance(existing, dict):
                raise KeyError("unknown node_id")
            merged = copy.deepcopy(existing)
            merged.update(copy.deepcopy(patch))
            node = normalize_node(
                node_id=str(node_id),
                kind=str(merged.get("kind") or "task"),
                task_id=str(merged.get("task_id") or ""),
                role=str(merged.get("role") or ""),
                label=str(merged.get("label") or ""),
                status=str(merged.get("status") or "ready"),
                state=merged.get("state"),
                metadata=merged.get("metadata") or {},
            )
            clean_task = str(node.get("task_id") or "")
            if clean_task and clean_task not in data["tasks"]:
                raise KeyError("unknown task_id")
            candidate = copy.deepcopy(graph)
            candidate["nodes"][str(node_id)] = node
            validation = graph_validate(
                candidate,
                tasks={
                    str(task_id): task
                    for task_id, task in data["tasks"].items()
                    if isinstance(task, dict)
                },
            )
            if not validation.get("ok"):
                raise RuntimeError(
                    "graph node update would make graph invalid: {0}".format(
                        "; ".join(validation.get("errors") or [])
                    )
                )
            graph["nodes"][str(node_id)] = node
            graph["revision"] = int(graph.get("revision") or 0) + 1
            graph["updated_at"] = time.time()
            self.save(data)
            return {
                "graph_id": str(graph_id),
                "revision": int(graph["revision"]),
                "node": copy.deepcopy(node),
                "validation": validation,
            }

    def graph_node_remove(
        self,
        graph_id: str,
        node_id: str,
        expected_revision: int = -1,
    ) -> Dict[str, object]:
        clean_node = str(node_id or "").strip()
        if not clean_node:
            raise ValueError("node_id is required")
        with self._write_lock():
            data = self.load()
            graph = data["graphs"].get(str(graph_id))
            if not isinstance(graph, dict):
                raise KeyError("unknown graph_id")
            self._graph_revision_guard(graph, expected_revision)
            if clean_node not in graph["nodes"]:
                raise KeyError("unknown node_id")
            candidate = copy.deepcopy(graph)
            candidate["nodes"].pop(clean_node, None)
            removed_edges = [
                str(edge_id)
                for edge_id, edge in list(candidate["edges"].items())
                if isinstance(edge, dict)
                and (
                    str(edge.get("source") or "") == clean_node
                    or str(edge.get("target") or "") == clean_node
                )
            ]
            for edge_id in removed_edges:
                candidate["edges"].pop(edge_id, None)
            if (
                str(data.get("active_graph_id") or "") == str(graph_id)
                and not candidate["nodes"]
            ):
                raise RuntimeError("cannot remove the last node from the active SwarmGraph")
            graph["nodes"] = candidate["nodes"]
            graph["edges"] = candidate["edges"]
            graph["revision"] = int(graph.get("revision") or 0) + 1
            graph["updated_at"] = time.time()
            self.save(data)
            return {
                "graph_id": str(graph_id),
                "revision": int(graph["revision"]),
                "removed_node_id": clean_node,
                "removed_edge_ids": sorted(removed_edges),
            }

    def graph_edge_add(
        self,
        graph_id: str,
        source: str,
        target: str,
        kind: str = "dependency",
        condition: object = None,
        edge_id: str = "",
        label: str = "",
        metadata: Optional[Dict[str, object]] = None,
        expected_revision: int = -1,
    ) -> Dict[str, object]:
        with self._write_lock():
            data = self.load()
            graph = data["graphs"].get(str(graph_id))
            if not isinstance(graph, dict):
                raise KeyError("unknown graph_id")
            self._graph_revision_guard(graph, expected_revision)
            clean_edge_id = str(edge_id or "").strip() or "edge_" + uuid.uuid4().hex[:10]
            edge = normalize_edge(
                edge_id=clean_edge_id,
                source=source,
                target=target,
                kind=kind,
                condition=condition,
                label=label,
                metadata=metadata,
            )
            if clean_edge_id in graph["edges"]:
                raise ValueError("edge_id already exists")
            if str(edge["source"]) not in graph["nodes"]:
                raise KeyError("unknown source node")
            if str(edge["target"]) not in graph["nodes"]:
                raise KeyError("unknown target node")
            candidate = copy.deepcopy(graph)
            candidate["edges"][clean_edge_id] = edge
            validation = graph_validate(
                candidate,
                tasks={
                    str(task_id): task
                    for task_id, task in data["tasks"].items()
                    if isinstance(task, dict)
                },
            )
            if not validation.get("ok"):
                raise RuntimeError(
                    "graph edge would make graph invalid: {0}".format(
                        "; ".join(validation.get("errors") or [])
                    )
                )
            graph["edges"][clean_edge_id] = edge
            graph["revision"] = int(graph.get("revision") or 0) + 1
            graph["updated_at"] = time.time()
            self.save(data)
            return {
                "graph_id": str(graph_id),
                "revision": int(graph["revision"]),
                "edge": copy.deepcopy(edge),
                "validation": validation,
            }

    def graph_edge_update(
        self,
        graph_id: str,
        edge_id: str,
        patch: Dict[str, object],
        expected_revision: int = -1,
    ) -> Dict[str, object]:
        if not isinstance(patch, dict) or not patch:
            raise ValueError("edge patch must be a non-empty object")
        allowed = {"source", "target", "kind", "condition", "label", "metadata"}
        unknown = sorted(set(patch) - allowed)
        if unknown:
            raise ValueError("unsupported edge patch field(s): {0}".format(", ".join(unknown)))
        with self._write_lock():
            data = self.load()
            graph = data["graphs"].get(str(graph_id))
            if not isinstance(graph, dict):
                raise KeyError("unknown graph_id")
            self._graph_revision_guard(graph, expected_revision)
            existing = graph["edges"].get(str(edge_id))
            if not isinstance(existing, dict):
                raise KeyError("unknown edge_id")
            merged = copy.deepcopy(existing)
            merged.update(copy.deepcopy(patch))
            edge = normalize_edge(
                edge_id=str(edge_id),
                source=str(merged.get("source") or ""),
                target=str(merged.get("target") or ""),
                kind=str(merged.get("kind") or "dependency"),
                condition=merged.get("condition"),
                label=str(merged.get("label") or ""),
                metadata=merged.get("metadata") or {},
            )
            if str(edge["source"]) not in graph["nodes"]:
                raise KeyError("unknown source node")
            if str(edge["target"]) not in graph["nodes"]:
                raise KeyError("unknown target node")
            candidate = copy.deepcopy(graph)
            candidate["edges"][str(edge_id)] = edge
            validation = graph_validate(
                candidate,
                tasks={
                    str(task_id): task
                    for task_id, task in data["tasks"].items()
                    if isinstance(task, dict)
                },
            )
            if not validation.get("ok"):
                raise RuntimeError(
                    "graph edge update would make graph invalid: {0}".format(
                        "; ".join(validation.get("errors") or [])
                    )
                )
            graph["edges"][str(edge_id)] = edge
            graph["revision"] = int(graph.get("revision") or 0) + 1
            graph["updated_at"] = time.time()
            self.save(data)
            return {
                "graph_id": str(graph_id),
                "revision": int(graph["revision"]),
                "edge": copy.deepcopy(edge),
                "validation": validation,
            }

    def graph_edge_remove(
        self,
        graph_id: str,
        edge_id: str,
        expected_revision: int = -1,
    ) -> Dict[str, object]:
        clean_edge = str(edge_id or "").strip()
        if not clean_edge:
            raise ValueError("edge_id is required")
        with self._write_lock():
            data = self.load()
            graph = data["graphs"].get(str(graph_id))
            if not isinstance(graph, dict):
                raise KeyError("unknown graph_id")
            self._graph_revision_guard(graph, expected_revision)
            if clean_edge not in graph["edges"]:
                raise KeyError("unknown edge_id")
            graph["edges"].pop(clean_edge, None)
            graph["revision"] = int(graph.get("revision") or 0) + 1
            graph["updated_at"] = time.time()
            self.save(data)
            return {
                "graph_id": str(graph_id),
                "revision": int(graph["revision"]),
                "removed_edge_id": clean_edge,
            }

    def graph_validate(self, graph_id: str = "") -> Dict[str, object]:
        data = self.load()
        clean_id = str(graph_id or "").strip() or str(data.get("active_graph_id") or "")
        if not clean_id:
            return {
                "ok": True,
                "active": False,
                "graph_id": "",
                "errors": [],
                "warnings": ["no active graph"],
                "node_count": 0,
                "edge_count": 0,
                "task_bindings": {},
                "entry_nodes": [],
                "terminal_nodes": [],
            }
        graph = data["graphs"].get(clean_id)
        if not isinstance(graph, dict):
            raise KeyError("unknown graph_id")
        result = graph_validate(
            graph,
            tasks={
                str(task_id): task
                for task_id, task in data["tasks"].items()
                if isinstance(task, dict)
            },
        )
        result["graph_id"] = clean_id
        result["revision"] = int(graph.get("revision") or 0)
        result["active"] = clean_id == str(data.get("active_graph_id") or "")
        return result

    def interrupt_create(
        self,
        prompt: str,
        task_id: str = "",
        payload: object = None,
        interrupt_id: str = "",
    ) -> Dict[str, object]:
        """Pause durable work for human input and persist resumable interrupt state."""
        clean_prompt = str(prompt or "").strip()
        clean_task = str(task_id or "").strip()
        if not clean_prompt:
            raise ValueError("prompt is required")
        json.dumps(payload, ensure_ascii=False)
        with self._write_lock():
            data = self.load()
            task = None
            if clean_task:
                task = data["tasks"].get(clean_task)
                if not isinstance(task, dict):
                    raise KeyError("unknown task_id")
                step_id = str(task.get("superstep_id") or "")
                step = data["supersteps"].get(step_id) if step_id else None
                if isinstance(step, dict) and step.get("status") in {"open", "ready"}:
                    raise RuntimeError(
                        "task belongs to an active superstep; abort/commit it before interrupting"
                    )

            active_id = str(interrupt_id or "").strip() or "int_" + uuid.uuid4().hex[:10]
            if active_id in data["interrupts"]:
                raise ValueError("interrupt_id already exists")
            now = time.time()
            record = {
                "interrupt_id": active_id,
                "status": "open",
                "task_id": clean_task,
                "prompt": clean_prompt,
                "payload": payload,
                "resume_value": None,
                "created_at": now,
                "resumed_at": 0.0,
            }
            data["interrupts"][active_id] = record
            if isinstance(task, dict):
                previous_worker = str(task.get("claimed_by") or "")
                task["status"] = "blocked"
                task["summary"] = "human_input: {0}".format(clean_prompt)
                task["interrupt_id"] = active_id
                task["claimed_by"] = ""
                task["lease_expires_at"] = 0.0
                task["superstep_id"] = ""
                task["updated_at"] = now
                for reservation in data.get("task_resource_reservations", {}).values():
                    if (
                        isinstance(reservation, dict)
                        and reservation.get("status") == "active"
                        and str(reservation.get("task_id") or "") == clean_task
                        and (not previous_worker or str(reservation.get("worker_name") or "") == previous_worker)
                    ):
                        reservation["status"] = "released"
                        reservation["lease_expires_at"] = 0.0
                        reservation["updated_at"] = now
            self.save(data)
            return copy.deepcopy(record)

    def interrupt_resume(self, interrupt_id: str, value: object = None) -> Dict[str, object]:
        """Resume one durable interrupt and make its blocked task runnable again."""
        clean_id = str(interrupt_id or "").strip()
        if not clean_id:
            raise ValueError("interrupt_id is required")
        json.dumps(value, ensure_ascii=False)
        with self._write_lock():
            data = self.load()
            record = data["interrupts"].get(clean_id)
            if not isinstance(record, dict):
                raise KeyError("unknown interrupt_id")
            if record.get("status") == "resumed":
                return copy.deepcopy(record)
            if record.get("status") != "open":
                raise RuntimeError("interrupt is not open")
            now = time.time()
            record["status"] = "resumed"
            record["resume_value"] = value
            record["resumed_at"] = now

            task_id = str(record.get("task_id") or "")
            task = data["tasks"].get(task_id) if task_id else None
            if (
                isinstance(task, dict)
                and str(task.get("interrupt_id") or "") == clean_id
                and str(task.get("status") or "") == "blocked"
            ):
                task["status"] = "ready"
                task["summary"] = "human_input resolved: {0}".format(
                    str(record.get("prompt") or "")
                )
                task["interrupt_id"] = ""
                task["retry_after"] = 0.0
                task["updated_at"] = now
            self.save(data)
            return copy.deepcopy(record)

    def interrupts(self, status: str = "") -> List[Dict[str, object]]:
        clean_status = str(status or "").strip().lower()
        if clean_status and clean_status not in {"open", "resumed"}:
            raise ValueError("invalid interrupt status")
        values = []
        for item in self.load()["interrupts"].values():
            if not isinstance(item, dict):
                continue
            if clean_status and str(item.get("status") or "").lower() != clean_status:
                continue
            values.append(copy.deepcopy(item))
        return sorted(
            values,
            key=lambda item: (item.get("created_at", 0.0), item.get("interrupt_id", "")),
        )

    def checkpoint_create(
        self,
        label: str = "",
        metadata: Optional[Dict[str, object]] = None,
        retain: int = 20,
        parent_checkpoint_id: str = "",
    ) -> Dict[str, object]:
        """Persist a bounded coordination-state checkpoint for recovery/time-travel."""
        json.dumps(metadata or {}, ensure_ascii=False)
        keep = max(1, min(int(retain or 1), 200))
        with self._write_lock():
            data = self.load()
            checkpoint_id = "cp_" + uuid.uuid4().hex[:10]
            now = time.time()
            clean_parent = str(parent_checkpoint_id or "").strip()
            if clean_parent:
                if clean_parent not in data["checkpoints"]:
                    raise KeyError("unknown parent_checkpoint_id")
            else:
                head = str(data.get("checkpoint_head_id") or "").strip()
                if head in data["checkpoints"]:
                    clean_parent = head
                elif data["checkpoints"]:
                    clean_parent = str(max(
                        data["checkpoints"].values(),
                        key=lambda item: (
                            float(item.get("created_at") or 0.0),
                            str(item.get("checkpoint_id") or ""),
                        ),
                    ).get("checkpoint_id") or "")
            snapshot = {
                "tasks": copy.deepcopy(data["tasks"]),
                "task_steps": copy.deepcopy(data["task_steps"]),
                "shared_status": copy.deepcopy(data["shared_status"]),
                "supersteps": copy.deepcopy(data["supersteps"]),
                "interrupts": copy.deepcopy(data["interrupts"]),
                "graphs": copy.deepcopy(data["graphs"]),
                "active_graph_id": str(data.get("active_graph_id") or ""),
            }
            record = {
                "checkpoint_id": checkpoint_id,
                "parent_checkpoint_id": clean_parent,
                "label": str(label or "").strip(),
                "metadata": dict(metadata or {}),
                "created_at": now,
                "snapshot": snapshot,
            }
            data["checkpoints"][checkpoint_id] = record
            data["checkpoint_head_id"] = checkpoint_id
            ordered = sorted(
                data["checkpoints"].values(),
                key=lambda item: float(item.get("created_at") or 0.0),
            )
            while len(ordered) > keep:
                oldest = ordered.pop(0)
                data["checkpoints"].pop(str(oldest.get("checkpoint_id") or ""), None)
            self.save(data)
            return {
                "checkpoint_id": checkpoint_id,
                "parent_checkpoint_id": clean_parent,
                "label": record["label"],
                "metadata": copy.deepcopy(record["metadata"]),
                "created_at": now,
            }

    def checkpoints(self) -> List[Dict[str, object]]:
        data = self.load()
        head_id = str(data.get("checkpoint_head_id") or "")
        values = []
        for record in data["checkpoints"].values():
            if not isinstance(record, dict):
                continue
            checkpoint_id = str(record.get("checkpoint_id") or "")
            values.append({
                "checkpoint_id": checkpoint_id,
                "parent_checkpoint_id": str(record.get("parent_checkpoint_id") or ""),
                "is_head": checkpoint_id == head_id,
                "label": str(record.get("label") or ""),
                "metadata": copy.deepcopy(record.get("metadata") or {}),
                "created_at": float(record.get("created_at") or 0.0),
            })
        return sorted(values, key=lambda item: (item["created_at"], item["checkpoint_id"]))

    def checkpoint_lineage(self, checkpoint_id: str = "") -> Dict[str, object]:
        data = self.load()
        clean_id = str(checkpoint_id or data.get("checkpoint_head_id") or "").strip()
        if not clean_id:
            return {
                "checkpoint_id": "",
                "head_checkpoint_id": str(data.get("checkpoint_head_id") or ""),
                "lineage": [],
                "truncated": False,
            }
        if clean_id not in data["checkpoints"]:
            raise KeyError("unknown checkpoint_id")

        lineage = []
        seen = set()
        cursor = clean_id
        truncated = False
        while cursor:
            if cursor in seen:
                truncated = True
                break
            seen.add(cursor)
            record = data["checkpoints"].get(cursor)
            if not isinstance(record, dict):
                truncated = True
                break
            lineage.append({
                "checkpoint_id": cursor,
                "parent_checkpoint_id": str(record.get("parent_checkpoint_id") or ""),
                "label": str(record.get("label") or ""),
                "created_at": float(record.get("created_at") or 0.0),
            })
            parent = str(record.get("parent_checkpoint_id") or "")
            if parent and parent not in data["checkpoints"]:
                truncated = True
                break
            cursor = parent

        return {
            "checkpoint_id": clean_id,
            "head_checkpoint_id": str(data.get("checkpoint_head_id") or ""),
            "lineage": lineage,
            "truncated": truncated,
        }

    def checkpoint_restore(self, checkpoint_id: str) -> Dict[str, object]:
        """Restore durable coordination state while preserving browser bindings/checkpoint history."""
        clean_id = str(checkpoint_id or "").strip()
        if not clean_id:
            raise ValueError("checkpoint_id is required")
        with self._write_lock():
            data = self.load()
            now = time.time()
            active_leases = [
                str(task.get("task_id") or "")
                for task in data["tasks"].values()
                if isinstance(task, dict)
                and str(task.get("status") or "") == "active"
                and float(task.get("lease_expires_at") or 0.0) > now
            ]
            active_steps = [
                str(step.get("step_id") or "")
                for step in data["supersteps"].values()
                if isinstance(step, dict) and step.get("status") in {"open", "ready"}
            ]
            active_task_steps = [
                "{0}:{1}".format(task_id, step_key)
                for task_id, steps in data["task_steps"].items()
                if isinstance(steps, dict)
                for step_key, record in steps.items()
                if isinstance(record, dict)
                and str(record.get("status") or "") == "active"
                and float(record.get("lease_expires_at") or 0.0) > now
            ]
            live_lanes = [
                str(lane.get("lane_id") or "")
                for lane in data["lanes"].values()
                if isinstance(lane, dict)
                and lane.get("status") in {"creating", "active", "committing", "recovery_required"}
            ]
            if active_leases or active_steps or active_task_steps or live_lanes:
                raise RuntimeError(
                    "cannot restore while active leases/task-steps/supersteps/MVCC lanes exist: "
                    "leases={0}, task_steps={1}, supersteps={2}, lanes={3}".format(
                        ",".join(active_leases) or "none",
                        ",".join(active_task_steps) or "none",
                        ",".join(active_steps) or "none",
                        ",".join(live_lanes) or "none",
                    )
                )
            record = data["checkpoints"].get(clean_id)
            if not isinstance(record, dict):
                raise KeyError("unknown checkpoint_id")
            snapshot = record.get("snapshot") or {}
            for key in ("tasks", "shared_status", "supersteps", "interrupts"):
                value = snapshot.get(key)
                if not isinstance(value, dict):
                    raise RuntimeError("checkpoint is missing valid {0}".format(key))
                data[key] = copy.deepcopy(value)
            task_steps_value = snapshot.get("task_steps", {})
            if not isinstance(task_steps_value, dict):
                raise RuntimeError("checkpoint has invalid task_steps")
            data["task_steps"] = copy.deepcopy(task_steps_value)
            graph_value = snapshot.get("graphs", {})
            if not isinstance(graph_value, dict):
                raise RuntimeError("checkpoint is missing valid graphs")
            data["graphs"] = copy.deepcopy(graph_value)
            restored_active_graph = str(snapshot.get("active_graph_id") or "")
            data["active_graph_id"] = (
                restored_active_graph
                if restored_active_graph in data["graphs"]
                else ""
            )

            recovered_steps = []
            for step in data["supersteps"].values():
                if not isinstance(step, dict) or step.get("status") not in {"open", "ready"}:
                    continue
                step_id = str(step.get("step_id") or "")
                step["status"] = "aborted"
                step["aborted_at"] = now
                step["error"] = "checkpoint restore invalidated live superstep"
                if step_id:
                    recovered_steps.append(step_id)

            for task in data["tasks"].values():
                if not isinstance(task, dict):
                    continue
                if str(task.get("status") or "") == "active":
                    task["lease_expires_at"] = 0.0
                if str(task.get("superstep_id") or "") in recovered_steps:
                    task["superstep_id"] = ""
                    task["updated_at"] = now
            data["checkpoint_head_id"] = clean_id
            self.save(data)
            return {
                "checkpoint_id": clean_id,
                "parent_checkpoint_id": str(record.get("parent_checkpoint_id") or ""),
                "head_checkpoint_id": clean_id,
                "label": str(record.get("label") or ""),
                "restored": True,
                "restored_at": now,
                "task_count": len(data["tasks"]),
                "interrupt_count": len(data["interrupts"]),
                "graph_count": len(data["graphs"]),
                "active_graph_id": str(data.get("active_graph_id") or ""),
                "recovered_supersteps": recovered_steps,
            }
