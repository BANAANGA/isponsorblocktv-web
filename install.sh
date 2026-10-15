#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY="https://github.com/BANAANGA/isponsorblocktv-web.git"
INSTALL_DIR="${ISPBTV_INSTALL_DIR:-${HOME}/.local/share/isponsorblocktv-web}"

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

command -v docker >/dev/null 2>&1 || fail "Docker is not installed. Install Docker Engine first: https://docs.docker.com/engine/install/"
command -v git >/dev/null 2>&1 || fail "Git is not installed. Install Git first: https://git-scm.com/downloads"
docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin is missing. Install it: https://docs.docker.com/compose/install/"
docker info >/dev/null 2>&1 || fail "Docker is not running or your user cannot access it. Start Docker and retry."

if [[ -d "${INSTALL_DIR}/.git" ]]; then
    origin="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
    [[ "$origin" == "$REPOSITORY" ]] || fail "${INSTALL_DIR} is a Git repository, but not this application."
    printf 'Updating installation in %s...\n' "$INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
elif [[ -e "$INSTALL_DIR" ]]; then
    fail "${INSTALL_DIR} already exists and is not an iSponsorBlockTV Web Git checkout. Set ISPBTV_INSTALL_DIR to another location."
else
    printf 'Installing to %s...\n' "$INSTALL_DIR"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth 1 "$REPOSITORY" "$INSTALL_DIR"
fi

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
    [[ -r /dev/tty ]] || fail "Run this installer from an interactive terminal so it can securely prompt for credentials."
    exec 3</dev/tty
    printf '\nCreate the administrator account for the web interface.\n'
    read -r -u 3 -p "Username: " username
    [[ "$username" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || fail "Use 1-64 letters, numbers, dots, underscores, or hyphens for the username."

    while true; do
        read -r -s -u 3 -p "Password (at least 12 letters, numbers, _ or -): " password
        printf '\n'
        [[ "$password" =~ ^[A-Za-z0-9_-]{12,128}$ ]] || {
            printf 'Use 12-128 letters, numbers, underscores, or hyphens.\n'
            continue
        }
        read -r -s -u 3 -p "Confirm password: " password_confirmation
        printf '\n'
        [[ "$password" == "$password_confirmation" ]] && break
        printf 'Passwords did not match. Try again.\n'
    done

    umask 077
    printf 'WEB_USERNAME=%s\nWEB_PASSWORD=%s\nCOOKIE_SECURE=false\n' \
        "$username" "$password" > "${INSTALL_DIR}/.env"
    unset password password_confirmation
    chmod 600 "${INSTALL_DIR}/.env"
fi

cd "$INSTALL_DIR"
docker compose config --quiet || fail "Compose configuration is invalid. Check ${INSTALL_DIR}/.env."
docker compose up --build -d

printf '\nInstallation complete. Open http://localhost:8000 to sign in.\n'
printf 'Install directory: %s\n' "$INSTALL_DIR"
printf 'Data is stored in the Docker volume isponsorblocktv-web_isponsorblocktv-data.\n'
printf 'Manage the service with: cd "%s" && docker compose logs -f\n' "$INSTALL_DIR"
