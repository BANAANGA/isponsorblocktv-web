# iSponsorBlockTV Web

An authenticated web management interface that runs the upstream
[iSponsorBlockTV](https://github.com/dmunozv04/iSponsorBlockTV) service and
keeps its configuration and logs in one place. The upstream service remains
responsible for playback monitoring, SponsorBlock segment handling, ad muting
and skipping, and device communication.

## Install with Docker Compose

### One-line install

On a Linux host with Docker Engine and the Docker Compose plugin installed and
running, run:

```sh
curl -fsSL https://raw.githubusercontent.com/BANAANGA/isponsorblocktv-web/main/install.sh | bash
```

The installer clones the app into
`~/.local/share/isponsorblocktv-web`, prompts you to create a username and
password, builds and starts the container, and keeps its configuration and logs
in a Docker volume. The password prompt is hidden; use 12-128 letters, numbers,
underscores, or hyphens. Then open `http://localhost:8000` and sign in.

To install into a different directory, set `ISPBTV_INSTALL_DIR` before running
the installer:

```sh
curl -fsSL https://raw.githubusercontent.com/BANAANGA/isponsorblocktv-web/main/install.sh \
  | ISPBTV_INSTALL_DIR="$HOME/apps/isponsorblocktv-web" bash
```

### Requirements and manual install

- Docker Engine with the Docker Compose plugin, running under your user.
- A computer or server that can reach the internet and, for automatic device
  discovery, the same local network as the TVs.

To install manually, run these commands from a terminal:

```sh
git clone https://github.com/BANAANGA/isponsorblocktv-web.git
cd isponsorblocktv-web
read -r -p "Username: " WEB_USERNAME
read -r -s -p "Password: " WEB_PASSWORD; printf '\n'
export WEB_USERNAME WEB_PASSWORD
docker compose up --build -d
```

The installer above is recommended: it validates the credentials and saves them
in a protected `.env` file so they persist across shell sessions and restarts.

Open `http://localhost:8000` on the host running Docker and sign in with the
credentials you configured. To sign in from another computer on the network,
use `http://<server-address>:8000`.

Compose stores the service configuration, logs, and generated session-signing
key in the persistent `isponsorblocktv-data` volume. Keep that volume when
updating or recreating the container. Back it up with your normal Docker volume
backup process.

If deploying behind an HTTPS reverse proxy, set `COOKIE_SECURE=true` in the
service's environment so the session cookie is only sent over HTTPS. Do not
expose the service to the public internet without HTTPS and a strong password.
For example, create a `.env` file next to `compose.yaml` and put your chosen
values in it:

```dotenv
WEB_USERNAME=admin
WEB_PASSWORD=replace-this-with-a-long-unique-password
COOKIE_SECURE=true
```

Restrict access to this file:

```sh
chmod 600 .env
```

Then start Compose with `docker compose up --build -d`; Compose loads `.env`
automatically.

### Local-network device discovery

Automatic discovery uses DIAL and Chromecast and requires local-network access.
On Linux, Docker deployments that need discovery can use host networking:
remove the `ports` section from the service in `compose.yaml` and add
`network_mode: host`. The web interface will then be available at
`http://<server-address>:8000`. Pairing with a YouTube TV pairing code works
without host networking.

### Update, inspect, and stop

Rebuild and restart after pulling a new version:

```sh
git pull
docker compose up --build -d
```

Inspect container output or stop the service:

```sh
docker compose logs -f
docker compose down
```

`docker compose down` keeps the named data volume. Do not use
`docker compose down --volumes` unless you intend to delete configuration and
saved logs.

## Usage

1. Sign in to the web manager.
2. Add a TV device. Choose **Pair device** and enter the 12-digit code shown
   in the YouTube app's **Settings → Link with TV code**, or choose
   **Discover devices** when Docker has local-network access.
3. Review device names and skip offsets. Configure SponsorBlock categories,
   minimum segment length, ad muting/skipping, autoplay, and other options.
4. Optionally add a YouTube Data API key and whitelist channels. A key is
   required to search for channels and use the whitelist; custom channel IDs
   can also be entered directly.
5. Choose **Save settings**. The service starts automatically when a device
   is paired; use the service controls to start or stop it later.
6. Open **Logs** to inspect the upstream service output. Logs are retained in
   the data volume and rotated after the active log file reaches 5 MB.

## Features

- Single-admin sign-in with signed, HTTP-only session cookies, CSRF protection
  for changes, and login rate limiting.
- Pairing-code setup, optional local-network discovery, and management of
  multiple TV devices and per-device skip offsets.
- All upstream configuration options: SponsorBlock categories, minimum skip
  length, skip-count reporting, native ad mute/skip, autoplay, join name,
  YouTube channel whitelist and API key, proxy setting, and SponsorBlock API
  URL, plus upstream debug logging and HTTP tracing switches.
- Start/stop controls and a live-refreshing log viewer for the upstream
  service, with log persistence and rotation.
- Video history with direct YouTube links and statistics for watched videos,
  skipped segments by category and device, skip events, and estimated time
  saved overall and over the last 30 days.
- Activity is recorded from the time this version is installed; the upstream
  service does not provide older playback history to backfill.
- Persistent upstream configuration in `/data/config.json`, web-manager
  settings in `/data/webui-settings.json`, and activity in
  `/data/activity.sqlite3`.

The web manager is licensed under GPL-3.0 and uses the GPL-3.0 upstream
iSponsorBlockTV package. Upstream code is installed as a pinned dependency, not
copied into this repository.
