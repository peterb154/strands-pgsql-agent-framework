#!/usr/bin/env bash
#
# strands-pg installer — shadcn-style: we copy files into your new agent
# directory, then get out of your way. No pip install, no runtime dep on
# this repo.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/peterb154/strands-pgsql-agent-framework/main/install.sh \
#     | bash -s -- my-agent
#
#   # Pin a specific version:
#   curl -sSL https://raw.githubusercontent.com/peterb154/strands-pgsql-agent-framework/v0.1.0/install.sh \
#     | bash -s -- my-agent --ref v0.1.0
#
#   # Paranoid mode (recommended for first-time users):
#   curl -sSL https://raw.githubusercontent.com/peterb154/strands-pgsql-agent-framework/main/install.sh -o install.sh
#   less install.sh   # read it before you run it
#   bash install.sh my-agent
#
# Env overrides:
#   STRANDS_PG_REPO  default: peterb154/strands-pgsql-agent-framework
#   STRANDS_PG_REF   default: latest git tag, or main if no tags exist

set -euo pipefail

REPO="${STRANDS_PG_REPO:-peterb154/strands-pgsql-agent-framework}"
REF="${STRANDS_PG_REF:-}"
TARGET=""
FORCE=false
REFRESH=false

usage() {
    cat <<EOF
Usage: install.sh <target-dir> [--ref <git-tag-or-branch>] [--force|--refresh]

Options:
  --ref <ref>    Git tag or branch to install from (default: latest tag, else main)
  --force, -f    Overlay ALL files, including templates (Dockerfile, app.py,
                 prompts/, etc.). Use when you want a clean re-stamp and accept
                 losing any customizations to templated files.
  --refresh      Framework-only upgrade: refreshes strands_pg/ + migrations/0*
                 and leaves app.py / tools/ / prompts/ / Dockerfile / compose
                 alone. Use this when bumping the framework version on an
                 existing agent.
  -h, --help     Show this help

Examples:
  install.sh my-agent                             # new agent, latest tag
  install.sh my-agent --ref v0.1.0                # pin a specific version
  install.sh existing --refresh --ref v0.7.0      # upgrade framework only
  install.sh existing --force --ref main          # re-stamp everything
  STRANDS_PG_REF=main install.sh my-agent
EOF
}

# ---- arg parse ------------------------------------------------------------

while (( $# )); do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --ref)
            REF="$2"
            shift 2
            ;;
        --ref=*)
            REF="${1#--ref=}"
            shift
            ;;
        -f|--force)
            FORCE=true
            shift
            ;;
        --refresh)
            REFRESH=true
            shift
            ;;
        -*)
            echo "unknown flag: $1" >&2
            usage
            exit 2
            ;;
        *)
            if [[ -z "$TARGET" ]]; then
                TARGET="$1"
            else
                echo "too many positional args: $1" >&2
                usage
                exit 2
            fi
            shift
            ;;
    esac
done

if [[ -z "$TARGET" ]]; then
    echo "error: target directory is required" >&2
    usage
    exit 2
fi

if [[ "$FORCE" == "true" ]] && [[ "$REFRESH" == "true" ]]; then
    echo "error: --force and --refresh are mutually exclusive" >&2
    exit 2
fi

if [[ -e "$TARGET" ]] && [[ "$FORCE" != "true" ]] && [[ "$REFRESH" != "true" ]]; then
    echo "error: $TARGET already exists — pick a new path, remove it first, or pass --force / --refresh" >&2
    exit 1
fi

if [[ ! -e "$TARGET" ]] && [[ "$REFRESH" == "true" ]]; then
    echo "error: --refresh requires an existing directory (you can't refresh a stamp that doesn't exist yet)" >&2
    exit 1
fi

if [[ -e "$TARGET" ]] && [[ "$FORCE" == "true" ]]; then
    echo "==> --force: stamping into existing directory $TARGET (full overlay, no delete)"
fi

if [[ "$REFRESH" == "true" ]]; then
    echo "==> --refresh: upgrading framework in $TARGET (templates untouched)"
fi

# ---- resolve ref ----------------------------------------------------------

if [[ -z "$REF" ]]; then
    echo "==> resolving latest release tag for $REPO..."
    # Ask GitHub's git endpoint for tags sorted by version. No auth required.
    REF="$(git ls-remote --tags --refs "https://github.com/${REPO}.git" \
              | awk -F/ '{print $NF}' \
              | sort -V \
              | tail -1 || true)"
    if [[ -z "$REF" ]]; then
        echo "    no tags found — falling back to main"
        REF="main"
    else
        echo "    using $REF"
    fi
fi

# ---- fetch ----------------------------------------------------------------

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> cloning ${REPO}@${REF}..."
if ! git clone --quiet --depth=1 --branch="$REF" "https://github.com/${REPO}.git" "$TMP/src" 2>/dev/null; then
    echo "error: failed to clone ${REPO}@${REF}" >&2
    echo "  check the ref exists: https://github.com/${REPO}/tree/${REF}" >&2
    exit 1
fi

# ---- stamp ----------------------------------------------------------------

echo "==> stamping into $TARGET..."
mkdir -p "$TARGET"

if [[ "$REFRESH" != "true" ]]; then
    # Template files (Dockerfile, compose, app.py, prompts, etc.) — the
    # shape of the new agent. Only copied on fresh stamp or --force.
    cp -R "$TMP/src/templates/agent/." "$TARGET/"

    # Convenience: create an empty tools/ so imports don't blow up.
    mkdir -p "$TARGET/tools"
    touch "$TARGET/tools/__init__.py"

    # Clean up the .gitkeep if we left it.
    rm -f "$TARGET/tools/.gitkeep"
fi

# Vendored framework source — user owns this now. ALWAYS refreshed, since
# bumping this is the whole point of --refresh.
mkdir -p "$TARGET/strands_pg"
cp -R "$TMP/src/src/strands_pg/." "$TARGET/strands_pg/"

# Framework migrations 001-099. User drops their own 100+ next to these.
# Only the framework-numbered migrations are refreshed; user's 100+ files
# are untouched because we copy by exact framework filename.
mkdir -p "$TARGET/migrations"
for mig in "$TMP/src/migrations"/*.sql; do
    cp -f "$mig" "$TARGET/migrations/"
done

# Record the stamp reference so `diff -r` later has something to compare to.
cat > "$TARGET/.strands-pg-ref" <<EOF
# Generated by install.sh; shows which upstream ref this agent was stamped from.
# Safe to commit or delete — it's purely informational.
repo=${REPO}
ref=${REF}
stamped_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF

# ---- finish ---------------------------------------------------------------

cat <<EOF

==> stamped from ${REPO}@${REF} into $TARGET

next steps:
  cd $TARGET
  cp .env.example .env
  # edit .env: set AWS_PROFILE to one with Bedrock access
  docker compose up --build

  # in another shell:
  curl -s localhost:8000/health
  curl -sX POST localhost:8000/chat \\
    -H 'content-type: application/json' \\
    -d '{"session_id":"you@example.com","message":"hello"}'

read $TARGET/README.md for what to edit next.
EOF
