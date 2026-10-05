# Secret Santa

A small command-line Secret Santa generator. It draws names with a
constraint solver ([OR-Tools CP-SAT](https://developers.google.com/optimization)),
so that **every rule you declare is guaranteed to hold** (or the program tells
you that no valid draw exists), and it writes one ready-to-send mail per
participant.

The interesting part is the rules file: couples, households, "not the same
person as last year", forced pairs and more, all declared in plain JSON.

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Files](#files)
- [Rules reference](#rules-reference)
- [Priorities and soft rules](#priorities-and-soft-rules)
- [Recipes](#recipes)
- [How the history works](#how-the-history-works)
- [Message template](#message-template)
- [Command-line options](#command-line-options)
- [Web interface](#web-interface)
- [Using Santa.py as a library](#using-santapy-as-a-library)
- [Development and tests](#development-and-tests)
- [Privacy](#privacy)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)

## Requirements

- Python 3.9+ and `ortools` (`pip install ortools`, ideally in a `venv/`)
- For `ssm.sh`: `bash` 4+, `jq`, and [`msmtp`](https://marlam.de/msmtp/)
  configured with a working account (only needed to send the mails)

## Quick start

```bash
python3 -m venv venv && source venv/bin/activate && pip install ortools

# 1. list the participants
cat > participants.json <<'EOF'
[
  {"name": "Alice", "email": "alice@example.org"},
  {"name": "Bob",   "email": "bob@example.org"},
  {"name": "Carol", "email": "carol@example.org"},
  {"name": "Dave",  "email": "dave@example.org"}
]
EOF

# 2. (optional) write some rules, see below
echo '{}' > rules.json

# 3. check that a valid draw exists without writing anything
#    (this also previews the mail text, see "Message template")
python3 Santa.py --dry-run

# 4. draw for real, then send
./ssm.sh -g
./ssm.sh -s              # sends the mails, moves each one to secretSantaFiles/sent/
```

Participants get a stable `id` the first time you draw for real (see
[Identifiers](#identifiers)); you never have to write one.

## Files

| File | Role |
|------|------|
| `participants.json` | Input. Array of `{"name", "email"}`, plus an `id` that the program adds. **Names must be unique**: they identify people in the rules. See [Identifiers](#identifiers). |
| `rules.json` | Input. The constraints, see [Rules reference](#rules-reference). Missing file = no rule. |
| `secretSantaFiles/<name>.mail` | Output. One mail per giver: first line is the address, the rest is a full MIME message (UTF-8). |
| `history/<year>.json` | Output. The complete draw of that year (by participant `id`), used by the `history` rule next year. |
| `message.txt` | Input, optional. The text of the mails, see [Message template](#message-template). Without it, a built-in message is used. |
| `Santa.py` | The generator: a command-line tool and an importable library, see [Using Santa.py as a library](#using-santapy-as-a-library). |
| `tests/` | The test suite, see [Development and tests](#development-and-tests). |
| `webui.py`, `web/index.html` | Optional. Local web interface with several *sets* (`sets/<name>/`, each holding its own files above), see [Web interface](#web-interface). |
| `ssm.sh` | Wrapper around `Santa.py`: generate, send the mails, add any kind of rule interactively, clean up. See [ssm.sh](#ssmsh). |

## Rules reference

`rules.json` is one JSON object. Every key is optional. Names must match
`participants.json` exactly; a typo is an error, never silently ignored.

```json
{
  "forbidden": [ {"from": "Alice", "to": "Bob"} ],
  "forced":    [ {"from": "Carol", "to": "Dave"} ],
  "couples":   [ ["Alice", "Bob"] ],
  "groups":    [ ["Eve", "Frank", "Grace"] ],
  "history":   { "last": 2 },
  "settings":  { "single_cycle": true }
}
```

| Key | Meaning | Direction |
|-----|---------|-----------|
| `forbidden` | `from` must **not** draw `to`. | one way |
| `forced` | `from` **must** draw `to`. | one way |
| `couples` | Each pair of people can't draw each other. Exactly 2 names per entry. | both ways |
| `groups` | Nobody in the group can draw anybody else in it (family, flat-share, team). Any size >= 2. | all ways |
| `history` | Don't repeat the pairs of the last `last` draws (default 1). Optional `directory` (default `history`). | as recorded |
| `settings.single_cycle` | The whole draw forms one loop (A→B→C→…→A). | n/a |

`couples` and `groups` are the same thing under the hood (a couple is a group
of two); the separate key exists because it reads better in the file and
validates the size.

Every entry also accepts two optional keys, `priority` and `soft`, described
in the next section. For `couples` and `groups`, which are plain lists of
names, use the object form to set them:

```json
{ "couples": [ ["Alice", "Bob"],
               { "members": ["Carol", "Dave"], "priority": 80, "soft": true } ] }
```

## Priorities and soft rules

Rules can contradict each other, or make the draw impossible together. Three
optional keys let you say what should give way. They work on `forbidden`,
`forced`, each `couples`/`groups` entry and on `history`.

| Key | Type | Meaning |
|-----|------|---------|
| `priority` | integer, higher wins | Decides who wins when two rules contradict each other **on the exact same pair** (e.g. `forced` Dave→Carol vs. last year's history forbidding Dave→Carol). |
| `soft` | `true` / `false` | A soft rule may be **violated** when the draw is otherwise impossible, lowest priority first. A hard rule (`soft: false`) never is for that reason. |
| `relax` | `"pair"` / `"rule"` | What gets sacrificed when a soft rule has to give way. `"pair"`: only the individual pairs that must be repeated. `"rule"`: the whole rule at once (all its pairs). |

Defaults:

| Rule | `priority` | `soft` | `relax` |
|------|-----------|--------|---------|
| `forced` | 100 | no | n/a (single pair) |
| `forbidden` | 50 | no | n/a (single pair) |
| `couples`, `groups` | 50 | no | `rule` |
| `history` | 10 (minus 1 per year of age) | **yes** | `pair` |

The defaults for `relax` are chosen for what each rule means. History is a
preference about each pair taken separately, so only the pairs that have to be
repeated are repeated. A couple or a group is one social unit: letting Alice
draw Bob while Bob still can't draw Alice would make little sense, so it is
given up as a whole.

What this gives you out of the box:

- **`forced` overrides `history`.** Force Dave→Carol every year if you like;
  the program says so (`⚖️ ... l'emporte sur ...`) and keeps the pair.
- **History degrades gracefully.** With `"history": {"last": 3}`, years get
  priorities 10, 9, 8 (most recent first). If three years of exclusions leave
  no valid draw, the program repeats as few pairs as possible, taking them from
  the oldest year first, and only then from the next one. It prints how many
  violations each rule ended up with (`⚠️ ... history 2023 (1 violation(s))`),
  and the history file of that run records them under `relaxed_rules`.
  The result is the true minimum at each priority level, not a rough
  approximation: a higher priority is never sacrificed to spare a lower one.
- **Hard rules stay hard.** If the hard rules alone are impossible, there is no
  draw and the program tells you so.

Two details that are easy to miss:

- **Direct conflicts follow priority even between hard rules.** A `forbidden`
  with `"priority": 200` beats a default `forced` on the same pair, and the
  forced pair is dropped (with a printed note). If both sides have the **same**
  priority, it is an error and nothing is drawn: set one higher.
- **You can change the granularity per rule.** `"relax": "rule"` on a history
  makes each year all-or-nothing; `"relax": "pair"` on a `couples`/`groups`
  entry lets its pairs be violated one at a time.
- **The program never says which pair was violated.** It prints counts per
  rule only, because naming the pair would reveal a real assignment of the
  result.

Examples:

```json
{
  "history": { "last": 2 },
  "couples": [ { "members": ["Alice", "Bob"], "soft": true } ],
  "forbidden": [ { "from": "Bob", "to": "Eve", "priority": 90 } ]
}
```

If things get tight, the older year of history gives way first, then the more
recent one, then the Alice+Bob couple (priority 50 > 10 and 9). `Bob→Eve` is
hard and never gives way.

## Recipes

### Partners shouldn't draw each other

```json
{ "couples": [["Alice", "Bob"], ["Carol", "Dave"]] }
```

Alice can't get Bob **and** Bob can't get Alice. Use `forbidden` instead if
you only want one direction (e.g. Alice can't draw Bob, but Bob may draw Alice).

### A whole household stays apart

```json
{ "groups": [["Mom", "Dad", "Grandma"], ["Uncle Tom", "Aunt Sue"]] }
```

Nobody in a group draws anybody from the same group. This needs enough people
outside each group; with groups that are too large no draw exists and the tool
says so.

### Never the same person two years in a row

```json
{ "history": { "last": 1 } }
```

That's all. Each run stores its result in `history/<year>.json`; next year
this rule forbids every pair found there. No hand-typed list of last year's
pairs. Use `"last": 3` to remember three years back. Rule of thumb: you need
roughly more than `last + 1` eligible receivers per person, otherwise the draw
becomes infeasible.

### One person must give to another (surprise for someone special)

```json
{ "forced": [{ "from": "Dave", "to": "Grandma" }] }
```

### Someone shouldn't give to a specific person (but not the reverse)

```json
{ "forbidden": [{ "from": "Bob", "to": "Eve" }] }
```

### No mutual swaps at all, for everyone

```json
{ "settings": { "single_cycle": true } }
```

The draw is a single ring, so A→B and B→A can never both happen. As a bonus
it also makes the draw harder to reverse-engineer if you only know one pair.
Combine freely with other rules.

### The kitchen sink

```json
{
  "couples":  [["Alice", "Bob"]],
  "groups":   [["Eve", "Frank", "Grace"]],
  "forbidden": [{ "from": "Dave", "to": "Carol" }],
  "forced":    [{ "from": "Heidi", "to": "Ivan" }],
  "history":  { "last": 2 },
  "settings": { "single_cycle": true }
}
```

All rules are combined (logical AND), and conflicts are settled by
[priorities](#priorities-and-soft-rules). A `forced` pair that is also in last
year's history simply wins over it (default priorities), so you can keep the
same `forced` rule year after year. If the hard rules cannot all be satisfied,
the program prints that no valid draw exists instead of silently dropping one.

## How the history works

After a successful (non-`--dry-run`) generation, the program writes
`history/<year>.json`:

```json
{
  "version": 2,
  "year": 2026,
  "generated_at": "2026-12-01T18:30:00",
  "relaxed_rules": [],
  "people": { "0fe74d5b": "Alice", "3c3b7eac": "Dave", "c2389bd8": "Carol" },
  "assignments": [
    { "from": "0fe74d5b", "to": "c2389bd8" },
    { "from": "3c3b7eac", "to": "0fe74d5b" }
  ]
}
```

People are recorded by `id`, not by name (`people` is only a readable
snapshot), so **renaming a participant never makes the history forget them**.
Older files, where `from` and `to` are names and there is no `version`, are
still read; the first real draw (or opening the set in the web interface)
converts them, keeping a `.bak` copy of each.

Details worth knowing:

- Only draws from years **before** the one being generated are considered.
  Re-running the generator in the same year (after fixing a typo, for
  instance) therefore overwrites that year's file and is *not* constrained by
  its own previous result.
- People who joined or left since are handled: pairs mentioning a name that is
  no longer in `participants.json` are simply skipped.
- To import a draw made before using this tool, use the *Importer un ancien
  tirage* button of the web interface, or write a name-based file by hand
  (`"assignments": [{"from": "Alice", "to": "Bob"}, ...]`, no `version`): it is
  accepted as it is and converted later.
- If an `id` ever disappears from `participants.json` (hand-editing), the
  recorded name in `people` is used to recognise the person.
- The year defaults to the current one; override it with `--year`.
- `relaxed_rules` lists `{"source", "violations"}` for the soft rules that were
  not fully respected, e.g. `{"source": "history 2024", "violations": 1}`. Only
  counts are stored, never the offending pairs. Old files without this key are
  read fine.

## Identifiers

A participant is `{"id", "name", "email"}`:

- `name` is what humans read, and what `rules.json` refers to. A name that
  matches nobody in a rule is a loud error, so a typo can't go unnoticed.
- `id` is a random token, created once and never changed. It is what the
  history stores. Without it, renaming "Alice" to "Alicia" would make last
  year's draw silently forget her, which is exactly the kind of failure nobody
  notices until the same pair comes up again.

You never write ids: the first real draw adds them to `participants.json` (the
old file is kept as `participants.json.bak`), and a dry run writes nothing. If
you rename someone by hand, change the `name` and leave the `id`, **and update
the rules that mention them** (the web interface does that for you).

## Message template

The text of the mails is not in the code: it lives in `message.txt`, which you
can edit freely (tone, party date, budget, language...). The format is a
mail in miniature: a `Subject:` line, **one blank line**, then the body.

```text
Subject: 🎅 Père Noël Secret {year}

Bonjour,

Cette année, tu es le Père Noël secret de {recipient} !

Budget : 20 € maximum. Joyeux Noël {year} !
```

Three variables are replaced for each participant, in the subject as well as
in the body:

| Variable | Replaced by |
|----------|-------------|
| `{recipient}` | The name of the person this participant gives a gift to. |
| `{santa}` | The name of the participant receiving the mail. |
| `{year}` | The draw year (`--year`, default: the current year). |

To write a literal brace, double it: `{{` and `}}`. Other characters, `$`
included, are plain text.

The template is checked **before** the draw, by rendering it once with dummy
names, so a mistake stops the program immediately instead of after the solver
has run. `--dry-run` prints that preview, with two [storybook characters](#preview-characters)
in place of the participants, which makes it a safe way to proofread your message:

```text
$ python3 Santa.py --dry-run
✅ Un tirage valide existe (rien n'a été écrit).
📧 Aperçu du message (message.txt, personnages fictifs) :
   Subject: 🎅 Père Noël Secret 2026
   ────────────────────────────────────────
   Bonjour,
   ...
```

#### Preview characters

Whenever the program renders your message without a real draw behind it (the
check before solving, `--dry-run`, the live preview of the web page), it
fills the variables with two **fictional characters**:

| Variable | Preview value | Plays the role of |
|----------|---------------|-------------------|
| `{santa}` | **Mère Noël** | the participant who receives the mail |
| `{recipient}` | **Rudolph** | the person they give a gift to |

They are made up on purpose: they read like real names in a sentence ("tu es
le Père Noël secret de Rudolph !"), and nobody in your list is called that, so
a preview can never be mistaken for a real mail. If you ever see them in text,
it is a preview, and nothing was drawn or sent. The two names are the constants
`PREVIEW_SANTA` and `PREVIEW_RECIPIENT` in `Santa.py`; the web page reads them
from there.

Rules the checker enforces, each with a clear error message:

- the first line must be `Subject: …` and the second line must be empty
  (this also refuses `To:`/`From:` lines, which the program sets itself);
- the body must not be empty;
- only `{santa}`, `{recipient}` and `{year}` exist: any other `{name}`, a
  lone brace, or something like `{0}` is an error rather than text that would
  leak into the mails.

Use `--template <file>` (or `./ssm.sh -g -t <file>`) to pick another file, for
example one per family. An explicit file that does not exist is an error; the
default `message.txt` simply falls back to the built-in text if absent. Files
saved by Windows editors (CRLF line endings, BOM) are accepted.

## Command-line options

```text
python3 Santa.py [--base-dir DIR] [--participants FILE] [--rules FILE]
                 [--output-dir DIR] [--history-dir DIR] [--year YEAR]
                 [--dry-run] [--emit-compiled FILE] [--template FILE]
                 [--seed N]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--base-dir` | `.` | Folder holding the project; every other path below is relative to it (unless absolute). One folder = one participant list with its rules, message and history, which is how the web interface keeps several lists. |
| `--participants` | `participants.json` | Participants file. |
| `--rules` | `rules.json` | Rules file. |
| `--output-dir` | `secretSantaFiles` | Where the `.mail` files go. |
| `--history-dir` | `history` | Where draws are stored and read back. |
| `--year` | current year | Year label of the history file. |
| `--dry-run` | off | Check feasibility only: no mail, no history file, no pair printed. Also prints a preview of the message with the [preview characters](#preview-characters) (Mère Noël and Rudolph). Safe to run as the organizer. |
| `--emit-compiled` | off | Also write the compiled rules (see [How it works](#how-it-works)) to the given file, for debugging. **Contains every pair of the history files**, so treat it like `history/`. |
| `--template` | `message.txt` | Message template file, see [Message template](#message-template). |
| `--seed` | off | **Tests only.** Makes the draw reproducible, which means anyone who knows the seed can redo it. A warning is printed. Never use it for a real draw. |

### ssm.sh

`ssm.sh` wraps the common cases so you rarely need to call `Santa.py` directly.

| Option | Action |
|--------|--------|
| `-g` | Generate the draw (mails + history file). Uses `venv/` if it exists, otherwise the system `python3`. |
| `-g -n` | Dry run: checks that a valid draw exists, writes nothing. |
| `-g -y <year>` | Record the draw under another year (4 digits). |
| `-g -e <file>` | Also write the compiled rules to `<file>` (debug, see the privacy notes). |
| `-g -t <file>` | Use another message template than `message.txt`. |
| `-s [-d <dir>]` | Send every `.mail` file of `<dir>` (default: `secretSantaFiles/` of the project) through `msmtp`. Each mail that went out is moved to `<dir>/sent/`, so after a failure you can run `-s` again and only the missing ones are sent. Exit code 1 if any failed. |
| `-D <dir>` | Work on another project folder instead of the current one, e.g. `-D sets/Family` for a set of the web interface. `ssm.sh` finds `Santa.py` (and `venv/`) next to itself, so it can be run from anywhere. |
| `-r` | Add a rule to `rules.json`, through menus (see below). |
| `-c` | Delete the generated `.mail` files, including the ones in `sent/`. |
| `-h` | Show the help. |

`-n`, `-y`, `-e` and `-t` only make sense with `-g`; used alone they are refused
rather than silently ignored. Actions run in the order generate, add a rule,
clean, send. If the draw fails (impossible rules), the script stops there:
combining `-g -s` can never send mails left over from a previous run.

`-r` offers every rule type of `rules.json`:

1. forbidden pair  2. forced pair  3. couple  4. group  5. history
6. single cycle

You pick people from a numbered list (names with spaces are fine). For types
1 to 5 you are then asked whether you want advanced options: `priority`,
`soft` and, for couples, groups and history, `relax`. Leave any answer empty to
keep the default. A couple or group without options is stored in the compact
list form (`["Alice", "Bob"]`). The file is rewritten atomically, and invalid
answers abort without touching it.

Typical session:

```bash
./ssm.sh -r              # add rules as needed
./ssm.sh -g -n           # are they satisfiable?
./ssm.sh -g              # draw for real
./ssm.sh -s              # send; mails that went out move to secretSantaFiles/sent/
./ssm.sh -c              # remove the mails once everything is sent

./ssm.sh -D sets/Family -g -n    # the same, on one set of the web interface
```

## Web interface

An optional, self-contained module: it edits the same files, calls the same
code, and does not change how `Santa.py` or `ssm.sh` work. Delete `webui.py` and
`web/` and nothing else changes. No dependency beyond Python's standard library
(and OR-Tools, already needed by `Santa.py`). The page is dark by design. Rule sections explain themselves in a small "?" bubble (hover, keyboard focus or tap). Icons are UTF-8 emoji, or [Tabler](https://tabler.io/icons) icons inlined as SVG so the page stays self-contained.

```sh
python3 webui.py                 # serves the current folder, opens the browser
python3 webui.py --dir ~/santa   # another root folder
python3 webui.py --port 0 --no-browser --verbose
```

`webui.py` must sit next to `Santa.py`, with `web/index.html` beside it. It talks to `Santa.py` through its [library API](#using-santapy-as-a-library) only.
Default port is 8765 (`0` picks a free one). Stop it with Ctrl+C.

### Sets: several lists, several rule books

A **set** is one participant list *together with* everything that depends on
it: `participants.json`, `rules.json`, `message.txt`, `history/` and the
generated mails. They are never separated, so rules can't end up pointing at
people from another list, and next year's history rule only ever sees the
draws of the same group.

Sets are the sub-folders of `sets/` under the root folder:

```text
santa/
├── Santa.py  ssm.sh  webui.py  web/
└── sets/
    ├── Family/    participants.json  rules.json  message.txt  history/
    ├── Office/    participants.json  rules.json  history/
    └── .trash/    deleted sets, kept
```

The switcher at the top of the page (the *Ensemble* button) lists them with a
one-line summary (people, rules, past draws) and opens another one in a click.
*Gérer les ensembles* (manage sets) is where you create, duplicate, rename and
delete them.

- **New, or copy of another set.** A copy takes the participants and the rules;
  copying the message and the history are two checkboxes (history is off by
  default: copy it to try a variant of the *same* group, leave it off for a
  new group; a copied history is as sensitive as the original, see [Privacy](#privacy)).
  After the copy the two sets evolve separately.
- **Unsaved changes are never lost silently.** Switching sets with pending
  edits asks whether to stay, discard, or save first.
- **Deleting moves the folder to `sets/.trash/`**; nothing is erased.
- **An existing project keeps working.** If the root folder itself already
  holds `participants.json` or `rules.json`, it appears as an extra set called
  "Dossier courant" (it can be duplicated, but not renamed or deleted from the
  page). You can duplicate it into a proper set whenever you like.
- **Set names are folder names**: up to 60 characters, accents and spaces are
  fine, but no `/ \ : * ? " < > |` and no leading or trailing dot.
- A set is an ordinary folder, so the command line works on it directly:
  `./ssm.sh -D sets/Family -g -n`, or `python3 Santa.py --base-dir sets/Family
  --dry-run`. Nothing needs to be linked or copied.
- **Opening a set written by an older version upgrades it once**: ids are added
  to `participants.json` and name-based history files are converted, with `.bak`
  copies. The page says so when it happens.

### Tabs

| Tab | What it does |
|-----|--------------|
| Participants | Edit names and emails; paste a list. Renaming someone rewrites the rules that mention them, removing someone offers to clean those rules up. |
| Règles | Couples, groups, forbidden and forced pairs, the history rule, `single_cycle`, each with the advanced `priority` / `soft` / `relax` options, or the raw JSON. A side panel re-checks feasibility as you type and says which soft rules would be given up (by count, never by pair). |
| Simulation | Runs 50 to 1000 throwaway draws with the *displayed* rules (saved or not) and shows how often each pair occurs. |
| Message | Edits `message.txt` with a live preview that uses the [preview characters](#preview-characters). |
| Tirage | Dry run, official draw, import of an older draw into `history/`, and setting a year aside. |

Everything on these tabs applies to the set currently open.

How it behaves:

- **Saving is all-or-nothing.** Participants, rules and template are validated
  together (including rules against the *new* participant list) before
  anything is written. Each overwritten file is first copied to `<file>.bak`.
- **The official draw is the same code as `ssm.sh -g`**: it calls
  `Project.draw()`, the call `Santa.py` itself makes, on the set's folder, so
  the result, the mails and the history file are identical. It needs a
  confirmation, and replacing an existing year needs a second one. It is
  disabled while there are unsaved changes, so the draw always uses what is on
  disk. Sending mails stays `ssm.sh -s`.
- **Setting a year aside** renames `history/<year>.json` to
  `<year>.json.bak`; it is no longer read, but nothing is lost.
- **Simulations are not the draw.** They use their own random numbers and are
  never stored. A heatmap cell at zero is a rule at work, and that includes
  the history of past years.

### Security and privacy

The server only listens on `127.0.0.1` and checks the `Host` and `Origin`
headers. A random token is generated at each start and embedded in the page;
every API call must carry it, and POST bodies must be JSON. Only the fixed
project files (and `history/<integer>.json`) can be read or written, always
inside a set that is on the server's own list; no path comes from the browser.
The page loads nothing from the outside.

Like the command line, the interface never shows who draws whom: neither for
the official draw nor, by default, for past years (history is listed as year,
size and concessions only).

**Unsafe mode** is the one opt-in exception. The gear menu in the top right
has a "Mode non sécurisé" switch; after a confirmation, the Draw section gets
a "Voir" button on every *past* year, which lists who gave to whom (names
follow renames, thanks to the ids). Three guard rails: the mode is off again
at every page load (nothing is stored), the request must explicitly say
`"unsafe": true` (`POST /api/history/read`), and the draw of the current
calendar year is refused whatever the request says, so the draw being played
stays secret. `Project.read_history(year)` is the library call behind it.

Don't expose the port to other machines (no tunnel, no
`--host`): the files it edits hold everyone's addresses.

## Using Santa.py as a library

The command line and the web interface are thin layers over the same API;
nothing in it prints, reads `sys.argv` or calls `sys.exit`.

```python
import Santa

project = Santa.Project("sets/family")      # one folder = one participant list
result = project.draw(2026, dry_run=True)    # -> DrawResult

for notice in result.notices:                # Notice(level, text)
    print(notice.level, notice.text)         # ok | info | warn | conflict | hint | error | file
if not result.feasible:
    ...                                      # a normal outcome, explained in the notices
```

| Piece | Role |
|-------|------|
| `Project(base, ...)` | The files of one draw. `draw()`, `plan()`, `load_people()`, `ensure_ids()`, history management (`list_history`, `read_history`, `import_history`, `migrate_history`, `set_aside_history`) and `upgrade()`. |
| `build_plan(people, rules, year)` | Check the people, compile the raw rules, settle conflicts: a `Plan`. Everything before the solver. |
| `solve(n, entries, single_cycle, seed=None)` | The CP-SAT model: a `Solution` (`assignment`, `violations`, `feasible`). |
| `Template.parse / load`, `build_mail` | The message and the `.mail` files. |
| `SantaError(message, hint)` | Bad input, in words meant for the user. "No valid draw" is *not* an error: it is `feasible == False`. |

Errors you can fix are `SantaError`; anything else is a bug or the disk
(`OSError`). A draw first renders every mail in memory, then writes the history,
then the mails, so a mail on disk always belongs to a recorded draw.

## Development and tests

```bash
python3 -m unittest discover -s tests        # about 10 seconds, no extra dependency
```

| File | What it checks |
|------|----------------|
| `test_solver.py` | The solver against an exhaustive search on random small instances: same optimum at every priority level, hard rules never broken. |
| `test_rules.py` | Rule expansion, options, conflicts by priority, participant checks. |
| `test_history.py` | History by id: renames, leavers, old name-based files and their conversion, import. |
| `test_template_mail.py` | Message parsing and errors, the preview characters, MIME structure of the mails. |
| `test_project.py` | `Project.draw` end to end: what is written, the writing order, dry runs that write nothing, several folders side by side. |
| `test_cli.py` | `Santa.py` as a user runs it: output, exit codes, `--base-dir`. |
| `test_ssm.py` | `ssm.sh` with a fake `msmtp`: project folders, `sent/` tracking, retries, exit codes. |
| `test_webui.py` | The web API over real HTTP: guards, sets, ids, simulation, draw, no pair ever returned. |

`pytest` also works if you have it. `--seed` exists for these tests; a real
draw never uses it.

## Privacy

Two things on disk reveal the whole draw: the `.mail` files and
`history/<year>.json`. If the organizer is also a participant, they will spoil
their own surprise by opening them. Options:

- Run `--dry-run` to test your rules, and only do the real run when you are
  ready, without looking at the output.
- Delete the `.mail` files once sent (`./ssm.sh -c`). That includes `secretSantaFiles/sent/`, where `-s` keeps the mails it has sent.
- Keep `history/` somewhere you won't browse (or encrypted), but do keep it:
  it is what powers the "not twice in a row" rule.
- Don't leave a file written by `--emit-compiled` lying around: it lists the
  past pairs too.
- The messages about conflicts between rules (`⚖️ ...`) can mention a pair from
  a past year when a `forced` rule overrides it. That is a pair of *last*
  year, not of the current draw.
- The web interface's [unsafe mode](#security-and-privacy) shows past pairs on
  screen; keep it off when someone else can see your display.

## How it works

The draw is modelled as a boolean matrix `x[i][j]` meaning "i gives to j".
The base constraints are "exactly one outgoing and one incoming arc per
person" and "no self-draw". Every rule is translated into forced-to-0
(forbidden) or forced-to-1 (forced) cells: `couples`, `groups` and `history`
are just ways to generate many forbidden cells. `single_cycle` swaps the base
constraints for a circuit constraint.

### Raw rules and compiled rules

`rules.json` is the **raw** file, the one you edit. At every run it is
compiled, in memory, into a flat list of atomic entries, one per directed
pair, each with a kind (forbid/force), a priority, a `soft` flag, the raw rule
it came from, and a bundle id. `couples`, `groups` and `history` vanish in this
step: they are only generators of forbid entries (history ones with low
priority). Entries of the same bundle are violated together; that is what
`relax: "rule"` means.

The compiled list is derived data (raw rules + history files + year). It is
recomputed each time and **never read back from disk**, so there is no second
file to keep in sync. `--emit-compiled` dumps it if you want to see exactly
what the solver received.

### Solving with priorities

1. Pairs that are both forced and forbidden are settled by priority
   (equal priority is an error).
2. Hard entries become plain constraints.
3. Soft entries become penalized constraints inside the solver. Priorities
   are optimized one level at a time, highest first: minimize the violations
   of that level, freeze the result, go to the next level.
4. Whatever freedom is left is spent on the random objective.

To make the result unpredictable, the solver maximizes a sum of random weights
drawn from the operating system's entropy source. Compared to seeding the
solver with a small integer, this prevents anyone from re-running the program
with every possible seed to recover the pairs.

## Troubleshooting

| Message | Likely cause |
|---------|--------------|
| `Participant inconnu « X » dans la règle ...` | Name in `rules.json` doesn't match `participants.json` (case and accents count). |
| `Noms en double` | Two participants share a name: add a surname or initial. |
| `Identifiants en double` | Two participants share an `id` (usually a copy-pasted entry in `participants.json`). Delete one `id`: a new one is created at the next real draw. |
| `N paire(s) de l'historique ignorée(s)` | Not an error: past draws mention people who are no longer participants. |
| `N mail(s) d'un tirage précédent n'ont pas été remplacés` | `secretSantaFiles/` still holds mails of people who aren't in this draw. Don't send them: run `./ssm.sh -c`. |
| `Aucun tirage ne respecte les règles strictes` | The hard rules contradict each other or leave too few options. Mark some `"soft": true`, shrink a group, or drop `single_cycle`/a `forced` rule. |
| `Conflit à priorité égale` | A `forced` and a `forbidden` target the same pair with the same priority. Give one a higher `priority`. |
| `Règle souple non respectée` | Not an error: a soft rule (by default some pairs of the oldest history year) had to be violated to make a draw possible. The number is how many pairs, or how many whole rules for `relax: "rule"`. |
| `variable inconnue {x}` | `message.txt` uses a variable that doesn't exist. Only `{santa}`, `{recipient}` and `{year}` do (typo?). |
| `modèle invalide` | A lone `{` or `}` in `message.txt`. Double it (`{{`, `}}`) to get a literal brace. |
| `la première ligne doit être « Subject: … »` / `la ligne 2 doit être vide` | `message.txt` doesn't follow the format: `Subject:` line, one blank line, then the body. |
| `msmtp` fails | Check your `~/.msmtprc`; try `echo test \| msmtp you@example.org` first. |
