# Architecture

```
                    ┌──────────────────────────┐
                    │      farm_atria.py       │  CLI: --check / --target / --verify
                    └────────────┬─────────────┘
                                 │
                    ┌────────────▼─────────────┐
                    │   atria_farmer.worker    │  Context, Stats, Cooldown,
                    │      worker_loop()       │  NamePool, N × threads
                    └──┬────┬────┬────┬────┬───┘
                       │    │    │    │    │
        ┌──────────────┘    │    │    │    └──────────────┐
        ▼                   ▼    │    ▼                   ▼
  ┌───────────┐   ┌──────────────┴─────────┐   ┌──────────────┐
  │ captcha   │   │        logto           │   │   router9    │
  │ 2captcha  │   │  registration flow     │   │  (optional)  │
  └───────────┘   └────────────┬───────────┘   └──────────────┘
                               │
                               ▼
                      ┌─────────────────┐        ┌──────────────┐
                      │    mailbox      │◄───────│  store       │
                      │  worker HTTP    │        │ keys.txt     │
                      └────────┬────────┘        │ keys.jsonl   │
                               │                 └──────────────┘
                               ▼
                 ┌──────────────────────────┐
                 │  Cloudflare Email Worker │
                 │   (worker/src/index.js)  │
                 └────────────┬─────────────┘
                              ▼
                        ┌───────────┐
                        │    D1     │
                        └───────────┘
```

---

## Modules

### `atria_farmer/config.py`
Resolves every setting from the environment, with a dependency-free `.env`
loader. `Config.validate()` fails fast on unusable values and collects
*advisory* warnings (missing proxy, wrong captcha region) separately, so a
technically-valid-but-suboptimal setup still runs.

`Config.redacted()` returns a copy with the captcha key and proxy credentials
masked — this is what `--check` prints and what ends up in logs.

### `atria_farmer/captcha.py`
`AliyunSolver` talks to 2captcha's JSON API and blocks until a token is ready.
Two design decisions are load-bearing:

- It **never** routes through the configured proxy. Solving for the `cn` region
  via a European exit node returns `ERROR_CAPTCHA_UNSOLVABLE`.
- `token_from_solution()` serialises the solution **verbatim**. The backend
  rejects any token it did not produce, including one with an extra key.

### `atria_farmer/logto.py`
The registration state machine. `register()` returns
`(session, verification_id)`; `confirm_otp()`, `complete_profile()` and
`create_api_key()` carry on from there.

`_check_rate_limit()` converts a `429` into a `RateLimited` exception carrying
the server's `retryAfter`, which the pool turns into a global cooldown.

Each account gets a **fresh session**, which matters twice over: the captcha
token is bound to the interaction that requested it, and the OTP throttle is
keyed to the session.

### `atria_farmer/mailbox.py`
A thin client for the worker's read API. `fetch()` is one-shot by design — the
worker deletes the row on read — and `wait()` polls until the code arrives.
`recent()` and `raw()` exist purely for debugging a delivery problem.

### `atria_farmer/router9.py`
Optional. Registers each new key as an `openai-compatible` connection in a
local 9router SQLite database. Every failure path is swallowed and logged; a
schema change or a permissions problem degrades to "keys still saved".

### `atria_farmer/store.py`
`ResultStore` appends to `keys.txt` and `keys.jsonl` under a lock, with
`flush()` + `fsync()` after every record — losing a paid-for account to an
untimely kill is worse than the syscall. `load_emails()` powers `--resume`.

### `atria_farmer/worker.py`
The pipeline plus the pool.

- `Stats` — atomic counters and a `done` predicate.
- `Cooldown` — a pool-wide pause engaged on `429`.
- `NamePool` — thread-safe allocator so two threads never pick the same
  address.
- `FarmError` — carries the e-mail address, so the failure log is actionable.
- `worker_loop()` — a safety valve stops the pool after `4 × target` attempts,
  which prevents an infinite spin on a broken configuration.

---

## Data flow for one account

| # | Step | Endpoint | Failure mode |
|---|---|---|---|
| 1 | Solve captcha | 2captcha `createTask` | `CaptchaError` → retry |
| 2 | Seed session | `GET /console` | network |
| 3 | Open interaction | `PUT /api/experience` | `RegistrationError` |
| 4 | Submit token | `POST /api/experience/captcha/verify` | `CaptchaRejected` → retry with a new token |
| 5 | Attach token | `PUT /api/experience` | `RegistrationError` |
| 6 | Request OTP | `POST …/verification-code` | `OtpRequestFailed`, `RateLimited` |
| 7 | Poll mailbox | `GET <worker>/?email=` | `VerificationFailed` |
| 8 | Confirm OTP | `POST …/verification-code/verify` | `VerificationFailed` |
| 9 | Profile + submit | `POST …/profile`, `…/identification`, `…/submit` | `RegistrationError` |
| 10 | Mint key | `POST /api/keys` | `KeyCreationFailed` |
| 11 | Persist | `keys.txt` + `keys.jsonl` | — |
| 12 | Inject (optional) | 9router SQLite | logged, non-fatal |

Steps 1–5 form a retry loop bounded by `ATRIA_CAPTCHA_ATTEMPTS`; everything
else is a single attempt per account.

---

## Concurrency

`--workers N` spawns N daemon threads sharing one `Context`. Contention points
and how they are handled:

| Shared state | Guard |
|---|---|
| Counters | `Stats._lock` |
| Cooldown deadline | `Cooldown._lock` |
| Allocated names | `NamePool._lock` |
| Output files | `ResultStore._lock` |
| 9router database | `Router9._lock` + SQLite `busy_timeout` |
| Shutdown | `threading.Event` |

`SIGINT`/`SIGTERM` set the stop event; threads finish the account they are on
and exit, so no partial record is ever written.

---

## Cost and throughput

| Quantity | Value |
|---|---|
| Captcha solve | 40–50 s wall clock |
| Token acceptance (first try) | ~50% |
| Wall clock per account | ~90–150 s |
| Captcha cost per account | ~$0.004 (two solves) |
| D1 writes per OTP | 1 (100,000/day free) |

Captcha latency dominates. Because it is network-bound and the solver is
stateless, raising `--workers` scales close to linearly until the proxy or the
platform throttles.

Measured on a 5-worker pool: **~50 minutes for 100 accounts**, bottlenecked
entirely by captcha solve time.
