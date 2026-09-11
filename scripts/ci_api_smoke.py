"""Run a process-level smoke test without requiring Redis or the OCR gateway."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from http.client import HTTPConnection
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = int(os.environ.get("CI_API_SMOKE_PORT", "18090"))
TOKEN = "ci-api-token"


def _request(method: str, path: str, *, token: str | None = None) -> tuple[int, bytes]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["X-API-Token"] = token
    connection = HTTPConnection(HOST, PORT, timeout=5)
    try:
        connection.request(
            method, path, body=b"" if method != "GET" else None, headers=headers
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _wait_for_root(process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.communicate(timeout=2)[0]
            raise RuntimeError(f"uvicorn exited before readiness:\n{output}")
        try:
            status, _ = _request("GET", "/")
        except OSError:
            time.sleep(0.2)
            continue
        if status == 200:
            return
        time.sleep(0.2)
    raise TimeoutError("API did not become ready within 30 seconds")


def _expect_status(
    method: str, path: str, expected: int, *, token: str | None = None
) -> bytes:
    status, body = _request(method, path, token=token)
    if status != expected:
        raise AssertionError(
            f"{method} {path}: expected {expected}, got {status}: {body!r}"
        )
    return body


def main() -> None:
    env = os.environ.copy()
    env.update(
        {
            "API_TOKEN": TOKEN,
            "AUTH_HEADER_NAME": "X-API-Token",
            "HOST": HOST,
            "PORT": str(PORT),
            "PYTHONPATH": os.pathsep.join(
                [str(PROJECT_ROOT / "src"), env.get("PYTHONPATH", "")]
            ),
        }
    )
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "contract_review_app.main:app",
        "--host",
        HOST,
        "--port",
        str(PORT),
        "--lifespan",
        "off",
    ]
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        # Do not leave a PIPE unread: structured request logs can fill it and
        # make the server block before the smoke request receives its response.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_root(process)
        root_body = _expect_status("GET", "/", 200)
        # Use a body-less protected route so the smoke test does not need a
        # multipart payload or a live Redis task record.
        _expect_status("GET", "/api/v1/tasks/ci-smoke", 401)
        openapi_body = _expect_status("GET", "/openapi.json", 200)
        openapi = json.loads(openapi_body)
        schemes = openapi.get("components", {}).get("securitySchemes", {})
        api_key_scheme_names = {
            name
            for name, scheme in schemes.items()
            if scheme.get("type") == "apiKey"
            and scheme.get("in") == "header"
            and scheme.get("name") == "X-API-Token"
        }
        if not api_key_scheme_names:
            raise AssertionError("OpenAPI does not expose an X-API-Token header scheme")
        protected_operation = (
            openapi.get("paths", {}).get("/api/v1/contract-review", {}).get("post", {})
        )
        if not any(
            api_key_scheme_names.intersection(requirement)
            for requirement in protected_operation.get("security", [])
        ):
            raise AssertionError(
                "contract review operation is missing API key security"
            )
        print(
            json.dumps(
                {
                    "root_status": 200,
                    "protected_without_token": 401,
                    "openapi_api_key": True,
                    "root_bytes": len(root_body),
                }
            )
        )
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


if __name__ == "__main__":
    main()
