#!/usr/bin/env python3
"""验证已部署的 Cloudflare Workers 网关基础健康和管理面。

用法：
  WORKERS_BASE_URL=https://example.workers.dev \
  ADMIN_USER=admin ADMIN_PASSWORD=... \
  uv run python scripts/verify_workers.py

可选：
  WORKERS_MODEL=synthetic/demo WORKERS_API_KEY=sk-... 同时验证数据面。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request


def request(
    base: str,
    path: str,
    opener: urllib.request.OpenerDirector,
    *,
    method: str = "GET",
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, object]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with opener.open(req, timeout=90) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = body.decode("utf-8", errors="replace")
        return exc.code, parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("WORKERS_BASE_URL"))
    parser.add_argument("--admin-user", default=os.environ.get("ADMIN_USER"))
    parser.add_argument("--admin-password", default=os.environ.get("ADMIN_PASSWORD"))
    parser.add_argument("--model", default=os.environ.get("WORKERS_MODEL"))
    parser.add_argument("--api-key", default=os.environ.get("WORKERS_API_KEY"))
    args = parser.parse_args()
    if not args.base_url:
        parser.error("--base-url or WORKERS_BASE_URL is required")

    failures: list[str] = []
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    status, health = request(args.base_url, "/api/health", opener)
    if status != 200 or not isinstance(health, dict) or health.get("runtime") != "python-workers":
        failures.append(f"health check failed: {status} {health}")
    else:
        print("ok: health")

    status, bootstrap = request(args.base_url, "/api/ui/bootstrap", opener)
    if status != 200:
        failures.append(f"bootstrap failed: {status} {bootstrap}")
    else:
        print("ok: bootstrap")

    if args.admin_user and args.admin_password:
        status, login = request(
            args.base_url,
            "/api/login",
            opener,
            method="POST",
            payload={"username": args.admin_user, "password": args.admin_password},
        )
        if status != 200:
            failures.append(f"login failed: {status} {login}")
        else:
            print("ok: login")
            for path in (
                "/api/providers",
                "/api/auth-groups",
                "/api/users",
                "/api/api-keys",
                "/api/model-mappings",
                "/api/settings/system",
                "/api/request-logs",
            ):
                status, result = request(args.base_url, path, opener)
                if status != 200:
                    failures.append(f"{path} failed: {status} {result}")
                else:
                    print(f"ok: {path}")

    key_headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    status, models = request(args.base_url, "/v1/models", opener, headers=key_headers)
    if status != 200:
        failures.append(f"/v1/models failed: {status} {models}")
    else:
        print(f"ok: /v1/models ({len(models.get('data', []))} models)")

    if args.model:
        status, result = request(
            args.base_url,
            "/v1/chat/completions",
            opener,
            method="POST",
            payload={"model": args.model, "messages": [{"role": "user", "content": "health check"}]},
            headers=key_headers,
        )
        if status != 200:
            failures.append(f"chat failed: {status} {result}")
        else:
            print("ok: chat completions")

    if failures:
        print("\n".join(f"FAIL: {failure}" for failure in failures), file=sys.stderr)
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
