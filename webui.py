#!/usr/bin/env python3
"""Local web interface for the Secret Santa generator.

What this is
------------
A secondary module that plugs into the project *through its public API*:
``Santa.py`` knows nothing about it, and the files it works on
(``participants.json``, ``rules.json``, ``message.txt``, ``history/``) stay the
single source of truth. You can use the web page, the command line, or both,
in any order.

    python3 webui.py            # then open the printed address

How it plugs in
---------------
* ``Santa.py`` is imported as a library. Validation, simulations and the draw
  itself call the same functions as the command line (``Project.draw``,
  ``build_plan``, ``solve``), so the page can never disagree with
  the program about what a rule means. The official draw is ``Project.draw``,
  the very call ``Santa.py`` makes for ``ssm.sh -g``.
* Everything is passed around as data: no console output is parsed and no
  process-wide state (current folder, ``sys.argv``) is touched, so the only
  lock left protects against two requests writing the same files at once.
* Two modules, two jobs. ``Santa.py`` makes the draw and records it in
  ``history/<year>.json``. ``sender.py`` turns that draw into messages and
  delivers them (e-mail through ``msmtp``, SMS through an SMSGate phone, or by
  hand), keeping only a small ``delivery/<year>.json`` of who has received
  theirs. This page orchestrates both and does neither itself, so the page, the
  command line and ``ssm.sh`` can be mixed: nothing is sent twice.

Sets
----
Several participant lists can live side by side. A *set* is a folder holding
everything that belongs together: ``participants.json``, ``rules.json``,
``message.txt``, ``history/`` and the generated mails. Sets are the
sub-folders of ``<root>/sets/``; if the root folder already holds a project
(``participants.json`` or ``rules.json``) it shows up as one more set, so
existing setups keep working untouched.

Every API call names its set in an ``X-Santa-Set`` header (percent-encoded);
the server builds a ``Santa.Project`` on that folder. Set names become folder
names, so they are validated like participant names.

Identifiers
-----------
Participants carry a stable ``id`` (see ``Santa.py``). The page keeps it with
each row and sends it back on save, so renaming someone changes ``name`` only;
new rows get an id from the server. Opening a set written by an older version
upgrades it once (ids added, old history converted), with ``.bak`` copies.

Security model
--------------
The page edits local files and can launch a draw, so the server is locked down:

* it listens on 127.0.0.1 only;
* the ``Host`` header must be the local address (blocks DNS-rebinding);
* every request needs a random token that is generated at startup and only
  embedded in the page served to you, and POSTs must be ``application/json``
  (so another web page cannot forge them);
* the only file names ever touched are the fixed ones above, plus
  ``history/<year>.json`` with an integer year, inside a set that is on the
  server's own list: no user-supplied paths.

Privacy
-------
The page never displays the pairs of the official draw, nor the pairs stored
in ``history/``. Simulations use throw-away draws; their matrix shows which
pairs are *allowed*, which reflects the history rule (past years only).
The delivery list shows who is waiting for a message, by which channel, never
what it says. The one exception is the text of a message to hand over by hand
(SMS, Messenger...): it is fetched on demand and copied to the clipboard, not
displayed.

Safety net
----------
Before overwriting a file, the previous version is copied to ``<file>.bak``.
A deleted history year is renamed to ``<year>.json.bak`` (ignored by
``Santa.py``), a deleted set is moved to ``sets/.trash/``: nothing is erased.
"""

import argparse
import datetime
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import Santa  # the generator, used as a library
except ModuleNotFoundError as e:
    if e.name == "Santa":
        sys.exit("❌ Santa.py doit se trouver à côté de webui.py.")
    sys.exit(f"❌ Module manquant : {e.name}. Activez le venv ou lancez : pip install ortools")

try:
    import sender  # messages and delivery, reads what Santa.py recorded
except ModuleNotFoundError as e:
    if e.name == "sender":
        sys.exit("❌ sender.py doit se trouver à côté de webui.py.")
    raise

if not hasattr(Santa, "Project"):
    sys.exit("❌ Ce Santa.py est trop ancien : webui.py a besoin de la version avec l'API Project.\n"
             f"   Fichier importé : {getattr(Santa, '__file__', '?')}\n"
             "   Remplacez-le par le Santa.py livré avec ce webui.py (mêmes version et dossier).")


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

MAX_BODY_BYTES = 1_000_000
SIM_MAX_RUNS_PER_REQUEST = 200
SIM_TIME_BUDGET = 8.0          # seconds of solving per /api/simulate request
SOLVE_TIME_LIMIT = 5.0         # per solver call, so a hard case can't hang the page

IMPORT_LINE_RE = re.compile(r"^\s*(.+?)\s*(?:->|=>|→|>)\s*(.+?)\s*$")

SETS_SUBDIR = "sets"           # <root>/sets/<name>/ : one folder per set
TRASH_SUBDIR = ".trash"        # deleted sets are moved here, never erased
LEGACY_ID = "."                # the root folder itself, when it already holds a project
MAX_SET_NAME = 60
BAD_SET_NAME_RE = re.compile(r"[\\/\x00-\x1f:*?\"<>|]")   # portable folder names

ROOT = Path.cwd()              # set in main(); sets live under ROOT/sets

# One request at a time may write: two tabs saving the same set must not interleave,
# and the solver is CPU-hungry anyway. (No global state is involved any more.)
LOCK = threading.RLock()


class ApiError(Exception):
    """A request that must be refused; ``status`` is the HTTP code."""

    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra


def api_error(e, status=422):
    """Turn a ``Santa.SantaError`` into the refusal the page shows."""
    extra = {"code": "exists"} if isinstance(e, Santa.HistoryExists) else {}
    return ApiError(e.message, 409 if extra else status, **extra)


# --------------------------------------------------------------------------
# Helpers shared by the endpoints
# --------------------------------------------------------------------------

def parse_year(body):
    try:
        year = int(body.get("year", datetime.date.today().year))
    except (TypeError, ValueError):
        raise ApiError("L'année doit être un nombre.")
    if not 1900 <= year <= 9999:
        raise ApiError("L'année doit comporter 4 chiffres.")
    return year


def parse_people(data):
    """Check a participants list coming from the page; return ``Person`` objects."""
    if not isinstance(data, list):
        raise ApiError("La liste des participants est absente.")
    try:
        people = [Santa.Person.from_dict(item, n) for n, item in enumerate(data, start=1)]
        Santa.check_people(people)
        sender.check_contacts(people)
    except Santa.SantaError as e:
        raise ApiError(e.message) from None
    return people


def plan_for(project, people, rules, year):
    """Compile the rules shown on the page against this set's history."""
    try:
        return project.plan(year, people, rules if rules is not None else {})
    except Santa.SantaError as e:
        raise api_error(e) from None


def read_template(project):
    path = sender.Sender(project.base).template_path
    if path.is_file():
        return {"text": path.read_text(encoding="utf-8-sig").replace("\r\n", "\n"), "exists": True}
    return {"text": sender.DEFAULT_TEMPLATE, "exists": False}


def check_template(text, year):
    """Parse a template and render it with the preview characters: ``(subject, body)``."""
    return sender.Template.parse(text, sender.DEFAULT_TEMPLATE_PATH).preview(year)


def lines_from(notices):
    """Notices as the page's console lines (``file`` is just information there)."""
    return [{"level": "info" if n.level == "file" else n.level, "text": n.text.strip()} for n in notices]


# --------------------------------------------------------------------------
# API endpoints inside a set: ``fn(project, body)`` -> JSON-able dict
# --------------------------------------------------------------------------

def api_state(project, _body=None):
    try:
        upgraded = project.upgrade()
    except Santa.SantaError:
        upgraded = {"ids_added": 0, "history_converted": []}      # unusable files: shown as they are
    upgraded = upgraded if (upgraded["ids_added"] or upgraded["history_converted"]) else None
    try:
        people = Santa.read_json(project.participants_path, [])
        rules = project.load_rules()
    except Santa.SantaError as e:
        raise ApiError(e.message, 500) from None
    return {
        "ok": True,
        "participants": people if isinstance(people, list) else [],
        "rules": rules,
        "template": read_template(project),
        "history": project.list_history(),
        "year": datetime.date.today().year,
        "project_dir": str(project.base),
        "upgraded": upgraded,
        "defaults": {"priority": Santa.DEFAULT_PRIORITY, "variables": list(sender.TEMPLATE_VARIABLES)},
    }


def api_validate(project, body):
    """Compile the rules shown on the page and check that a draw exists.

    Always answers 200: problems are part of the result, not transport errors.
    """
    year = parse_year(body)
    result = {"ok": True, "errors": [], "notes": [], "infos": [], "feasible": None,
              "violations": [], "summary": [], "hard": 0, "soft": 0}
    try:
        people = parse_people(body.get("participants"))
        plan = project.plan(year, people, body.get("rules") or {})
    except ApiError as e:
        result.update(ok=False, errors=[e.message])
        return result
    except Santa.SantaError as e:
        result.update(ok=False, errors=[e.message])
        return result

    groups = {}
    for e in plan.entries:
        g = groups.setdefault((e.source, e.priority), {
            "source": e.source, "kind": e.kind, "priority": e.priority, "soft": e.soft,
            "bundled": e.bundle is not None, "count": 0})
        g["count"] += 1
    result.update(
        notes=plan.conflicts, infos=[n.text for n in plan.notices],
        summary=sorted(groups.values(), key=lambda g: (-g["priority"], g["source"])),
        hard=sum(1 for e in plan.entries if not e.soft), soft=sum(1 for e in plan.entries if e.soft))

    solution = Santa.solve(len(people), plan.entries, bool(plan.settings.get("single_cycle", False)),
                           time_limit=SOLVE_TIME_LIMIT)
    result["feasible"] = solution.feasible
    result["violations"] = [{"source": s, "count": c} for s, c in solution.violations]
    return result


def api_save(project, body):
    """Validate everything that is sent, then write it; all or nothing.

    The cross-check always uses what will be on disk afterwards (new content
    where provided, current content otherwise), so ``participants.json`` and
    ``rules.json`` can never end up disagreeing about who exists.
    """
    year = parse_year(body)
    saved = []
    new_people, new_rules, new_template = body.get("participants"), body.get("rules"), body.get("template")

    try:
        people_data = new_people if new_people is not None else Santa.read_json(project.participants_path, [])
        rules_data = new_rules if new_rules is not None else project.load_rules()
    except Santa.SantaError as e:
        raise api_error(e) from None
    people = parse_people(people_data)
    Santa.assign_ids(people)                       # new rows get their id here
    plan_for(project, people, rules_data, year)
    if new_template is not None:
        try:
            check_template(new_template, year)
        except Santa.SantaError as e:
            raise api_error(e) from None

    if new_people is not None:
        project.save_people(people)
        saved.append(project.participants_file)
    if new_rules is not None:
        Santa.write_atomic(project.rules_path, Santa.dump_json(rules_data), backup=True)
        saved.append(project.rules_file)
    if new_template is not None:
        text = new_template.replace("\r\n", "\n").rstrip("\n") + "\n"
        Santa.write_atomic(sender.Sender(project.base).template_path, text, backup=True)
        saved.append(sender.DEFAULT_TEMPLATE_PATH)
    return {"ok": True, "saved": saved, "state": api_state(project)}


def api_simulate(project, body):
    """Run a batch of throw-away draws and count who drew whom.

    Uses the rules sent by the page (saved or not). Draws are never written
    anywhere. The page calls this repeatedly with small batches, which keeps
    each request short and lets the user stop.
    """
    year = parse_year(body)
    try:
        runs = max(1, min(int(body.get("runs", 20)), SIM_MAX_RUNS_PER_REQUEST))
    except (TypeError, ValueError):
        raise ApiError("Le nombre de tirages doit être un entier.")
    people = parse_people(body.get("participants"))
    plan = plan_for(project, people, body.get("rules") or {}, year)

    n = len(people)
    single = bool(plan.settings.get("single_cycle", False))
    counts = [[0] * n for _ in range(n)]
    violation_totals, runs_with_violation, done, infeasible = Counter(), 0, 0, False
    deadline = time.monotonic() + SIM_TIME_BUDGET

    while done < runs and time.monotonic() < deadline:
        solution = Santa.solve(n, plan.entries, single, time_limit=SOLVE_TIME_LIMIT)
        if not solution.feasible:
            infeasible = True
            break
        for i, j in solution.assignment.items():
            counts[i][j] += 1
        for source, count in solution.violations:
            violation_totals[source] += count
        runs_with_violation += bool(solution.violations)
        done += 1

    def pairs(kind, soft=None):
        return sorted({tuple(e.pair) for e in plan.entries
                       if e.kind == kind and (soft is None or e.soft == soft)})

    return {
        "ok": True, "names": [p.name for p in people], "runs_done": done,
        "infeasible": infeasible, "counts": counts, "notes": plan.conflicts,
        "violations": dict(violation_totals), "runs_with_violation": runs_with_violation,
        "hard_forbidden": [list(p) for p in pairs("forbid", soft=False)],
        "soft_forbidden": [list(p) for p in pairs("forbid", soft=True)],
        "forced": [list(p) for p in pairs("force")],
    }


def api_template_preview(project, body):
    year = parse_year(body)
    try:
        subject, text = check_template(str(body.get("text", "")), year)
    except Santa.SantaError as e:
        return {"ok": False, "error": e.message}
    return {"ok": True, "subject": subject, "body": text,
            "santa": sender.PREVIEW_SANTA, "recipient": sender.PREVIEW_RECIPIENT}


def api_draw(project, body):
    """Run ``Project.draw``: the same call the command line makes (``-n`` for a dry run).

    It works on the files on disk, so the page only offers it once everything
    is saved. Its result never contains a pair.
    """
    year = parse_year(body)
    dry = bool(body.get("dry_run"))
    if not dry:
        if body.get("confirm") is not True:
            raise ApiError("Une confirmation est nécessaire pour lancer le tirage officiel.")
        if (project.history_path / f"{year}.json").exists() and body.get("overwrite") is not True:
            raise ApiError(f"Un tirage {year} existe déjà.", 409, code="exists")

    try:
        # The draw knows nothing about messages; check the template first anyway, so that a typo
        # is reported before the solver runs rather than at sending time.
        template, template_notices = sender.Sender(project.base).load_template()
        result = project.draw(year, dry_run=dry)
    except Santa.SantaError as e:
        lines = [{"level": "error", "text": e.message}]
        if e.hint:
            lines.append({"level": "hint", "text": e.hint})
        return {"ok": False, "status": 1, "dry_run": dry, "lines": lines, "history": project.list_history()}
    except OSError as e:
        return {"ok": False, "status": 1, "dry_run": dry, "history": project.list_history(),
                "lines": [{"level": "error", "text": f"Erreur de fichier : {e}"}]}

    lines = lines_from(template_notices + result.notices)
    if result.feasible and dry:
        subject, text = template.preview(year)
        lines.append({"level": "info", "text": f"Aperçu du message ({template.source}, personnages fictifs) :"})
        lines += [{"level": "preview", "text": t} for t in
                  [f"Subject: {subject}", "─" * 40] + text.rstrip("\n").split("\n")]
        lost = contactless(project)
        if lost:
            lines.append({"level": "warn", "text": f"Sans moyen de contact : {', '.join(lost)}. Le tirage reste possible ; "
                                                   "leur message sera à remettre en main propre."})
    elif result.feasible:
        lines.append({"level": "info", "text": "Le tirage est enregistré. Onglet Envoi : les messages se fabriquent et partent de là "
                                               "(ou en ligne de commande : sender.py)."})
        lost = contactless(project)
        if lost:
            lines.append({"level": "warn", "text": f"Sans moyen de contact : {', '.join(lost)}. Ajoutez-en un quand vous voulez : "
                                                   "l'envoi prend les contacts du moment."})
    return {"ok": result.feasible, "status": 0 if result.feasible else 1, "dry_run": dry,
            "lines": lines, "history": project.list_history()}


def contactless(project):
    """Names of the participants nobody can reach (a warning at draw time, never an error)."""
    try:
        return [p.name for p in project.load_people(check=False) if sender.Contacts(p).channel == "none"]
    except Santa.SantaError:
        return []


SMS_FILE = "sms.json"          # in the root folder: the phone is yours, not a set's


def read_sms_config():
    """The saved SMSGate settings (``{}`` if none). Lives next to the sets, shared by all of them."""
    try:
        data = Santa.read_json(ROOT / SMS_FILE, {})
    except Santa.SantaError:
        return {}
    return data if isinstance(data, dict) else {}


def sms_gateway():
    """A ``sender.SmsGateway`` from the saved settings, or ``None`` if not configured."""
    return sender.load_sms_config(ROOT / SMS_FILE)


def deliveries_payload(project, year=None):
    """Who has a message in this draw, by which channel, and where it stands. Never what it says."""
    try:
        status = sender.Sender(project.base).status(year)
    except Santa.SantaError as e:
        return {"ok": True, "year": None, "deliveries": [], "reason": e.message, "years": [],
                "mail_ready": sender.mail_available(), "sms_ready": sms_gateway() is not None}
    return {"ok": True, "year": status.year, "generated_at": status.generated_at, "years": sender.Sender(project.base).years(),
            "mail_ready": sender.mail_available(), "sms_ready": sms_gateway() is not None,
            "notices": [n.text for n in status.notices],
            "deliveries": [{"name": d.name, "channel": d.channel, "contact": d.contact, "state": d.state, "how": d.how}
                           for d in status.deliveries]}


def optional_year(body):
    return parse_year(body) if body.get("year") is not None else None


def api_deliveries(project, body=None):
    return deliveries_payload(project, optional_year(body or {}))


def person_name(body):
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise ApiError("Il manque le nom de la personne.")
    return name


def api_send(project, body):
    """E-mail one person's message with msmtp; the page calls this once per person, so it can show progress."""
    year = optional_year(body)
    try:
        sender.Sender(project.base).send_mail(person_name(body), year)
    except Santa.SantaError as e:
        raise ApiError(e.message + (f" {e.hint}" if e.hint else ""), 422) from None
    return deliveries_payload(project, year)


def api_send_sms(project, body):
    """SMS one person's message through the configured SMSGate phone."""
    year = optional_year(body)
    gateway = sms_gateway()
    if gateway is None:
        raise ApiError("La passerelle SMS n'est pas configurée (Envoi, « Réglages SMS »).", 422)
    try:
        sender.Sender(project.base).send_sms(person_name(body), gateway, year)
    except Santa.SantaError as e:
        raise ApiError(e.message + (f" {e.hint}" if e.hint else ""), 422) from None
    return deliveries_payload(project, year)


def api_message(project, body):
    """The text of a message to hand over by hand.

    It says who the person drew: the page copies it to the clipboard and does
    not display it (only when the clipboard is refused, and the organizer asks).
    """
    try:
        return {"ok": True, **sender.Sender(project.base).text_for(person_name(body), optional_year(body))}
    except Santa.SantaError as e:
        raise api_error(e, 404) from None


def api_mark_sent(project, body):
    """Record that a message was handed over."""
    year = optional_year(body)
    try:
        sender.Sender(project.base).mark_delivered(person_name(body), year)
    except Santa.SantaError as e:
        raise api_error(e, 404) from None
    return deliveries_payload(project, year)


def api_history_import(project, body):
    """Record a draw made before using this tool (so the history rule knows it)."""
    year = parse_year(body)
    pairs = []
    for n, line in enumerate(str(body.get("text", "")).splitlines(), start=1):
        if not line.strip():
            continue
        m = IMPORT_LINE_RE.match(line)
        if not m:
            raise ApiError(f"Ligne {n} illisible : « {line.strip()} ». Format attendu : Alice > Bob.")
        pairs.append((m.group(1), m.group(2)))
    try:
        unknown = project.import_history(year, pairs, overwrite=body.get("overwrite") is True)
    except Santa.HistoryExists as e:
        raise ApiError(e.message, 409, code="exists") from None
    except Santa.SantaError as e:
        raise ApiError(e.message) from None
    return {"ok": True, "history": project.list_history(), "unknown": unknown}


def api_history_delete(project, body):
    """Set a draw aside: ``<year>.json`` becomes ``<year>.json.bak`` (not deleted)."""
    year = parse_year(body)
    try:
        project.set_aside_history(year)
    except Santa.SantaError as e:
        raise ApiError(e.message, 404) from None
    return {"ok": True, "history": project.list_history()}


# --------------------------------------------------------------------------
# Sets (a set = one folder with participants, rules, message and history)
# --------------------------------------------------------------------------

def sets_dir():
    return ROOT / SETS_SUBDIR


def valid_set_name(raw):
    """Return a clean set name, or raise ``ApiError``. It becomes a folder name."""
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not name:
        raise ApiError("Donnez un nom à l'ensemble.")
    if len(name) > MAX_SET_NAME:
        raise ApiError(f"Le nom est trop long ({MAX_SET_NAME} caractères au maximum).")
    if BAD_SET_NAME_RE.search(name):
        raise ApiError("Le nom ne peut pas contenir / \\ : * ? \" < > |.")
    if name.startswith(".") or name.endswith("."):
        raise ApiError("Le nom ne peut pas commencer ni finir par un point.")
    return name


def legacy_exists():
    return any((ROOT / f).is_file() for f in ("participants.json", "rules.json"))


def set_path(set_id):
    """Folder of an existing set. Never builds a path out of anything but a listed name."""
    if set_id == LEGACY_ID:
        if not legacy_exists():
            raise ApiError("Cet ensemble n'existe plus.", 404, code="no_set")
        return ROOT
    try:
        clean = valid_set_name(set_id)
    except ApiError:
        clean = None
    if clean is None or clean != set_id:
        raise ApiError("Ensemble inconnu.", 404, code="no_set")
    path = sets_dir() / set_id
    if not path.is_dir() or path.resolve().parent != sets_dir().resolve():
        raise ApiError("Cet ensemble n'existe plus.", 404, code="no_set")
    return path


def summarize_set(path, set_id, name):
    """Cheap overview shown in the switcher. Never includes any pair."""
    def load(file):
        try:
            return json.loads((path / file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    people, rules = load("participants.json"), load("rules.json")
    rules = rules if isinstance(rules, dict) else {}
    count = lambda k: len(rules[k]) if isinstance(rules.get(k), list) else 0
    years = []
    hist = path / "history"
    for p in sorted(hist.glob("*.json")) if hist.is_dir() else []:
        if p.stem.isdigit():
            years.append(int(p.stem))
    mtimes = [(path / f).stat().st_mtime for f in ("participants.json", "rules.json", sender.DEFAULT_TEMPLATE_PATH)
              if (path / f).is_file()]
    settings = rules.get("settings")
    return {
        "id": set_id, "name": name, "legacy": set_id == LEGACY_ID,
        "path": str(path),
        "participants": len(people) if isinstance(people, list) else 0,
        "rules": {"couples": count("couples"), "groups": count("groups"),
                  "forbidden": count("forbidden"), "forced": count("forced"),
                  "history": rules.get("history") not in (None, False),
                  "single_cycle": bool(settings.get("single_cycle")) if isinstance(settings, dict) else False},
        "history_years": years,
        "modified": datetime.datetime.fromtimestamp(max(mtimes)).isoformat(timespec="seconds") if mtimes else None,
    }


def list_sets():
    out = []
    d = sets_dir()
    if d.is_dir():
        for p in d.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                out.append(summarize_set(p, p.name, p.name))
    out.sort(key=lambda s: s["name"].casefold())
    if legacy_exists():
        out.insert(0, summarize_set(ROOT, LEGACY_ID, "Dossier courant"))
    return out


def sets_payload(**extra):
    sets = list_sets()
    return {"ok": True, "sets": sets, "default": sets[0]["id"] if sets else None,
            "root": str(ROOT), **extra}


def api_sets(_body=None):
    return sets_payload()


def name_taken(name):
    folded = name.casefold()
    return any(s["name"].casefold() == folded for s in list_sets() if not s["legacy"]) or \
        (folded == "dossier courant" and legacy_exists())


def api_sets_create(body):
    """New empty set, or a copy of another one (optionally with message/history).

    A copy keeps the participants' ids, which is what lets a copied history
    keep meaning the same people.
    """
    name = valid_set_name(body.get("name"))
    if name_taken(name):
        raise ApiError(f"Un ensemble « {name} » existe déjà.", 409, code="exists")
    source = None
    if body.get("from") is not None:
        source = set_path(body["from"])
    target = sets_dir() / name
    sets_dir().mkdir(exist_ok=True)
    target.mkdir()
    try:
        if source is not None:
            files = ["participants.json", "rules.json"]
            if body.get("copy_message", True):
                files.append(sender.DEFAULT_TEMPLATE_PATH)
            for f in files:
                if (source / f).is_file():
                    shutil.copy2(source / f, target / f)
            if body.get("copy_history") and (source / "history").is_dir():
                (target / "history").mkdir()
                for p in (source / "history").glob("*.json"):
                    shutil.copy2(p, target / "history" / p.name)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return sets_payload(id=name)


def api_sets_rename(body):
    path = set_path(body.get("id"))
    if body.get("id") == LEGACY_ID:
        raise ApiError("Le dossier courant ne se renomme pas d'ici : c'est le dossier du projet.")
    name = valid_set_name(body.get("name"))
    if name.casefold() != body["id"].casefold() and name_taken(name):
        raise ApiError(f"Un ensemble « {name} » existe déjà.", 409, code="exists")
    if name != body["id"]:
        os.rename(path, sets_dir() / (name + ".renaming"))     # two steps: also handles case-only renames
        os.rename(sets_dir() / (name + ".renaming"), sets_dir() / name)
    return sets_payload(id=name)


def api_sets_delete(body):
    """Move a set to ``sets/.trash/``; nothing is erased."""
    path = set_path(body.get("id"))
    if body.get("id") == LEGACY_ID:
        raise ApiError("Le dossier courant ne se supprime pas d'ici : c'est le dossier du projet.")
    trash = sets_dir() / TRASH_SUBDIR
    trash.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = trash / f"{path.name}-{stamp}"
    os.rename(path, dest)
    return sets_payload(trashed=str(dest))


GET_ROUTES = {"/api/state": api_state, "/api/deliveries": api_deliveries}
POST_ROUTES = {
    "/api/validate": api_validate, "/api/save": api_save, "/api/simulate": api_simulate,
    "/api/template/preview": api_template_preview, "/api/draw": api_draw,
    "/api/send": api_send, "/api/send-sms": api_send_sms, "/api/deliveries/message": api_message, "/api/deliveries/mark": api_mark_sent,
    "/api/history/import": api_history_import, "/api/history/delete": api_history_delete,
}
# Routes about the list of sets itself: they take no ``project`` and need no set header.
def api_sms_get(_body=None):
    """The SMS settings for the form. The password itself is never sent back."""
    c = read_sms_config()
    return {"ok": True, "url": c.get("url", ""), "username": c.get("username", ""),
            "country_code": c.get("country_code", ""), "has_password": bool(c.get("password"))}


def sms_from_body(body):
    """Settings from the form; an empty password keeps the saved one."""
    saved = read_sms_config()
    c = {k: str(body.get(k, "")).strip() for k in ("url", "username", "country_code")}
    c["password"] = str(body.get("password") or "") or saved.get("password", "")
    return c


def api_sms_save(body):
    c = sms_from_body(body)
    if not any(str(body.get(k) or "").strip() for k in ("url", "username", "password", "country_code")):
        # A completely empty form means "forget the gateway" (an empty password alone keeps the saved one).
        path = ROOT / SMS_FILE
        if path.exists():
            path.unlink()
        return api_sms_get()
    try:
        sender.SmsGateway(c["url"], c["username"], c["password"], country_code=c["country_code"])   # validates the form
    except Santa.SantaError as e:
        raise api_error(e) from None
    Santa.write_atomic(ROOT / SMS_FILE, Santa.dump_json(c))                       # temp files are created private (0600)
    return api_sms_get()


def api_sms_test(body):
    """Ask the phone's ``/health`` with the settings on the form (saved or not)."""
    c = sms_from_body(body)
    try:
        sender.SmsGateway(c["url"], c["username"], c["password"], country_code=c["country_code"]).health()
    except Santa.SantaError as e:
        raise ApiError(e.message + (f" {e.hint}" if e.hint else ""), 422) from None
    return {"ok": True}


ROOT_GET = {"/api/sets": api_sets, "/api/sms": api_sms_get}
ROOT_POST = {"/api/sms/save": api_sms_save, "/api/sms/test": api_sms_test, "/api/sets/create": api_sets_create, "/api/sets/rename": api_sets_rename,
             "/api/sets/delete": api_sets_delete}


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'none'; script-src 'unsafe-inline'; "
                                "style-src 'unsafe-inline'; connect-src 'self'; img-src data:; "
                                "form-action 'none'; base-uri 'none'"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "SantaWeb"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- plumbing ----------------------------------------------------------

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _fail(self, status, message, **extra):
        self._send(status, {"ok": False, "error": message, **extra})

    def _guard(self, api):
        """Common checks. Returns True if the request may proceed."""
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            self._fail(403, "Hôte non autorisé.")
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.allowed_origins:
            self._fail(403, "Origine non autorisée.")
            return False
        if api and not secrets.compare_digest(self.headers.get("X-Santa-Token", ""), self.server.token):
            self._fail(403, "Jeton invalide : rechargez la page.")
            return False
        return True

    # -- verbs -------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            if not self._guard(api=False):
                return
            page = HERE / "web" / "index.html"
            if not page.is_file():
                return self._fail(500, "web/index.html est introuvable.")
            html = page.read_text(encoding="utf-8").replace("__TOKEN__", self.server.token)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if path in GET_ROUTES or path in ROOT_GET:
            if not self._guard(api=True):
                return
            return self._dispatch(path, {})
        self._fail(404, "Introuvable.")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in POST_ROUTES and path not in ROOT_POST:
            return self._fail(404, "Introuvable.")
        if not self._guard(api=True):
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._fail(415, "Content-Type application/json attendu.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._fail(400, "Content-Length invalide.")
        if length > MAX_BODY_BYTES:
            return self._fail(413, "Requête trop volumineuse.")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._fail(400, "Corps JSON invalide.")
        self._dispatch(path, body)

    def _dispatch(self, path, body):
        try:
            with LOCK:
                if path in ROOT_GET or path in ROOT_POST:
                    result = {**ROOT_GET, **ROOT_POST}[path](body)
                else:
                    raw = unquote(self.headers.get("X-Santa-Set", ""))
                    if not raw:
                        raise ApiError("Aucun ensemble sélectionné.", 400, code="no_set")
                    project = Santa.Project(set_path(raw))
                    result = {**GET_ROUTES, **POST_ROUTES}[path](project, body)
        except ApiError as e:
            return self._fail(e.status, e.message, **e.extra)
        except Exception as e:  # last resort: report, don't kill the server
            sys.stderr.write(f"Erreur interne sur {path} : {e!r}\n")
            return self._fail(500, f"Erreur interne : {e}")
        self._send(200, result)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, verbose):
        super().__init__(("127.0.0.1", port), Handler)
        port = self.server_address[1]
        self.token = secrets.token_urlsafe(24)
        self.verbose = verbose
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}


def main():
    ap = argparse.ArgumentParser(description="Interface web locale pour le Père Noël secret.")
    ap.add_argument("--dir", default=".", help="root folder: its sets/ sub-folders each hold one participant "
                                               "list with its rules (default: current folder)")
    ap.add_argument("--port", type=int, default=8765, help="port to listen on (0 = any free port)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    args = ap.parse_args()

    global ROOT
    ROOT = Path(args.dir).resolve()
    if not ROOT.is_dir():
        sys.exit(f"❌ Dossier introuvable : {args.dir}")
    try:
        server = Server(args.port, args.verbose)
    except OSError as e:
        sys.exit(f"❌ Impossible d'écouter sur le port {args.port} ({e.strerror}). Essayez --port 0.")

    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"🎅 Interface prête : {url}")
    print(f"   Dossier racine : {ROOT}  (ensembles dans {ROOT / SETS_SUBDIR})")
    print("   Ctrl+C pour arrêter.")
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
