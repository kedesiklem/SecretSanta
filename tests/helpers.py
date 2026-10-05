"""Shared test helpers: build throw-away projects on disk."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import Santa  # noqa: E402

NAMES = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank"]


def people_json(names=NAMES, ids=False):
    return [{**({"id": f"id_{n.lower()}"} if ids else {}), "name": n, "email": f"{n.lower()}@example.org"}
            for n in names]


class ProjectCase(unittest.TestCase):
    """Each test gets an empty temporary folder in ``self.dir`` (a ``Path``)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, rel, data):
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def read(self, rel):
        return json.loads((self.dir / rel).read_text(encoding="utf-8"))

    def project(self, names=NAMES, rules=None, ids=False, **kw):
        self.write("participants.json", people_json(names, ids))
        if rules is not None:
            self.write("rules.json", rules)
        return Santa.Project(self.dir, **kw)

    def pairs_of(self, year):
        """Name pairs of a recorded draw (tests may look; the program never shows them)."""
        data = self.read(f"history/{year}.json")
        names = data["people"]
        return {(names[a["from"]], names[a["to"]]) for a in data["assignments"]}
