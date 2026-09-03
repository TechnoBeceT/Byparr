# Repository Instructions

## Project overview

- FastAPI service that mimics the FlareSolverr-style API for bypassing anti-bot pages using Camoufox.
- Entry point: main app in main.py; routes and request flow in src/endpoints.py, challenge handling in src/challenge.py, response bodies in src/content.py, models in src/models.py.
- SessionManager owns retained browser lifecycles; disposable requests use the same BrowserFactory resource boundary without retention.

## Architecture and data flow

- Request flow: POST /v1 -> SessionManager/disposable acquisition -> read_item() -> page.goto() -> detect the interstitial -> ClickSolver interaction -> return LinkResponse. A successful solver return is authoritative even when bootstrap marker markup remains.
- BrowserFactory directly constructs AsyncCamoufox and opens a page-bound ClickSolver as one resource. Every configured proxy fails before Camoufox starts. SessionManager exclusively owns retained resources, serializes use per isolation key, and evicts resources after fatal browser closure.
- Challenge detection requires both playwright_captcha's Cloudflare indicator selectors and the standard interstitial title; after detection, successful completion is the ClickSolver return rather than marker disappearance.
- Challenge-free navigation returns once DOM content is captured; only a detected-and-solved interstitial performs the post-solver network-idle wait.
- Health check hits /v1 internally with <https://google.com> and fails if status is not OK.
- Logging: LogRequest middleware logs only POST /v1 timing and outcome; other paths pass through.

## Key modules and patterns

- Models use Pydantic v2 with camelCase aliasing for responses (see src/models.py).
- LinkResponse.invalid() is the standard error response shape; keep fields consistent with FlareSolverr style.
- BrowserFactory supports direct browser construction and enters ClickSolver for the same resource lifetime.

## Config and environment

- Core env vars in src/consts.py: HOST, PORT, PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD, LOG_LEVEL, VERSION.
- Proxy environment variables and request headers remain parseable for compatibility, but proxy operation is unsupported.
- VERSION strips leading "v" for tag-style values.

## Developer workflows

- Local run: uv sync && uv run main.py
- Init mode: uv run main.py --init (pre-warms health check via browser setup)
- Tests: uv sync --group test && uv run pytest --retries 5
- Docker troubleshooting: docker build --target test .

## Tests and external dependencies

- tests/main_test.py calls real websites; tests are network-dependent and may be skipped based on upstream status.
- HTTP client tests use starlette.testclient + httpx; avoid mocking unless needed for local-only changes.
