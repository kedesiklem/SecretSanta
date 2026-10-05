"""Raw rules -> compiled entries, priorities and conflicts."""
import unittest

from helpers import Santa, people_json


def people(names=("Alice", "Bob", "Carol", "Dave")):
    return [Santa.Person(p["name"], p["email"]) for p in people_json(list(names))]


def compile_(rules, names=("Alice", "Bob", "Carol", "Dave"), year=2026, base="."):
    return Santa.build_plan(people(names), rules, year, "history", base)


class Expansion(unittest.TestCase):
    def test_forbidden_and_forced_defaults(self):
        plan = compile_({"forbidden": [{"from": "Alice", "to": "Bob"}], "forced": [{"from": "Carol", "to": "Dave"}]})
        by_kind = {e.kind: e for e in plan.entries}
        self.assertEqual((by_kind["forbid"].pair, by_kind["forbid"].priority, by_kind["forbid"].soft), ((0, 1), 50, False))
        self.assertEqual((by_kind["force"].pair, by_kind["force"].priority), ((2, 3), 100))

    def test_couple_is_two_pairs_in_one_bundle(self):
        plan = compile_({"couples": [["Alice", "Bob"]]})
        self.assertEqual(sorted(e.pair for e in plan.entries), [(0, 1), (1, 0)])
        self.assertEqual(len({e.bundle for e in plan.entries}), 1)
        self.assertNotIn(None, {e.bundle for e in plan.entries})

    def test_group_of_three_is_six_pairs(self):
        plan = compile_({"groups": [["Alice", "Bob", "Carol"]]})
        self.assertEqual(len(plan.entries), 6)

    def test_options_priority_soft_relax(self):
        plan = compile_({"couples": [{"members": ["Alice", "Bob"], "priority": 7, "soft": True, "relax": "pair"}]})
        self.assertEqual({(e.priority, e.soft, e.bundle) for e in plan.entries}, {(7, True, None)})

    def test_unknown_name_is_a_loud_error(self):
        with self.assertRaisesRegex(Santa.SantaError, "Zorro"):
            compile_({"forbidden": [{"from": "Zorro", "to": "Bob"}]})

    def test_malformed_rules_are_reported_not_crashed(self):
        for bad in ({"forbidden": [{"from": "Alice"}]}, {"couples": [["Alice"]]}, {"forbidden": "x"},
                    {"couples": [{"members": ["Alice", "Bob"], "relax": "nope"}]}, {"history": "yes"}):
            with self.subTest(bad=bad), self.assertRaises(Santa.SantaError):
                compile_(bad)
        with self.assertRaises(Santa.SantaError):
            Santa.compile_rules([], people(), 2026, "history")

    def test_self_forced_is_refused(self):
        with self.assertRaises(Santa.SantaError):
            compile_({"forced": [{"from": "Alice", "to": "Alice"}]})


class Conflicts(unittest.TestCase):
    def test_higher_priority_wins_on_the_same_pair(self):
        plan = compile_({"forced": [{"from": "Alice", "to": "Bob"}], "forbidden": [{"from": "Alice", "to": "Bob"}]})
        self.assertEqual([e.kind for e in plan.entries], ["force"])
        self.assertEqual(len(plan.conflicts), 1)

    def test_lower_forced_loses_against_stronger_forbidden(self):
        plan = compile_({"forced": [{"from": "Alice", "to": "Bob", "priority": 5}], "forbidden": [{"from": "Alice", "to": "Bob"}]})
        self.assertEqual([e.kind for e in plan.entries], ["forbid"])

    def test_equal_priority_is_an_error_with_a_hint(self):
        with self.assertRaises(Santa.SantaError) as cm:
            compile_({"forced": [{"from": "Alice", "to": "Bob", "priority": 50}], "forbidden": [{"from": "Alice", "to": "Bob"}]})
        self.assertIn("priorité", cm.exception.hint)


class PeopleChecks(unittest.TestCase):
    def check(self, items):
        Santa.check_people([Santa.Person(*i) if not isinstance(i, Santa.Person) else i for i in items])

    def test_valid(self):
        self.check([("A", "a@x.org"), ("B", "b@x")])

    def test_rejections(self):
        for bad in ([("A", "a@x.org")], [("A", "a@x.org"), ("A", "b@x.org")], [("A", "nope"), ("B", "b@x.org")],
                    [("A/B", "a@x.org"), ("B", "b@x.org")], [(".hid", "a@x.org"), ("B", "b@x.org")],
                    [("", "a@x.org"), ("B", "b@x.org")], [("A\\B", "a@x.org"), ("B", "b@x.org")]):
            with self.subTest(bad=bad), self.assertRaises(Santa.SantaError):
                self.check(bad)

    def test_duplicate_or_bad_ids(self):
        with self.assertRaises(Santa.SantaError):
            self.check([Santa.Person("A", "a@x.org", "same"), Santa.Person("B", "b@x.org", "same")])
        with self.assertRaises(Santa.SantaError):
            self.check([Santa.Person("A", "a@x.org", "bad id!"), Santa.Person("B", "b@x.org")])

    def test_assign_ids_fills_only_the_missing_ones_and_is_unique(self):
        ps = [Santa.Person("A", "a@x", "keep"), Santa.Person("B", "b@x"), Santa.Person("C", "c@x")]
        self.assertEqual(Santa.assign_ids(ps), 2)
        self.assertEqual(ps[0].id, "keep")
        self.assertEqual(len({p.id for p in ps}), 3)
        self.assertEqual(Santa.assign_ids(ps), 0)


if __name__ == "__main__":
    unittest.main()
