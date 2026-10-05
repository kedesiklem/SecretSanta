"""ssm.sh with a fake msmtp: it drives Santa.py (-g) and sender.py (-m -l -s), nothing more."""
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
        self.assertTrue((proj / "history/2026.json").exists())
        self.assertFalse((proj / "secretSantaFiles").exists())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_missing_project_folder_is_an_error(self):
        r = self.ssm("-D", str(self.dir / "nope"), "-g")
        self.assertEqual(r.returncode, 1)
        self.assertIn("n'existe pas", r.stderr)

    def test_generation_options_still_need_g(self):
        self.assertEqual(self.ssm("-n").returncode, 1)

    def test_message_options_need_a_message_action(self):
        self.assertEqual(self.ssm("-t", "m.txt").returncode, 1)

    def test_failed_generation_stops_before_sending(self):
        self.project(names=("A", "B", "C"), rules={"forced": [{"from": "A", "to": "B"}, {"from": "A", "to": "C"}]})
        r = self.ssm("-g", "-s")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.sent_to(), [])                    # never send after a failed draw


class Sending(SsmCase):
    def setUp(self):
        super().setUp()
        self.write("participants.json", [{"name": "Alice", "email": "alice@example.org"},
                                         {"name": "Bob", "email": "bob@example.org"},
                                         {"name": "Carol", "phone": "0600000003"}])
        self.write("rules.json", {})

    def test_draw_then_send_then_status(self):
        r = self.ssm("-g", "-s", "-y", "2026")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.sent_to(), ["alice@example.org", "bob@example.org"])
        self.assertIn("2 message(s) envoyé(s), 0 échec(s)", r.stdout)
        self.assertIn("1 message(s) restent à remettre", r.stdout)          # Carol only has a phone
        self.assertTrue((self.dir / "delivery/2026.json").exists())
        self.assertFalse((self.dir / "secretSantaFiles").exists())
        st = self.ssm("-l")
        self.assertEqual(st.returncode, 0, st.stderr)
        self.assertIn("✅ Alice", st.stdout)
        self.assertIn("⏳ Carol", st.stdout)

    def test_running_again_never_sends_twice(self):
        self.ssm("-g", "-s")
        self.ssm("-s")
        self.assertEqual(len(self.sent_to()), 2)

    def test_failure_exits_one_and_a_retry_sends_only_that_one(self):
        self.ssm("-g")
        r = self.ssm("-s", fail_for="bob@example.org")
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 échec", r.stdout)
        self.log.unlink()
        r = self.ssm("-s")
        self.assertEqual((r.returncode, self.sent_to()), (0, ["bob@example.org"]))

    def test_project_folder_and_preview(self):
        proj = self.dir / "p"
        for f in ("participants.json", "rules.json"):
            proj.mkdir(exist_ok=True)
            (self.dir / f).rename(proj / f)
        elsewhere = self.dir / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(self.ssm("-D", str(proj), "-g", cwd=elsewhere).returncode, 0)
        r = self.ssm("-D", str(proj), "-m", cwd=elsewhere)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Rudolph", r.stdout)
        self.assertEqual(self.ssm("-D", str(proj), "-s", cwd=elsewhere).returncode, 0)
        self.assertEqual(self.sent_to(), ["alice@example.org", "bob@example.org"])
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_sending_without_a_draw_is_a_clear_error(self):
        r = self.ssm("-s")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Aucun tirage", r.stdout + r.stderr)


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
