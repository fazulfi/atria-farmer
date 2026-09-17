#!/usr/bin/env python3
"""Atria Farmer — command-line entry point.

Registers accounts on the Atria platform end to end and stores the resulting
API keys.  Configuration comes from the environment (or a local ``.env``); see
``.env.example`` and ``docs/SETUP.md``.

Typical use::

    cp .env.example .env && $EDITOR .env
    python farm_atria.py --check          # validate config and connectivity
    python farm_atria.py --target 10      # register 10 accounts
    python farm_atria.py --verify atr_…   # smoke-test an existing key
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time

import requests

from atria_farmer import __version__
from atria_farmer import ops
from atria_farmer.captcha import AliyunSolver
from atria_farmer.config import Config, ConfigError
from atria_farmer.logto import check_api_key
from atria_farmer.mailbox import Mailbox
from atria_farmer.router9 import Router9
from atria_farmer.store import ResultStore, summarise
from atria_farmer.worker import Cooldown, Context, NamePool, Stats, worker_loop

BANNER = r"""
   _  _    _         ___
  /_\| |_ _(_)_ __  | __|_ _ _ _ _ __
 / _ \  _| | | '  \ | _|| ' \ '_/ -_)
/_/ \_\__|_|_|_|_|_\|_| |_||_|_\___|   v{version}
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="farm_atria.py",
        description="Register Atria accounts (captcha + e-mail OTP + API key).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Configuration is read from the environment or .env — see .env.example.\n"
            "Nothing is written to disk except the key files."
        ),
    )
    parser.add_argument("--target", type=int, help="total number of accounts wanted")
    parser.add_argument("--workers", type=int, help="parallel worker threads")
    parser.add_argument("--keys-file", help="output file for keys (default keys.txt)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and connectivity, then exit",
    )
    parser.add_argument(
        "--verify",
        metavar="API_KEY",
        help="smoke-test an API key against /v1/models, then exit",
    )
    parser.add_argument(
        "--verify-all",
        action="store_true",
        help="re-test every key in the ledger and report which are dead",
    )
    parser.add_argument(
        "--list",
        dest="list_keys",
        action="store_true",
        help="summarise the ledger, then exit",
    )
    parser.add_argument(
        "--models",
        action="store_true",
        help="list the models available to a key from the ledger",
    )
    parser.add_argument(
        "--check-domain",
        metavar="DOMAIN",
        help="test a domain against the platform's live e-mail blocklist",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip addresses already present in the key file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the resolved plan without registering anything",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="extra logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def parse_args(argv=None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    if not args.verify and not args.check:
        pass  # target/workers are optional; env provides the defaults
    return args


def configure_logging(verbose: bool):
    """Return a thread-safe logger.

    In verbose mode every line is printed.  Otherwise the output keeps the
    lines that carry a result — per-account summaries, creations, failures,
    cooldowns — and drops the step-by-step chatter, which keeps a 5-worker run
    readable.
    """
    lock = threading.Lock()
    interesting = (
        "ready in",
        "failed",
        "rejected",
        "no OTP",
        "created",
        "9router:",
    )

    def log(message: str) -> None:
        if not verbose and message.startswith("[W"):
            # Per-account summary lines look like "[W3] [7/100] …".
            is_summary = "] [" in message
            if not is_summary and not any(token in message for token in interesting):
                return
        stamp = time.strftime("%H:%M:%S")
        with lock:
            print(f"{stamp} {message}", flush=True)

    return log


def cmd_verify(args) -> int:
    print("Checking API key against /v1/models …")
    ok, detail = check_api_key(args.verify)
    print(("  OK   " if ok else "  FAIL ") + detail)
    return 0 if ok else 1


def cmd_check_domain(domain: str) -> int:
    """Test a domain against the platform's live blocklist.

    Worth running before committing to a domain: a blocklisted address is only
    refused at OTP time, after a captcha has already been paid for.
    """
    print(f"Checking {domain} against the platform blocklist …")
    try:
        blocklist = ops.fetch_blocklist()
    except ops.BlocklistError as exc:
        print(f"  could not read the blocklist: {exc}", file=sys.stderr)
        return 1

    entry = ops.match_blocklist(domain, blocklist)
    if entry:
        print(f"  BLOCKED — matched {entry!r}")
        print(f"  ({len(blocklist)} entries in the list)")
        print("  Registration from this domain is refused with")
        print("  'session.email_blocklist.email_not_allowed'.")
        return 1

    print(f"  OK — not on the list ({len(blocklist)} entries checked)")
    return 0


def cmd_list(store: ResultStore) -> int:
    records = store.load_records()
    if not records:
        print(f"No keys recorded in {store.text_path}")
        return 1

    domains = {}
    for email, _ in records:
        domain = email.rsplit("@", 1)[-1].lower()
        domains[domain] = domains.get(domain, 0) + 1

    print(f"ledger      : {store.text_path}")
    print(f"jsonl       : {store.json_path}")
    print(f"accounts    : {len(records)}")
    print(f"quota total : {len(records) * 100:,}M tokens")
    print("domains     :")
    for domain, count in sorted(domains.items(), key=lambda kv: -kv[1]):
        print(f"  {domain:<34} {count}")
    print("most recent :")
    for email, key in records[-5:]:
        print(f"  {email:<40} {key[:20]}…")
    return 0


def cmd_models(store: ResultStore, conf: Config) -> int:
    records = store.load_records()
    if not records:
        print(f"No keys in {store.text_path}", file=sys.stderr)
        return 1
    email, key = records[-1]
    print(f"Scanning models with {email} …")
    try:
        models = ops.scan_models(key, timeout=conf.http_timeout, proxies=conf.proxies)
    except RuntimeError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1
    for model in models:
        print(f"  {model}")
    print(f"  ({len(models)} model(s))")
    return 0


def cmd_verify_all(store: ResultStore, conf: Config, log) -> int:
    records = store.load_records()
    if not records:
        print(f"No keys in {store.text_path}", file=sys.stderr)
        return 1

    workers = min(8, len(records))
    print(f"Verifying {len(records)} key(s) with {workers} worker(s) …\n")
    results = ops.verify_many(
        records,
        workers=workers,
        timeout=conf.http_timeout,
        proxies=conf.proxies,
        log=log,
    )
    print(ops.summarise_verification(results))

    dead = [item for item in results if not item.alive]
    if dead:
        path = store.text_path.with_name("keys_dead.txt")
        with open(path, "a", encoding="utf-8") as handle:
            for item in dead:
                handle.write(f"{item.email} | {item.api_key} | {item.detail}\n")
        print(f"\n  dead keys appended to {path}")
    return 0


def preflight(conf: Config, log) -> bool:
    """Validate everything that can be validated without spending money."""
    healthy = True

    print("\n-- configuration " + "-" * 56)
    for key, value in conf.redacted().items():
        print(f"   {key:<18} {value}")

    if conf.warnings:
        print("\n-- warnings " + "-" * 59)
        for warning in conf.warnings:
            print(f"   ! {warning}")

    print("\n-- connectivity " + "-" * 55)
    mailbox = Mailbox(conf.cf_api_base, timeout=20, session=_session(conf))
    if mailbox.health():
        print(f"   mailbox worker     OK   {conf.cf_api_base}")
    else:
        print(f"   mailbox worker     FAIL {conf.cf_api_base}")
        healthy = False

    solver = AliyunSolver(
        conf.captcha_key,
        scene_id=conf.captcha_scene,
        prefix=conf.captcha_prefix,
        region=conf.captcha_region,
        website_url=conf.signin_url,
        lib_url=conf.captcha_lib,
        user_agent=conf.user_agent,
    )
    balance = solver.balance()
    if balance is None:
        print("   captcha account    FAIL (key rejected or API unreachable)")
        healthy = False
    else:
        print(f"   captcha account    OK   balance ${balance:.2f}")
        if balance < conf.target * 0.01:
            print(f"   ! balance may be low for {conf.target} accounts (~$0.004 each)")

    print(f"   sign-in endpoint   {conf.signin_url}")

    # The domain is refused at OTP time if it is blocklisted, which costs a
    # captcha solve to discover.  Check it up front instead.
    try:
        blocklist = ops.fetch_blocklist(user_agent=conf.user_agent)
        entry = ops.match_blocklist(conf.cf_domain, blocklist)
        if entry:
            print(f"   e-mail domain      FAIL {conf.cf_domain} is BLOCKED (matched {entry!r})")
            healthy = False
        else:
            print(f"   e-mail domain      OK   {conf.cf_domain} is not blocklisted")
    except ops.BlocklistError as exc:
        print(f"   e-mail domain      ?    could not read the blocklist ({exc})")

    if conf.ninerouter_password:
        router = Router9(
            base_url=conf.ninerouter_url,
            password=conf.ninerouter_password,
            node_name=conf.ninerouter_node,
        )
        if router.login():
            node = router.ensure_node()
            print(f"   9router            OK   {router.describe()}")
            if node:
                print(f"   registered keys    {router.count()}")
        else:
            print(f"   9router            FAIL {router.describe()}")
            healthy = False

    print("-" * 72)
    return healthy


def _session(conf: Config) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": conf.user_agent})
    return session


def run(conf: Config, args, log) -> int:
    store = ResultStore(conf.keys_file)
    failure_log = f"{conf.keys_file.rsplit('.', 1)[0]}_failures.txt"

    taken = store.load_emails() if args.resume else set()
    if taken:
        log(f"resuming — {len(taken)} address(es) already recorded")

    # ``--target`` is the total number of accounts wanted, not the number to
    # add this run.  Without this, resuming a finished ledger keeps creating
    # fresh accounts and quietly overspends on captcha solves.
    remaining = conf.target - len(taken)
    if args.resume and remaining <= 0:
        log(f"already at {len(taken)} account(s); target {conf.target} reached — nothing to do")
        return 0
    if args.resume:
        conf.target = remaining
        log(f"will create {remaining} more to reach {len(taken) + remaining}")

    router = Router9(
        base_url=conf.ninerouter_url,
        password=conf.ninerouter_password,
        node_name=conf.ninerouter_node,
        log=log,
    )
    if conf.ninerouter_password:
        if router.login():
            node = router.ensure_node()
            if node:
                log(f"9router injection enabled ({router.describe()}, {router.count()} key(s) present)")
            else:
                log("9router reachable but the provider node could not be prepared")
        else:
            log(f"9router injection unavailable ({router.describe()})")

    stop_event = threading.Event()
    stats = Stats(target=conf.target)
    cooldown = Cooldown(log)

    ctx = Context(
        config=conf,
        solver=AliyunSolver(
            conf.captcha_key,
            scene_id=conf.captcha_scene,
            prefix=conf.captcha_prefix,
            region=conf.captcha_region,
            website_url=conf.signin_url,
            lib_url=conf.captcha_lib,
            user_agent=conf.user_agent,
        ),
        mailbox=Mailbox(conf.cf_api_base, timeout=20, session=_session(conf)),
        router=router,
        store=store,
        stats=stats,
        names=NamePool(taken),
        stop_event=stop_event,
        cooldown=cooldown,
        log=log,
    )

    def handle_signal(signum, _frame):
        log(f"signal {signum} received — finishing in-flight work, then stopping")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    print("\n-- run " + "-" * 64)
    print(f"   target  : {conf.target} account(s) with {conf.workers} worker(s)")
    print(f"   output  : {store.describe()}")
    print(f"   failures: {failure_log}")
    print("-" * 72 + "\n")

    threads = [
        threading.Thread(
            target=worker_loop,
            args=(ctx, wid, failure_log),
            name=f"worker-{wid}",
            daemon=True,
        )
        for wid in range(1, conf.workers + 1)
    ]
    for index, thread in enumerate(threads):
        thread.start()
        time.sleep(0.3 if index else 0)

    try:
        while not stop_event.is_set() and not stats.done:
            time.sleep(1)
            done, failed, attempts = stats.snapshot()
            if conf.verbose:
                print(
                    f"      … {done} ok / {failed} failed / {attempts} attempts "
                    f"({stats.elapsed:.0f}s)",
                    flush=True,
                )
    except KeyboardInterrupt:
        stop_event.set()

    stop_event.set()
    for thread in threads:
        thread.join(timeout=5)

    succeeded, failed, _ = stats.snapshot()
    print(summarise(succeeded, failed, stats.elapsed))
    print(f"  keys written to {store.text_path}")
    return 0 if succeeded else 1


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.verify:
        return cmd_verify(args)

    if args.check_domain:
        return cmd_check_domain(args.check_domain)

    try:
        conf = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.target is not None:
        conf.target = args.target
    if args.workers is not None:
        conf.workers = args.workers
    if args.keys_file:
        conf.keys_file = args.keys_file
    if args.verbose:
        conf.verbose = True
    conf.validate()

    log = configure_logging(conf.verbose)
    print(BANNER.format(version=__version__))

    store = ResultStore(conf.keys_file)

    # Ledger commands never register anything, so they skip preflight.
    if args.list_keys:
        return cmd_list(store)
    if args.models:
        return cmd_models(store, conf)
    if args.verify_all:
        return cmd_verify_all(store, conf, log)

    if args.check:
        return 0 if preflight(conf, log) else 1

    if not preflight(conf, log):
        print("\nPreflight failed — fix the items above before running.", file=sys.stderr)
        return 2

    if args.dry_run:
        print("\nDry run — nothing was registered.")
        return 0

    return run(conf, args, log)


if __name__ == "__main__":
    sys.exit(main())
