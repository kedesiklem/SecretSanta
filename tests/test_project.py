"""Project.draw end to end: what gets written, in which order, and what never does."""
import os
import unittest
from unittest import mock

from helpers import ProjectCase, Santa


def levels(result):
    return [n.level for n in result.notices]


class DryRun(ProjectCase):
    def test_writes_nothing(self):
        p = self.project(rules={"couples": [["Alice", "Bob"]]})
        before = (self.dir / "participants.json").read_bytes()
        result = p.draw(2026, dry_run=True)
        self.assertTrue(result.feasible)
        self.assertIsNone(result.history_file)
        self.assertEqual(sorted(os.listdir(self.dir)), ["participants.json", "rules.json"])
        self.assertEqual((self.dir / "participants.json").read_bytes(), before)       # not even ids
        self.assertEqual(levels(result)[-1], "ok")

    def test_a_broken_message_template_is_not_the_drawers_business(self):
        p = self.project(rules={})
        self.write("message.txt", "no subject here")                                  # only sender.py reads it
        self.assertTrue(p.draw(2026, dry_run=True).feasible)


class RealDraw(ProjectCase):
    def test_valid_draw_writes_only_the_history_and_ids(self):
        p = self.project(rules={"couples": [["Alice", "Bob"]]})
        result = p.draw(2026, seed=7)
        self.assertTrue(result.feasible)
        self.assertTrue(result.history_file.endswith("history/2026.json"))
        pairs = self.pairs_of(2026)
        self.assertEqual(len(pairs), 6)
        self.assertEqual(sorted(a for a, _ in pairs), sorted(b for _, b in pairs))      # a permutation
        self.assertTrue(all(a != b for a, b in pairs))
        self.assertNotIn(("Alice", "Bob"), pairs)
        self.assertNotIn(("Bob", "Alice"), pairs)
        self.assertTrue((self.dir / "participants.json.bak").exists())
        self.assertTrue(all("id" in x for x in self.read("participants.json")))
        # The drawer produces the draw and nothing else: no mail, no message, no delivery state.
        self.assertEqual(sorted(os.listdir(self.dir)),
                         ["history", "participants.json", "participants.json.bak", "rules.json"])
        self.assertEqual(os.listdir(self.dir / "history"), ["2026.json"])

    def test_contacts_are_kept_verbatim_and_never_required(self):
        self.write("participants.json", [{"name": "A", "email": "a@x.org", "phone": "+33 6 00 00 00 01", "prefer": "phone", "note": "x"},
                                         {"name": "B"}])
        Santa.Project(self.dir).draw(2026, seed=1)
        people = {x["name"]: x for x in self.read("participants.json")}
        self.assertEqual({k: v for k, v in people["A"].items() if k != "id"},
                         {"name": "A", "email": "a@x.org", "phone": "+33 6 00 00 00 01", "prefer": "phone", "note": "x"})
        self.assertEqual(set(people["B"]), {"id", "name"})

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
    def test_nothing_is_left_half_done_when_the_history_cannot_be_saved(self):
        p = self.project(rules={})
        real = Santa.write_atomic

        def fail_on_history(path, data, **kw):
            if "history" in str(path):
                raise OSError("read-only")
            return real(path, data, **kw)

        with mock.patch.object(Santa, "write_atomic", fail_on_history), self.assertRaises(OSError):
            p.draw(2026, seed=1)
        self.assertFalse(list((self.dir / "history").glob("*.json")) if (self.dir / "history").exists() else [])


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
        self.assertTrue((self.dir / "history/2026.json").exists())
        self.assertEqual(os.listdir(other), [])

    def test_two_projects_side_by_side_do_not_mix(self):
        a = Santa.Project(self.dir / "a")
        b = Santa.Project(self.dir / "b")
        self.write("a/participants.json", [{"name": "A1"}, {"name": "A2"}])
        self.write("b/participants.json", [{"name": "B1"}, {"name": "B2"}])
        a.draw(2026, seed=1)
        self.assertTrue((self.dir / "a/history/2026.json").exists())
        self.assertFalse((self.dir / "b/history").exists())

    def test_custom_names(self):
        self.write("who.json", [{"name": "A"}, {"name": "B"}])
        p = Santa.Project(self.dir, participants="who.json", rules="r.json", history_dir="h")
        self.write("r.json", {})
        p.draw(2026, seed=1)
        self.assertTrue((self.dir / "h/2026.json").exists())


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
        self.write("participants.json", [{"name": "A"}, {"name": "A"}])
        with self.assertRaisesRegex(Santa.SantaError, "en double"):
            Santa.Project(self.dir).draw(2026, dry_run=True)


if __name__ == "__main__":
    unittest.main()
