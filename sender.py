#!/usr/bin/env python3
"""Turn a Secret Santa draw into messages, and deliver them.

Where this fits
---------------
``Santa.py`` makes the draw and records it in ``history/<year>.json``; that file
is its only output. This module is the other half: it reads that draw, the
participants (with their contacts) and the message template, renders each
person's message *in memory*, and sends it, by e-mail (``msmtp``) or SMS (an
Android phone running SMSGate), or hands it to a human for any other channel.
``Santa.py`` knows nothing about this file; this one only reads what
``Santa.py`` wrote and imports its small helpers.

No message is ever written to disk. The only trace of delivery is
``delivery/<year>.json``: which participant (by id) was delivered, when, and
how. It holds no content and no pair, and it is ignored once the draw it
belongs to is replaced (a redrawn year starts again from "nobody received").

Contacts
--------
In ``participants.json``, next to ``id`` and ``name``, all optional:

* ``email``      an address (sent through ``msmtp``)
* ``phone``      a number (SMS through SMSGate, or WhatsApp, Signal... by hand)
* ``messenger``  a Messenger name or link (by hand)
* ``other``      free text such as "hand it over at lunch" (by hand)
* ``prefer``     which of these to use when several are set; default is the
                 order above

Nothing is needed to *draw*; one contact is needed to *deliver*. The channel is
decided now, from the current file, never frozen at draw time: add a number
after the draw and the next delivery uses it.

Message template
----------------
``message.txt``: first line ``Subject: ...``, one blank line, then the body.
``{santa}``, ``{recipient}`` and ``{year}`` are replaced for each person
(``{{`` / ``}}`` for a literal brace). It is parsed and rendered once with
fictional characters when loaded, so a typo fails immediately. The subject is
for e-mail only: an SMS or a chat message gets the body.

Library use
-----------
::

    import sender
    s = sender.Sender("sets/family")
    status = s.status()                    # latest draw: who is waiting, by which channel
    s.send_mail("Alice")                   # raises SantaError with the reason
    text = s.text_for("Bob")["text"]       # body to paste into a chat

Privacy: ``status()`` never says who drew whom. ``text_for`` and ``--show``
return a message, which does. They exist for hand delivery; callers decide
what to do with them.
"""

import argparse
import base64
import datetime
import email.policy
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import Santa
from Santa import Notice, SantaError

# --------------------------------------------------------------------------
# Contacts
# --------------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[^@\s-][^@\s]*@[^@\s]+$")        # lenient on purpose; never starts with "-" (msmtp option)
PHONE_RE = re.compile(r"^\+?[0-9][0-9 .()/-]*$")           # digits and the usual separators; 6 digits at least
CHANNELS = ("email", "phone", "messenger", "other")        # in default order of preference
CHANNEL_LABELS = {"email": "e-mail", "phone": "SMS", "messenger": "Messenger", "other": "autre moyen", "none": "aucun contact"}
MAX_CONTACT = 300


class Contacts:
    """How one person can be reached, read from the extra fields of their ``Santa.Person``."""

    def __init__(self, person):
        raw = person.extra
        for key in (*CHANNELS, "prefer"):
            value = raw.get(key, "")
            setattr(self, key, value.strip() if isinstance(value, str) else "")

    def all(self):
        """``[(channel, value), ...]``, the preferred one first."""
        found = [(c, getattr(self, c)) for c in CHANNELS if getattr(self, c)]
        found.sort(key=lambda cv: cv[0] != self.prefer)          # stable: keeps CHANNELS order otherwise
        return found

    @property
    def channel(self):
        """The channel to use, or ``"none"`` when nobody gave any contact."""
        found = self.all()
        return found[0][0] if found else "none"

    @property
    def contact(self):
        found = self.all()
        return found[0][1] if found else ""


def check_contacts(people):
    """Raise ``SantaError`` if a contact is malformed. Missing contacts are fine."""
    for p in people:
        for key in (*CHANNELS, "prefer"):
            if not isinstance(p.extra.get(key, ""), str):
                raise SantaError(f"{p.name} : « {key} » doit être du texte.")
        c = Contacts(p)
        if c.email and not EMAIL_RE.match(c.email):
            raise SantaError(f"{p.name} : adresse e-mail invalide.")
        if c.phone and not (PHONE_RE.match(c.phone) and sum(ch.isdigit() for ch in c.phone) >= 6):
            raise SantaError(f"{p.name} : numéro de téléphone invalide (chiffres, espaces, « + », « ( ) . - / »).")
        for key in ("messenger", "other"):
            value = getattr(c, key)
            if len(value) > MAX_CONTACT or Santa.BAD_NAME_RE.search(value):
                raise SantaError(f"{p.name} : « {key} » est trop long ou contient un retour à la ligne.")
        if c.prefer and c.prefer not in CHANNELS:
            raise SantaError(f"{p.name} : moyen préféré inconnu « {c.prefer} » ({', '.join(CHANNELS)}).")
        if c.prefer and not getattr(c, c.prefer):
            raise SantaError(f"{p.name} : moyen préféré « {c.prefer} », mais il n'est pas renseigné.")


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


def build_mime(template, giver, receiver, year, address):
    """The complete RFC 5322 message for ``giver``, as bytes (headers, blank line, UTF-8 body).

    ``EmailMessage`` takes care of the blank line between headers and body and
    of the encoding.
    """
    subject, body = template.render(santa=giver, recipient=receiver, year=year)
    msg = EmailMessage()
    msg["To"] = address
    msg["Subject"] = subject
    msg.set_content(body, charset="utf-8")
    return msg.as_bytes()


def mail_command(command=None):
    """The program that sends e-mail, as an argument list: ``command``, else ``$SANTA_MSMTP``, else ``msmtp``."""
    return shlex.split(command or os.environ.get("SANTA_MSMTP") or "msmtp")


def mail_available(command=None):
    """Is the sending program installed? (Found on ``PATH`` or given as a path.)"""
    argv = mail_command(command)
    return bool(argv) and shutil.which(argv[0]) is not None


# --------------------------------------------------------------------------
# SMS through an Android phone (SMSGate)
# --------------------------------------------------------------------------

def normalize_phone(number, country_code=""):
    """Clean a phone number for an SMS gateway: no spaces or separators, ``00`` becomes ``+``.

    With a ``country_code`` such as ``"+33"``, a national number starting with
    ``0`` is converted (``06 12 34 56 78`` -> ``+33612345678``). Without one
    the number is only cleaned, never guessed.
    """
    n = re.sub(r"[ .()/-]", "", number.strip())
    if n.startswith("00"):
        n = "+" + n[2:]
    if country_code and n.startswith("0"):
        n = country_code + n[1:]
    return n


class SmsGateway:
    """Send SMS through an Android phone running SMSGate (sms-gate.app, Apache-2.0).

    The phone app serves a small HTTP API on the local network (``http://<phone ip>:8080``
    in "Local Server" mode); ``url`` is that base address, with the path prefix
    for the cloud variant (``https://api.sms-gate.app/3rdparty/v1``) if you use
    it. Calls are authenticated with HTTP Basic (the credentials the app shows).

    ``send`` returns when the phone has *accepted* the message and, for up to
    ``wait`` seconds, reports no failure: the message then is ``Sent`` /
    ``Delivered``, or still ``Pending`` / ``Processed`` (the phone queues and
    rate-limits on purpose). A refusal or a reported failure raises ``SantaError``.

    Mind the privacy cost: over plain ``http`` the text (which says who the
    person drew) crosses your network unencrypted. Use it on a network you trust.
    """

    FAILED, DONE = {"Failed"}, {"Sent", "Delivered"}

    def __init__(self, url, username, password, *, country_code="", timeout=15, wait=10):
        parts = urllib.parse.urlsplit(url or "")
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise SantaError("Adresse de la passerelle SMS invalide : attendu http://adresse:port (sans identifiants dedans).")
        if not username or not password:
            raise SantaError("Identifiant et mot de passe de la passerelle SMS requis (ils sont affichés dans l'appli).")
        if country_code and not re.fullmatch(r"\+[0-9]{1,4}", country_code):
            raise SantaError("Indicatif pays invalide : attendu par exemple +33.")
        self.base = url.rstrip("/")
        self.country_code, self.timeout, self.wait = country_code, timeout, wait
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._auth = f"Basic {token}"

    def _call(self, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": self._auth, "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SantaError("La passerelle SMS refuse l'identifiant ou le mot de passe.") from None
            detail = e.read().decode("utf-8", "replace").strip()[:200]
            raise SantaError(f"La passerelle SMS a répondu {e.code}" + (f" : {detail}" if detail else ".")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise SantaError(f"Téléphone injoignable ({reason}).",
                             "Vérifiez que l'appli SMSGate tourne, que « Local Server » est activé, "
                             "et que le PC et le téléphone sont sur le même réseau.") from None
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise SantaError("Réponse illisible de la passerelle SMS (est-ce bien SMSGate ?).") from None

    def health(self):
        """Raise ``SantaError`` unless the gateway answers and accepts the credentials."""
        self._call("GET", "/health")
        return True

    def send(self, number, text):
        """Send ``text`` to ``number``; return the last known state (``Sent``, ``Pending``...)."""
        to = normalize_phone(number, self.country_code)
        if not to:
            raise SantaError("Numéro de téléphone vide.")
        reply = self._call("POST", "/message", {"textMessage": {"text": text}, "phoneNumbers": [to]})
        message_id, state = reply.get("id"), reply.get("state") or "Pending"
        deadline = time.monotonic() + self.wait
        while message_id and state not in self.FAILED | self.DONE and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            reply = self._call("GET", f"/message/{urllib.parse.quote(str(message_id), safe='')}")
            state = reply.get("state") or state
        if state in self.FAILED:
            errors = [r.get("error") for r in reply.get("recipients", []) if isinstance(r, dict) and r.get("error")]
            raise SantaError("Le téléphone n'a pas pu envoyer le SMS" + (f" : {errors[0]}" if errors else "."))
        return state


def load_sms_config(path):
    """A ``SmsGateway`` from a JSON file ``{"url", "username", "password", "country_code"?}``, or None if absent/incomplete."""
    try:
        data = Santa.read_json(path, {})
    except SantaError:
        return None
    if not isinstance(data, dict) or not all(data.get(k) for k in ("url", "username", "password")):
        return None
    try:
        return SmsGateway(data["url"], data["username"], data["password"], country_code=data.get("country_code", ""))
    except SantaError:
        return None


# --------------------------------------------------------------------------
# The sender: from a recorded draw to delivered messages
# --------------------------------------------------------------------------

@dataclass
class Delivery:
    """One person's message and where it stands. Never says what it contains.

    ``channel``/``contact`` come from the *current* participants file.
    ``state`` is ``pending`` or ``sent``; ``how`` says how a sent one went
    (``email``, ``sms`` or ``manual``).
    """
    name: str
    channel: str
    contact: str
    state: str
    how: str = ""


@dataclass
class Status:
    """The delivery picture of one draw: ``deliveries`` in participant order."""
    year: int
    generated_at: Optional[str]
    deliveries: list
    notices: list = field(default_factory=list)


class Sender:
    """Messages for one project folder (the same folder ``Santa.Project`` works on)."""

    def __init__(self, base=".", *, participants="participants.json", history_dir="history",
                 template=None, state_dir="delivery"):
        self.base = Path(base)
        self.project = Santa.Project(base, participants=participants, history_dir=history_dir)
        self.template_file, self.state_dir = template, state_dir

    @property
    def template_path(self):
        return self.base / (self.template_file or DEFAULT_TEMPLATE_PATH)

    def load_template(self):
        """``(Template, notices)``: ``message.txt`` if present, else the built-in text."""
        return Template.load(self.template_file, self.base)

    # -- which draw ------------------------------------------------------

    def years(self):
        """Years of the recorded draws that were really drawn here (not imported), oldest first."""
        return [h["year"] for h in self.project.list_history() if h.get("year") is not None and not h.get("imported")]

    def _draw(self, year=None):
        """``(year, document, people, {giver index: receiver index})`` for ``year`` (default: the latest)."""
        years = self.years()
        if year is None:
            if not years:
                raise SantaError("Aucun tirage enregistré : lancez d'abord le tirage (Santa.py).")
            year = years[-1]
        elif year not in years:
            raise SantaError(f"Aucun tirage enregistré pour {year}.")
        data = Santa.read_json(self.project.history_path / f"{year}.json")
        people = self.project.load_people(check=False)
        pairs, _ = Santa.resolve_draw(data, people)
        return year, data, people, dict(pairs)

    @staticmethod
    def _stamp(data):
        digest = hashlib.sha1(json.dumps(data.get("assignments", []), sort_keys=True).encode()).hexdigest()[:12]
        return f"{data.get('generated_at', '')}#{digest}"

    # -- delivery state --------------------------------------------------

    def _state_path(self, year):
        return self.base / self.state_dir / f"{year}.json"

    def _delivered(self, year, data):
        """``{key: {"at", "how"}}`` for this very draw; empty if the draw was replaced since."""
        doc = Santa.read_json(self._state_path(year), {})
        if not isinstance(doc, dict) or doc.get("draw") != self._stamp(data):
            return {}
        return doc.get("delivered", {})

    @staticmethod
    def _key(person):
        return person.id or person.name

    def _record(self, year, data, person, how):
        delivered = dict(self._delivered(year, data))
        delivered[self._key(person)] = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "how": how}
        Santa.write_atomic(self._state_path(year), Santa.dump_json(
            {"year": year, "draw": self._stamp(data), "delivered": delivered}))

    # -- looking ---------------------------------------------------------

    def status(self, year=None):
        """A ``Status``: everyone who has a message in this draw, with their channel and state."""
        year, data, people, mapping = self._draw(year)
        done = self._delivered(year, data)
        out = []
        for i, p in enumerate(people):
            if i not in mapping:
                continue                                    # joined after the draw: nothing to send
            c = Contacts(p)
            mark = done.get(self._key(p))
            out.append(Delivery(p.name, c.channel, c.contact, "sent" if mark else "pending", (mark or {}).get("how", "")))
        notices = []
        missing = len(data.get("assignments", [])) - len(mapping)
        if missing > 0:
            notices.append(Notice("warn", f"{missing} participant(s) du tirage ne sont plus dans la liste : leur message est ignoré."))
        return Status(year, data.get("generated_at"), out, notices)

    def preview(self, year=None):
        """``(subject, body, source)`` with fictional characters (never real names)."""
        template, _ = self.load_template()
        if year is None:
            years = self.years()
            year = years[-1] if years else datetime.date.today().year
        subject, body = template.preview(year)
        return subject, body, template.source

    def _find(self, name, year):
        """``(year, document, person, receiver)`` for a participant of the draw, else ``SantaError``."""
        year, data, people, mapping = self._draw(year)
        for i, p in enumerate(people):
            if p.name == name:
                if i not in mapping:
                    raise SantaError(f"{name} n'a pas de message dans le tirage {year}.")
                return year, data, p, people[mapping[i]]
        raise SantaError(f"{name} ne fait pas partie des participants.")

    def _pending(self, name, year):
        year, data, person, receiver = self._find(name, year)
        if self._key(person) in self._delivered(year, data):
            raise SantaError(f"{name} a déjà reçu son message (tirage {year}).")
        return year, data, person, receiver

    def text_for(self, name, year=None):
        """``{"year", "channel", "contact", "text"}``: the body to hand over, subject left out. Reveals a pair."""
        year, data, person, receiver = self._pending(name, year)
        template, _ = self.load_template()
        _, body = template.render(santa=person.name, recipient=receiver.name, year=year)
        c = Contacts(person)
        return {"year": year, "channel": c.channel, "contact": c.contact, "text": body}

    # -- delivering ------------------------------------------------------

    def send_mail(self, name, year=None, *, command=None, timeout=60):
        """E-mail the message with ``msmtp`` to the person's *current* address, then record it.

        On failure nothing is recorded, so sending again is always safe.
        ``command`` (or ``$SANTA_MSMTP``) replaces ``msmtp``.
        """
        year, data, person, receiver = self._pending(name, year)
        address = Contacts(person).email
        if not address:
            raise SantaError(f"{name} n'a pas d'adresse e-mail.")
        template, _ = self.load_template()
        message = build_mime(template, person.name, receiver.name, year, address)
        argv = mail_command(command) + [address]
        try:
            proc = subprocess.run(argv, input=message, capture_output=True, timeout=timeout)
        except FileNotFoundError:
            raise SantaError(f"« {argv[0]} » est introuvable : l'envoi par e-mail utilise msmtp.",
                             "Installez et configurez msmtp (~/.msmtprc), ou remettez les messages autrement.") from None
        except subprocess.TimeoutExpired:
            raise SantaError(f"L'envoi à {name} n'a pas abouti en {timeout} s.") from None
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise SantaError(f"Échec de l'envoi à {name}" + (f" : {detail[-1]}" if detail else "."))
        self._record(year, data, person, "email")

    def send_sms(self, name, gateway, year=None):
        """SMS the body to the person's *current* number through ``gateway``, then record it."""
        year, data, person, receiver = self._pending(name, year)
        number = Contacts(person).phone
        if not number:
            raise SantaError(f"{name} n'a pas de numéro de téléphone.")
        template, _ = self.load_template()
        _, body = template.render(santa=person.name, recipient=receiver.name, year=year)
        state = gateway.send(number, body)
        self._record(year, data, person, "sms")
        return state

    def mark_delivered(self, name, year=None, how="manual"):
        """Record that a message was handed over by a human."""
        year, data, person, _ = self._pending(name, year)
        self._record(year, data, person, how)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(description="Messages du Père Noël secret : aperçu, envoi, suivi.")
    ap.add_argument("--base-dir", default=".", metavar="DIR", help="folder of the project (default: current folder)")
    ap.add_argument("--participants", default="participants.json")
    ap.add_argument("--history-dir", default="history")
    ap.add_argument("--template", metavar="FILE", help=f"message template (default: {DEFAULT_TEMPLATE_PATH} if present)")
    ap.add_argument("--year", type=int, help="which draw (default: the latest)")
    ap.add_argument("--sms-config", default="sms.json", metavar="FILE",
                    help='SMSGate settings, JSON {"url", "username", "password", "country_code"} (default: sms.json in --base-dir)')
    what = ap.add_argument_group("actions")
    what.add_argument("--status", action="store_true", help="who has received their message, by which channel (never the pairs)")
    what.add_argument("--preview", action="store_true", help="show the message with fictional characters")
    what.add_argument("--send", action="store_true", help="e-mail every pending message to people reached by e-mail")
    what.add_argument("--sms", action="store_true", help="SMS every pending message to people reached by phone (needs SMSGate)")
    what.add_argument("--show", metavar="NAME", help="print NAME's message, to hand it over (it names the person drawn)")
    what.add_argument("--mark", metavar="NAME", help="record that NAME's message was handed over")
    return ap


def _fail(e):
    print(f"❌ {e.message}")
    if e.hint:
        print(f"💡 {e.hint}")


def main(argv=None):
    """Command-line entry point; returns the process exit code."""
    ap = build_parser()
    args = ap.parse_args(argv)
    if not (args.status or args.preview or args.send or args.sms or args.show or args.mark):
        ap.error("choisissez une action : --status, --preview, --send, --sms, --show ou --mark")
    s = Sender(args.base_dir, participants=args.participants, history_dir=args.history_dir, template=args.template)
    failures = 0
    try:
        if args.preview:
            _, notices = s.load_template()
            Santa.print_notices(notices)
            subject, body, source = s.preview(args.year)
            print(f"📧 Aperçu du message ({source}, personnages fictifs) :")
            print(f"   Subject: {subject}\n   " + "─" * 40)
            for line in body.rstrip("\n").split("\n"):
                print(f"   {line}")
        if args.show:
            print(s.text_for(args.show, args.year)["text"], end="")
        if args.mark:
            s.mark_delivered(args.mark, args.year)
            print(f"✅ Message de {args.mark} marqué comme remis.")
        if args.send or args.sms:
            gateway = None
            if args.sms:
                gateway = load_sms_config(Path(args.base_dir) / args.sms_config)
                if gateway is None:
                    raise SantaError(f"Passerelle SMS non configurée ({args.sms_config}).",
                                     'Fichier JSON {"url": "http://<ip du téléphone>:8080", "username": "...", "password": "..."}.')
            status = s.status(args.year)
            sent = 0
            wanted = {c for c, on in (("email", args.send), ("phone", args.sms)) if on}
            for d in status.deliveries:
                if d.state != "pending" or d.channel not in wanted:
                    continue
                try:
                    if d.channel == "email":
                        s.send_mail(d.name, status.year)
                        print(f"✉️  Mail envoyé à {d.name}")
                    else:
                        s.send_sms(d.name, gateway, status.year)
                        print(f"💬 SMS envoyé à {d.name}")
                    sent += 1
                except SantaError as e:
                    failures += 1
                    print(f"❌ {e.message}", file=sys.stderr)
            print(f"📬 {sent} message(s) envoyé(s), {failures} échec(s).")
            if failures:
                print("   Les échecs ne sont pas enregistrés : relancez la commande pour les renvoyer.", file=sys.stderr)
            left = [d for d in s.status(status.year).deliveries if d.state == "pending"]
            if left:
                print(f"✋ {len(left)} message(s) restent à remettre ({', '.join(d.name for d in left)}) : "
                      "interface web, ou --show / --mark.")
        if args.status:
            status = s.status(args.year)
            Santa.print_notices(status.notices)
            print(f"📋 Tirage {status.year}" + (f" ({status.generated_at})" if status.generated_at else ""))
            for d in status.deliveries:
                where = CHANNEL_LABELS[d.channel] + (f" : {d.contact}" if d.contact else "")
                print(f"   {'✅' if d.state == 'sent' else '⏳'} {d.name} — {where}" + (f" ({d.how})" if d.how else ""))
    except SantaError as e:
        _fail(e)
        return 1
    except OSError as e:
        print(f"❌ Erreur de fichier : {e}")
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
