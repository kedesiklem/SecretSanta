"""SMS through an SMSGate phone (faked): the client, and sending a pending message."""
import unittest

from fake_smsgate import FakeSmsGate
from helpers import Santa
import sender
from test_sender import SenderCase


class Numbers(unittest.TestCase):
    def test_cleaning_and_country_code(self):
        n = sender.normalize_phone
        self.assertEqual(n("06 12 34 56 78", "+33"), "+33612345678")
        self.assertEqual(n("06.12.34.56.78"), "0612345678")              # no country code given: never guessed
        self.assertEqual(n("0033 6 12 34 56 78"), "+33612345678")
        self.assertEqual(n("+44 (0)20 7946 0000".replace("(0)", "")), "+442079460000")
        self.assertEqual(n("+33 6 12 34 56 78", "+33"), "+33612345678")      # already international: untouched


class Gateway(unittest.TestCase):
    def setUp(self):
        self.phone = FakeSmsGate()
        self.addCleanup(self.phone.close)

    def gw(self, **kw):
        return sender.SmsGateway(kw.pop("url", self.phone.url), kw.pop("username", "user"), kw.pop("password", "secret"), wait=3, **kw)

    def test_settings_are_validated(self):
        for url, user, pw, extra in [("ftp://x", "u", "p", {}), ("http://u:p@host", "u", "p", {}), ("", "u", "p", {}),
                                     ("http://h", "", "p", {}), ("http://h", "u", "", {}), ("http://h", "u", "p", {"country_code": "33"})]:
            with self.subTest(url=url, user=user, extra=extra):
                with self.assertRaises(Santa.SantaError):
                    sender.SmsGateway(url, user, pw, **extra)

    def test_health_and_wrong_password(self):
        self.assertTrue(self.gw().health())
        with self.assertRaisesRegex(Santa.SantaError, "refuse"):
            self.gw(password="nope").health()

    def test_unreachable_phone_has_a_hint(self):
        self.phone.close()
        with self.assertRaises(Santa.SantaError) as cm:
            sender.SmsGateway(self.phone.url, "user", "secret", timeout=2).health()
        self.assertIn("injoignable", cm.exception.message)
        self.assertIn("même réseau", cm.exception.hint)

    def test_send_waits_for_the_state_and_sends_the_cleaned_number(self):
        state = self.gw(country_code="+33").send("06 12 34 56 78", "Salut")
        self.assertEqual(state, "Sent")
        self.assertEqual(self.phone.received[0]["numbers"], ["+33612345678"])
        self.assertEqual(self.phone.received[0]["text"], "Salut")

    def test_a_failure_reported_by_the_phone_is_an_error(self):
        self.phone.final_state = "Failed"
        with self.assertRaisesRegex(Santa.SantaError, "No service"):
            self.gw().send("+33612345678", "x")

    def test_still_pending_after_the_wait_counts_as_accepted(self):
        self.phone.polls_before_final = 10 ** 6                        # never leaves Pending
        gw = sender.SmsGateway(self.phone.url, "user", "secret", wait=1)
        self.assertEqual(gw.send("+33612345678", "x"), "Pending")

    def test_bad_request_is_reported(self):
        with self.assertRaisesRegex(Santa.SantaError, "400"):
            self.gw()._call("POST", "/message", {"phoneNumbers": []})       # the fake refuses a body without a text


class SendingAMessage(SenderCase):
    def setUp(self):
        super().setUp()
        self.phone = FakeSmsGate()
        self.addCleanup(self.phone.close)
        self.gw = sender.SmsGateway(self.phone.url, "user", "secret", country_code="+33", wait=3)

    def test_the_body_goes_out_without_the_subject_and_is_recorded(self):
        self.sender.send_sms("Bob", self.gw)
        sent = self.phone.received[0]
        self.assertEqual(sent["numbers"], ["+33600000002"])
        self.assertIn("Père Noël secret", sent["text"])
        self.assertNotIn("Subject", sent["text"])
        self.assertEqual(self.states()["Bob"], ("phone", "sent"))
        self.assertEqual(self.sender.status().deliveries[1].how, "sms")
        with self.assertRaisesRegex(Santa.SantaError, "déjà reçu"):
            self.sender.send_sms("Bob", self.gw)                        # never twice

    def test_failure_keeps_the_message_pending(self):
        self.phone.final_state = "Failed"
        with self.assertRaises(Santa.SantaError):
            self.sender.send_sms("Bob", self.gw)
        self.assertEqual(self.states()["Bob"][1], "pending")

    def test_someone_without_a_number_cannot_be_texted(self):
        with self.assertRaisesRegex(Santa.SantaError, "pas de numéro"):
            self.sender.send_sms("Alice", self.gw)                      # Alice only has an address
        self.assertEqual(self.phone.received, [])

    def test_cli_texts_only_people_reached_by_phone(self):
        import json, subprocess, sys
        from helpers import ROOT
        (self.dir / "sms.json").write_text(json.dumps({"url": self.phone.url, "username": "user", "password": "secret",
                                                       "country_code": "+33"}), encoding="utf-8")
        r = subprocess.run([sys.executable, str(ROOT / "sender.py"), "--base-dir", str(self.dir), "--sms"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(m["numbers"][0] for m in self.phone.received), ["+33600000002", "+33600000006"])
        self.assertEqual({k: v[1] for k, v in self.states().items() if v[0] == "phone"}, {"Bob": "sent", "Frank": "sent"})


if __name__ == "__main__":
    unittest.main()
