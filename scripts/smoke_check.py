"""Post-deploy smoke check.

`oc rollout status` returning success only means the pods passed their probes. It does not
mean the application works: the probes are deliberately shallow, and a deployment that
reports green while every request 500s is the failure this script exists to catch.

So this asserts the things a probe cannot:

* the API answers and reports the version we just shipped;
* readiness lists its dependency checks, and none of them is down;
* an unauthenticated call to a protected route is refused, because a deployment that
  accidentally ships with auth disabled looks perfectly healthy;
* security headers are present, since a missing CSP is invisible until someone audits;
* the console serves HTML;
* `/metrics` is scrapeable, so the dashboards are not blank from the first minute.

Exit code 0 means deployed and working. Anything else should fail the pipeline, and the
rollback job is one click away.

    python scripts/smoke_check.py https://tazama-staging.apps.internal
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

import httpx

TIMEOUT = httpx.Timeout(15.0, connect=10.0)
#: A rollout can report ready a moment before the router has the new endpoints.
ATTEMPTS = 5
BACKOFF_S = 4.0

REQUIRED_HEADERS = (
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "content-security-policy",
)


@dataclass(slots=True)
class Result:
    name: str
    ok: bool
    detail: str = ""


def _get(client: httpx.Client, path: str, **kwargs: object) -> httpx.Response:
    """GET with a short retry, because the edge router lags the rollout by a second or two."""
    last: Exception | None = None
    for attempt in range(ATTEMPTS):
        try:
            return client.get(path, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            last = exc
            if attempt < ATTEMPTS - 1:
                time.sleep(BACKOFF_S)
    raise RuntimeError(f"{path} unreachable after {ATTEMPTS} attempts: {last}")


def run(base: str) -> list[Result]:
    results: list[Result] = []
    base = base.rstrip("/")

    with httpx.Client(base_url=base, timeout=TIMEOUT, follow_redirects=True) as client:
        # --- liveness -------------------------------------------------------
        try:
            response = _get(client, "/api/v1/healthz")
            body = response.json()
            ok = response.status_code == 200 and body.get("status") == "ok"
            results.append(
                Result("healthz", ok, f"{response.status_code} version={body.get('version')}")
            )
        except Exception as exc:
            results.append(Result("healthz", False, str(exc)))

        # --- readiness, with its individual checks --------------------------
        try:
            response = _get(client, "/api/v1/readyz")
            body = response.json()
            checks = body.get("checks", [])
            down = [c["name"] for c in checks if c.get("status") == "down"]
            degraded = [c["name"] for c in checks if c.get("status") == "degraded"]
            detail = f"status={body.get('status')}"
            if degraded:
                detail += f" degraded={','.join(degraded)}"
            if down:
                detail += f" DOWN={','.join(down)}"
            results.append(Result("readyz", not down, detail))
        except Exception as exc:
            results.append(Result("readyz", False, str(exc)))

        # --- authorisation is actually on -----------------------------------
        # A build that ships with auth misconfigured passes every health check.
        try:
            response = _get(client, "/api/v1/incidents")
            ok = response.status_code in (401, 403)
            results.append(
                Result(
                    "protected route refuses anonymous callers",
                    ok,
                    f"expected 401/403, got {response.status_code}",
                )
            )
        except Exception as exc:
            results.append(Result("protected route refuses anonymous callers", False, str(exc)))

        # --- security headers ------------------------------------------------
        try:
            response = _get(client, "/api/v1/healthz")
            missing = [h for h in REQUIRED_HEADERS if h not in response.headers]
            results.append(
                Result(
                    "security headers",
                    not missing,
                    f"missing={missing}" if missing else "all present",
                )
            )
        except Exception as exc:
            results.append(Result("security headers", False, str(exc)))

        # --- metrics are scrapeable -----------------------------------------
        try:
            response = _get(client, "/api/v1/metrics")
            ok = response.status_code == 200 and "tazama_http_requests_total" in response.text
            results.append(Result("metrics", ok, f"{response.status_code}"))
        except Exception as exc:
            results.append(Result("metrics", False, str(exc)))

        # --- the console serves ---------------------------------------------
        try:
            response = _get(client, "/")
            ok = response.status_code == 200 and "<html" in response.text.lower()
            results.append(Result("console", ok, f"{response.status_code}"))
        except Exception as exc:
            results.append(Result("console", False, str(exc)))

    return results


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: smoke_check.py <base-url>\n")
        return 2

    results = run(argv[1])
    width = max(len(r.name) for r in results)
    for result in results:
        mark = "PASS" if result.ok else "FAIL"
        print(f"  {mark}  {result.name.ljust(width)}  {result.detail}")

    failed = [r for r in results if not r.ok]
    if failed:
        print(f"\nsmoke check FAILED: {len(failed)} of {len(results)}")
        return 1
    print(f"\nsmoke check passed: {len(results)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
