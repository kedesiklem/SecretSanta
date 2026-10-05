"""sender.py: contacts, delivery state and sending e-mail (with a fake msmtp)."""
import os
import stat
import subprocess
import sys
import unittest

from helpers import ROOT, ProjectCase, Santa
import sender


def person(**kw):
    return Santa.Person.from_dict({"name": "A", **kw})


class Contacts(unittest.TestCase):
    def test_everything_but_the_name_is_optional(self):
        p = person()
        c = sender.Contacts(p)
        self.assertEqual((c.channel, c.contact, p.to_dict()), ("none", "", {"name": "A"}))
        sender.check_contacts([p, person(email="e@x.org")])

    def test_channel_order_and_preference(self):
        p = person(phone="+33 6 12 34 56 78", messenger="alice.b", email="a@x.org")
        self.assertEqual([c for c, _ in sender.Contacts(p).all()], ["email", "phone", "messenger"])
        p = person(phone="+33 6 12 34 56 78", messenger="alice.b", email="a@x.org", prefer="messenger")
        c = sender.Contacts(p)
        self.assertEqual((c.channel, c.contact), ("messenger", "alice.b"))
        self.assertEqual(Santa.Person.from_dict(p.to_dict()).extra["prefer"], "messenger")   # round trip

    def test_validation(self):
        bad = [dict(email="nope"), dict(email="-oQ@x.org"), dict(phone="12"), dict(phone="abc 123 456"),
               dict(messenger="a\nb"), dict(other="x" * 400), dict(prefer="fax"), dict(prefer="phone"),
               dict(email="a@x.org", prefer="messenger"), dict(phone=123)]
        for extra in bad:
            with self.subTest(extra), self.assertRaises(Santa.SantaError):
                sender.check_contacts([person(**extra), Santa.Person("B")])


def fake_msmtp(directory):
    """A msmtp stand-in: logs the recipient and the message, fails for FAIL_FOR."""
    path = directory / "msmtp"
    path.write_text('#!/bin/bash\ncat >> "$MSMTP_LOG"\necho "TO $1" >> "$MSMTP_LOG"\n'
                    'for a in $FAIL_FOR; do [ "$1" == "$a" ] && { echo "550 refused" >&2; exit 1; }; done\nexit 0\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


MIXED = [
    {"name": "Alice", "email": "alice@x.org"},
    {"name": "Bob", "phone": "+33 6 00 00 00 02"},
    {"name": "Carol", "messenger": "carol.m"},
    {"name": "Dave", "other": "at lunch"},
    {"name": "Eve"},
    {"name": "Frank", "email": "frank@x.org", "phone": "0600000006", "prefer": "phone"},
]


class SenderCase(ProjectCase):
    """A drawn project where everybody can be reached differently."""

    def setUp(self):
        super().setUp()
        self.write("participants.json", MIXED)
        Santa.Project(self.dir).draw(2026, seed=3)
        self.sender = sender.Sender(self.dir)

    def states(self):
        return {d.name: (d.channel, d.state) for d in self.sender.status().deliveries}


class Status(SenderCase):
    def test_the_draw_alone_creates_no_message_and_no_state(self):
        self.assertFalse((self.dir / "delivery").exists())
        self.assertFalse((self.dir / "secretSantaFiles").exists())

    def test_listing_follows_the_participants_and_never_has_content(self):
        st = self.sender.status()
        self.assertEqual(st.year, 2026)
        ds = st.deliveries
        self.assertEqual([d.name for d in ds], ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank"])
        self.assertEqual([d.channel for d in ds], ["email", "phone", "messenger", "other", "none", "phone"])
        self.assertEqual(ds[0].contact, "alice@x.org")
        self.assertTrue(all(d.state == "pending" for d in ds))

    def test_no_draw_is_a_clear_error(self):
        empty = sender.Sender(self.dir, history_dir="nothing")
        with self.assertRaisesRegex(Santa.SantaError, "Aucun tirage"):
            empty.status()
        with self.assertRaisesRegex(Santa.SantaError, "2020"):
            self.sender.status(2020)

    def test_the_channel_follows_the_contacts_as_they_are_now_not_at_the_draw(self):
        self.assertEqual(self.states()["Eve"], ("none", "pending"))
        proj = self.sender.project
        people = proj.load_people()
        next(p for p in people if p.name == "Eve").extra["email"] = "eve@x.org"
        proj.save_people(people)
        self.assertEqual(self.states()["Eve"], ("email", "pending"))

    def test_preview_never_uses_real_names(self):
        subject, body, source = self.sender.preview()
        self.assertIn(sender.PREVIEW_RECIPIENT, body)
        self.assertFalse(any(n in body for n in ("Alice", "Bob", "Carol")))


class Handing(SenderCase):
    def test_text_is_the_body_without_the_subject_and_marking_is_once(self):
        m = self.sender.text_for("Bob")
        self.assertEqual((m["channel"], m["contact"], m["year"]), ("phone", "+33 6 00 00 00 02", 2026))
        self.assertIn("Père Noël secret", m["text"])
        self.assertNotIn("Subject", m["text"])
        self.sender.mark_delivered("Bob")
        self.assertEqual(self.states()["Bob"], ("phone", "sent"))
        with self.assertRaisesRegex(Santa.SantaError, "déjà reçu"):
            self.sender.mark_delivered("Bob")
        with self.assertRaisesRegex(Santa.SantaError, "déjà reçu"):
            self.sender.text_for("Bob")

    def test_unknown_names_are_refused_and_nothing_is_written(self):
        for bad in ("../participants", "a/b", ".hidden", "", "Zed"):
            with self.subTest(bad), self.assertRaises(Santa.SantaError):
                self.sender.mark_delivered(bad)
        self.assertFalse((self.dir / "delivery").exists())

    def test_a_new_draw_makes_sent_ones_pending_again(self):
        self.sender.mark_delivered("Alice")
        Santa.Project(self.dir).draw(2026, seed=4)               # same year, replaced
        self.assertEqual(self.states()["Alice"], ("email", "pending"))

    def test_state_survives_renaming_nobody_and_follows_the_id(self):
        self.sender.mark_delivered("Alice")
        proj = self.sender.project
        people = proj.load_people()
        next(p for p in people if p.name == "Alice").name = "Alicia"
        proj.save_people(people)
        self.assertEqual(self.states()["Alicia"][1], "sent")

    def test_the_state_file_holds_no_pair(self):
        self.sender.mark_delivered("Alice")
        text = (self.dir / "delivery/2026.json").read_text(encoding="utf-8")
        self.assertIn("manual", text)
        self.assertNotIn("assignments", text)


class Mail(SenderCase):
    def setUp(self):
        super().setUp()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.cmd = str(fake_msmtp(self.bin))
        self.log = self.dir / "msmtp.log"
        os.environ["MSMTP_LOG"] = str(self.log)
        self.addCleanup(os.environ.pop, "MSMTP_LOG", None)
        self.addCleanup(os.environ.pop, "FAIL_FOR", None)

    def test_send_records_and_pipes_a_complete_message(self):
        self.sender.send_mail("Alice", command=self.cmd)
        self.assertEqual(self.states()["Alice"], ("email", "sent"))
        logged = self.log.read_text(encoding="utf-8")
        self.assertIn("TO alice@x.org", logged)
        self.assertIn("To: alice@x.org", logged)
        self.assertIn("Subject:", logged)
        with self.assertRaisesRegex(Santa.SantaError, "déjà reçu"):
            self.sender.send_mail("Alice", command=self.cmd)         # never twice

    def test_failure_records_nothing_and_a_retry_works(self):
        os.environ["FAIL_FOR"] = "alice@x.org"
        with self.assertRaisesRegex(Santa.SantaError, "550 refused"):
            self.sender.send_mail("Alice", command=self.cmd)
        self.assertEqual(self.states()["Alice"], ("email", "pending"))
        os.environ["FAIL_FOR"] = ""
        self.sender.send_mail("Alice", command=self.cmd)
        self.assertEqual(self.states()["Alice"], ("email", "sent"))

    def test_missing_msmtp_has_a_hint(self):
        with self.assertRaises(Santa.SantaError) as cm:
            self.sender.send_mail("Alice", command=str(self.dir / "nope"))
        self.assertIn("introuvable", cm.exception.message)
        self.assertTrue(cm.exception.hint)

    def test_env_var_replaces_the_command(self):
        os.environ["SANTA_MSMTP"] = self.cmd
        self.addCleanup(os.environ.pop, "SANTA_MSMTP", None)
        self.sender.send_mail("Alice")
        self.assertEqual(self.states()["Alice"][1], "sent")

    def test_the_address_is_the_current_one(self):
        proj = self.sender.project
        people = proj.load_people()
        next(p for p in people if p.name == "Eve").extra["email"] = "eve@x.org"    # added AFTER the draw
        proj.save_people(people)
        self.sender.send_mail("Eve", command=self.cmd)
        self.assertIn("To: eve@x.org", self.log.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(Santa.SantaError, "pas d'adresse"):
            self.sender.send_mail("Bob", command=self.cmd)           # Bob only has a phone

    def test_a_custom_message_file_is_used(self):
        self.write("message.txt", "Subject: Cadeau {year}\n\nPour {recipient} de la part de {santa}\n")
        self.sender.send_mail("Alice", command=self.cmd)
        self.assertIn("Subject: Cadeau 2026", self.log.read_text(encoding="utf-8"))


class Cli(SenderCase):
    def run_sender(self, *args, cwd=None, **env):
        e = {**os.environ, **env}
        return subprocess.run([sys.executable, str(ROOT / "sender.py"), "--base-dir", str(self.dir), *args],
                              cwd=cwd or self.dir, capture_output=True, text=True, timeout=60, env=e)

    def test_needs_an_action(self):
        self.assertEqual(self.run_sender().returncode, 2)

    def test_status_preview_show_mark(self):
        r = self.run_sender("--status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Alice", r.stdout)
        self.assertNotIn("→", r.stdout)                                   # never a pair
        self.assertIn("Rudolph", self.run_sender("--preview").stdout)
        self.assertIn("Père Noël secret", self.run_sender("--show", "Bob").stdout)
        self.assertIn("marqué", self.run_sender("--mark", "Bob").stdout)
        self.assertIn("✅ Bob", self.run_sender("--status").stdout)

    def test_send_only_touches_people_reached_by_email(self):
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        cmd = fake_msmtp(bin_dir)
        r = self.run_sender("--send", SANTA_MSMTP=str(cmd), MSMTP_LOG=str(self.dir / "m.log"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("message(s) envoyé(s), 0 échec(s)", r.stdout)
        self.assertIn("restent à remettre", r.stdout)
        self.assertEqual({k: v[1] for k, v in self.states().items()},
                         {"Alice": "sent", "Bob": "pending", "Carol": "pending", "Dave": "pending", "Eve": "pending", "Frank": "pending"})
        again = self.run_sender("--send", SANTA_MSMTP=str(cmd), MSMTP_LOG=str(self.dir / "m.log"))
        self.assertIn("0 message(s) envoyé(s)", again.stdout)             # never twice

    def test_failures_exit_one_and_leave_the_message_pending(self):
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        cmd = fake_msmtp(bin_dir)
        r = self.run_sender("--send", SANTA_MSMTP=str(cmd), MSMTP_LOG=str(self.dir / "m.log"), FAIL_FOR="alice@x.org")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.states()["Alice"][1], "pending")

    def test_sms_without_a_gateway_is_a_clear_error(self):
        r = self.run_sender("--sms")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Passerelle SMS non configurée", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)


class Independence(unittest.TestCase):
    def test_santa_py_knows_nothing_about_delivery(self):
        source = (ROOT / "Santa.py").read_text(encoding="utf-8")
        for word in ("msmtp", "smtplib", "EmailMessage", "SmsGateway", "sender"):
            self.assertNotIn(word, source.replace("sender.py", "").replace("``sender``", ""), word)


if __name__ == "__main__":
    unittest.main()
