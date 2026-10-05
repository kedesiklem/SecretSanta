"""ssm.sh with a fake msmtp: project folders, sent/ tracking, exit codes."""
import os
import stat
import subprocess
import unittest

from helpers import ROOT, ProjectCase


class SsmCase(ProjectCase):
    def setUp(self):
        super().setUp()
        self.bin = self.dir / "fakebin"
        self.bin.mkdir()
        self.log = self.dir / "msmtp.log"
        fake = self.bin / "msmtp"
        # Records each recipient; fails for the addresses listed in $FAIL_FOR (space separated).
        fake.write_text('#!/bin/bash\ncat > /dev/null\necho "$1" >> "$MSMTP_LOG"\n'
                        'for a in $FAIL_FOR; do [ "$1" == "$a" ] && exit 1; done\nexit 0\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def ssm(self, *args, cwd=None, stdin=None, fail_for=""):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "MSMTP_LOG": str(self.log), "FAIL_FOR": fail_for}
        return subprocess.run(["bash", str(ROOT / "ssm.sh"), *args], cwd=cwd or self.dir, env=env, input=stdin,
                              capture_output=True, text=True, timeout=180)

    def sent_to(self):
        return sorted(self.log.read_text().split()) if self.log.exists() else []


class ProjectFolder(SsmCase):
    def test_works_from_anywhere_on_a_given_project_folder(self):
        proj = self.dir / "sets" / "Famille"
        self.project(rules={})                                  # files land in self.dir; move them into the set
        for f in ("participants.json", "rules.json"):
            (proj).mkdir(parents=True, exist_ok=True)
            (self.dir / f).rename(proj / f)
        elsewhere = self.dir / "elsewhere"
        elsewhere.mkdir()
        r = self.ssm("-D", str(proj), "-g", "-n", cwd=elsewhere)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Un tirage valide existe", r.stdout)
        r = self.ssm("-D", str(proj), "-g", "-y", "2026", cwd=elsewhere)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((proj / "history/2026.json").exists() and (proj / "secretSantaFiles/Alice.mail").exists())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_missing_project_folder_is_an_error(self):
        r = self.ssm("-D", str(self.dir / "nope"), "-g")
        self.assertEqual(r.returncode, 1)
        self.assertIn("n'existe pas", r.stderr)

    def test_generation_options_still_need_g(self):
        self.assertEqual(self.ssm("-n").returncode, 1)

    def test_failed_generation_stops_before_sending(self):
        self.project(names=("A", "B", "C"), rules={"forced": [{"from": "A", "to": "B"}, {"from": "A", "to": "C"}]})
        (self.dir / "secretSantaFiles").mkdir()
        (self.dir / "secretSantaFiles/A.mail").write_text("old@x.org\nstale")
        r = self.ssm("-g", "-s")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.sent_to(), [])                    # a stale mail must never go out after a failed draw


class Sending(SsmCase):
    def make_mails(self, names=("alice", "bob", "carol")):
        d = self.dir / "secretSantaFiles"
        d.mkdir(exist_ok=True)
        for n in names:
            (d / f"{n}.mail").write_text(f"{n}@example.org\nTo: {n}@example.org\nSubject: s\n\nbody\n")
        return d

    def test_default_folder_sends_all_and_moves_them_to_sent(self):
        d = self.make_mails()
        r = self.ssm("-s")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.sent_to(), ["alice@example.org", "bob@example.org", "carol@example.org"])
        self.assertEqual(sorted(p.name for p in (d / "sent").iterdir()), ["alice.mail", "bob.mail", "carol.mail"])
        self.assertEqual(list(d.glob("*.mail")), [])
        self.assertIn("3 mail(s) envoyé(s), 0 échec(s)", r.stdout)

    def test_running_again_never_sends_twice(self):
        self.make_mails()
        self.ssm("-s")
        self.ssm("-s")
        self.assertEqual(len(self.sent_to()), 3)

    def test_failure_keeps_the_mail_exits_one_and_a_retry_sends_only_that_one(self):
        d = self.make_mails()
        r = self.ssm("-s", fail_for="bob@example.org")
        self.assertEqual(r.returncode, 1)
        self.assertEqual([p.name for p in d.glob("*.mail")], ["bob.mail"])
        self.assertIn("1 échec", r.stdout)
        self.log.unlink()
        r = self.ssm("-s")
        self.assertEqual((r.returncode, self.sent_to()), (0, ["bob@example.org"]))

    def test_explicit_folder_and_project_folder(self):
        proj = self.dir / "p"
        (proj / "secretSantaFiles").mkdir(parents=True)
        (proj / "secretSantaFiles/x.mail").write_text("x@example.org\nbody")
        self.assertEqual(self.ssm("-D", str(proj), "-s").returncode, 0)
        self.assertEqual(self.sent_to(), ["x@example.org"])
        other = self.dir / "other"
        other.mkdir()
        (other / "y.mail").write_text("y@example.org\nbody")
        self.assertEqual(self.ssm("-s", "-d", str(other)).returncode, 0)
        self.assertEqual(self.sent_to(), ["x@example.org", "y@example.org"])

    def test_missing_mail_folder_is_an_error(self):
        r = self.ssm("-s")
        self.assertEqual(r.returncode, 1)
        self.assertIn("n'existe pas", r.stderr)

    def test_clear_removes_pending_and_sent_mails(self):
        d = self.make_mails()
        self.ssm("-s", fail_for="bob@example.org")
        self.assertTrue((d / "sent").exists())
        self.assertEqual(self.ssm("-c").returncode, 0)
        self.assertEqual(list(d.rglob("*.mail")), [])
        self.assertFalse((d / "sent").exists())


class AddingRules(SsmCase):
    def test_interactive_forbidden_rule_in_a_project_folder(self):
        proj = self.dir / "proj"
        self.write("proj/participants.json", [{"name": n, "email": f"{n}@x.org"} for n in ("Ann", "Ben", "Cy")])
        # menu choice 1 (forbidden), giver #1, receiver #2, no advanced options
        r = self.ssm("-D", str(proj), "-r", stdin="1\n1\n2\nn\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read("proj/rules.json"), {"forbidden": [{"from": "Ann", "to": "Ben"}]})


if __name__ == "__main__":
    unittest.main()
