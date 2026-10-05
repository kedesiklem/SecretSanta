"""Project.draw end to end: what gets written, in which order, and what never does."""
import email
import os
import unittest
from email import policy
from unittest import mock

from helpers import ProjectCase, Santa


def levels(result):
    return [n.level for n in result.notices]


class DryRun(ProjectCase):
    def test_writes_nothing_and_previews_the_message(self):
        p = self.project(rules={"couples": [["Alice", "Bob"]]})
        before = (self.dir / "participants.json").read_bytes()
        result = p.draw(2026, dry_run=True)
        self.assertTrue(result.feasible)
        self.assertEqual(sorted(os.listdir(self.dir)), ["participants.json", "rules.json"])
        self.assertEqual((self.dir / "participants.json").read_bytes(), before)       # not even ids
        subject, body, source = result.preview
        self.assertIn("Rudolph", body)
        self.assertEqual(levels(result)[-1], "ok")

    def test_bad_message_stops_before_anything_else(self):
        p = self.project(rules={})
        self.write("message.txt", "no subject here")
        with self.assertRaisesRegex(Santa.SantaError, "Subject"):
            p.draw(2026, dry_run=True)


class RealDraw(ProjectCase):
    def test_valid_draw_files_ids_and_mail_contents(self):
        p = self.project(rules={"couples": [["Alice", "Bob"]]})
        self.write("message.txt", "Subject: Noël {year}\n\nTu offres à {recipient}.\n")
        result = p.draw(2026, seed=7)
        self.assertTrue(result.feasible)
        self.assertEqual(sorted(result.mails), sorted(["Alice", "Bob", "Carol", "Dave", "Eve", "Frank"]))
        pairs = self.pairs_of(2026)
        self.assertEqual(len(pairs), 6)
        self.assertEqual(sorted(a for a, _ in pairs), sorted(b for _, b in pairs))      # a permutation
        self.assertTrue(all(a != b for a, b in pairs))
        self.assertNotIn(("Alice", "Bob"), pairs)
        self.assertNotIn(("Bob", "Alice"), pairs)
        for giver, receiver in pairs:                                                   # each mail names its recipient
            raw = (self.dir / "secretSantaFiles" / f"{giver}.mail").read_bytes()
            address, rest = raw.split(b"\n", 1)
            self.assertEqual(address.decode(), f"{giver.lower()}@example.org")
            msg = email.message_from_bytes(rest, policy=policy.default)
            self.assertEqual(msg["Subject"], "Noël 2026")
            self.assertIn(f"Tu offres à {receiver}.", msg.get_content())
        self.assertTrue((self.dir / "participants.json.bak").exists())
        self.assertTrue(all("id" in x for x in self.read("participants.json")))

    def test_rules_hold_over_many_draws(self):
        p = self.project(rules={"couples": [["Alice", "Bob"]], "forced": [{"from": "Carol", "to": "Dave"}],
                                "forbidden": [{"from": "Eve", "to": "Frank"}]})
        for seed in range(15):
            self.assertTrue(p.draw(2026, seed=seed).feasible)
            pairs = self.pairs_of(2026)
            self.assertIn(("Carol", "Dave"), pairs)
            self.assertFalse({("Alice", "Bob"), ("Bob", "Alice"), ("Eve", "Frank")} & pairs)

    def test_infeasible_is_a_result_and_writes_nothing(self):
        p = self.project(names=("Alice", "Bob", "Carol"),
                         rules={"forced": [{"from": "Alice", "to": "Bob"}, {"from": "Alice", "to": "Carol"}]})
        result = p.draw(2026)
        self.assertFalse(result.feasible)
        self.assertEqual(levels(result)[-2:], ["error", "hint"])
        self.assertEqual(sorted(os.listdir(self.dir)), ["participants.json", "rules.json"])

    def test_soft_violations_are_reported_as_counts(self):
        # With three people Alice must give to Bob or Carol: forbidding both softly costs exactly one violation.
        p = self.project(names=("Alice", "Bob", "Carol"),
                         rules={"forbidden": [{"from": "Alice", "to": "Bob", "soft": True, "priority": 10},
                                              {"from": "Alice", "to": "Carol", "soft": True, "priority": 10}]})
        result = p.draw(2026, seed=1)
        self.assertEqual(sum(c for _, c in result.violations), 1)
        self.assertIn("warn", levels(result))
        self.assertEqual(sum(r["violations"] for r in self.read("history/2026.json")["relaxed_rules"]), 1)

    def test_stale_mails_from_an_older_draw_are_flagged(self):
        p = self.project(rules={})
        self.write("secretSantaFiles/Zed.mail", "old")
        result = p.draw(2026, seed=1)
        warn = [n.text for n in result.notices if n.level == "warn"]
        self.assertTrue(any("Zed" in t and "ssm.sh -c" in t for t in warn), warn)

    def test_seed_is_flagged_as_tests_only_and_is_reproducible(self):
        p = self.project(rules={})
        r1 = p.draw(2026, seed=5)
        self.assertTrue(any("--seed" in n.text for n in r1.notices))
        first = self.pairs_of(2026)
        p.draw(2026, seed=5)
        self.assertEqual(first, self.pairs_of(2026))

    def test_emit_compiled(self):
        p = self.project(rules={"couples": [["Alice", "Bob"]]})
        p.draw(2026, dry_run=True, emit_compiled="compiled.json")
        data = self.read("compiled.json")
        self.assertEqual(len(data["entries"]), 2)
        self.assertEqual({e["kind"] for e in data["entries"]}, {"forbid"})


class WritingOrder(ProjectCase):
    def test_history_is_written_before_the_mails(self):
        p = self.project(rules={})
        real = Santa.write_atomic

        def fail_on_mail(path, data, **kw):
            if str(path).endswith(".mail"):
                raise OSError("disk full")
            return real(path, data, **kw)

        with mock.patch.object(Santa, "write_atomic", fail_on_mail), self.assertRaises(OSError):
            p.draw(2026, seed=1)
        self.assertTrue((self.dir / "history/2026.json").exists())       # a draw is never mailed without being recorded
        self.assertFalse(list(self.dir.glob("secretSantaFiles/*.mail")))

    def test_no_mail_when_the_history_cannot_be_saved(self):
        p = self.project(rules={})
        real = Santa.write_atomic

        def fail_on_history(path, data, **kw):
            if "history" in str(path):
                raise OSError("read-only")
            return real(path, data, **kw)

        with mock.patch.object(Santa, "write_atomic", fail_on_history), self.assertRaises(OSError):
            p.draw(2026, seed=1)
        self.assertFalse((self.dir / "secretSantaFiles").exists())

    def test_every_mail_is_rendered_before_the_first_file_is_written(self):
        p = self.project(rules={})
        with mock.patch.object(Santa, "build_mail", side_effect=Santa.SantaError("boom")):
            with self.assertRaises(Santa.SantaError):
                p.draw(2026, seed=1)
        self.assertFalse((self.dir / "history").exists())
        self.assertFalse((self.dir / "secretSantaFiles").exists())


class Locations(ProjectCase):
    def test_everything_is_relative_to_the_base_folder_not_the_current_one(self):
        other = self.dir / "elsewhere"
        other.mkdir()
        p = self.project(rules={})
        cwd = os.getcwd()
        os.chdir(other)
        try:
            self.assertTrue(p.draw(2026, seed=1).feasible)
        finally:
            os.chdir(cwd)
        self.assertTrue((self.dir / "secretSantaFiles/Alice.mail").exists())
        self.assertEqual(os.listdir(other), [])

    def test_two_projects_side_by_side_do_not_mix(self):
        a = Santa.Project(self.dir / "a")
        b = Santa.Project(self.dir / "b")
        self.write("a/participants.json", [{"name": "A1", "email": "a1@x"}, {"name": "A2", "email": "a2@x"}])
        self.write("b/participants.json", [{"name": "B1", "email": "b1@x"}, {"name": "B2", "email": "b2@x"}])
        a.draw(2026, seed=1)
        self.assertTrue((self.dir / "a/history/2026.json").exists())
        self.assertFalse((self.dir / "b/history").exists())
        self.assertEqual(sorted(p.name for p in (self.dir / "a/secretSantaFiles").iterdir()), ["A1.mail", "A2.mail"])

    def test_custom_names(self):
        self.write("who.json", [{"name": "A", "email": "a@x"}, {"name": "B", "email": "b@x"}])
        p = Santa.Project(self.dir, participants="who.json", rules="r.json", history_dir="h", output_dir="out")
        self.write("r.json", {})
        p.draw(2026, seed=1)
        self.assertTrue((self.dir / "h/2026.json").exists() and (self.dir / "out/A.mail").exists())


class InputErrors(ProjectCase):
    def test_messages(self):
        with self.assertRaisesRegex(Santa.SantaError, "introuvable"):
            Santa.Project(self.dir).draw(2026, dry_run=True)
        self.write("participants.json", "{ nope")
        with self.assertRaisesRegex(Santa.SantaError, "JSON valide"):
            Santa.Project(self.dir).draw(2026, dry_run=True)
        self.write("participants.json", {"name": "A"})
        with self.assertRaisesRegex(Santa.SantaError, "liste"):
            Santa.Project(self.dir).draw(2026, dry_run=True)
        self.write("participants.json", [{"name": "A"}, {"name": "B", "email": "b@x"}])
        with self.assertRaisesRegex(Santa.SantaError, "email"):
            Santa.Project(self.dir).draw(2026, dry_run=True)


if __name__ == "__main__":
    unittest.main()
