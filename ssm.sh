#!/bin/bash

# ===============================================================
# 🎅 Secret Santa Manager
#
# Thin wrapper around Santa.py: generate the draw, send the mails,
# add rules to rules.json interactively, clean up generated files.
#
# Works on one "project folder" (participants.json, rules.json, message.txt,
# history/, secretSantaFiles/): the current folder, or the one given with
# -D, e.g. one of the sets/<name>/ folders of the web interface. Santa.py is
# found next to this script, wherever you run it from.
#
# Needs: bash 4+, jq (for -r), msmtp (for -s), python3 with ortools
# (a venv/ next to this script is used if present).
# ===============================================================

# === CONFIG ===
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_PATH="venv/bin/activate"        # looked up next to this script, then in the current folder
SANTA_SCRIPT="$SCRIPT_DIR/Santa.py"
RULES_NAME="rules.json"
PARTICIPANTS_NAME="participants.json"
MAIL_DIR_NAME="secretSantaFiles"
BASE_DIR="."                        # project folder, see -D

# === OPTIONS ===
run_santa=false
send_mails=false
add_rule=false
clear_mails=false
dry_run=false
directory=""
base_given=false
year=""
emit_file=""
template_file=""

# === HELPERS ===

usage() {
    echo "Usage : $0 [options]"
    echo
    echo "Actions :"
    echo "  -g                Générer le tirage (mails + historique)"
    echo "  -s [-d <dossier>] Envoyer les mails (.mail) ; défaut : $MAIL_DIR_NAME/ du projet."
    echo "                    Chaque mail envoyé est rangé dans <dossier>/sent/ : on peut relancer"
    echo "                    après un échec sans renvoyer à tout le monde."
    echo "  -r                Ajouter une règle à $RULES_NAME (interactif)"
    echo "  -c                Supprimer les fichiers .mail générés (et ceux de sent/)"
    echo "  -h                Afficher cette aide"
    echo
    echo "Dossier du projet :"
    echo "  -D <dossier>      Travailler sur ce dossier (participants.json, rules.json, history/...)"
    echo "                    au lieu du dossier courant, par ex. un ensemble de l'interface web :"
    echo "                    -D sets/Famille"
    echo
    echo "Options de génération (avec -g) :"
    echo "  -n                Test à blanc : vérifie qu'un tirage existe, n'écrit rien"
    echo "  -y <année>        Année enregistrée dans l'historique (défaut : année en cours)"
    echo "  -e <fichier>      Écrit aussi les règles compilées (debug ; contient les paires"
    echo "                    de l'historique, à traiter comme history/)"
    echo "  -t <fichier>      Modèle de message (défaut : message.txt s'il existe)"
    echo
    echo "Exemples :"
    echo "  $0 -g -n                       # règles satisfiables ? (affiche aussi l'aperçu du message)"
    echo "  $0 -g                          # tirage pour l'année en cours"
    echo "  $0 -g -y 2026 -e compiled.json # tirage 2026 + règles compilées"
    echo "  $0 -D sets/Famille -g          # tirage de l'ensemble « Famille »"
    echo "  $0 -s                          # envoi des mails du projet"
    echo "  $0 -r                          # ajouter une règle"
    echo "  $0 -c                          # nettoyer les .mail"
    exit "${1:-1}"
}

die() {
    echo "❌ $*" >&2
    exit 1
}

# Print participant names, one per line, from participants.json
load_participants() {
    [ -f "$PARTICIPANTS_FILE" ] || die "Fichier $PARTICIPANTS_FILE introuvable."
    jq -r '.[].name' "$PARTICIPANTS_FILE"
}

# Apply a jq filter to rules.json in place, without ever leaving a
# half-written file: write to a temp file, then move it over the original.
# Usage: update_rules [jq options...] '<filter>'
update_rules() {
    local tmp
    tmp=$(mktemp)
    if jq "$@" "$RULES_FILE" > "$tmp"; then
        mv "$tmp" "$RULES_FILE"
    else
        rm -f "$tmp"
        die "Échec de la mise à jour de $RULES_FILE."
    fi
}

# Let the user pick one participant; the result is left in $PICKED.
pick_one() {
    echo "$1"
    PICKED=""
    select PICKED in "${participants[@]}"; do
        [[ -n "$PICKED" ]] && break
    done
    [[ -n "$PICKED" ]] || die "Aucune sélection."
}

# Let the user pick several participants; the result is left in the
# $picked array. Duplicates are refused (names are compared exactly, so
# names containing spaces are fine).
pick_many() {
    local m p dup
    picked=()
    echo "Choisir les membres un par un, puis « TERMINER » :"
    select m in "${participants[@]}" "TERMINER"; do
        [[ "$m" == "TERMINER" ]] && break
        [[ -z "$m" ]] && continue
        dup=false
        for p in "${picked[@]}"; do [[ "$p" == "$m" ]] && dup=true; done
        if $dup; then
            echo "  (déjà choisi)"
        else
            picked+=("$m")
            echo "  + $m"
        fi
    done
}

# Optional keys of a rule, collected as a JSON object in $EXTRAS.
#   $1 = "relax" to also ask about the pair/rule relaxation mode.
# Anything left empty keeps the program's default (see README).
ask_options() {
    EXTRAS='{}'
    local adv prio soft relax hint="priorité, souple"
    [[ "$1" == relax ]] && hint+=", relâchement"
    read -rp "Options avancées ($hint) ? (o/N) : " adv
    [[ "$adv" =~ ^[oOyY]$ ]] || return 0

    read -rp "  Priorité (entier, plus haut = plus fort ; vide = défaut) : " prio
    if [[ -n "$prio" ]]; then
        [[ "$prio" =~ ^-?[0-9]+$ ]] || die "La priorité doit être un entier."
        EXTRAS=$(jq -c --argjson p "$prio" '. + {priority: $p}' <<< "$EXTRAS")
    fi

    read -rp "  Règle souple, violable si nécessaire ? (o/n, vide = défaut) : " soft
    case "$soft" in
        o|O|y|Y) EXTRAS=$(jq -c '. + {soft: true}'  <<< "$EXTRAS") ;;
        n|N)     EXTRAS=$(jq -c '. + {soft: false}' <<< "$EXTRAS") ;;
        "")      ;;
        *)       die "Réponse invalide : $soft" ;;
    esac

    if [[ "$1" == relax ]]; then
        read -rp "  Relâchement : pair (paire par paire) ou rule (règle entière) ? (vide = défaut) : " relax
        case "$relax" in
            pair|rule) EXTRAS=$(jq -c --arg r "$relax" '. + {relax: $r}' <<< "$EXTRAS") ;;
            "")        ;;
            *)         die "Relâchement invalide : $relax (pair ou rule)." ;;
        esac
    fi
}

# Add a rule through menus; supports every rule type of rules.json.
add_rule_interactive() {
    command -v jq > /dev/null || die "jq est requis pour ajouter une règle."
    mapfile -t participants < <(load_participants)
    [ -f "$RULES_FILE" ] || echo '{}' > "$RULES_FILE"
    PS3="> "

    echo "Type de règle :"
    echo "  1. Interdite      (A ne tire pas B)"
    echo "  2. Forcée         (A tire B)"
    echo "  3. Couple         (A et B ne se tirent pas, dans les deux sens)"
    echo "  4. Groupe         (personne du groupe ne tire quelqu'un du groupe)"
    echo "  5. Historique     (ne pas répéter les N derniers tirages)"
    echo "  6. Anneau unique  (tout le tirage forme une seule boucle)"
    read -rp "Choix (1-6) : " choice

    case "$choice" in
        1|2)
            local type from to
            [[ "$choice" == 1 ]] && type="forbidden" || type="forced"
            pick_one "Choisir le donneur :";  from="$PICKED"
            pick_one "Choisir le receveur :"; to="$PICKED"
            [[ "$from" != "$to" ]] || die "Le donneur et le receveur doivent différer."
            ask_options
            update_rules --arg type "$type" --arg from "$from" --arg to "$to" \
                --argjson extra "$EXTRAS" '.[$type] += [{from: $from, to: $to} + $extra]'
            echo "✅ Règle ajoutée : $from → $to ($type)"
            ;;
        3)
            local a b members
            pick_one "Choisir la première personne :"; a="$PICKED"
            pick_one "Choisir la deuxième personne :"; b="$PICKED"
            [[ "$a" != "$b" ]] || die "Un couple doit contenir deux personnes différentes."
            members=$(jq -nc --arg a "$a" --arg b "$b" '[$a, $b]')
            ask_options relax
            # Keep the compact list form when there are no options.
            update_rules --argjson members "$members" --argjson extra "$EXTRAS" \
                '.couples += [if ($extra | length) == 0 then $members else ({members: $members} + $extra) end]'
            echo "✅ Couple ajouté : $a + $b"
            ;;
        4)
            local members
            pick_many
            [ "${#picked[@]}" -ge 2 ] || die "Un groupe doit contenir au moins 2 personnes."
            members=$(printf '%s\n' "${picked[@]}" | jq -R . | jq -sc .)
            ask_options relax
            update_rules --argjson members "$members" --argjson extra "$EXTRAS" \
                '.groups += [if ($extra | length) == 0 then $members else ({members: $members} + $extra) end]'
            echo "✅ Groupe ajouté : ${picked[*]}"
            ;;
        5)
            local n
            read -rp "Nombre de tirages passés à éviter [1] : " n
            n=${n:-1}
            [[ "$n" =~ ^[1-9][0-9]*$ ]] || die "Le nombre de tirages doit être un entier >= 1."
            ask_options relax
            if jq -e '.history' "$RULES_FILE" > /dev/null; then
                echo "ℹ️  Remplace la règle d'historique existante."
            fi
            update_rules --argjson n "$n" --argjson extra "$EXTRAS" '.history = ({last: $n} + $extra)'
            echo "✅ Historique : les $n dernier(s) tirage(s) seront évités."
            ;;
        6)
            local on
            read -rp "Activer l'anneau unique ? (O/n) : " on
            case "$on" in
                n|N) update_rules '.settings.single_cycle = false'; echo "✅ Anneau unique désactivé." ;;
                *)   update_rules '.settings.single_cycle = true';  echo "✅ Anneau unique activé." ;;
            esac
            ;;
        *)
            die "Choix invalide."
            ;;
    esac
}

# Delete generated mails.
clear_mails_func() {
    if [ -d "$MAIL_DIR" ]; then
        rm -f "$MAIL_DIR"/*.mail "$MAIL_DIR"/sent/*.mail
        rmdir "$MAIL_DIR/sent" 2> /dev/null   # only if now empty
        echo "🧹 Fichiers .mail supprimés dans $MAIL_DIR."
    else
        echo "Aucun dossier $MAIL_DIR trouvé."
    fi
}

# === OPTION PARSING ===
while getopts "gsd:rcny:e:t:D:h" opt; do
    case $opt in
        g) run_santa=true ;;
        s) send_mails=true ;;
        d) directory="$OPTARG" ;;
        r) add_rule=true ;;
        c) clear_mails=true ;;
        n) dry_run=true ;;
        y) year="$OPTARG" ;;
        e) emit_file="$OPTARG" ;;
        t) template_file="$OPTARG" ;;
        D) BASE_DIR="$OPTARG"; base_given=true ;;
        h) usage 0 ;;
        *) usage 1 ;;
    esac
done

# -n, -y, -e and -t only make sense together with -g: refuse silently ignored flags.
if ! $run_santa && { $dry_run || [ -n "$year" ] || [ -n "$emit_file" ] || [ -n "$template_file" ]; }; then
    die "Les options -n, -y, -e et -t s'utilisent avec -g."
fi
if [ -n "$year" ] && ! [[ "$year" =~ ^[0-9]{4}$ ]]; then
    die "L'année doit comporter 4 chiffres (reçu : $year)."
fi

[ -d "$BASE_DIR" ] || die "Le dossier du projet n'existe pas : $BASE_DIR"
BASE_DIR="${BASE_DIR%/}"
[ -n "$BASE_DIR" ] || BASE_DIR="/"
RULES_FILE="$BASE_DIR/$RULES_NAME"
PARTICIPANTS_FILE="$BASE_DIR/$PARTICIPANTS_NAME"
MAIL_DIR="${directory:-$BASE_DIR/$MAIL_DIR_NAME}"   # -d overrides it for -s and -c alike

# === ACTIONS ===
if $run_santa; then
    santa_args=()
    $base_given && santa_args+=(--base-dir "$BASE_DIR")
    $dry_run && santa_args+=(--dry-run)
    [ -n "$year" ] && santa_args+=(--year "$year")
    [ -n "$emit_file" ] && santa_args+=(--emit-compiled "$emit_file")
    [ -n "$template_file" ] && santa_args+=(--template "$template_file")

    if $dry_run; then
        echo "🎅 Test à blanc du tirage Secret Santa..."
    else
        echo "🎅 Génération du tirage Secret Santa..."
    fi

    venv_active=false
    for candidate in "$SCRIPT_DIR/$ENV_PATH" "$ENV_PATH"; do
        if [ -f "$candidate" ]; then
            source "$candidate"
            venv_active=true
            break
        fi
    done
    $venv_active || echo "ℹ️  $ENV_PATH introuvable : utilisation du python3 du système."

    python3 "$SANTA_SCRIPT" "${santa_args[@]}"
    santa_status=$?
    $venv_active && deactivate

    # If the draw failed, stop here: a following -s must never send stale mails.
    [ "$santa_status" -eq 0 ] || exit "$santa_status"
fi

if $add_rule; then
    add_rule_interactive
fi

if $clear_mails; then
    clear_mails_func
fi

if $send_mails; then
    [ -d "$MAIL_DIR" ] || die "Le dossier des mails n'existe pas : $MAIL_DIR"
    echo "📨 Envoi des mails de $MAIL_DIR..."
    sent=0
    failed=0
    for mail in "$MAIL_DIR"/*.mail; do
        if [ -f "$mail" ]; then
            # Line 1 = bare address (msmtp argument); the rest is a complete
            # RFC 5322 message (headers, blank line, UTF-8 body) piped as-is.
            recipient=$(head -n 1 "$mail")
            if tail -n +2 "$mail" | msmtp "$recipient"; then
                echo "✉️  Mail envoyé à $recipient"
                # Move it aside so that running -s again never sends it twice.
                mkdir -p "$MAIL_DIR/sent" && mv -f "$mail" "$MAIL_DIR/sent/"
                sent=$((sent + 1))
            else
                echo "❌ Échec de l'envoi pour $mail" >&2
                failed=$((failed + 1))
            fi
        fi
    done
    echo "📬 $sent mail(s) envoyé(s), $failed échec(s)."
    if [ "$failed" -gt 0 ]; then
        echo "   Les mails en échec sont restés dans $MAIL_DIR : relancez -s pour les renvoyer." >&2
        exit 1
    fi
fi

if ! $run_santa && ! $send_mails && ! $add_rule && ! $clear_mails; then
    usage 1
fi
