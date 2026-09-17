#!/usr/bin/env python3
"""Provision and deploy the Cloudflare side of Atria Farmer.

Creates the D1 database, applies the schema, uploads the Email Worker built by
``build.py``, and points the domain's catch-all Email Routing rule at it.

Everything is idempotent: re-running the script reuses an existing database,
re-uploads the worker, and re-applies the routing rule.

Required environment
--------------------
``CLOUDFLARE_API_TOKEN``
    API token with ``Workers Scripts: Edit``, ``D1: Edit``, ``Zone: Read`` and
    ``Email Routing Rules: Edit`` for the target zone.
``CLOUDFLARE_ACCOUNT_ID``
    Account that owns the zone.
``ATRIA_CF_DOMAIN``
    Domain whose catch-all should deliver to the worker.

Optional
--------
``ATRIA_WORKER_NAME``     worker name (default ``atria-otp``)
``ATRIA_D1_NAME``         database name (default ``atria-otp``)
``ATRIA_CF_FORWARD_TO``   also forward every message to this address
``ATRIA_DIST_FILE``       pre-built bundle (default ``worker/dist/atria-otp.js``)

Usage::

    python worker/build.py
    python worker/deploy.py --all
    python worker/deploy.py --show
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import requests

HERE = Path(__file__).resolve().parent
API = "https://api.cloudflare.com/client/v4"
COMPATIBILITY_DATE = "2024-09-23"

SCHEMA = """
CREATE TABLE IF NOT EXISTS otp (
    email      TEXT PRIMARY KEY,
    code       TEXT NOT NULL,
    subject    TEXT,
    snippet    TEXT,
    created_at INTEGER NOT NULL
);
"""


class CloudflareError(RuntimeError):
    """A Cloudflare API call failed."""


class Cloudflare:
    """Very small wrapper around the Cloudflare REST API."""

    def __init__(self, token: str, account_id: str) -> None:
        self.token = token
        self.account_id = account_id
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    def call(self, method: str, path: str, **kwargs):
        url = f"{API}{path}"
        response = self.session.request(method, url, timeout=120, **kwargs)
        try:
            payload = response.json()
        except ValueError:
            raise CloudflareError(f"{method} {path} -> HTTP {response.status_code} (non-JSON)")

        if not payload.get("success", False):
            errors = payload.get("errors") or [{}]
            first = errors[0]
            raise CloudflareError(
                f"{method} {path} -> {first.get('code')}: {first.get('message')}"
            )
        return payload.get("result")

    # ── account ─────────────────────────────────────────────────────────────
    def verify(self) -> dict:
        """Validate the token.

        The token used here is account-scoped, so ``/user/tokens/verify``
        answers ``Invalid API Token`` even when the token is perfectly valid.
        Reading the account itself is the reliable check.
        """
        return self.call("GET", f"/accounts/{self.account_id}")

    def find_zone(self, domain: str) -> dict:
        zones = self.call("GET", "/zones", params={"per_page": 50})
        for zone in zones:
            if zone["name"] == domain:
                return zone
        raise CloudflareError(
            f"zone {domain!r} not found on this account "
            f"(available: {', '.join(z['name'] for z in zones)})"
        )

    # ── d1 ──────────────────────────────────────────────────────────────────
    def find_database(self, name: str) -> Optional[dict]:
        databases = self.call("GET", f"/accounts/{self.account_id}/d1/database")
        for database in databases:
            if database.get("name") == name:
                return database
        return None

    def create_database(self, name: str) -> dict:
        return self.call(
            "POST", f"/accounts/{self.account_id}/d1/database", json={"name": name}
        )

    def query(self, database_id: str, sql: str, params=None):
        body = {"sql": sql}
        if params:
            body["params"] = params
        return self.call(
            "POST",
            f"/accounts/{self.account_id}/d1/database/{database_id}/query",
            json=body,
        )

    # ── workers ─────────────────────────────────────────────────────────────
    def upload_worker(self, name: str, bundle: str, database_id: str, forward_to: str) -> dict:
        metadata = {
            "main_module": "worker.js",
            "compatibility_date": COMPATIBILITY_DATE,
            "bindings": [
                {"type": "d1", "name": "DB", "id": database_id},
            ],
            "observability": {"enabled": True},
        }
        if forward_to:
            metadata["bindings"].append(
                {"type": "plain_text", "name": "FORWARD_TO", "text": forward_to}
            )

        files = {
            "metadata": (None, json.dumps(metadata), "application/json"),
            "worker.js": ("worker.js", bundle, "application/javascript+module"),
        }
        return self.call(
            "PUT", f"/accounts/{self.account_id}/workers/scripts/{name}", files=files
        )

    def enable_subdomain(self, name: str) -> None:
        self.call(
            "POST",
            f"/accounts/{self.account_id}/workers/scripts/{name}/subdomain",
            json={"enabled": True},
        )

    def workers_subdomain(self) -> Optional[str]:
        result = self.call("GET", f"/accounts/{self.account_id}/workers/subdomain")
        return (result or {}).get("subdomain")

    # ── email routing ───────────────────────────────────────────────────────
    def routing_status(self, zone_id: str) -> dict:
        return self.call("GET", f"/zones/{zone_id}/email/routing") or {}

    def catch_all(self, zone_id: str) -> dict:
        return self.call("GET", f"/zones/{zone_id}/email/routing/rules/catch_all") or {}

    def set_catch_all_worker(self, zone_id: str, worker: str) -> dict:
        return self.call(
            "PUT",
            f"/zones/{zone_id}/email/routing/rules/catch_all",
            json={
                "enabled": True,
                "name": f"{worker} catch-all",
                "matchers": [{"type": "all"}],
                "actions": [{"type": "worker", "value": [worker]}],
            },
        )


# ── environment ──────────────────────────────────────────────────────────────
def load_env() -> dict:
    """Read configuration, allowing ``.env`` to fill in the blanks."""
    env_file = HERE.parent / ".env"
    if env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

    required = {
        "token": "CLOUDFLARE_API_TOKEN",
        "account_id": "CLOUDFLARE_ACCOUNT_ID",
        "domain": "ATRIA_CF_DOMAIN",
    }
    missing = [name for name in required.values() if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            "missing environment variable(s): " + ", ".join(missing) +
            "\nSee docs/SETUP.md for the required token permissions."
        )

    return {
        "token": os.environ["CLOUDFLARE_API_TOKEN"],
        "account_id": os.environ["CLOUDFLARE_ACCOUNT_ID"],
        "domain": os.environ["ATRIA_CF_DOMAIN"],
        "worker": os.environ.get("ATRIA_WORKER_NAME", "atria-otp"),
        "database": os.environ.get("ATRIA_D1_NAME", "atria-otp"),
        "forward_to": os.environ.get("ATRIA_CF_FORWARD_TO", ""),
        "dist": Path(
            os.environ.get("ATRIA_DIST_FILE", str(HERE / "dist" / "atria-otp.js"))
        ),
    }


# ── steps ────────────────────────────────────────────────────────────────────
def step_database(cf: Cloudflare, name: str) -> dict:
    existing = cf.find_database(name)
    if existing:
        print(f"  database        reuse   {name} ({existing['uuid']})")
        return existing
    created = cf.create_database(name)
    print(f"  database        create  {name} ({created['uuid']})")
    return created


def step_schema(cf: Cloudflare, database_id: str) -> None:
    cf.query(database_id, SCHEMA)
    print("  schema          applied otp(email, code, subject, snippet, created_at)")


def step_deploy(cf: Cloudflare, env: dict, database_id: str) -> None:
    if not env["dist"].is_file():
        raise SystemExit(
            f"{env['dist']} not found — run `python worker/build.py` first"
        )
    bundle = env["dist"].read_text(encoding="utf-8")
    cf.upload_worker(env["worker"], bundle, database_id, env["forward_to"])
    print(f"  worker          upload  {env['worker']} ({len(bundle):,} bytes)")

    cf.enable_subdomain(env["worker"])
    subdomain = cf.workers_subdomain()
    if subdomain:
        print(f"  endpoint        live    https://{env['worker']}.{subdomain}.workers.dev")


def step_route(cf: Cloudflare, env: dict, zone: dict) -> None:
    status = cf.routing_status(zone["id"])
    if not status.get("enabled"):
        raise SystemExit(
            f"Email Routing is disabled for {env['domain']} — enable it in the "
            "Cloudflare dashboard first (Email > Email Routing)"
        )
    before = cf.catch_all(zone["id"]).get("actions")
    cf.set_catch_all_worker(zone["id"], env["worker"])
    after = cf.catch_all(zone["id"]).get("actions")
    print(f"  routing         set     {before} -> {after}")


def step_show(cf: Cloudflare, env: dict) -> None:
    account = cf.verify()
    print(f"  account         {account.get('name')} ({cf.account_id})")
    zone = cf.find_zone(env["domain"])
    print(f"  zone            {zone['name']} ({zone['status']})")

    database = cf.find_database(env["database"])
    print(f"  database        {database['uuid'] if database else '(not created)'}")

    scripts = cf.call("GET", f"/accounts/{cf.account_id}/workers/scripts")
    names = ", ".join(script["id"] for script in scripts) or "(none)"
    print(f"  workers         {names}")

    status = cf.routing_status(zone["id"])
    catch = cf.catch_all(zone["id"])
    actions = (catch.get("actions") or [{}])[0]
    print(
        f"  email routing   enabled={status.get('enabled')} "
        f"catch-all={actions.get('type')}:{actions.get('value')}"
    )


# ── main ─────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true", help="database, schema, worker, routing")
    parser.add_argument("--database", action="store_true", help="create the D1 database")
    parser.add_argument("--schema", action="store_true", help="apply the SQL schema")
    parser.add_argument("--deploy", action="store_true", help="upload the worker")
    parser.add_argument("--route", action="store_true", help="point catch-all at the worker")
    parser.add_argument("--show", action="store_true", help="print current state and exit")
    args = parser.parse_args(argv)

    if not any(vars(args).values()):
        parser.print_help()
        return 1

    env = load_env()
    cf = Cloudflare(env["token"], env["account_id"])

    print(f"Cloudflare deployment — {env['domain']} / {env['worker']}")
    try:
        if args.show:
            step_show(cf, env)
            return 0

        cf.verify()
        zone = cf.find_zone(env["domain"])

        database = cf.find_database(env["database"])
        if args.all or args.database:
            database = step_database(cf, env["database"])
        if database is None:
            raise SystemExit(
                f"database {env['database']!r} does not exist — run with --database first"
            )

        if args.all or args.schema:
            step_schema(cf, database["uuid"])
        if args.all or args.deploy:
            step_deploy(cf, env, database["uuid"])
        if args.all or args.route:
            step_route(cf, env, zone)
    except CloudflareError as exc:
        print(f"\nCloudflare API error: {exc}", file=sys.stderr)
        return 1

    print("\nDone. Verify with:")
    print("  curl '<endpoint>/?email=probe@<domain>'    # expect 404 NOT_FOUND")
    print("  curl '<endpoint>/recent'                   # expect []")
    return 0


if __name__ == "__main__":
    sys.exit(main())
