"""Isolated regression for duplicate effectful bridge requests within one WAS Send.

Exercises genuine stock-derived SwarmRuntime and DurableStore against a fake
bridge ONLY. No actual browser or ChatGPT content. The fake bridge counts calls
at the potentially effectful /send interface.
"""
import tempfile
import unittest
from pathlib import Path

from web_agent_swarm.state import DurableStore
from web_agent_swarm.swarm import SwarmRuntime


class ControlledBridge:
    def __init__(self, send_reply=None, generating=False):
        self.calls = []
        self.send_reply = send_reply or {"ok": False, "http_status": 504,
                                         "error": "Timed out waiting for extension response",
                                         "mayHaveExecuted": True}
        self.generating = generating

    def tabs(self):
        return {"ok": True, "tabs": [{
            "id": 4, "windowId": 1, "index": 0, "active": True,
            "url": "https://chatgpt.com/c/rrrr3333",
        }]}

    def inspect(self, tab_id, tail=1):
        return {"ok": True, "snapshot": {"generating": self.generating}}

    def send(self, tab_id, text, command_id="", expected_url=""):
        self.calls.append((tab_id, text, command_id))
        if len(self.calls) > 1:
            # A once-unknown effect cannot be safely retried by this API.
            return {"ok": True, "error": "unsafe synthetic repeated dispatch"}
        return dict(self.send_reply)


class RuntimeSendEffectBoundaries(unittest.TestCase):
    def run_case(self, bridge):
        with tempfile.TemporaryDirectory() as temp:
            store = DurableStore(Path(temp))
            store.bind("reviewer", {
                "kind": "worker", "role": "reviewer",
                "conversation_id": "rrrr3333",
                "url_at_bind": "https://chatgpt.com/c/rrrr3333",
            })
            runtime = SwarmRuntime(bridge=bridge, store=store)
            outcome = runtime.send("reviewer", "Bounded offline message")
            transactions = store.send_transactions()
            return outcome, transactions

    def test_timeout_after_possible_send_never_replayed(self):
        bridge = ControlledBridge()
        outcome, transactions = self.run_case(bridge)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["attempts"], 1)
        self.assertEqual(len(bridge.calls), 1)
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["status"], "ack_timeout")
        self.assertTrue(transactions[0]["may_have_executed"])

    def test_explicit_pre_effect_failure_still_not_blindly_replayed(self):
        bridge = ControlledBridge(send_reply={
            "ok": False, "error": "synthetic tab not ready",
            "mayHaveExecuted": False
        })
        outcome, transactions = self.run_case(bridge)
        self.assertFalse(outcome["ok"])
        self.assertEqual(len(bridge.calls), 1)
        self.assertEqual(transactions[0]["status"], "failed")

    def test_first_success_is_one_effectful_dispatch(self):
        bridge = ControlledBridge(send_reply={"ok": True, "status": "synthetic ACK"})
        outcome, transactions = self.run_case(bridge)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["attempts"], 1)
        self.assertEqual(len(bridge.calls), 1)
        self.assertEqual(transactions[0]["status"], "ok")

    def test_non_timeout_502_after_possible_click_remains_unknown_effect(self):
        # The real extension can return a 502 with mayHaveExecuted=true when
        # its content script clicked but the callback failed. It is NOT safe
        # to record that outcome as an ordinary pre-effect 'failed' send.
        bridge = ControlledBridge(send_reply={
            "ok": False, "http_status": 502,
            "error": "Synthetic callback lost after modelled click",
            "mayHaveExecuted": True,
        })
        outcome, transactions = self.run_case(bridge)
        self.assertFalse(outcome["ok"])
        self.assertEqual(len(bridge.calls), 1)
        self.assertTrue(outcome["send_health"]["may_have_executed"])
        self.assertEqual(transactions[0]["status"], "unknown")
        self.assertTrue(transactions[0]["may_have_executed"])

    def test_acknowledged_click_receipt_records_possible_effect(self):
        bridge = ControlledBridge(send_reply={
            "ok": True, "status": "synthetic click acknowledgement",
            "mayHaveExecuted": True,
        })
        outcome, transactions = self.run_case(bridge)
        self.assertTrue(outcome["ok"])
        self.assertTrue(outcome["send_health"]["may_have_executed"])
        self.assertEqual(transactions[0]["status"], "ok")
        self.assertTrue(transactions[0]["may_have_executed"])

    def test_generating_tab_has_zero_effectful_dispatch(self):
        bridge = ControlledBridge(generating=True)
        outcome, transactions = self.run_case(bridge)
        self.assertFalse(outcome["ok"])
        self.assertTrue(outcome["busy"])
        self.assertEqual(len(bridge.calls), 0)
        self.assertEqual(transactions[0]["status"], "busy")


if __name__ == "__main__":
    unittest.main()
