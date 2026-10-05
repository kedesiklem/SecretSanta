#!/usr/bin/env python3
"""Secret Santa draw, modelled as a constraint-satisfaction problem.

Overview
--------
The draw is a permutation: every participant gives exactly one gift and
receives exactly one gift, and nobody draws themselves. On top of that the
user can add constraints through ``rules.json`` (see README.md):

* ``forbidden``  - "A must not draw B" (one directional pair)
* ``forced``     - "A must draw B"
* ``couples``    - two people who must not draw each other (both directions)
* ``groups``     - N people, nobody in the group may draw anybody in it
                   (a household, a team...). ``couples`` is the N=2 case.
* ``history``    - "don't repeat the last N draws". Every successful run
                   writes ``history/<year>.json``; this rule reads those files
                   back, so nobody has to re-type last year's pairs by hand.
                   People are recorded by a stable ``id`` (see below), so
                   renaming a participant never makes the history forget them.
* ``settings``   - ``single_cycle`` forces one big loop A -> B -> C -> ... -> A,
                   which rules out mutual draws (A <-> B) by construction.

Using it as a library
---------------------
The command line is a thin layer over a small API; nothing below prints,
reads ``sys.argv`` or calls ``sys.exit``::

    import Santa
    project = Santa.Project("sets/family")        # a folder = one participant list
    result = project.draw(2026, dry_run=True)      # -> DrawResult
    for notice in result.notices:                  # Notice(level, text)
        print(notice.level, notice.text)
    if not result.feasible: ...

Bad input raises ``SantaError`` (a message meant for the user); "no valid draw"
is a normal result (``feasible == False``), not an exception. The building
blocks (``build_plan``, ``solve``, ``Template``, ``build_mail``) are public too.

Identifiers
-----------
A participant is ``{"id", "name", "email"}``. The ``name`` is what humans read
and what ``rules.json`` refers to (a typo there is a loud error). The ``id`` is
a random token that never changes, and it is what ``history/`` stores: a
history that silently forgot people after a rename would be a *quiet* failure.
Files without ids still work: the first real draw adds them to
``participants.json`` and converts old name-based history files (both with a
``.bak`` backup); a dry run never writes anything.

Raw rules vs compiled entries
-----------------------------
``rules.json`` is the *raw* file, written by a human. At every run it is
compiled into a flat list of atomic ``Entry`` objects, one per directed pair:

    (kind, pair, priority, soft, source, bundle)

``couples``, ``groups`` and ``history`` all disappear in this step: they are
just generators of "forbid" entries. The compiled list is derived data (raw
rules + history files + year), recomputed each time and never read back from
disk; ``--emit-compiled`` can dump it for debugging.

Priorities, soft rules and ``relax``
------------------------------------
Every entry carries:

* ``priority`` (int, higher wins). It decides who wins when two entries
  contradict each other on the exact same pair (forced vs forbidden). Equal
  priorities are an error: the program refuses to guess.
* ``soft`` (bool). A soft entry may be violated when the draw would otherwise
  be impossible; a hard entry never is.
* ``bundle``. Entries sharing a bundle are given up *together*. ``relax: "rule"``
  (the default for couples and groups) puts all pairs of one raw rule in one
  bundle, so a couple never ends up half-forbidden. ``relax: "pair"`` (the
  default for history) leaves each pair on its own, so only the pairs that
  really have to be repeated are repeated.

Defaults: forced = 100, forbidden/couples/groups = 50, history = 10 and soft
(minus one per year of age, so older years give way first).

How the solver deals with soft entries
--------------------------------------
Soft entries are not removed in rounds; they become *penalized* constraints
inside the CP-SAT model. Distinct soft priorities are optimized one after the
other, highest first: maximize the number of satisfied units of that priority,
freeze that optimum as a constraint, move to the next priority. The result is
the minimum possible number of violations at each level, with higher
priorities never sacrificed for lower ones. Finally, the remaining freedom is
spent on a random objective, so the draw is unpredictable.

Message template
----------------
The text of the mails lives in ``message.txt``, not in the code: first line
``Subject: ...``, one blank line, then the body. ``{santa}``, ``{recipient}``
and ``{year}`` are replaced for each participant (``{{`` / ``}}`` for a literal
brace). The template is parsed and rendered once with dummy values *before*
the solver runs, so a typo fails immediately instead of after the draw, and
``--dry-run`` shows that preview. Without a file, a built-in default is used.

Design notes
------------
* Unknown names in a rule are a hard error, not a warning: a silently ignored
  "forbidden" rule is worse than a crash, because the organizer believes the
  guarantee holds.
* The random objective uses OS entropy rather than a small integer seed: a
  seed in 1..10000 allows only 10,000 outcomes, which is brute-forceable by
  anyone with the participant list.
* Constraints are applied to a boolean matrix ``x[i, j]`` ("i gives to j")
  because ``AddCircuit`` (used for ``single_cycle``) needs arc literals.
* Messages never name *which* pair of the result violated a soft rule: that
  would leak a real assignment. They only give counts per source rule.
"""

import argparse
import datetime
import itertools
import json
import os
import random
import re
import secrets
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from email.message import EmailMessage
from itertools import permutations
from pathlib import Path
from typing import Optional

from ortools.sat.python import cp_model


# --------------------------------------------------------------------------
# Errors and notices
# --------------------------------------------------------------------------

class SantaError(Exception):
    """Something the user must fix (bad rule, bad file...). The text is for humans.

    Library functions raise this instead of calling ``sys.exit``; only the
    command line turns it into an exit code.
    """

    def __init__(self, message, hint=None):
        super().__init__(message)
        self.message, self.hint = message, hint


class HistoryExists(SantaError):
    """A draw is already recorded for that year and ``overwrite`` was not asked."""


@dataclass(frozen=True)
class Notice:
    """One line of feedback. ``level`` is the only thing front ends need to switch on.

    ``ok`` (done), ``info``, ``warn`` (soft rule not respected, stale files...),
    ``conflict`` (a rule lost against a stronger one), ``hint``, ``error``
    (a normal outcome such as "no valid draw", not an exception), ``file``
    (something was written).
    """
    level: str
    text: str


NOTICE_ICONS = {"ok": "✅ ", "info": "ℹ️  ", "warn": "⚠️  ", "conflict": "⚖️  ",
                "hint": "💡 ", "error": "❌ ", "file": "🗂️  "}


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

DEFAULT_PRIORITY = {"force": 100, "forbid": 50, "history": 10}
RELAX_MODES = ("pair", "rule")
HISTORY_VERSION = 2

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")        # lenient on purpose: local addresses are fine
BAD_NAME_RE = re.compile(r"[\\/\x00-\x1f]")        # names become file names (<name>.mail)


@dataclass(eq=False)  # eq=False: entries are compared by identity, not content
class Entry:
    """One atomic compiled constraint on a directed pair ``(giver, receiver)``.

    ``kind`` is ``"forbid"`` or ``"force"``; ``source`` is a human-readable
    label of the raw rule it came from; entries with the same non-None
    ``bundle`` are satisfied or given up together.
    """
    kind: str
    pair: tuple
    priority: int
    soft: bool
    source: str
    bundle: Optional[int]


class Person:
    """A participant: a stable ``id`` (None until assigned), a unique display name, a mail address."""

    def __init__(self, name, email, id=None):
        self.name = name
        self.email = email
        self.id = id

    def to_dict(self):
        out = {"id": self.id} if self.id else {}
        return {**out, "name": self.name, "email": self.email}

    @classmethod
    def from_dict(cls, data, position=None):
        where = f"Participant {position}" if position else "Participant"
        if not isinstance(data, dict):
            raise SantaError(f"{where} : format inattendu (un objet avec « name » et « email » est attendu).")
        try:
            name, email = data["name"], data["email"]
        except KeyError as e:
            raise SantaError(f"{where} : le champ {e.args[0]} est manquant.") from None
        if not isinstance(name, str) or not isinstance(email, str):
            raise SantaError(f"{where} : « name » et « email » doivent être du texte.")
        pid = data.get("id")
        if pid is not None and not isinstance(pid, str):
            raise SantaError(f"{where} : « id » doit être du texte.")
        return cls(name=name.strip(), email=email.strip(), id=pid or None)

    def __repr__(self):
        return f"Person(name='{self.name}', email='{self.email}', id={self.id!r})"


def check_people(people):
    """Validate a participant list: the checks the draw relies on.

    Names identify people in the rules *and* become file names (``<name>.mail``),
    so they must be unique and safe; ids, when present, must be unique too.
    """
    for n, p in enumerate(people, start=1):
        if not p.name:
            raise SantaError(f"Participant {n} : le nom est vide.")
        if BAD_NAME_RE.search(p.name) or p.name.startswith("."):
            raise SantaError(f"« {p.name} » : le nom ne peut pas contenir de « / », de « \\ » "
                             "ni commencer par un point (il sert de nom de fichier).")
        if not EMAIL_RE.match(p.email):
            raise SantaError(f"{p.name} : adresse e-mail invalide.")
        if p.id is not None and not ID_RE.match(p.id):
            raise SantaError(f"{p.name} : identifiant invalide « {p.id} » "
                             "(lettres, chiffres, « - » et « _ », 40 caractères au plus).")
    names = [p.name for p in people]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise SantaError(f"Noms en double : {', '.join(dupes)}.")
    ids = [p.id for p in people if p.id]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SantaError(f"Identifiants en double : {', '.join(dupes)}.")
    if len(people) < 2:
        raise SantaError("Il faut au moins 2 participants.")


def assign_ids(people):
    """Give an id to everyone who lacks one (in place). Returns how many were added."""
    taken = {p.id for p in people if p.id}
    added = 0
    for p in people:
        if not p.id:
            while True:
                candidate = secrets.token_hex(4)
                if candidate not in taken:
                    break
            p.id = candidate
            taken.add(candidate)
            added += 1
    return added


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

_REQUIRED = object()


def read_json(path, default=_REQUIRED):
    """Read a JSON file; return ``default`` if it does not exist.

    A *malformed* file is never swallowed: we want to stop and tell the user
    rather than run a draw with half of the rules missing.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        if default is _REQUIRED:
            raise SantaError(f"Fichier {path} introuvable.") from None
        return default
    except json.JSONDecodeError as e:
        raise SantaError(f"{path} n'est pas un JSON valide : {e}") from None


def dump_json(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_atomic(path, data, *, backup=False):
    """Write ``data`` (str or bytes) so that a reader never sees half a file.

    The content goes to a temporary file in the same folder and is then moved
    over the target (atomic on one filesystem). With ``backup=True`` the
    previous version, if any, is first copied to ``<path>.bak``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, f"{path}.bak")
    binary = isinstance(data, bytes)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb" if binary else "w", **({} if binary else {"encoding": "utf-8", "newline": "\n"})) as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# History (the "ad hoc" rule: don't draw the same person two years in a row)
# --------------------------------------------------------------------------

def resolve_draw(data, people):
    """Turn one history file into a set of ``(giver_index, receiver_index)``.

    Version 2 files name people by ``id``; if an id matches nobody but its
    recorded name matches a current person who has no id yet, that person is
    used. Version 1 files (no ``version``) name people by name. Pairs that
    mention somebody who is no longer a participant are dropped.
    Returns ``(pairs, dropped)``.
    """
    by_name = {p.name: i for i, p in enumerate(people)}
    pairs, dropped = set(), 0
    if data.get("version", 1) >= 2:
        by_id = {p.id: i for i, p in enumerate(people) if p.id}
        recorded = data.get("people", {})
        idless = {p.name: i for i, p in enumerate(people) if not p.id}

        def find(key):
            if key in by_id:
                return by_id[key]
            return idless.get(recorded.get(key))
    else:
        find = by_name.get
    for a in data.get("assignments", []):
        i, j = find(a["from"]), find(a["to"])
        if i is None or j is None:
            dropped += 1
        else:
            pairs.add((i, j))
    return pairs, dropped


def read_history_draws(history_dir, before_year, last_n, people):
    """Return ``[(year, pairs, dropped), ...]``, most recent first.

    Only draws from years strictly *before* ``before_year`` count. Otherwise,
    re-running the generator in the same year (after fixing a typo, say) would
    forbid the draw it just produced and make the result drift for no reason.
    """
    history_dir = Path(history_dir)
    if not history_dir.is_dir():
        return None
    draws = []
    for path in sorted(history_dir.glob("*.json")):
        data = read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("year"), int):
            raise SantaError(f"{path} n'est pas un fichier d'historique valide (champ « year » absent).")
        if data["year"] < before_year:
            draws.append(data)
    draws.sort(key=lambda d: d["year"], reverse=True)
    out = []
    for d in draws[:last_n]:
        pairs, dropped = resolve_draw(d, people)
        out.append((d["year"], pairs, dropped))
    return out


def history_document(year, people, assignment, violations=(), *, imported=False, names=None):
    """The JSON document written to ``history/<year>.json`` (version 2).

    ``violations`` is a list of ``(source, count)``: the soft rules that could
    not be fully respected. Only counts are stored, never the offending pairs.
    ``people`` is stored as an ``id -> name`` snapshot, to make the file
    readable and to survive an id being lost.

    NOTE: this file reveals every pair. Anyone who is both organizer and
    participant spoils their own surprise by opening it (see README, "Privacy").
    """
    if any(not p.id for p in people):
        raise SantaError("Des participants n'ont pas d'identifiant : impossible d'enregistrer l'historique.")
    doc = {
        "version": HISTORY_VERSION,
        "year": year,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "relaxed_rules": [{"source": s, "violations": c} for s, c in violations],
        "people": {p.id: p.name for p in people},
        "assignments": [{"from": people[i].id, "to": people[j].id} for i, j in sorted(assignment.items())],
    }
    if imported:
        doc["imported"] = True
    return doc


# --------------------------------------------------------------------------
# Raw rules -> compiled entries
# --------------------------------------------------------------------------

@dataclass
class Plan:
    """Result of compiling the rules: what the solver will be given.

    ``notices`` are informational lines (history size...); ``conflicts`` are
    the notes about rules that lost against a stronger one.
    """
    people: list
    entries: list
    settings: dict
    notices: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)


def compile_rules(rules, people, year, history_dir, base="."):
    """Translate the raw JSON rules into a flat list of ``Entry`` objects.

    Returns ``(entries, settings, notices)``. Everything above the two
    primitives ("forbid this pair" / "force this pair") is expanded here.
    Rules name people by *name*; a name that matches nobody is an error.
    """
    if not isinstance(rules, dict):
        raise SantaError("Les règles doivent être un objet JSON.")
    index = {p.name: i for i, p in enumerate(people)}
    bundle_ids = itertools.count()
    notices = []

    def idx(name, where):
        if name not in index:
            raise SantaError(f"Participant inconnu « {name} » dans la règle {where}.")
        return index[name]

    def opts(entry, default_priority, default_soft=False, default_relax="rule"):
        """Read the optional ``priority`` / ``soft`` / ``relax`` keys."""
        relax = entry.get("relax", default_relax)
        if relax not in RELAX_MODES:
            raise SantaError(f"« relax » doit valoir {' ou '.join(RELAX_MODES)}, pas {relax!r}.")
        return (int(entry.get("priority", default_priority)),
                bool(entry.get("soft", default_soft)),
                relax)

    def expand(kind, pairs, prio, soft, relax, source):
        """One entry per pair; one shared bundle id if ``relax == "rule"``."""
        pairs = sorted(pairs)
        if kind == "forbid":
            pairs = [p for p in pairs if p[0] != p[1]]  # (i, i) is impossible anyway
        bundle = next(bundle_ids) if relax == "rule" and len(pairs) > 1 else None
        return [Entry(kind, p, prio, soft, source, bundle) for p in pairs]

    out = []
    try:
        for r in rules.get("forbidden", []):
            a, b = idx(r["from"], "forbidden"), idx(r["to"], "forbidden")
            prio, soft, relax = opts(r, DEFAULT_PRIORITY["forbid"])
            out += expand("forbid", {(a, b)}, prio, soft, relax,
                          f"forbidden {r['from']}→{r['to']}")

        for r in rules.get("forced", []):
            a, b = idx(r["from"], "forced"), idx(r["to"], "forced")
            if a == b:
                raise SantaError(f"Règle forced impossible : {r['from']} → lui/elle-même.")
            prio, soft, relax = opts(r, DEFAULT_PRIORITY["force"])
            out += expand("force", {(a, b)}, prio, soft, relax,
                          f"forced {r['from']}→{r['to']}")

        # An entry is either a bare list of names or {"members": [...], "priority": ...}.
        # A group of N people expands to N*(N-1) forbidden ordered pairs.
        for key in ("couples", "groups"):
            for item in rules.get(key, []):
                o = item if isinstance(item, dict) else {"members": item}
                members = o["members"]
                if key == "couples" and len(members) != 2:
                    raise SantaError(f"Un « couple » doit contenir 2 noms : {members}")
                ids = [idx(n, key) for n in members]
                prio, soft, relax = opts(o, DEFAULT_PRIORITY["forbid"])
                out += expand("forbid", set(permutations(ids, 2)), prio, soft, relax,
                              f"{key[:-1]} {'+'.join(members)}")

        hist = rules.get("history")
        if hist is True:
            hist = {}                       # "history": true means "with the defaults"
        if hist not in (None, False):       # note: {} is a rule too (defaults), not "no rule"
            if not isinstance(hist, dict):
                raise SantaError("« history » doit être un objet, par exemple {\"last\": 2}.")
            last_n = int(hist.get("last", 1))
            prio0, soft, relax = opts(hist, DEFAULT_PRIORITY["history"],
                                      default_soft=True, default_relax="pair")
            directory = Path(base) / hist.get("directory", history_dir)
            draws = read_history_draws(directory, year, last_n, people)
            if draws is None:
                notices.append(Notice("info", f"Pas de dossier d'historique ({directory}) : règle ignorée."))
                draws = []
            total = dropped = 0
            for age, (y, pairs, lost) in enumerate(draws):
                total += len(pairs)
                dropped += lost
                # One priority level per year: older years give way first.
                out += expand("forbid", pairs, prio0 - age, soft, relax, f"history {y}")
            notices.append(Notice("info", f"Historique : {total} paire(s) interdite(s) "
                                          f"({len(draws)} tirage(s) précédent(s))."))
            if dropped:
                notices.append(Notice("info", f"{dropped} paire(s) de l'historique ignorée(s) : "
                                              "personnes qui ne participent plus."))
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise SantaError(f"Règle mal formée ({type(e).__name__} : {e}).") from None

    settings = rules.get("settings", {})
    return out, settings if isinstance(settings, dict) else {}, notices


def resolve_pair_conflicts(entries, people):
    """Settle direct contradictions: a pair both forced and forbidden.

    For each such pair the highest priority wins and the entries on the other
    side are removed (for that pair only). Equal top priorities are an error.
    Returns ``(entries, notes)``.
    """
    by_pair = defaultdict(list)
    for e in entries:
        by_pair[e.pair].append(e)

    dropped, notes = set(), []
    for pair, es in by_pair.items():
        forcers = [e for e in es if e.kind == "force"]
        forbidders = [e for e in es if e.kind == "forbid"]
        if not forcers or not forbidders:
            continue
        top_force = max(forcers, key=lambda e: e.priority)
        top_forbid = max(forbidders, key=lambda e: e.priority)
        who = f"{people[pair[0]].name}→{people[pair[1]].name}"

        if top_force.priority == top_forbid.priority:
            raise SantaError(f"Conflit à priorité égale ({top_force.priority}) pour {who} : "
                             f"« {top_force.source} » contre « {top_forbid.source} ».",
                             hint="Donnez une priorité différente à l'une des deux.")

        winner, losers = ((top_force, forbidders) if top_force.priority > top_forbid.priority
                          else (top_forbid, forcers))
        for loser in losers:
            dropped.add(id(loser))
            notes.append(f"{who} : « {winner.source} » (priorité {winner.priority}) "
                         f"l'emporte sur « {loser.source} » (priorité {loser.priority}).")

    return [e for e in entries if id(e) not in dropped], notes


def build_plan(people, rules, year, history_dir="history", base="."):
    """Check the people, compile the raw rules and settle conflicts: a ``Plan``.

    This is everything that happens before the solver, so a front end can
    validate rules as they are typed without drawing anything.
    """
    check_people(people)
    entries, settings, notices = compile_rules(rules, people, year, history_dir, base)
    entries, conflicts = resolve_pair_conflicts(entries, people)
    return Plan(people, entries, settings, notices, conflicts)


def dump_compiled(path, plan):
    """Write the compiled entries to ``path`` (``--emit-compiled``).

    Debug aid only: it is never read back. It contains every pair coming from
    the history files, i.e. it is as sensitive as ``history/``.
    """
    people = plan.people
    data = {
        "settings": plan.settings,
        "entries": [
            {"kind": e.kind, "from": people[e.pair[0]].name, "to": people[e.pair[1]].name,
             "priority": e.priority, "soft": e.soft, "source": e.source, "bundle": e.bundle}
            for e in plan.entries
        ],
    }
    write_atomic(path, dump_json(data))


# --------------------------------------------------------------------------
# Solver
# --------------------------------------------------------------------------

@dataclass
class Solution:
    """Outcome of ``solve``. ``assignment`` is ``{giver: receiver}`` (indices), or None.

    ``violations`` is a list of ``(source, count)``, highest priority first:
    how many units of each soft rule had to be given up. Only counts, never pairs.
    """
    assignment: Optional[dict]
    violations: list

    @property
    def feasible(self):
        return self.assignment is not None


def _satisfied(entry, assignment):
    """Is ``entry`` respected by a finished ``{giver: receiver}`` assignment?"""
    drawn = assignment.get(entry.pair[0]) == entry.pair[1]
    return drawn == (entry.kind == "force")


def solve(n, entries, single_cycle=False, time_limit=20.0, seed=None):
    """Find an assignment for ``n`` people under the compiled ``entries``: a ``Solution``.

    ``x[i, j]`` is true when i gives a gift to j. Without ``single_cycle`` we
    ask for exactly one outgoing and one incoming arc per person; with it,
    ``AddCircuit`` additionally requires a single loop through everybody.

    Hard entries are plain constraints. Soft entries are grouped into *units*
    (a whole bundle, or a single entry) with a satisfaction literal ``s``:
    ``s`` implies all the unit's entries hold. Priorities are then optimized
    lexicographically (see module docstring), and the last solve spends the
    remaining freedom on random weights.

    ``seed`` makes the whole thing reproducible, which also makes it
    *guessable*: it exists for tests, never use it for a real draw.
    """
    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    model = cp_model.CpModel()
    x = {(i, j): model.NewBoolVar(f"x_{i}_{j}")
         for i in range(n) for j in range(n) if i != j}

    if single_cycle:
        model.AddCircuit([(i, j, lit) for (i, j), lit in x.items()])
    else:
        for i in range(n):
            model.AddExactlyOne(x[i, j] for j in range(n) if j != i)
            model.AddExactlyOne(x[j, i] for j in range(n) if j != i)

    # Group soft entries into units; hard entries become plain constraints.
    groups = defaultdict(list)
    for e in entries:
        want = 1 if e.kind == "force" else 0
        if not e.soft:
            model.Add(x[e.pair] == want)
        else:
            key = ("bundle", e.bundle) if e.bundle is not None else ("entry", id(e))
            groups[key].append(e)

    units = []  # (priority, source, satisfaction literal, entries)
    for es in groups.values():
        s = model.NewBoolVar("unit_satisfied")
        for e in es:
            model.Add(x[e.pair] == (1 if e.kind == "force" else 0)).OnlyEnforceIf(s)
        units.append((es[0].priority, es[0].source, s, es))

    def run():
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = time_limit
        if seed is not None:
            solver.parameters.num_workers = 1
            solver.parameters.random_seed = seed % (2 ** 31)
        status = solver.Solve(model)
        return status in (cp_model.OPTIMAL, cp_model.FEASIBLE), solver

    # Highest priority first: minimize violations, freeze, go down one level.
    for prio in sorted({u[0] for u in units}, reverse=True):
        tier = [u[2] for u in units if u[0] == prio]
        model.Maximize(sum(tier))
        ok, solver = run()
        if not ok:
            return Solution(None, [])
        model.Add(sum(tier) >= int(round(solver.ObjectiveValue())))

    model.Maximize(sum(rng.randint(0, 1_000_000) * lit for lit in x.values()))
    ok, solver = run()
    if not ok:
        return Solution(None, [])
    assignment = {i: j for (i, j), lit in x.items() if solver.Value(lit)}

    # Report from the real assignment, not from the literals, and only counts.
    counts = Counter()
    prio_of = {}
    for prio, source, _, es in units:
        if not all(_satisfied(e, assignment) for e in es):
            counts[source] += 1
            prio_of[source] = max(prio_of.get(source, prio), prio)
    violations = sorted(counts.items(), key=lambda sc: -prio_of[sc[0]])
    return Solution(assignment, violations)


# --------------------------------------------------------------------------
# Message template
# --------------------------------------------------------------------------

TEMPLATE_VARIABLES = ("santa", "recipient", "year")
DEFAULT_TEMPLATE_PATH = "message.txt"
DEFAULT_TEMPLATE = (
    "Subject: Père Noël Secret\n"
    "\n"
    "Tu es le Père Noël secret de {recipient} !\n"
)

# Fictional stand-ins used whenever a message is rendered for a preview or a
# validation pass. They are storybook characters on purpose: nobody is called
# that, so a preview can never be mistaken for a real mail.
PREVIEW_SANTA = "Mère Noël"
PREVIEW_RECIPIENT = "Rudolph"


class Template:
    """The mail text: a subject and a body, both may contain ``{variables}``.

    Build one with ``Template.parse`` (from text) or ``Template.load`` (from a
    file); both validate it by rendering it once with the preview characters,
    so every mistake surfaces *now*, before the solver runs.
    """

    def __init__(self, subject, body, source="message"):
        self.subject, self.body, self.source = subject, body, source

    @classmethod
    def parse(cls, text, source="message"):
        """Format: first line ``Subject: ...``, then one blank line, then the body.

        Header-like lines (``To:``, ``From:``...) right after the subject are
        refused instead of silently becoming part of the body: the recipient is
        set by the program, and a wrong header here would be easy to miss.
        """
        lines = text.replace("\r\n", "\n").split("\n")   # tolerate Windows editors
        m = re.match(r"(?i)\s*subject:\s*(\S.*)$", lines[0])
        if not m:
            raise SantaError(f"{source} : la première ligne doit être « Subject: … ».")
        if len(lines) > 1 and lines[1].strip():
            raise SantaError(f"{source} : la ligne 2 doit être vide (le corps du message vient après).")
        body = "\n".join(lines[2:]).strip("\n")
        if not body.strip():
            raise SantaError(f"{source} : le corps du message est vide.")
        template = cls(m.group(1).strip(), body + "\n", source)
        template.preview(2000)          # validate now: unknown variable, stray brace...
        return template

    @classmethod
    def load(cls, path=None, base="."):
        """Read a template file; return ``(template, notices)``.

        ``path`` is None when no file was asked for explicitly: ``message.txt``
        is used if present, else the built-in text. An explicit path that does
        not exist is an error.
        """
        candidate = Path(base) / (path or DEFAULT_TEMPLATE_PATH)
        if candidate.is_file():
            # utf-8-sig drops a BOM that some editors add
            return cls.parse(candidate.read_text(encoding="utf-8-sig"), str(path or DEFAULT_TEMPLATE_PATH)), []
        if path:
            raise SantaError(f"Modèle de message introuvable : {path}")
        return (cls.parse(DEFAULT_TEMPLATE, "message par défaut"),
                [Notice("info", f"{DEFAULT_TEMPLATE_PATH} introuvable : message par défaut.")])

    def render(self, **values):
        """Fill in the variables; turn any template mistake into a clear error.

        ``format_map`` is strict on purpose: an unknown ``{variable}`` raises
        instead of leaking into the mail as literal text.
        """
        try:
            return self.subject.format_map(values).strip(), self.body.format_map(values)
        except KeyError as e:
            names = ", ".join("{" + v + "}" for v in TEMPLATE_VARIABLES)
            raise SantaError(f"{self.source} : variable inconnue {{{e.args[0]}}}. Disponibles : {names}.") from None
        except (ValueError, IndexError, AttributeError) as e:
            raise SantaError(f"{self.source} : modèle invalide ({e}). "
                             f"Pour une accolade littérale, doublez-la : {{{{ ou }}}}.") from None

    def preview(self, year):
        """``(subject, body)`` rendered with the preview characters."""
        return self.render(santa=PREVIEW_SANTA, recipient=PREVIEW_RECIPIENT, year=year)


# --------------------------------------------------------------------------
# Mail files
# --------------------------------------------------------------------------

def build_mail(santa, recipient, template, year):
    """Build one ``.mail`` file as bytes.

    Layout: first line = bare recipient address (used by ssm.sh as the
    ``msmtp`` argument), then a complete RFC 5322 message. ``EmailMessage``
    takes care of the blank line between headers and body and of UTF-8
    encoding.
    """
    subject, body = template.render(santa=santa.name, recipient=recipient.name, year=year)
    msg = EmailMessage()
    msg["To"] = santa.email
    msg["Subject"] = subject
    msg.set_content(body, charset="utf-8")
    return santa.email.encode() + b"\n" + msg.as_bytes()


# --------------------------------------------------------------------------
# Project: one folder = one participant list with its rules, message and history
# --------------------------------------------------------------------------

@dataclass
class DrawResult:
    """What ``Project.draw`` reports. Never contains who draws whom.

    ``feasible`` is False when no draw satisfies the strict rules; that is a
    normal outcome, explained in ``notices``. ``preview`` is
    ``(subject, body, source)`` of the message with the preview characters,
    only set for a dry run.
    """
    year: int
    dry_run: bool
    feasible: bool
    notices: list
    violations: list = field(default_factory=list)
    preview: Optional[tuple] = None
    mails: list = field(default_factory=list)       # names of the people whose mail was written
    history_file: Optional[str] = None


class Project:
    """The files of one draw, all relative to ``base``.

    ``base`` is what makes several lists possible side by side (one folder
    each) without ever changing the current directory. The other arguments are
    names relative to ``base`` (or absolute paths) and default to the standard
    layout. ``template=None`` means ``message.txt`` if it exists, else the
    built-in message.
    """

    def __init__(self, base=".", *, participants="participants.json", rules="rules.json",
                 history_dir="history", output_dir="secretSantaFiles", template=None):
        self.base = Path(base)
        self.participants_file, self.rules_file = participants, rules
        self.history_dir, self.output_dir, self.template = history_dir, output_dir, template

    # -- paths -----------------------------------------------------------

    def path(self, name):
        return self.base / name

    @property
    def participants_path(self):
        return self.path(self.participants_file)

    @property
    def rules_path(self):
        return self.path(self.rules_file)

    @property
    def history_path(self):
        return self.path(self.history_dir)

    @property
    def output_path(self):
        return self.path(self.output_dir)

    @property
    def template_path(self):
        return self.path(self.template or DEFAULT_TEMPLATE_PATH)

    # -- reading and writing the inputs ----------------------------------

    def load_people(self, *, check=True):
        data = read_json(self.participants_path)
        if not isinstance(data, list):
            raise SantaError(f"{self.participants_file} doit contenir une liste de participants.")
        people = [Person.from_dict(item, n) for n, item in enumerate(data, start=1)]
        if check:
            check_people(people)
        return people

    def save_people(self, people):
        write_atomic(self.participants_path, dump_json([p.to_dict() for p in people]), backup=True)

    def load_rules(self):
        return read_json(self.rules_path, default={})

    def ensure_ids(self):
        """Load the participants, giving an id to anyone lacking one and saving the file if so.

        Returns ``(people, added)``.
        """
        people = self.load_people()
        added = assign_ids(people)
        if added:
            self.save_people(people)
        return people, added

    def plan(self, year, people=None, rules=None):
        """Compile this project's rules (or the given ones) for ``year``: a ``Plan``."""
        return build_plan(people if people is not None else self.load_people(),
                          rules if rules is not None else self.load_rules(),
                          year, self.history_dir, self.base)

    # -- the draw ----------------------------------------------------------

    def draw(self, year, *, dry_run=False, emit_compiled=None, seed=None, time_limit=20.0):
        """Check the inputs, solve, and (unless ``dry_run``) write mails and history.

        Order matters: the message is validated *first* (a typo must not cost
        a solver run), every mail is rendered in memory *before* any file is
        written, and the history goes down *before* the mails, so a mail that
        exists always belongs to a recorded draw.

        Raises ``SantaError`` for bad input; "no valid draw" is a result.
        """
        template, notices = Template.load(self.template, self.base)
        people = self.load_people()
        plan = self.plan(year, people)
        notices += plan.notices + [Notice("conflict", c) for c in plan.conflicts]

        if emit_compiled:
            dump_compiled(self.path(emit_compiled), plan)
            notices.append(Notice("file", f"Règles compilées écrites dans {emit_compiled} "
                                          "(⚠️  contient les paires de l'historique)."))

        solution = solve(len(people), plan.entries, bool(plan.settings.get("single_cycle", False)),
                         time_limit=time_limit, seed=seed)
        if not solution.feasible:
            notices += [Notice("error", "Aucun tirage ne respecte les règles strictes."),
                        Notice("hint", "Vérifiez qu'elles ne se contredisent pas, ou marquez-en certaines "
                                       '"soft": true pour qu\'elles puissent être violées.')]
            return DrawResult(year, dry_run, False, notices)
        notices += [Notice("warn", f"Règle souple non respectée : {source} ({count} violation(s))")
                    for source, count in solution.violations]

        if dry_run:
            notices.append(Notice("ok", "Un tirage valide existe (rien n'a été écrit)."))
            subject, body = template.preview(year)
            return DrawResult(year, True, True, notices, solution.violations,
                              preview=(subject, body, template.source))

        if seed is not None:
            notices.append(Notice("warn", "Tirage reproductible (--seed) : à réserver aux tests, "
                                          "n'importe qui connaissant la graine peut le refaire."))
        people, added = self.ensure_ids()
        if added:
            notices.append(Notice("info", f"Identifiants ajoutés à {self.participants_file} "
                                          f"({added} participant(s)) ; l'ancienne version est dans {self.participants_file}.bak."))
        converted = self.migrate_history()
        if converted:
            notices.append(Notice("info", "Historique converti aux identifiants : "
                                          f"{', '.join(map(str, converted))} (copies .bak conservées)."))

        mails = {p.name: build_mail(p, people[solution.assignment[i]], template, year)
                 for i, p in enumerate(people)}
        history_file = self.history_path / f"{year}.json"
        write_atomic(history_file, dump_json(history_document(year, people, solution.assignment,
                                                              solution.violations)))
        for name, data in mails.items():
            write_atomic(self.output_path / f"{name}.mail", data)

        notices.append(Notice("ok", f"Tirage généré : {len(people)} mails dans {self.output_dir}/"))
        notices.append(Notice("file", f"Historique enregistré dans {self.history_dir}/{year}.json"))
        stale = sorted(p.stem for p in self.output_path.glob("*.mail") if p.stem not in mails)
        if stale:
            notices.append(Notice("warn", f"{len(stale)} mail(s) d'un tirage précédent n'ont pas été remplacés "
                                          f"({', '.join(stale)}) : ne les envoyez pas, supprimez-les (ssm.sh -c)."))
        return DrawResult(year, False, True, notices, solution.violations,
                          mails=list(mails), history_file=str(history_file))

    # -- history management --------------------------------------------------

    def _history_files(self):
        d = self.history_path
        return sorted(d.glob("*.json")) if d.is_dir() else []

    def list_history(self):
        """Metadata of past draws, oldest first. Pairs are deliberately left out."""
        out = []
        for p in self._history_files():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                out.append({
                    "year": data["year"],
                    "generated_at": data.get("generated_at"),
                    "count": len(data.get("assignments", [])),
                    "imported": bool(data.get("imported")),
                    "relaxed_rules": data.get("relaxed_rules", []),
                    "by_id": data.get("version", 1) >= 2,
                })
            except (OSError, ValueError, KeyError, AttributeError, TypeError):
                out.append({"year": None, "file": p.name, "error": "illisible"})
        return sorted(out, key=lambda h: (h.get("year") is None, h.get("year") or 0))

    def read_history(self, year):
        """The pairs of one past draw, as names: ``{"year", "pairs": [(giver, receiver)], ...}``.

        This is the one place where the library hands out *who gives to whom*
        from a stored draw; everything else (``list_history``, the web
        interface) stays count-only. Callers decide whether showing it is
        acceptable. Names come from the current participant list when the id
        is still known (so a renamed person shows under the new name), else
        from the name recorded in the file. Raises ``SantaError`` if there is
        no such draw.
        """
        for path in self._history_files():
            data = read_json(path)
            if isinstance(data, dict) and data.get("year") == year:
                break
        else:
            raise SantaError(f"Aucun tirage enregistré pour {year}.")
        by_id = {p.id: p.name for p in self.load_people() if p.id}
        recorded = data.get("people", {}) if data.get("version", 1) >= 2 else {}

        def label(key):
            return by_id.get(key) or recorded.get(key) or str(key)

        pairs = [(label(a.get("from")), label(a.get("to"))) for a in data.get("assignments", [])]
        return {"year": year, "pairs": pairs, "generated_at": data.get("generated_at"),
                "imported": bool(data.get("imported")), "relaxed_rules": data.get("relaxed_rules", [])}

    def migrate_history(self):
        """Convert name-based (version 1) history files to ids. Returns the years converted.

        People still in the list get their id; people who left get a fresh one
        (the same one in every file, so they stay recognisable across years).
        Each converted file keeps a ``.bak`` copy. Does nothing, and needs no
        participants file, when there is nothing to convert.
        """
        legacy = []
        for p in self._history_files():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("year"), int) and data.get("version", 1) < 2:
                legacy.append((p, data))
        if not legacy:
            return []
        people, _ = self.ensure_ids()
        ids = {p.name: p.id for p in people}
        taken = set(ids.values())
        departed = {}

        def ident(name):
            if name in ids:
                return ids[name]
            if name not in departed:
                while True:
                    candidate = secrets.token_hex(4)
                    if candidate not in taken:
                        break
                taken.add(candidate)
                departed[name] = candidate
            return departed[name]

        years = []
        for path, data in legacy:
            snapshot, assignments = {}, []
            for a in data.get("assignments", []):
                for name in (a["from"], a["to"]):
                    snapshot[ident(name)] = name
                assignments.append({"from": ident(a["from"]), "to": ident(a["to"])})
            doc = {"version": HISTORY_VERSION, "year": data["year"],
                   "generated_at": data.get("generated_at"),
                   "relaxed_rules": data.get("relaxed_rules", []),
                   "people": snapshot, "assignments": assignments}
            if data.get("imported"):
                doc["imported"] = True
            write_atomic(path, dump_json(doc), backup=True)
            years.append(data["year"])
        return sorted(years)

    def import_history(self, year, pairs, overwrite=False):
        """Record a draw made before using this tool. ``pairs`` is ``[(giver, receiver)]`` by name.

        Everyone must appear exactly once as giver and once as receiver. Names
        that are not (or no longer) participants are accepted: they get an id
        of their own. Returns the sorted list of such names.
        """
        path = self.history_path / f"{year}.json"
        if path.exists() and not overwrite:
            raise HistoryExists(f"Un tirage {year} existe déjà.")
        if len(pairs) < 2:
            raise SantaError("Il faut au moins 2 paires.")
        givers, receivers = [a for a, _ in pairs], [b for _, b in pairs]
        if len(set(givers)) != len(givers):
            raise SantaError("Une personne apparaît deux fois comme donneur.")
        if len(set(receivers)) != len(receivers):
            raise SantaError("Une personne apparaît deux fois comme receveur.")
        if set(givers) != set(receivers):
            raise SantaError("Chaque participant doit être à la fois donneur et receveur.")
        if any(a == b for a, b in pairs):
            raise SantaError("Personne ne peut se tirer soi-même.")

        try:
            current, _ = self.ensure_ids()
        except SantaError:
            current = []                   # no usable participants yet: everybody is a stranger
        ids = {p.name: p.id for p in current}
        taken = set(ids.values())
        unknown = sorted(set(givers) - set(ids))
        for name in unknown:
            while True:
                candidate = secrets.token_hex(4)
                if candidate not in taken:
                    break
            taken.add(candidate)
            ids[name] = candidate
        doc = {"version": HISTORY_VERSION, "year": year,
               "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
               "imported": True, "relaxed_rules": [],
               "people": {ids[n]: n for n in set(givers)},
               "assignments": [{"from": ids[a], "to": ids[b]} for a, b in pairs]}
        write_atomic(path, dump_json(doc), backup=True)
        return unknown

    def set_aside_history(self, year):
        """``<year>.json`` becomes ``<year>.json.bak``: no longer read, nothing lost."""
        path = self.history_path / f"{year}.json"
        if not path.exists():
            raise SantaError(f"Aucun tirage {year} dans l'historique.")
        os.replace(path, f"{path}.bak")

    def upgrade(self):
        """Bring an older project up to date: ids in the participants, history by id.

        Safe to call at any time (it only writes when something is missing,
        always keeping ``.bak`` copies). Returns ``{"ids_added": n, "history_converted": [years]}``.
        """
        _, added = self.ensure_ids()
        return {"ids_added": added, "history_converted": self.migrate_history()}


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def print_notices(notices, out=None):
    for n in notices:
        print(f"{NOTICE_ICONS.get(n.level, '')}{n.text}", file=out or sys.stdout)


def print_preview(preview, out=None):
    subject, body, source = preview
    p = lambda s: print(s, file=out or sys.stdout)
    p(f"📧 Aperçu du message ({source}, personnages fictifs) :")
    p(f"   Subject: {subject}")
    p("   " + "─" * 40)
    for line in body.rstrip("\n").split("\n"):
        p(f"   {line}")


def build_parser():
    ap = argparse.ArgumentParser(description="Tirage du Père Noël secret.")
    ap.add_argument("--base-dir", default=".", metavar="DIR",
                    help="folder holding the project files; every other path is relative to it "
                         "(default: current folder)")
    ap.add_argument("--participants", default="participants.json")
    ap.add_argument("--rules", default="rules.json")
    ap.add_argument("--output-dir", default="secretSantaFiles")
    ap.add_argument("--history-dir", default="history")
    ap.add_argument("--year", type=int, default=datetime.date.today().year,
                    help="year recorded in the history file (default: this year)")
    ap.add_argument("--dry-run", action="store_true",
                    help="only check that a valid draw exists; write no mail, no history, print no pairs")
    ap.add_argument("--emit-compiled", metavar="FILE",
                    help="also write the compiled entries to FILE (debug; as sensitive as history/)")
    ap.add_argument("--template", metavar="FILE",
                    help=f"message template (default: {DEFAULT_TEMPLATE_PATH} if present, "
                         "else a built-in text)")
    ap.add_argument("--seed", type=int, metavar="N",
                    help="make the draw reproducible (TESTS ONLY: anyone knowing the seed can redo the draw)")
    return ap


def main(argv=None):
    """Command-line entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    project = Project(args.base_dir, participants=args.participants, rules=args.rules,
                      history_dir=args.history_dir, output_dir=args.output_dir, template=args.template)
    try:
        result = project.draw(args.year, dry_run=args.dry_run, emit_compiled=args.emit_compiled,
                              seed=args.seed)
    except SantaError as e:
        print(f"❌ {e.message}")
        if e.hint:
            print(f"💡 {e.hint}")
        return 1
    except OSError as e:
        print(f"❌ Erreur de fichier : {e}")
        return 1
    print_notices(result.notices)
    if result.preview:
        print_preview(result.preview)
    return 0 if result.feasible else 1


if __name__ == "__main__":
    sys.exit(main())
