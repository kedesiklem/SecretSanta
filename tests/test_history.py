"""History by id: renames, legacy files, leavers, imports."""
import unittest

from helpers import ProjectCase, Santa, people_json

HARD_HISTORY = {"history": {"last": 1, "soft": False}}


class HistoryById(ProjectCase):
    def draw(self, project, year, **kw):
        result = project.draw(year, seed=kw.pop("seed", year), **kw)
        self.assertTrue(result.feasible, result.notices)
        return result

    def test_draw_records_version_2_with_ids_and_a_name_snapshot(self):
        p = self.project(rules={})
        self.draw(p, 2025)
        data = self.read("history/2025.json")
        ids = {x["id"]: x["name"] for x in self.read("participants.json")}
        self.assertEqual((data["version"], data["people"]), (2, ids))
        self.assertTrue(all(a["from"] in ids and a["to"] in ids for a in data["assignments"]))

    def test_rename_does_not_make_the_history_forget_anybody(self):
        p = self.project(rules=HARD_HISTORY)
        self.draw(p, 2025)
        before = self.pairs_of(2025)
        people = self.read("participants.json")
        people[0]["name"] = "Alicia"              # same id, new name
        self.write("participants.json", people)
        plan = p.plan(2026)
        self.assertEqual(len(plan.entries), 6)    # all six pairs still forbidden
        renamed = {("Alicia" if a == "Alice" else a, "Alicia" if b == "Alice" else b) for a, b in before}
        got = {(p.load_people()[e.pair[0]].name, p.load_people()[e.pair[1]].name) for e in plan.entries}
        self.assertEqual(got, renamed)

    def test_next_draw_never_repeats_last_years_pairs_even_after_a_rename(self):
        p = self.project(rules=HARD_HISTORY)
        self.draw(p, 2025)
        last = self.pairs_of(2025)
        people = self.read("participants.json")
        people[2]["name"] = "Caroline"
        self.write("participants.json", people)
        self.draw(p, 2026)
        names = {"Carol": "Caroline"}
        last = {(names.get(a, a), names.get(b, b)) for a, b in last}
        self.assertFalse(last & self.pairs_of(2026))

    def test_same_year_rerun_does_not_forbid_itself(self):
        p = self.project(rules=HARD_HISTORY)
        self.draw(p, 2025)
        plan = p.plan(2025)
        self.assertEqual(plan.entries, [])

    def test_two_years_with_decaying_priority(self):
        p = self.project(rules={"history": {"last": 2}})
        self.draw(p, 2024)
        self.draw(p, 2025)
        entries = p.plan(2026).entries
        self.assertEqual({(e.source, e.priority, e.soft) for e in entries}, {("history 2025", 10, True), ("history 2024", 9, True)})

    def test_leavers_are_dropped_with_an_info_notice_and_newcomers_are_fine(self):
        p = self.project(rules=HARD_HISTORY)
        self.draw(p, 2025)
        people = [x for x in self.read("participants.json") if x["name"] != "Frank"]
        people.append({"name": "Gina", "email": "gina@example.org"})
        self.write("participants.json", people)
        plan = p.plan(2026)
        self.assertTrue(any("ignorée" in n.text for n in plan.notices))
        self.assertTrue(all(5 not in e.pair for e in plan.entries))   # index 5 is now Gina, who has no past
        self.assertTrue(p.draw(2026, seed=1).feasible)

    def test_history_rule_forms(self):
        p = self.project(rules={})
        self.draw(p, 2025)
        for rule, expected in (({}, 6), (True, 6), (False, 0), (None, 0)):
            with self.subTest(rule=rule):
                self.write("rules.json", {"history": rule})
                self.assertEqual(len(p.plan(2026).entries), expected)

    def test_missing_history_folder_is_a_notice_not_an_error(self):
        p = self.project(rules={"history": {}})
        plan = p.plan(2026)
        self.assertEqual(plan.entries, [])
        self.assertEqual(plan.notices[0].level, "info")

    def test_unreadable_history_file_stops_the_draw(self):
        p = self.project(rules={"history": {}})
        self.write("history/2020.json", "{ not json")
        with self.assertRaises(Santa.SantaError):
            p.plan(2026)

    def test_id_lost_from_participants_falls_back_on_the_recorded_name(self):
        p = self.project(rules=HARD_HISTORY)
        self.draw(p, 2025)
        self.write("participants.json", people_json())          # hand-edited: ids gone
        self.assertEqual(len(p.plan(2026).entries), 6)


class LegacyHistory(ProjectCase):
    def legacy(self, year=2025, names=("Alice", "Bob", "Carol", "Dave", "Eve", "Frank")):
        n = list(names)
        self.write(f"history/{year}.json", {"year": year, "generated_at": "2025-12-01T10:00:00", "relaxed_rules": [],
                                            "assignments": [{"from": a, "to": b} for a, b in zip(n, n[1:] + n[:1])]})

    def test_name_based_files_still_work(self):
        p = self.project(rules=HARD_HISTORY)
        self.legacy()
        self.assertEqual(len(p.plan(2026).entries), 6)

    def test_migration_keeps_pairs_makes_a_backup_and_survives_renames(self):
        p = self.project(rules=HARD_HISTORY)
        self.legacy()
        self.assertEqual(p.migrate_history(), [2025])
        self.assertTrue((self.dir / "history/2025.json.bak").exists())
        self.assertEqual(self.read("history/2025.json")["version"], 2)
        self.assertTrue(all("id" in x for x in self.read("participants.json")))
        people = self.read("participants.json")
        people[1]["name"] = "Robert"
        self.write("participants.json", people)
        self.assertEqual(len(p.plan(2026).entries), 6)
        self.assertEqual(p.migrate_history(), [])           # idempotent

    def test_migration_gives_leavers_a_stable_id_across_files(self):
        p = self.project(names=("Alice", "Bob", "Carol"), rules={})
        self.legacy(2024, ("Alice", "Bob", "Carol", "Zed"))
        self.legacy(2025, ("Alice", "Zed", "Bob", "Carol"))
        p.migrate_history()
        zed = {next(k for k, v in self.read(f"history/{y}.json")["people"].items() if v == "Zed") for y in (2024, 2025)}
        self.assertEqual(len(zed), 1)

    def test_upgrade_reports_what_it_did_and_is_idempotent(self):
        p = self.project(rules={})
        self.legacy()
        self.assertEqual(p.upgrade(), {"ids_added": 6, "history_converted": [2025]})
        self.assertEqual(p.upgrade(), {"ids_added": 0, "history_converted": []})


class ImportAndManage(ProjectCase):
    PAIRS = [("Alice", "Bob"), ("Bob", "Carol"), ("Carol", "Alice")]

    def test_import_by_names_resolves_to_ids_and_flags_strangers(self):
        p = self.project(names=("Alice", "Bob", "Carol"), rules=HARD_HISTORY)
        self.assertEqual(p.import_history(2025, self.PAIRS), [])
        self.assertTrue(self.read("history/2025.json")["imported"])
        self.assertEqual(len(p.plan(2026).entries), 3)
        self.assertEqual(p.import_history(2024, [("Alice", "Zed"), ("Zed", "Alice")]), ["Zed"])

    def test_import_validation(self):
        p = self.project(names=("Alice", "Bob", "Carol"), rules={})
        for label, pairs in {"one pair": [("Alice", "Bob")], "dup giver": [("Alice", "Bob"), ("Alice", "Carol")],
                             "dup receiver": [("Alice", "Bob"), ("Carol", "Bob")],
                             "not a permutation": [("Alice", "Bob"), ("Bob", "Zed")],
                             "self": [("Alice", "Alice"), ("Bob", "Bob")]}.items():
            with self.subTest(label), self.assertRaises(Santa.SantaError):
                p.import_history(2025, pairs)

    def test_import_refuses_to_overwrite_unless_told(self):
        p = self.project(names=("Alice", "Bob", "Carol"), rules={})
        p.import_history(2025, self.PAIRS)
        with self.assertRaises(Santa.HistoryExists):
            p.import_history(2025, self.PAIRS)
        p.import_history(2025, self.PAIRS, overwrite=True)

    def test_import_works_before_any_participants_exist(self):
        p = Santa.Project(self.dir)
        self.assertEqual(sorted(p.import_history(2025, self.PAIRS)), ["Alice", "Bob", "Carol"])

    def test_set_aside_renames_and_stops_reading_it(self):
        p = self.project(names=("Alice", "Bob", "Carol"), rules=HARD_HISTORY)
        p.import_history(2025, self.PAIRS)
        p.set_aside_history(2025)
        self.assertTrue((self.dir / "history/2025.json.bak").exists())
        self.assertEqual(p.plan(2026).entries, [])
        with self.assertRaises(Santa.SantaError):
            p.set_aside_history(2025)

    def test_listing_gives_metadata_only(self):
        p = self.project(names=("Alice", "Bob", "Carol"), rules={})
        p.import_history(2025, self.PAIRS)
        self.write("history/2023.json", "garbage")
        listing = p.list_history()
        self.assertEqual([h["year"] for h in listing], [2025, None])
        self.assertEqual(listing[0]["count"], 3)
        self.assertNotIn("assignments", str(listing))


if __name__ == "__main__":
    unittest.main()
