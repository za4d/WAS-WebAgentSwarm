"""SR3 DI-01b — actual DurableStore and on-disk state, no synthetic shim.

Only disposable local files; no bridge, browser, account, or real Send.
"""
import tempfile
import unittest
from pathlib import Path

from web_agent_swarm.state import DurableStore


class SR3DiagnosticIntegrity(unittest.TestCase):
    def test_uncertain_attempt_never_relabels_definite_outcome_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            db=DurableStore(root)
            ident="r8_"+"d"*32
            db.send_transaction_record(
                ident, name="reviewer", tab_id=73, status="attempting",
                may_have_executed=True, relay_attempt_id="d"*32,
                relay_recipient="R", relay_generation=1, body_sha256="e"*64,
                expected_url="https://chatgpt.com/c/rrrr3333",
            )
            reopened=DurableStore(root)
            original=reopened.send_transaction_get(ident)
            for status in ("failed","busy","ok"):
                with self.subTest(status=status):
                    with self.assertRaisesRegex(ValueError,"uncertain R8 Send"):
                        reopened.send_transaction_record(
                            ident,status=status,result={"ok":status=="ok"},
                            may_have_executed=True,
                        )
                    self.assertEqual(DurableStore(root).send_transaction_get(ident),original)
            uncertain=reopened.send_transaction_record(
                ident,status="unknown",result={"error":"unknown callback"},
                may_have_executed=True,
            )
            self.assertEqual(uncertain["status"],"unknown")
            self.assertTrue(uncertain["may_have_executed"])

    def test_terminal_null_result_cannot_receive_late_success_or_failure(self):
        for initial in ("unknown","ack_timeout","failed"):
            with self.subTest(initial=initial),tempfile.TemporaryDirectory() as d:
                store=DurableStore(Path(d))
                ident="r8_"+"b"*32
                store.send_transaction_record(ident,name="worker",tab_id=72,
                    relay_attempt_id="b"*32,status="attempting",
                    relay_recipient="R",relay_generation=1,
                    body_sha256="a"*64,
                    expected_url="https://chatgpt.com/c/rrrr3333")
                store.send_transaction_record(ident,status=initial,
                    may_have_executed=(initial!="failed"))
                reopened=DurableStore(Path(d))
                before=reopened.send_transaction_get(ident)
                self.assertIsNone(before["result"])
                for data in ({"ok":True}, {"ok":False,"error":"late"}):
                    with self.assertRaisesRegex(ValueError,"immutable terminal"):
                        reopened.send_transaction_record(
                            ident,status=initial,result=data,
                        )
                    self.assertEqual(DurableStore(Path(d)).send_transaction_get(ident),before)
                replay=reopened.send_transaction_record(ident,status=initial)
                self.assertEqual(replay["result"],None)

    def test_legitimate_pre_effect_and_first_completion_still_work(self):
        with tempfile.TemporaryDirectory() as d:
            store=DurableStore(Path(d))
            ident="r8_"+"a"*32
            store.send_transaction_record(ident,name="reviewer",tab_id=73,
                status="attempting",relay_attempt_id="a"*32)
            done=store.send_transaction_record(ident,status="ok",
                result={"ok":True},may_have_executed=True)
            self.assertEqual(done["status"],"ok")
            self.assertEqual(done["result"],{"ok":True})
            with self.assertRaisesRegex(ValueError,"immutable terminal"):
                store.send_transaction_record(ident,status="ok",result={"ok":False})

        with tempfile.TemporaryDirectory() as d:
            store=DurableStore(Path(d))
            ident="r8_"+"c"*32
            store.send_transaction_record(ident,name="reviewer",tab_id=73,
                status="attempting",relay_attempt_id="c"*32)
            result=store.send_transaction_record(ident,status="failed",
                result={"ok":False,"pre_effect":True},may_have_executed=False)
            self.assertEqual(result["status"],"failed")
            self.assertFalse(result["may_have_executed"])

    def test_uncertain_standalone_receipt_cannot_downgrade(self):
        with tempfile.TemporaryDirectory() as d:
            store=DurableStore(Path(d))
            store.send_transaction_record("plain",name="worker",tab_id=19,
                status="attempting",may_have_executed=True)
            with self.assertRaisesRegex(ValueError,"uncertain R8 Send"):
                store.send_transaction_record("plain",status="failed")
            self.assertEqual(store.send_transaction_get("plain")["status"],"attempting")


if __name__=="__main__":
    unittest.main()
