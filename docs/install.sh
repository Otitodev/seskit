#!/bin/sh
# Install SESKit on a server.
#
#   curl -fsSL https://otitodev.github.io/seskit/install.sh | sh
#
# Clones the repository, writes a .env with a generated SECRET_KEY, and brings
# the stack up. Everything it does is three commands you could run yourself,
# and the manual path is documented for anyone who would rather:
# https://otitodev.github.io/seskit/getting-started/installation/
#
# POSIX sh, because it is piped to whatever /bin/sh is on the box. No bashisms.
#
# It refuses rather than guesses. A script that installs Docker for you, picks
# a firewall rule, or edits somebody's system on a whim is a script nobody
# should pipe into a shell - so where a prerequisite is missing this says which
# one and stops.
#
# Environment overrides:
#   SESKIT_VERSION     a release tag, or "main". Default: latest release.
#   SESKIT_DIR         where to clone. Default: ./seskit
#   SESKIT_PUBLIC_URL  where this instance is reachable from outside.
#                      Default: guessed from the public IP.
#   SESKIT_EVENT_PREFIX
#                      the name this instance's SQS queue, SNS topic and SES
#                      configuration set are created under. Default: "seskit-"
#                      plus six random hex digits, so two instances on one AWS
#                      account never share a queue.
#
# Safe to run twice: an existing checkout is left alone, and an existing .env is
# never overwritten, so a rerun cannot rotate SECRET_KEY and lock every project
# out of its stored AWS credentials - or rename its event resources.

set -eu

REPO="https://github.com/Otitodev/seskit.git"
API="https://api.github.com/repos/Otitodev/seskit/releases/latest"
DIR="${SESKIT_DIR:-seskit}"

# Which version to install.
#
#   SESKIT_VERSION=v0.2.0  ./install.sh    a specific release
#   SESKIT_VERSION=main    ./install.sh    the development branch
#
# Empty means "the latest published release", resolved below. Pinning matters
# because this URL is the same one for everybody: without it, a bad commit on
# main reaches every new install within minutes of being pushed.
VERSION="${SESKIT_VERSION:-}"

red() { printf '\033[31m%s\033[0m\n' "$1" >&2; }
say() { printf '  %s\n' "$1"; }

need() {
    if ! command -v "$1" >/dev/null 2>&1; then
        red "SESKit needs $1, which is not installed."
        printf '%s\n' "$2" >&2
        exit 1
    fi
}

# ------------------------------------------------------------ prerequisites ---

need git "Install git with your package manager, then run this again."
need docker "Install Docker: https://docs.docker.com/engine/install/"

if ! docker compose version >/dev/null 2>&1; then
    red "SESKit needs the Docker Compose plugin."
    echo "Install it: https://docs.docker.com/compose/install/" >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    red "Docker is installed but not running, or this user cannot reach it."
    echo "Start it, or add yourself to the docker group and log in again." >&2
    exit 1
fi

# ----------------------------------------------------------------- version ---

latest_release() {
    # The tag of the most recent GitHub release, or nothing.
    #
    # Nothing is the honest answer in two cases that must not be fatal: the
    # project has published no release yet, and this box cannot reach the API.
    # Both fall through to main below, with a line saying so - an installer
    # that refused to run because a version lookup failed would be worse than
    # one that told you what it was installing.
    #
    # Split on commas and cut on quotes rather than a sed backreference: this
    # has to run under whatever /bin/sh the host has, and the simpler tool is
    # the one that behaves the same on all of them.
    curl -fsSL --max-time 10 "$API" 2>/dev/null |
        tr ',' '
' |
        grep '"tag_name"' |
        head -n 1 |
        cut -d'"' -f4
}

if [ -z "$VERSION" ]; then
    VERSION=$(latest_release || true)
fi

if [ -z "$VERSION" ]; then
    VERSION="main"
    say "no published release found - installing from main"
else
    say "installing $VERSION"
fi

# ------------------------------------------------------------------- clone ---

if [ -d "$DIR/.git" ]; then
    say "using the existing checkout in $DIR"
    # Moved to the requested version rather than left wherever it was. A rerun
    # that silently kept an old checkout would report success and change
    # nothing, which is the failure hardest to notice.
    (cd "$DIR" && git fetch --quiet --tags origin && git checkout --quiet "$VERSION" 2>/dev/null) ||
        say "could not move the checkout to $VERSION - leaving it as it is"
else
    if [ -e "$DIR" ]; then
        red "$DIR already exists and is not a git checkout."
        echo "Move it, or set SESKIT_DIR to another directory." >&2
        exit 1
    fi
    git clone --quiet --branch "$VERSION" --depth 1 "$REPO" "$DIR" 2>/dev/null ||
        git clone --quiet "$REPO" "$DIR"
    say "cloned into $DIR"
fi

cd "$DIR"

# ---------------------------------------------------------------- public URL ---

# Asks a third party what this machine looks like from outside. `hostname -I`
# answers with a private address behind NAT, which is exactly the case where
# the answer matters.
detect_public_url() {
    if [ -n "${SESKIT_PUBLIC_URL:-}" ]; then
        printf '%s\n' "$SESKIT_PUBLIC_URL"
        return
    fi
    ip=$(curl -fsSL --max-time 5 https://api.ipify.org 2>/dev/null || true)
    [ -n "$ip" ] && printf 'http://%s:8000\n' "$ip"
}

PUBLIC_URL=$(detect_public_url || true)

# ------------------------------------------------------------------- secret ---

# Generated once and never regenerated. It signs sessions and derives the key
# that stored AWS credentials are encrypted with, so replacing it on a rerun
# would silently disconnect every project.
if [ -f .env ]; then
    say "keeping the existing .env"
else
    cp .env.example .env

    if command -v openssl >/dev/null 2>&1; then
        SECRET=$(openssl rand -base64 32 | tr -d '\n=/+' )
    else
        # /dev/urandom is on every system this script can run on. base64 is
        # not always, so fall back to hex through od.
        SECRET=$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')
    fi

    # Event resources are named after this instance, not after "seskit".
    # Two instances on one AWS account and region with the default name
    # adopt each other's queue, and each then receives a random half of the
    # other's delivery events; removing events on either deletes the
    # other's. Seen on a real host. Six hex digits from the same source as
    # the secret; the IAM policy in the docs matches any seskit-* name.
    if [ -n "${SESKIT_EVENT_PREFIX:-}" ]; then
        PREFIX="$SESKIT_EVENT_PREFIX"
    else
        PREFIX="seskit-$(od -An -tx1 -N3 /dev/urandom | tr -d ' \n')"
    fi

    # A temporary file rather than sed -i, which takes a different argument on
    # BSD and GNU and silently creates a backup file on one of them.
    sed -e "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" \
        -e "s|^EVENT_RESOURCE_PREFIX=.*|EVENT_RESOURCE_PREFIX=${PREFIX}|" \
        -e "s|^EVENT_CONFIGURATION_SET=.*|EVENT_CONFIGURATION_SET=${PREFIX}|" \
        .env > .env.tmp
    mv .env.tmp .env
    say "event resources will be named ${PREFIX}-events and ${PREFIX}"

    # PUBLIC_BASE_URL is where this instance is reachable from outside. Unset,
    # every message goes out with no one-click unsubscribe header at all - a
    # silent omission that costs sender reputation and that nobody notices,
    # because nothing fails.
    #
    # Guessed from the public IP, which is right often enough to be worth
    # doing and wrong in ways that are visible: behind a proxy or on a real
    # domain you change one line, and the AWS page tells you what it sees.
    if [ -n "${PUBLIC_URL}" ]; then
        printf 'PUBLIC_BASE_URL=%s\n' "$PUBLIC_URL" >> .env
        say "set PUBLIC_BASE_URL to $PUBLIC_URL"
    fi

    chmod 600 .env
    say "wrote .env with a generated SECRET_KEY"
    FRESH=1
fi

# --------------------------------------------------------------------- run ---

if [ "${FRESH:-0}" = 1 ]; then
    say "building and starting - the first build takes a few minutes"
else
    say "building and starting"
fi
docker compose up -d --wait

# The address the instance is reachable at, which is what was written to
# .env - on a rerun, what an earlier run wrote. `hostname -I` was here
# before and printed the private address on every cloud host, so the one
# link a first-time user copies was dead.
DASHBOARD=$(grep '^PUBLIC_BASE_URL=' .env | cut -d= -f2-)
DASHBOARD="${DASHBOARD:-${PUBLIC_URL:-http://localhost:8000}}"

printf '\n'
say "SESKit is running."
printf '\n'
printf '  Dashboard   %s\n' "$DASHBOARD"
printf '  Mailpit     http://localhost:8025    (local mail, until AWS is connected)\n'
printf '\n'
printf '  Next:\n'
printf '    1. Open the dashboard and create the owner account.\n'
printf '       The first registration claims the instance and closes signup.\n'
printf '    2. Get an AWS access key and paste it in:\n'
printf '       https://otitodev.github.io/seskit/guides/get-an-access-key/\n'
printf '\n'
printf '  Put a TLS terminator in front before you send anything real:\n'
printf '  https://otitodev.github.io/seskit/operating/deploying/\n'
printf '\n'
