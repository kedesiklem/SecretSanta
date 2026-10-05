"""The web interface's API, over real HTTP on a throw-away server."""
import http.client
import json
import re
import threading
import unittest
import urllib.parse
from pathlib import Path
import tempfile

from helpers import Santa, people_json

import webui  # noqa: E402  (helpers put the project folder on sys.path)

NAMES = ["Alice", "Bob", "Carol", "Dave", "Eve", "Uncle Tom"]


class ApiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        webui.ROOT = cls.root
        cls.server = webui.Server(0, False)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.token = ""
        st, html = cls.req("GET", "/", token=False)
        cls.token = re.search(r'TOKEN = "([^"]+)"', html).group(1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    @classmethod
    def req(cls, method, path, body=None, *, set=None, token=True, host=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=120)
        h = {"Host": host or f"127.0.0.1:{cls.port}"}
        if token:
            h["X-Santa-Token"] = cls.token
        if set is not None:
            h["X-Santa-Set"] = urllib.parse.quote(set)
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode()

    # -- per-test sets ---------------------------------------------------

    def setUp(self):
        self.sid = f"t{self.id().split('.')[-1][:40]}"
        st, r = self.req("POST", "/api/sets/create", {"name": self.sid})
        self.assertEqual(st, 200, r)
        self.dir = self.root / "sets" / self.sid

    def post(self, path, body):
        return self.req("POST", path, body, set=self.sid)

    def get(self, path):
        return self.req("GET", path, set=self.sid)

    def save_people(self, names=NAMES, **extra):
        st, r = self.post("/api/save", {"participants": [{"name": n, "email": f"{n.lower().replace(' ', '')}@example.org"} for n in names], **extra})
        self.assertEqual(st, 200, r)
        return r["state"]

    def validate(self, rules, people=None, **kw):
        people = people or [{"name": n, "email": f"{n.lower().replace(' ', '')}@example.org"} for n in NAMES]
        return self.post("/api/validate", {"participants": people, "rules": rules, "year": 2026, **kw})[1]


class Guards(ApiCase):
    def test_page_and_token(self):
        self.assertGreater(len(self.token), 20)

    def test_refusals(self):
        self.assertEqual(self.req("GET", "/", token=False, host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/api/sets", token=False)[0], 403)
        self.assertEqual(self.req("GET", "/api/sets", headers={"X-Santa-Token": "nope"}, token=False)[0], 403)
        self.assertEqual(self.req("POST", "/api/validate", {}, set=self.sid, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("GET", "/api/nope")[0], 404)
        self.assertEqual(self.req("GET", "/../Santa.py", token=False)[0], 404)
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("POST", "/api/validate", "{}", {"Host": f"127.0.0.1:{self.port}", "X-Santa-Token": self.token, "Content-Type": "text/plain"})
        self.assertEqual(c.getresponse().status, 415)

    def test_set_header_is_required_and_checked(self):
        self.assertEqual(self.req("GET", "/api/state")[1].get("code"), "no_set")
        self.assertEqual(self.req("GET", "/api/state", set="nope")[0], 404)
        self.assertEqual(self.req("GET", "/api/state", headers={"X-Santa-Set": "..%2F..%2Fetc"})[0], 404)
        self.assertEqual(self.req("GET", "/api/state", headers={"X-Santa-Set": "%2Fetc"})[0], 404)


class Participants(ApiCase):
    def test_empty_state(self):
        st, s = self.get("/api/state")
        self.assertEqual((st, s["participants"], s["rules"], s["history"]), (200, [], {}, []))
        self.assertFalse(s["template"]["exists"])

    def test_validation(self):
        good = [{"name": n, "email": f"{n.lower()}@x.org"} for n in NAMES[:3]]
        cases = {"duplicate": good + [good[0]], "bad email": [{"name": "A", "email": "nope"}, good[1]],
                 "slash": [{"name": "A/B", "email": "a@x.org"}, good[1]], "dotfile": [{"name": ".x", "email": "a@x.org"}, good[1]],
                 "single": [good[0]], "no email": [{"name": "A"}, good[1]], "bad id": [{"id": "x y!", "name": "A", "email": "a@x.org"}, good[1]]}
        for label, people in cases.items():
            with self.subTest(label):
                self.assertEqual(self.post("/api/save", {"participants": people})[0], 400)

    def test_ids_are_assigned_on_save_and_kept_on_rename(self):
        state = self.save_people()
        ids = {p["name"]: p["id"] for p in state["participants"]}
        self.assertEqual(len(set(ids.values())), 6)
        renamed = [{**p, "name": "Alicia"} if p["name"] == "Alice" else p for p in state["participants"]]
        st, r = self.post("/api/save", {"participants": renamed})
        self.assertEqual(st, 200)
        after = {p["id"]: p["name"] for p in r["state"]["participants"]}
        self.assertEqual(after[ids["Alice"]], "Alicia")

    def test_save_keeps_backup(self):
        self.save_people()
        self.save_people(NAMES[:4])
        self.assertEqual(len(json.loads((self.dir / "participants.json.bak").read_text())), 6)

    def test_rules_cannot_name_missing_people(self):
        self.save_people()
        st, r = self.post("/api/save", {"rules": {"couples": [["Alice", "Zed"]]}})
        self.assertEqual(st, 422)
        self.assertIn("Zed", r["error"])


class RulesValidation(ApiCase):
    def test_validate_cases(self):
        r = self.validate({})
        self.assertTrue(r["ok"] and r["feasible"] and r["hard"] == 0)
        r = self.validate({"couples": [["Alice", "Zed"]]})
        self.assertFalse(r["ok"])
        self.assertIn("Zed", r["errors"][0])
        r = self.validate({"forbidden": [{"to": "Bob"}]})
        self.assertFalse(r["ok"])
        r = self.validate({"forced": [{"from": "Dave", "to": "Carol", "priority": 50}], "forbidden": [{"from": "Dave", "to": "Carol"}]})
        self.assertIn("priorité égale", r["errors"][0])
        r = self.validate({"forced": [{"from": "Dave", "to": "Carol"}], "forbidden": [{"from": "Dave", "to": "Carol", "priority": 10}]})
        self.assertTrue(r["ok"] and r["feasible"] and len(r["notes"]) == 1)
        r = self.validate({"forced": [{"from": "Dave", "to": "Bob"}, {"from": "Dave", "to": "Carol"}]})
        self.assertTrue(r["ok"] and r["feasible"] is False)
        r = self.validate({"couples": [{"members": ["Alice", "Bob"], "soft": True}], "groups": [["Carol", "Dave", "Eve", "Uncle Tom"]]})
        self.assertEqual({g["source"] for g in r["summary"]}, {"couple Alice+Bob", "group Carol+Dave+Eve+Uncle Tom"})
        self.assertEqual(self.post("/api/validate", {"participants": [], "year": "abc"})[0], 400)

    def test_history_rule_and_priorities(self):
        self.save_people()
        txt = "Alice > Bob\nBob > Carol\nCarol > Dave\nDave -> Eve\nEve → Uncle Tom\nUncle Tom => Alice"
        self.assertEqual(self.post("/api/history/import", {"year": 2025, "text": txt})[0], 200)
        r = self.validate({"history": {"last": 1}})
        self.assertTrue(any(g["source"] == "history 2025" and g["count"] == 6 and g["soft"] for g in r["summary"]))
        r = self.validate({"history": {"last": 1}, "forced": [{"from": "Alice", "to": "Bob"}]})
        self.assertTrue(r["ok"] and r["feasible"] and any("Alice→Bob" in n for n in r["notes"]))


class History(ApiCase):
    TXT = "Alice > Bob\nBob > Carol\nCarol > Dave\nDave -> Eve\nEve → Uncle Tom\nUncle Tom => Alice"

    def test_import_validation_and_overwrite(self):
        self.save_people()
        st, r = self.post("/api/history/import", {"year": 2025, "text": "Alice > Bob\nBob > Alice\nx"})
        self.assertTrue(st == 400 and "Ligne 3" in r["error"])
        self.assertEqual(self.post("/api/history/import", {"year": 2025, "text": "Alice > Bob\nBob > Carol"})[0], 400)
        self.assertEqual(self.post("/api/history/import", {"year": 2025, "text": "Alice > Alice\nBob > Bob"})[0], 400)
        st, r = self.post("/api/history/import", {"year": 2025, "text": self.TXT})
        self.assertEqual((st, r["history"][0]["count"], r["history"][0]["imported"], r["unknown"]), (200, 6, True, []))
        self.assertNotIn("Alice", json.dumps(self.get("/api/state")[1]["history"]))
        self.assertEqual(self.post("/api/history/import", {"year": 2025, "text": self.TXT})[1].get("code"), "exists")
        self.assertEqual(self.post("/api/history/import", {"year": 2025, "text": self.TXT, "overwrite": True})[0], 200)

    def test_set_aside(self):
        self.save_people()
        self.post("/api/history/import", {"year": 2025, "text": self.TXT})
        st, r = self.post("/api/history/delete", {"year": 2025})
        self.assertTrue(st == 200 and r["history"] == [] and (self.dir / "history/2025.json.bak").exists())
        self.assertEqual(self.post("/api/history/delete", {"year": 1999})[0], 404)


    def test_read_needs_unsafe_and_a_past_year(self):
        people = self.save_people()["participants"]
        self.post("/api/history/import", {"year": 2025, "text": self.TXT})
        self.assertEqual(self.post("/api/history/read", {"year": 2025})[0], 403)                  # not opted in
        self.assertEqual(self.post("/api/history/read", {"year": 2025, "unsafe": "yes"})[0], 403)  # must be literally true
        this_year = __import__("datetime").date.today().year
        self.post("/api/history/import", {"year": this_year, "text": self.TXT})
        st, r = self.post("/api/history/read", {"year": this_year, "unsafe": True})
        self.assertEqual(st, 403)                                                                  # current draw stays secret
        self.assertNotIn("Alice", json.dumps(r))
        st, r = self.post("/api/history/read", {"year": 2025, "unsafe": True})
        self.assertEqual(st, 200)
        self.assertEqual(r["pairs"][0], {"from": "Alice", "to": "Bob"})
        self.assertEqual(len(r["pairs"]), 6)
        self.assertEqual(self.post("/api/history/read", {"year": 1999, "unsafe": True})[0], 404)
        renamed = [{**p, "name": "Alicia"} if p["name"] == "Alice" else p for p in people]
        self.post("/api/save", {"participants": renamed})
        r = self.post("/api/history/read", {"year": 2025, "unsafe": True})[1]
        self.assertEqual(r["pairs"][0]["from"], "Alicia")                                          # follows the id

class LegacyUpgrade(ApiCase):
    def test_opening_an_old_set_adds_ids_and_converts_history_once(self):
        n = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank"]
        (self.dir / "participants.json").write_text(json.dumps(people_json(n)), encoding="utf-8")
        (self.dir / "rules.json").write_text(json.dumps({"history": {"soft": False}}), encoding="utf-8")
        (self.dir / "history").mkdir()
        (self.dir / "history/2025.json").write_text(json.dumps({"year": 2025, "assignments": [
            {"from": a, "to": b} for a, b in zip(n, n[1:] + n[:1])]}), encoding="utf-8")
        st, s = self.get("/api/state")
        self.assertEqual(s["upgraded"], {"ids_added": 6, "history_converted": [2025]})
        self.assertTrue(all(p.get("id") for p in s["participants"]))
        self.assertTrue(s["history"][0]["by_id"])
        self.assertIsNone(self.get("/api/state")[1]["upgraded"])          # only once
        renamed = [{**p, "name": "Alicia"} if p["name"] == "Alice" else p for p in s["participants"]]
        self.assertEqual(self.post("/api/save", {"participants": renamed})[0], 200)
        r = self.post("/api/validate", {"participants": renamed, "rules": {"history": {"soft": False}}, "year": 2026})[1]
        self.assertEqual(r["hard"], 6)                                     # the renamed person is still remembered


class Simulation(ApiCase):
    def test_counts_and_rules(self):
        people = self.save_people()["participants"]
        rules = {"couples": [["Alice", "Bob"]], "forbidden": [{"from": "Carol", "to": "Dave"}], "forced": [{"from": "Eve", "to": "Uncle Tom"}]}
        st, r = self.post("/api/simulate", {"participants": people, "year": 2026, "runs": 40, "rules": rules})
        cnt, ix = r["counts"], {n: i for i, n in enumerate(r["names"])}
        self.assertTrue(st == 200 and r["runs_done"] == 40 and not r["infeasible"])
        self.assertTrue(all(sum(row) == 40 for row in cnt) and all(sum(cnt[i][j] for i in range(6)) == 40 for j in range(6)))
        self.assertTrue(all(cnt[i][i] == 0 for i in range(6)))
        self.assertEqual(cnt[ix["Alice"]][ix["Bob"]] + cnt[ix["Bob"]][ix["Alice"]] + cnt[ix["Carol"]][ix["Dave"]], 0)
        self.assertEqual(cnt[ix["Eve"]][ix["Uncle Tom"]], 40)
        self.assertIn([ix["Carol"], ix["Dave"]], r["hard_forbidden"])
        self.assertIn([ix["Eve"], ix["Uncle Tom"]], r["forced"])

    def test_infeasible_and_cap(self):
        people = self.save_people()["participants"]
        r = self.post("/api/simulate", {"participants": people, "year": 2026, "runs": 5,
                                        "rules": {"forced": [{"from": "Dave", "to": "Bob"}, {"from": "Dave", "to": "Carol"}]}})[1]
        self.assertTrue(r["infeasible"] and r["runs_done"] == 0)
        r = self.post("/api/simulate", {"participants": people, "year": 2026, "runs": 100000, "rules": {}})[1]
        self.assertLessEqual(r["runs_done"], 200)


class Message(ApiCase):
    def test_preview_uses_the_storybook_characters(self):
        st, r = self.post("/api/template/preview", {"text": "Subject: Salut {year}\n\nTu offres à {recipient} !\n", "year": 2030})
        self.assertTrue(r["ok"] and r["subject"] == "Salut 2030" and "Rudolph" in r["body"])
        self.assertEqual((r["santa"], r["recipient"]), ("Mère Noël", "Rudolph"))

    def test_errors(self):
        r = self.post("/api/template/preview", {"text": "Subject: x\n\nHello {recipent}\n"})[1]
        self.assertTrue(not r["ok"] and "recipent" in r["error"])
        self.save_people()
        self.assertEqual(self.post("/api/save", {"template": "Subject: x\n\n{oops}\n"})[0], 422)


class Draw(ApiCase):
    def test_dry_run_confirmation_official_and_overwrite(self):
        self.save_people(rules={"couples": [["Alice", "Carol"]], "history": {}})
        st, r = self.post("/api/draw", {"year": 2026, "dry_run": True})
        self.assertTrue(r["ok"] and any(l["level"] == "preview" for l in r["lines"]))
        self.assertFalse((self.dir / "secretSantaFiles").exists())
        self.assertEqual(self.post("/api/draw", {"year": 2026})[0], 400)
        st, r = self.post("/api/draw", {"year": 2026, "confirm": True})
        self.assertTrue(r["ok"] and any("Tirage généré" in l["text"] for l in r["lines"]), r)
        self.assertEqual(len(list((self.dir / "secretSantaFiles").glob("*.mail"))), 6)
        data = json.loads((self.dir / "history/2026.json").read_text())
        names = data["people"]
        pairs = {(names[a["from"]], names[a["to"]]) for a in data["assignments"]}
        self.assertFalse({("Alice", "Carol"), ("Carol", "Alice")} & pairs)
        shown = json.dumps(r["lines"])
        self.assertFalse(any(f"{a}" in l["text"] and f"{b}" in l["text"] for l in r["lines"] for a, b in pairs))
        self.assertNotIn("assignments", json.dumps(r["history"]))
        st, r = self.post("/api/draw", {"year": 2026, "confirm": True})
        self.assertEqual((st, r["code"]), (409, "exists"))
        self.assertTrue(self.post("/api/draw", {"year": 2026, "confirm": True, "overwrite": True})[1]["ok"])

    def test_impossible_rules_fail_cleanly(self):
        self.save_people(rules={"forced": [{"from": "Dave", "to": "Bob"}, {"from": "Dave", "to": "Carol"}]})
        st, r = self.post("/api/draw", {"year": 2031, "confirm": True})
        self.assertTrue(st == 200 and not r["ok"] and r["status"] == 1)
        self.assertTrue(any(l["level"] == "error" for l in r["lines"]))
        self.assertFalse((self.dir / "history/2031.json").exists())

    def test_draw_in_one_set_does_not_touch_another(self):
        self.save_people()
        other = self.sid + "-b"
        self.req("POST", "/api/sets/create", {"name": other, "from": self.sid})
        self.post("/api/draw", {"year": 2026, "confirm": True})
        self.assertTrue((self.dir / "history/2026.json").exists())
        self.assertFalse((self.root / "sets" / other / "history").exists())


class Sets(ApiCase):
    def test_name_rules(self):
        for bad in ["", "   ", "a/b", "..", ".hidden", "x\\y", "a" * 61, "end."]:
            with self.subTest(bad=bad):
                self.assertEqual(self.req("POST", "/api/sets/create", {"name": bad})[0], 400)
        self.assertEqual(self.req("POST", "/api/sets/create", {"name": self.sid.upper()})[0], 409)

    def test_copy_keeps_ids_and_optionally_history(self):
        self.save_people()
        self.post("/api/history/import", {"year": 2025, "text": History.TXT})
        self.post("/api/save", {"rules": {"couples": [["Alice", "Bob"]]}})
        a, b = f"{self.sid}-copy", f"{self.sid}-hist"
        self.req("POST", "/api/sets/create", {"name": a, "from": self.sid, "copy_history": False})
        self.req("POST", "/api/sets/create", {"name": b, "from": self.sid, "copy_history": True})
        self.assertFalse((self.root / "sets" / a / "history").exists())
        self.assertTrue((self.root / "sets" / b / "history/2025.json").exists())
        ids = lambda name: [p["id"] for p in json.loads((self.root / "sets" / name / "participants.json").read_text())]
        self.assertEqual(ids(self.sid), ids(b))
        r = self.req("POST", "/api/validate", {"participants": json.loads((self.root / "sets" / b / "participants.json").read_text()),
                                               "rules": {"history": {"soft": False}}, "year": 2026}, set=b)[1]
        self.assertEqual(r["hard"], 6)                      # the copied history still means the same people

    def test_listing_unicode_rename_delete(self):
        self.save_people()
        uni = f"Équipe café ☕ {self.sid}"
        self.req("POST", "/api/sets/create", {"name": uni, "from": self.sid})
        listing = {x["id"]: x for x in self.req("GET", "/api/sets")[1]["sets"]}
        self.assertEqual(listing[self.sid]["participants"], 6)
        self.assertNotIn("assignments", json.dumps(listing))
        self.assertEqual(self.req("GET", "/api/state", set=uni)[0], 200)
        new = uni.replace("Équipe", "Team")
        self.assertEqual(self.req("POST", "/api/sets/rename", {"id": uni, "name": new})[0], 200)
        self.assertEqual(self.req("POST", "/api/sets/rename", {"id": new, "name": new.upper()})[0], 200)       # case-only
        self.assertEqual(self.req("POST", "/api/sets/rename", {"id": new.upper(), "name": self.sid})[0], 409)
        st, r = self.req("POST", "/api/sets/delete", {"id": new.upper()})
        self.assertTrue(st == 200 and Path(r["trashed"]).is_dir())
        self.assertTrue(all(not x["name"].startswith(".") for x in self.req("GET", "/api/sets")[1]["sets"]))
        self.assertIn(self.req("POST", "/api/sets/delete", {"id": "."})[0], (400, 404))


if __name__ == "__main__":
    unittest.main()
