# Byparr

<p align="center">
   <img src="icon/logo-byparr.svg" alt="Byparr logo" width="120" />
</p>

> [!IMPORTANT]
> This software does not **guarantee** (only greatly increases the chance) that any challenge will be bypassed. While this tool passes the initial browser check, Cloudflare and other captcha providers likely require valid network traffic originating from the user’s public IP address to mark a connection as legitimate. If any website does not pass the challenge, please run troubleshooting steps and check if other websites work before you create an GitHub issue.

This is the [TechnoBeceT-maintained fork](https://github.com/TechnoBeceT/Byparr) of
[Byparr](https://github.com/ThePhaseless/Byparr), distributed under the
[GNU GPLv3](LICENSE). It includes local modifications and is not an official
upstream image or endorsement.

## Options

| Environment Variable | Default   | Description                                                                                                                                                       |
| -------------------- | --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `HOST`               | `0.0.0.0` | Host address to bind the server to. Use `0.0.0.0` to bind to all IPv4 interfaces, `::` for all IPv6 interfaces, or `127.0.0.1`/`localhost` for local access only. |
| `PORT`               | `8191`    | Port to bind the server to.                                                                                                                                       |
| `PROXY_SERVER`       | None      | Compatibility-only proxy endpoint in the form `protocol://host:port`.                                                                                              |
| `PROXY_USERNAME`     | None      | Compatibility-only proxy username.                                                                                                                                |
| `PROXY_PASSWORD`     | None      | Compatibility-only proxy password.                                                                                                                                |
| `OWUI_API_KEY`       | None      | Bearer token for `/load` endpoint authentication. Must match `EXTERNAL_WEB_LOADER_API_KEY` in Open WebUI.                                                         |
| `BROWSER_LOCALE`     | `en-US`   | Explicit [BCP-47](https://www.rfc-editor.org/rfc/bcp/bcp47.txt) locale override for direct browser launches.                                                       |
| `SESSION_TTL_SECONDS` | `900` | Idle lifetime for a retained browser session. Minimum `1`. |
| `SESSION_MAX_SESSIONS` | `8` | Maximum retained browser sessions. Minimum `1`. |
| `SESSION_LIFECYCLE_TIMEOUT_SECONDS` | `120` | Deadline in seconds for a retained session's browser lifecycle work (opening, retiring, or waiting for replacement). Minimum `1`. |

#### Proxy compatibility

`PROXY_SERVER`, `PROXY_USERNAME`, and `PROXY_PASSWORD` remain accepted only for
configuration compatibility. Proxied browser launches are unsupported and fail
before browser startup.

#### Browser language

Set `BROWSER_LOCALE` to a [BCP-47](https://www.rfc-editor.org/rfc/bcp/bcp47.txt)
language tag such as `de-DE`, `fr-FR`, `pl-PL`, or `zh-CN` to override the
browser's language and `Accept-Language` header. Direct browser launches default
to `en-US`.

Valid tags are maintained in the [IANA Language Subtag Registry](https://www.iana.org/assignments/language-subtag-registry/language-subtag-registry). For a friendlier list, see [List of ISO 639-1 codes](https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes) (language) combined with an [ISO 3166-1 alpha-2](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2) region code for the full tag, e.g. `pt-BR`.

## Tags

- `v*`/`latest` - Releases published by this fork from version tags
- `sha-...` - Revision-derived mutable tag; use an image digest for an immutable reference

The fork's images are published as `ghcr.io/technobecet/byparr`. The image
contains this repository's GPLv3 `LICENSE`; its OCI source label links to the
[corresponding fork source](https://github.com/TechnoBeceT/Byparr). Pull
requests and `v*` tag pushes build and test; manual dispatch builds, tests,
and publishes. Ordinary branch pushes do not trigger this workflow, and neither
branch pushes nor pull requests publish an image.

## Browser architecture

Each browser resource combines an `AsyncCamoufox` browser context with a
page-bound `ClickSolver`. `BrowserFactory` opens and closes both components as
one lifecycle. Disposable requests close that resource when the response is
complete; named requests place it under `SessionManager`, which serializes use
per isolation key, applies idle and capacity limits, and retires the resource
when its page, context, or browser closes unexpectedly.

Challenge detection and solving are separate checks. Byparr requires both a
dependency-provided Cloudflare marker and the standard interstitial page title,
then delegates the browser interaction to `ClickSolver` and treats its
successful return as authoritative. Bootstrap challenge markup can remain in
the resulting page even after Cloudflare accepts the browser.

Challenge-free pages return after their DOM content is captured. They do not
wait for global network idle, because persistent background requests are not
part of the response contract. A solved interstitial still receives the
post-solver network-idle wait before its response is collected.

## Usage

> [!IMPORTANT]
> Support for NAS devices (like Synology) is minimal. Please report issues, but do not expect it to be fixed quickly. The only ARM device I have is a free Ampere Oracle VM, so I can only test ARM support on that. See [#22](https://github.com/ThePhaseless/Byparr/issues/22) and [#3](https://github.com/ThePhaseless/Byparr/issues/3)

### Docker Compose setup

1. Review settings in `compose.yaml`.
2. Start the service:

```bash
docker compose up -d
```

### Docker install

1. Pull and run the image:

   ```bash
   docker run -p 8191:8191 ghcr.io/technobecet/byparr:latest
   ```

2. Optional: set env vars using `-e` or `--env-file`.

### Local install

1. Install ([or update when Python version changes](https://github.com/astral-sh/uv/issues/17887)) [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. Clone this fork - `git clone https://github.com/TechnoBeceT/Byparr`
3. Run `uv run main.py`
4. Enjoy!

### API Docs

Once running, open:

- `http://localhost:8191/docs`
- `http://localhost:8191/` (redirects to `/docs`)

### Named browser sessions

`request.get` remains disposable by default: omit `session`, or pass an empty
or whitespace-only value, to get a new browser context for that request. Add a
non-blank session name when a site needs cookies or other browser state to
survive across requests:

```bash
curl -X POST http://localhost:8191/v1 \
  -H 'content-type: application/json' \
  -d '{"cmd":"request.get","url":"https://example.com/account","session":"my-account"}'
```

The returned navigation envelope remains FlareSolverr-compatible:

```json
{
  "status": "ok",
  "message": "Success",
  "solution": {
    "url": "https://example.com/account",
    "status": 200,
    "cookies": [],
    "userAgent": "...",
    "headers": {},
    "response": "...",
    "contentType": "text/html"
  },
  "startTimestamp": 0,
  "endTimestamp": 0,
  "version": "..."
}
```

For retained requests, the isolation key is the configured session name and the
target's registrable domain. For example, `reader.example.com` and
`api.example.com` share a named session, while a different registrable domain
does not. Only one request may use an individual key at once; requests for
different keys can proceed concurrently.

Create is a lazy, idempotent declaration: it validates the name but opens no
browser until the first matching `request.get`.

```bash
curl -X POST http://localhost:8191/v1 \
  -H 'content-type: application/json' \
  -d '{"cmd":"sessions.create","session":"my-account"}'
```

```json
{
  "status": "ok",
  "message": "Session created successfully.",
  "session": "my-account",
  "startTimestamp": 0,
  "endTimestamp": 0,
  "version": "..."
}
```

Destroy is also idempotent. It resets every retained domain entry for the exact
normalized configured name:

```bash
curl -X POST http://localhost:8191/v1 \
  -H 'content-type: application/json' \
  -d '{"cmd":"sessions.destroy","session":"my-account"}'
```

```json
{
  "status": "ok",
  "message": "The session has been removed.",
  "startTimestamp": 0,
  "endTimestamp": 0,
  "version": "..."
}
```

Retained sessions consume one browser context per isolation key. Idle entries
are evicted least-recently-used when capacity is needed and expire after
`SESSION_TTL_SECONDS`. If every retained entry is busy, a new key receives
HTTP `503` with `{"detail":"Browser session capacity is unavailable"}`.
Sessions survive requests but never process or container restarts. Session
names, cookie values, and proxy credentials are not persisted, and they are
not logged in plaintext.

### Open WebUI Integration

Byparr can serve as an external web loader for [Open WebUI](https://github.com/open-webui/open-webui), allowing it to fetch web content through Byparr's anti-bot bypassing capabilities.

Configure Open WebUI with these environment variables:

```bash
WEB_LOADER_ENGINE=external
EXTERNAL_WEB_LOADER_URL=http://byparr:8191/load
EXTERNAL_WEB_LOADER_API_KEY=your-secret-key  # Optional, must match OWUI_API_KEY
```

The `/load` endpoint accepts `POST` requests with `{"urls": ["https://..."]}` and returns extracted text content for RAG pipelines.

## Troubleshooting

### Docker troubleshooting

1. Clone repo to the host that has issues with Byparr.
2. Run `docker build --target test .`
3. Depending of the build success:
   1. If run successfully, try updating container or if already on newest stable release create an issue for creating new release with new dependencies
   2. If build fails, try troubleshooting on another host/using other method

#### Proxmox OCI / LXC browser launch errors

If you are running Byparr as an OCI container in Proxmox (or another LXC-based setup) and see a `FileNotFoundError` from `multiprocessing.synchronize`/`camoufox` when processing requests, increase the service's shared memory in `compose.yaml`:

```yaml
services:
  byparr:
    shm_size: 512mb
    stdin_open: true
    tty: true
```

`shm_size: 512mb` is usually enough; `stdin_open` and `tty` are only needed if your orchestrator runs the container without a TTY.

### Local troubleshooting

1. Download [uv](https://docs.astral.sh/uv/getting-started/installation/)
2. Download dependencies using `uv sync --group test`
3. Run tests with `uv run pytest --retries 3` (You can add `-n auto` for parallelization)
4. If you see any `F` character in terminal, that means test failed even after retries.
5. Depending of the test success:
   1. If run successfully, try updating container or if already on newest stable release create an issue for creating new release with new dependencies
   2. If test fails, try troubleshooting on another host/using other method
