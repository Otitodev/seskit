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
# Safe to run twice: an existing checkout is left alone, and an existing .env is
# never overwritten, so a rerun cannot rotate SECRET_KEY and lock every project
# out of its stored AWS credentials.

set -eu

REPO="https://github.com/Otitodev/seskit.git"
DIR="${SESKIT_DIR:-seskit}"

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

# ------------------------------------------------------------------- clone ---

if [ -d "$DIR/.git" ]; then
    say "using the existing checkout in $DIR"
else
    if [ -e "$DIR" ]; then
        red "$DIR already exists and is not a git checkout."
        echo "Move it, or set SESKIT_DIR to another directory." >&2
        exit 1
    fi
    git clone --quiet "$REPO" "$DIR"
    say "cloned into $DIR"
fi

cd "$DIR"

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

    # A temporary file rather than sed -i, which takes a different argument on
    # BSD and GNU and silently creates a backup file on one of them.
    sed "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" .env > .env.tmp
    mv .env.tmp .env
    chmod 600 .env
    say "wrote .env with a generated SECRET_KEY"
fi

# --------------------------------------------------------------------- run ---

say "building and starting - the first build takes a few minutes"
docker compose up -d --wait

printf '\n'
say "SESKit is running."
printf '\n'
printf '  Dashboard   http://%s:8000\n' "$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost)"
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
