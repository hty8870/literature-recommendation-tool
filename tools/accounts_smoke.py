"""Live two-turn account acceptance against a disposable loopback native Luna trial."""

from __future__ import annotations

import argparse
import json
import secrets
import time
from urllib.parse import urlsplit

import httpx

SEED_QUERY = (
    "在选定的 aBiotech 样例库检索讨论大豆 LEAFY1/LEAFY2 与花器官发育的论文。"
    "请给出一条主要匹配，并说明摘要不能证明的内容。"
)
FOLLOWUP = "不要新增检索。用一句话总结上一轮最相关论文已报告的证据，不延伸到开花时间。"


def require(response: httpx.Response, status: int) -> dict:
    if response.status_code != status:
        try:
            code = response.json().get("code", "unexpected_status")
        except ValueError:
            code = "non_json"
        raise AssertionError(f"Expected {status}; received {response.status_code}: {code}")
    return response.json() if status != 204 else {}


def complete(client: httpx.Client, run: dict, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while run["status"] in {"pending", "running"}:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Run did not finalize: {run['run_id']}")
        time.sleep(2)
        run = require(client.get(f"/api/v1/agent/runs/{run['run_id']}"), 200)
    if run["status"] != "completed" or not run.get("summary") or run.get("model") != "gpt-6-luna":
        raise AssertionError(f"Luna run incomplete: {run['run_id']} {run.get('error', {})}")
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8876")
    parser.add_argument("--disposable", action="store_true", required=True)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    origin = urlsplit(args.base)
    if origin.scheme != "http" or origin.hostname != "127.0.0.1" or origin.path not in ("", "/"):
        raise SystemExit("A fresh loopback-only trial server is required.")
    with (
        httpx.Client(
            base_url=args.base,
            trust_env=False,
            timeout=20,
            headers={"Origin": args.base.rstrip("/")},
        ) as owner,
        httpx.Client(base_url=args.base, trust_env=False, timeout=20) as guest,
    ):
        health = require(guest.get("/healthz"), 200)
        caps = require(guest.get("/api/v1/libraries"), 200)
        assert health["reason_model"] == "gpt-6-luna" and caps["accounts_enabled"]
        assert {entry["library_id"] for entry in caps["libraries"]} >= {
            "europepmc-online",
            "europepmc-snapshot",
            "abiotech-sample",
        }
        username = "acceptance-" + secrets.token_hex(5)
        credential = secrets.token_urlsafe(24)
        start = time.monotonic()
        account = require(
            owner.post(
                "/api/v1/account/register", json={"username": username, "password": credential}
            ),
            201,
        )["account"]
        owner.headers["X-Workspace-Account"] = account["id"]
        first = complete(
            owner,
            require(
                owner.post(
                    "/api/v1/agent/runs",
                    json={
                        "input_type": "abstract",
                        "abstract": SEED_QUERY,
                        "library_ids": ["abiotech-sample"],
                    },
                ),
                202,
            ),
            args.timeout,
        )
        conv_path = f"/api/v1/conversations/{first['conversation_id']}"
        query_path = f"/api/v1/searches/{first['latest_query_id']}"
        settings = require(owner.get(query_path), 200)
        reasons = [
            entry["reason"]
            for entry in settings["candidate_diagnostics"]
            if entry.get("reason") and entry["reason"].get("status") == "generated"
        ]
        assert (
            first["queries"] and reasons and all(item["model"] == "gpt-6-luna" for item in reasons)
        )
        events = owner.get(query_path + "/events")
        assert events.status_code == 200 and "event: " in events.text
        last = require(owner.get(conv_path), 200)
        second = complete(
            owner,
            require(
                owner.post(
                    conv_path + "/turns",
                    json={
                        "message": FOLLOWUP,
                        "active_query_id": first["latest_query_id"],
                        "expected_revision": last["revision"],
                    },
                ),
                202,
            ),
            args.timeout,
        )
        assert second["conversation_id"] == first["conversation_id"]
        require(owner.post("/api/v1/account/logout"), 204)
        owner.headers.pop("X-Workspace-Account")
        require(guest.get(conv_path), 404)
        require(guest.get(query_path + "/events"), 404)
        require(guest.get("/api/v1/conversations"), 401)
        identity = require(
            owner.post(
                "/api/v1/account/login", json={"username": username, "password": credential}
            ),
            200,
        )
        assert identity["account"]["id"] == account["id"]
        saved = require(owner.get(conv_path), 200)
        assert (
            len(saved["messages"]) == 4 and len(saved["runs"]) == 2 and len(saved["artifacts"]) >= 1
        )
        require(owner.post("/api/v1/account/delete", json={"current_password": credential}), 204)
        assert require(owner.get("/api/v1/account/session"), 200) == {"account": None}
        print(
            json.dumps(
                {
                    "model": first["model"],
                    "candidate_reasons": len(reasons),
                    "first_turn_queries": len(first["queries"]),
                    "followup_queries": len(second["queries"]),
                    "messages_after_login": len(saved["messages"]),
                    "history_restored": True,
                    "guest_isolated": True,
                    "test_account_removed": True,
                    "wall_seconds": round(time.monotonic() - start, 2),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
