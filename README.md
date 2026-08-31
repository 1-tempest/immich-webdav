# immich-webdav

A read-only WebDAV gateway that exposes each Immich user's own albums as a
mountable network drive — built for browsing and pulling full-resolution
photos into a desktop workflow (e.g. mounting on Windows for use with
photo-book software), not as a general Immich client.

Every file it serves is a web-safe image: JPEG/PNG/GIF/BMP/WebP/AVIF
originals pass through untouched, and RAW/HEIC are served as Immich's
full-resolution JPEG/WebP `fullsize` derivative — you never get a `.CR2` or
`.HEIC` your software can't open.

Photos only. Video is explicitly out of scope.

## Why this exists, and why it works the way it does

- **Auth is Immich API keys, not Immich passwords.** If your Immich instance
  uses OIDC with password login disabled, there's no way to
  turn a WebDAV client's username/password into a live Immich session
  headlessly — OIDC requires a real browser redirect, and neither Immich nor
  most identity providers support the Resource Owner Password Credentials
  grant that would let you skip it. So: each user logs into Immich's web UI
  once (via your normal OIDC flow) and creates their own API key under
  Account Settings → API Keys. That key is what goes in the WebDAV client's
  password field. The username field is never checked — put anything in it.

- **No per-user config, and no separate credential check.** The server never
  validates the password on its own — it accepts any non-empty password as an
  API key and lets the first real Immich call decide. Every listing / asset
  fetch already sends the key as `x-api-key`; if Immich returns `401/403` the
  server turns that into a WebDAV `401`, so a bad key fails the mount and a
  revoked key stops working on the next request (no cache, no lag). Whatever
  the key's Immich account can see is exactly what's browsable — Immich's
  permission model does all the scoping. No local user database, no album ID
  allowlist, no username→anything mapping.

- **No shared filesystem mount required.** Unlike WebDAV wrappers that read
  asset bytes straight off Immich's media library volume, this one only
  ever talks to the Immich API. That means the container needs no access to
  your photo library on disk at all — meaningful since this is designed to
  sit on the open internet (behind your own reverse proxy / WAF), not just
  on a trusted LAN.

- **Photos get converted or passed through based on Immich's own
  "web-supported" classification, not a guess.** Immich only ever generates
  a `fullsize` derivative (a full-resolution JPEG/WebP conversion) for
  formats it doesn't consider natively browser-viewable — this is a hard
  gate in Immich's own code
  ([`media.service.ts`](https://github.com/immich-app/immich/blob/v3.1.0/server/src/services/media.service.ts),
  `isGenerateFullsize`), not a preference. `src/immich_webdav/formats.py`
  mirrors that exact set
  ([`mime-types.ts`](https://github.com/immich-app/immich/blob/v3.1.0/server/src/utils/mime-types.ts)),
  pinned to Immich v3.1.0. **If you upgrade Immich across a major version,
  re-check that file for changes before assuming this list is still
  accurate** — it's the one piece of this project most likely to drift out
  from under a server upgrade.

- **`image.fullsize.enabled` must be on in Immich's admin settings.** That
  toggle gates the entire `fullsize` feature — without it, no fullsize
  derivatives exist for anything (except equirectangular/360 panoramas,
  which are unconditional). This can't be checked at runtime: reading
  `/api/system-config` requires an admin-scoped permission that a regular
  user's self-issued API key won't have. Verify it once, by hand, as an
  admin.

- **Listings are served stale-while-revalidate; a browse never waits on
  Immich.** The album list is cached `CACHE_TTL_SECONDS` (default 2s). An
  album's asset list is cached too, but once it's that old the *cached*
  listing is still returned immediately and a **background** task checks the
  album's cheap `(assetCount, updatedAt)` signature
  (`GET /api/albums/{id}?withoutAssets=true`) — the full paginated asset
  list (and its size prefetch) is rebuilt only when that signature actually
  moved. So opening a large album stays snappy instead of blocking ~0.5s
  per request while every page is re-fetched, and a newly added asset shows
  up a poll or two later (WebDAV clients poll continuously). Only the very
  first access to an album after startup blocks. See `SwrCache` in
  `src/immich_webdav/cache.py`.

- **Every asset's exact byte size is resolved when its album is listed, then
  cached for two weeks.** There's no bulk, accurate way to learn it ahead of
  time — the `fullsize` derivative's size isn't in Immich's API at all, and
  `exifInfo.fileSizeInByte` isn't the same number as what's actually streamed,
  so it must *not* be trusted as a `Content-Length`. Instead, when a folder is
  listed, the gateway fires a one-byte range probe per asset
  (`Range: bytes=0-0` → `Content-Range: …/<total>`, run
  `SIZE_PROBE_CONCURRENCY`-wide) and caches each real total — but only when
  it's a positive integer. So a PROPFIND reports a correct
  `getcontentlength` on the very first listing, which is what
  `rclone mount --vfs-cache-mode full` needs (it sizes its download from the
  listing and treats a missing/zero length as an empty file). This is a
  one-time cost per album per two-week window; a warm listing is instant, and
  the cache is written to `CACHE_DIR/asset-sizes.json` so a restart keeps it.
  A `2xx` response from Immich whose declared `Content-Length` is `<= 0`
  (observed for assets with no generated derivative, and transiently) is
  rejected as a failed fetch — a GET of it returns `500` rather than a silent
  0-byte file, and nothing is cached, so the next access retries for real.
  See `size_cache` and `_prefetch_sizes` in `resources.py`.

- **Range requests are proxied to Immich.** A ranged GET is forwarded with
  its `Range` header and answered with `206 Partial Content` +
  `Content-Range`, transferring only the requested bytes (Immich falls back
  to a full `200` for anything it can't satisfy, which the gateway then
  trims). This is what makes chunked/multi-threaded downloaders and
  `rclone mount --vfs-cache-mode full` (which enforces
  declared-size == bytes-received) work correctly against the gateway.

## Configuration

All configuration is environment variables — see `src/immich_webdav/config.py`
for the authoritative list.

| Variable                    | Default   | Meaning                                                                 |
| ---------------------------- | --------- | ------------------------------------------------------------------------ |
| `IMMICH_URL`                 | `http://immich-server:2283` | Where the asset-serving path talks to Immich. May be an internal/Docker-network address. |
| `IMMICH_EXTERNAL_URL`        | *(falls back to `IMMICH_URL`)* | A real, externally-reachable Immich URL. Used **only** by the `/setup` page (browsers and your identity provider's redirect-URI matching need a public address). Set this explicitly once `IMMICH_URL` is internal. |
| `SERVICE_EXTERNAL_URL`       | *(auto-detected)* | This service's own public base URL, e.g. `https://album.example.com`. Used by `/setup` for the OAuth redirect URI and the generated rclone config. Left unset it's derived from `X-Forwarded-Proto` / `Host` — set it if you're not behind a proxy that sets those. |
| `CACHE_DIR`                  | `/cache` | Where the on-disk cache (`asset-sizes.json`) is written. **Leave it alone** — the image sets it. Mount a volume at `/cache` to keep the cache across container recreation (see below). |
| `WEBDAV_HOST`                | `0.0.0.0` | Bind address                                                            |
| `WEBDAV_PORT`                | `1700`    | Bind port                                                               |
| `CACHE_TTL_SECONDS`          | `2.0`     | Album-list TTL; also the stale-while-revalidate recheck interval for an album's asset list (cached listing served immediately, signature rechecked in the background past this age) |
| `SIZE_CACHE_TTL_SECONDS`     | `1209600` (2 weeks) | How long a learned asset byte-size is trusted before re-checking |
| `REQUEST_TIMEOUT_SECONDS`    | `30.0`    | Timeout for Immich listing/content requests                            |
| `REQUEST_MAX_RETRIES`        | `3`       | Retries for retryable listing failures (timeouts, 5xx, 429)             |
| `PAGE_SIZE`                  | `1000`    | Page size for Immich's asset search endpoint (max Immich allows)        |
| `MAX_CONCURRENT_REQUESTS`    | `4`       | Fan-out width when an album needs more than one page of results         |
| `SIZE_PROBE_CONCURRENCY`     | `32`      | Fan-out width for the per-asset byte-size probes done on a cold album listing (`0` disables listing-time probing; sizes are then learned on first GET) |
| `LOG_LEVEL`                  | `INFO`    | Standard Python logging level                                          |

## Running it

This is meant to sit behind a reverse proxy that terminates TLS (Traefik,
Caddy, nginx) — WebDAV is plain HTTP, and Windows' native WebDAV client
refuses to send Basic Auth credentials over an unencrypted connection by
default, so plaintext HTTP isn't a usable deployment target regardless of
whether you're exposing it publicly.

It's meant to be added to your existing Immich Compose stack, so it shares
the network and can reach `immich-server` directly:

```yaml
services:
  immich-webdav:
    image: ghcr.io/1-tempest/immich-webdav:latest
    container_name: immich-webdav
    restart: always
    environment:
      IMMICH_EXTERNAL_URL: https://photos.example.com
      # IMMICH_URL: http://immich-server:2283
      # SERVICE_EXTERNAL_URL: https://album.example.com
    volumes:
      - immich-webdav-cache:/cache
    ports:
      - "1700:1700"   # drop this if your reverse proxy is on the same Docker network

volumes:
  immich-webdav-cache:
```

Only `IMMICH_EXTERNAL_URL` is normally required, and only for the
[setup page](#self-service-setup) — the WebDAV routes work with the defaults
alone.

Point your reverse proxy at port `1700` and terminate TLS there. Keep
upstream keep-alive on and don't buffer whole response bodies — Traefik and
Caddy are fine by default; nginx needs `proxy_http_version 1.1;` and
`proxy_buffering off;`.

### The `/cache` volume

`/cache/asset-sizes.json` holds the per-asset byte sizes the gateway learns by
probing Immich (needed so `rclone --vfs-cache-mode full` gets correct sizes).
It works with or without a volume:

- **No volume:** the file lives in the container layer. Survives
  `docker restart` / `docker compose restart`. Lost when the container is
  *recreated* (image upgrade, `docker rm`, `compose down`) — the gateway then
  re-probes each album's sizes on its first browse (~a few hundred ms per
  album, once, then cached ~2 weeks).
- **Named volume** (`-v immich-webdav-cache:/cache`): survives recreation,
  ownership handled automatically.
- **Bind mount** (`-v ./cache:/cache`): survives recreation, but the container
  runs as uid `10001`, so the host directory must be writable by it:
  ```bash
  mkdir -p ./cache && sudo chown 10001:10001 ./cache
  ```
  Without that the gateway logs a warning and falls back to in-memory (still
  works, just doesn't persist).

Only asset sizes are persisted — the album/asset listing caches are in-memory
only, since listings must reflect current Immich state, not a stale snapshot.

### Getting a credential

**Easiest — the setup page.** Send the user to `https://<your-host>/`. They
pick password or SSO, sign in to Immich once, and the page hands back a
PowerShell script (rclone install + a persistent mount) with a freshly
minted, narrowly-scoped key already baked in. See
[Self-service setup](#self-service-setup) below — it needs one bit of
Immich / identity-provider config first.

**Manual.** Each user:

1. Logs into your Immich web UI as normal (via your OIDC provider).
2. Goes to Account Settings → API Keys → New API Key.
3. Uses that key as the password when mounting the WebDAV share. The
   username can be anything — it isn't checked.

### Mounting on Windows

```
net use Z: https://your-domain/ <api-key> /user:anything
```

or via Explorer: This PC → Map Network Drive → enter the URL, then the API
key as the password when prompted.

## Self-service setup

`GET /` is an unauthenticated HTML page that lets a user provision themselves
without ever seeing the Immich admin UI. It's safe to serve at the root
because WebDAV clients only ever `PROPFIND` / `OPTIONS` the root and `GET`
files below it — a bare `GET /` isn't part of any mount or browse. Its API
lives under `/setup/*`. The flow:

1. The page asks Immich (server-side, via `IMMICH_EXTERNAL_URL`) whether
   password login is enabled and shows the password form only if so. "Log in
   via SSO" is always offered.
2. **Password:** the form posts to the service, which calls Immich's
   `/api/auth/login`. **SSO:** the service does the PKCE dance against Immich's
   `/api/oauth/authorize` → the browser is redirected to your IdP → your IdP
   redirects back to `/setup/callback` → the service finishes the exchange via
   `/api/oauth/callback`.
3. Either way the service uses the resulting session to mint an API key
   scoped to exactly `album.read, asset.read, asset.view, asset.download` —
   nothing broader (the gateway never reads user info).
4. A single-line PowerShell command (rclone download + WinFsp install, remote
   config, a persistent "at log on" mount that runs windowless via
   `wscript.exe`) is rendered inline with the key already in it — one line so
   it pastes into the console and runs atomically. The response is
   `Cache-Control: no-store`; there is no separate URL that serves the key, so
   it only ever appears as the direct result of a just-completed login. An
   **Advanced** section on the page exposes the tweakable mount options
   (drive letter, VFS cache mode/size/age, dir-cache-time, volume label,
   extra flags); each value is whitelist-validated before it's templated in.
   `--read-only` is always on — the gateway is read-only.

All Immich calls happen server-side — browser JS never talks to Immich
directly (it would be blocked cross-origin anyway).

> **⚠️ Admin prerequisite — one-time, and the SSO path will not work without
> it.** Add this service's callback URL —
> `https://<your-service-host>/setup/callback` (e.g.
> `https://album.example.com/setup/callback`) — to the **allowed redirect
> URIs** of the OAuth/OIDC client Immich uses, registered with your identity
> provider. The provider refuses the redirect otherwise, no matter how
> correct the request is. This is separate from Immich's own OAuth config.

Set `IMMICH_EXTERNAL_URL` (and, if you're not behind a proxy that sets
`X-Forwarded-*`, `SERVICE_EXTERNAL_URL`).

**Brute-force protection for `/setup/login`.** It's an unauthenticated proxy
to Immich's `/api/auth/login`, so without help every attempt reaches Immich as
this container's IP. Two mitigations are built in: the real client IP (from
`X-Forwarded-For`) is passed through to Immich so its own login throttle works
per-attacker, and each rejected/throttled attempt logs one line to stdout:

```
WARNING ... setup: login rejected ip=203.0.113.7
WARNING ... setup: login throttled ip=203.0.113.7
```

Point fail2ban / a CrowdSec log acquisition at the container logs and ban on
`setup: login (rejected|throttled) ip=(?P<ip>\S+)`.

## Security notes

- Behind a reverse proxy, put rate limiting / an IPS in front of it
  (Traefik + CrowdSec, fail2ban, etc.) — an internet-facing Basic Auth
  endpoint will get scanned regardless of how strong the credential is.
  Since there's no local credential check, each guessed password costs one
  `GET /api/albums` against Immich, so front-door rate limiting matters.
- `/setup` is intentionally unauthenticated (it's how you *get* a
  credential). It proxies straight to Immich's login endpoints, so Immich's
  own rate limiting / lockout applies — but keep front-door limits on too.
- `dir_browser` is disabled — there's no HTML directory listing exposed,
  only real WebDAV responses.

## What's deliberately not here

- Video support.
