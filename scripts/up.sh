#!/usr/bin/env bash
# =============================================================================
# platform/scripts/up.sh — POSIX equivalent of `make boot` / `make up-all`
# =============================================================================
# Implements platform-mimari-foundation task 10.3 / Requirement 2.8 for hosts
# without GNU make AND platform-real-usage-gaps R2 (R2.1, R2.2, R2.4).
#
# Default semantics ("boot bundle"):
#
#   docker compose \
#     -f infra/docker-compose.yml \
#     -f infra/docker-compose.dev.yml \
#     up -d
#
# (NO --profile flags — only services without a `profiles:` key start:
# postgres, vault, admin-dashboard-api, admin-dashboard-ui. Operators drive
# the rest from the admin-dashboard Setup Wizard.)
#
# Opt-in full-stack semantics (`--all` flag or `up-all` subcommand):
#
#   docker compose \
#     -f infra/docker-compose.yml \
#     -f infra/docker-compose.dev.yml \
#     --profile <p1> --profile <p2> ... \
#     up -d
#
# The profile list is DERIVED from config/services.manifest.json — every
# entry whose `kind` is one of {infra, http_service, worker, sidecar, ui}
# contributes its `compose_profile` field. This keeps the manifest as the
# single source of truth (requirements §1.1, §1.10, §2.1).
#
# Usage:
#   ./scripts/up.sh             # default: boot bundle (no profiles)
#   ./scripts/up.sh up          # same as default
#   ./scripts/up.sh up --all    # full stack (every manifest profile)
#   ./scripts/up.sh up-all      # same as `up --all`
#   ./scripts/up.sh boot        # explicit boot bundle (alias of default)
#   ./scripts/up.sh down        # docker compose down (with profiles)
#   ./scripts/up.sh logs        # docker compose logs -f --tail=200
#   ./scripts/up.sh ps          # docker compose ps
#   ./scripts/up.sh restart     # down + boot
#   ./scripts/up.sh profiles    # print the derived profile list
#   ./scripts/up.sh -- foo bar  # passthrough: docker compose <profiles> foo bar
#
# Environment overrides:
#   PY        Python interpreter used to parse the manifest (default: python3).
#   COMPOSE   Compose CLI (default: "docker compose"). Set to "docker-compose"
#             on legacy hosts.
# =============================================================================
set -euo pipefail

# Resolve repo paths relative to this script regardless of caller's CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PY="${PY:-python3}"
COMPOSE="${COMPOSE:-docker compose}"

COMPOSE_BASE="$PLATFORM_DIR/infra/docker-compose.yml"
COMPOSE_DEV="$PLATFORM_DIR/infra/docker-compose.dev.yml"
MANIFEST="$PLATFORM_DIR/config/services.manifest.json"

if [[ ! -f "$MANIFEST" ]]; then
    echo "up.sh: manifest not found: $MANIFEST" >&2
    exit 1
fi

# --- Derive profile list from manifest ---------------------------------------
# One profile per stdout line; kinds restricted to the foundation enum.
read_profiles() {
    "$PY" - "$MANIFEST" <<'PYEOF'
import json, sys
manifest_path = sys.argv[1]
with open(manifest_path, "r", encoding="utf-8") as fh:
    manifest = json.load(fh)
kinds = {"infra", "http_service", "worker", "sidecar", "ui"}
for entry in manifest["services"]:
    if entry["kind"] in kinds:
        print(entry["compose_profile"])
PYEOF
}

mapfile -t PROFILES < <(read_profiles)

if [[ ${#PROFILES[@]} -eq 0 ]]; then
    echo "up.sh: no profiles derived from manifest — nothing to do" >&2
    exit 1
fi

# Build "--profile p1 --profile p2 ..." in a way that survives `set -u`.
PROFILE_FLAGS=()
for p in "${PROFILES[@]}"; do
    PROFILE_FLAGS+=( --profile "$p" )
done

# Splitting $COMPOSE on whitespace is intentional so that the default
# "docker compose" (two words) and the legacy "docker-compose" (one word)
# both work.
# shellcheck disable=SC2206
COMPOSE_BOOT_ARGV=( $COMPOSE -f "$COMPOSE_BASE" -f "$COMPOSE_DEV" )
# shellcheck disable=SC2206
COMPOSE_FULL_ARGV=( "${COMPOSE_BOOT_ARGV[@]}" "${PROFILE_FLAGS[@]}" )

# --- Subcommand dispatch ------------------------------------------------------
cmd="${1:-up}"
shift || true

# Parse `up` subcommand flags: --all switches to full-stack semantics.
all_flag=0
if [[ "$cmd" == "up" ]]; then
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --all)
                all_flag=1
                shift
                ;;
            *)
                break
                ;;
        esac
    done
fi

case "$cmd" in
    up)
        if [[ $all_flag -eq 1 ]]; then
            exec "${COMPOSE_FULL_ARGV[@]}" up -d "$@"
        else
            exec "${COMPOSE_BOOT_ARGV[@]}" up -d "$@"
        fi
        ;;
    boot)
        exec "${COMPOSE_BOOT_ARGV[@]}" up -d "$@"
        ;;
    up-all)
        exec "${COMPOSE_FULL_ARGV[@]}" up -d "$@"
        ;;
    down)
        exec "${COMPOSE_FULL_ARGV[@]}" down "$@"
        ;;
    logs)
        exec "${COMPOSE_FULL_ARGV[@]}" logs -f --tail=200 "$@"
        ;;
    ps)
        exec "${COMPOSE_FULL_ARGV[@]}" ps "$@"
        ;;
    restart)
        "${COMPOSE_FULL_ARGV[@]}" down
        exec "${COMPOSE_BOOT_ARGV[@]}" up -d
        ;;
    profiles)
        printf '%s\n' "${PROFILES[@]}"
        ;;
    --)
        exec "${COMPOSE_FULL_ARGV[@]}" "$@"
        ;;
    -h|--help|help)
        sed -n '1,55p' "$0"
        ;;
    *)
        echo "up.sh: unknown subcommand: $cmd" >&2
        echo "Try: up [--all] | up-all | boot | down | logs | ps | restart | profiles | -- <args>" >&2
        exit 2
        ;;
esac
