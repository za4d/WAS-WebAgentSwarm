"""HTTP client for the local browser companion bridge."""

import json
import os
from pathlib import Path
from typing import Dict, Optional
from urllib import error, parse, request


BRIDGE_PROTOCOL_MIN = 5
BRIDGE_PROTOCOL_MAX = 5


def default_bridge_token_file() -> Path:
    configured = os.environ.get("WEB_SWARM_BRIDGE_TOKEN_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".web-agent-swarm" / "bridge-token").resolve()


def load_bridge_token() -> str:
    configured = str(os.environ.get("WEB_SWARM_BRIDGE_TOKEN") or "").strip()
    if configured:
        return configured
    path = default_bridge_token_file()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class BridgeClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 4.0,
        token: Optional[str] = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("WEB_SWARM_BRIDGE_URL")
            or "http://127.0.0.1:8788"
        ).rstrip("/")
        self.timeout = max(0.2, float(timeout))
        self.token_file = default_bridge_token_file()
        self.token = str(token if token is not None else load_bridge_token()).strip()

    def _json(
        self,
        path: str,
        method: str = "GET",
        payload: Optional[Dict[str, object]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, object]:
        data = None
        if not self.token:
            self.token = load_bridge_token()
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-WAS-Token"] = self.token
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with request.urlopen(req, timeout=timeout or self.timeout) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
                return body if isinstance(body, dict) else {
                    "ok": False,
                    "error": "bridge returned non-object JSON",
                }
        except error.HTTPError as exc:
            if exc.code == 401:
                refreshed = load_bridge_token()
                if refreshed and refreshed != self.token:
                    self.token = refreshed
                    return self._json(path, method=method, payload=payload, timeout=timeout)
            try:
                body = json.loads(exc.read().decode("utf-8") or "{}")
                if isinstance(body, dict):
                    body.setdefault("ok", False)
                    body.setdefault("http_status", exc.code)
                    return body
            except Exception:
                pass
            return {"ok": False, "http_status": exc.code, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": "bridge unavailable: {0}".format(exc)}

    def health(self) -> Dict[str, object]:
        result = self._json("/health", timeout=1.5)
        result["base_url"] = self.base_url
        result["token_configured"] = bool(self.token)
        result["token_file"] = str(self.token_file)
        if result.get("ok"):
            bridge_min = int(result.get("protocolMin") or result.get("protocolVersion") or 0)
            bridge_max = int(result.get("protocolMax") or result.get("protocolVersion") or 0)
            result["protocol_compatible"] = (
                bridge_max >= BRIDGE_PROTOCOL_MIN
                and bridge_min <= BRIDGE_PROTOCOL_MAX
            )
        else:
            result["protocol_compatible"] = False
        return result

    def tabs(self) -> Dict[str, object]:
        return self._json("/tabs", timeout=6.0)

    def events(
        self,
        since: int = 0,
        limit: int = 100,
        wait_ms: int = 0,
    ) -> Dict[str, object]:
        query = parse.urlencode({
            "since": max(0, int(since or 0)),
            "limit": max(1, min(int(limit or 100), 512)),
            "waitMs": max(0, min(int(wait_ms or 0), 30000)),
        })
        timeout = max(self.timeout, (max(0, int(wait_ms or 0)) / 1000.0) + 2.0)
        return self._json("/events?" + query, timeout=timeout)

    def open_tab(
        self,
        url: str = "https://chatgpt.com/",
        active: bool = False,
    ) -> Dict[str, object]:
        result = self._json(
            "/open",
            method="POST",
            payload={"url": url, "active": bool(active)},
            timeout=12.0,
        )
        if not result.get("ok"):
            result.setdefault(
                "policy",
                "current_connected_browser_profile_only",
            )
            result.setdefault(
                "recovery",
                (
                    "Reload/update the Web Agent Swarm extension in the currently "
                    "connected browser profile; WAS will not launch another Edge/Chrome profile."
                ),
            )
        return result

    def inspect(self, tab_id: int, tail: int = 6) -> Dict[str, object]:
        query = parse.urlencode({
            "tabId": int(tab_id),
            "tail": max(1, min(int(tail), 20)),
        })
        return self._json("/inspect?" + query, timeout=7.0)

    def close_tab(self, tab_id: int) -> Dict[str, object]:
        return self._json(
            "/close",
            method="POST",
            payload={"tabId": int(tab_id)},
            timeout=9.0,
        )

    def refresh_tab(self, tab_id: int) -> Dict[str, object]:
        """Recycle one exact ChatGPT conversation into a fresh renderer/tab.

        This deliberately uses only list/open/close primitives already supported by
        the deployed extension. Reopening the exact conversation URL and retiring
        the stale renderer is equivalent to a hard refresh for WAS purposes and
        does not change conversation identity or durable swarm ownership.
        """
        target_id = int(tab_id)
        tabs_result = self.tabs()
        if not tabs_result.get("ok"):
            return tabs_result
        target = next(
            (
                item
                for item in tabs_result.get("tabs", [])
                if isinstance(item, dict) and int(item.get("id", -1)) == target_id
            ),
            None,
        )
        if not target:
            return {"ok": False, "error": "target tab was not found", "tabId": target_id}
        url = str(target.get("url") or "")
        if not url.startswith("https://chatgpt.com/"):
            return {"ok": False, "error": "target is not a ChatGPT tab", "tabId": target_id}

        opened = self.open_tab(url=url, active=bool(target.get("active")))
        new_tab = opened.get("tab") if opened.get("ok") else None
        if not new_tab:
            return {
                "ok": False,
                "error": opened.get("error") or "failed to reopen ChatGPT conversation",
                "tabId": target_id,
            }
        new_id = int(new_tab.get("id", -1))
        if new_id < 0 or new_id == target_id:
            return {"ok": False, "error": "reopened ChatGPT tab id is invalid", "tabId": target_id}

        closed = self.close_tab(target_id)
        if not closed.get("ok"):
            # Fail closed: do not leave a duplicate replacement behind.
            self.close_tab(new_id)
            return {
                "ok": False,
                "error": closed.get("error") or "failed to close stale ChatGPT tab",
                "tabId": target_id,
            }
        return {
            "ok": True,
            "oldTabId": target_id,
            "tabId": new_id,
            "tab": new_tab,
            "recycled": True,
        }

    def command_status(self, command_id: str) -> Dict[str, object]:
        clean = str(command_id or "").strip()[:128]
        if not clean:
            return {"ok": False, "error": "command_id is required"}
        query = parse.urlencode({"commandId": clean})
        return self._json("/command?" + query, timeout=7.0)

    def send(
        self,
        tab_id: int,
        text: str,
        command_id: str = "",
        expected_url: str = "",
    ) -> Dict[str, object]:
        message = str(text or "").strip()
        if not message:
            return {"ok": False, "error": "text is required"}
        clean_command_id = str(command_id or "").strip()
        pinned_url = str(expected_url or "").strip()
        if not clean_command_id or not pinned_url:
            return {"ok": False, "error": "command_id and pinned expected_url are required"}
        payload = {
            "tabId": int(tab_id),
            "text": message,
            "commandId": clean_command_id,
            "expectedUrl": pinned_url,
        }
        return self._json(
            "/send",
            method="POST",
            payload=payload,
            timeout=14.0,
        )
