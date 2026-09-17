# Atria Farmer

Automated account registration for the Atria platform — captcha solving, e-mail
OTP retrieval, and API key minting, end to end.

```
solve Aliyun captcha  ->  register (Logto)  ->  receive OTP  ->  confirm
                      ->  complete profile  ->  mint API key
```

Runs multi-threaded, resumes cleanly after a crash, and is safe to leave
unattended: every credential comes from the environment, nothing is hard-coded,
and no secrets are ever written to the log.

---

## Contents

| Path | Purpose |
|---|---|
| `farm_atria.py` | CLI entry point |
| `atria_farmer/` | The library: config, captcha, registration, mailbox, storage |
| `worker/` | Cloudflare Email Worker that receives and serves OTP e-mails |
| `docs/` | Setup, architecture, and the reverse-engineering notes |
| `.env.example` | Configuration template |

---

## Requirements

- Python 3.10+
- A [2captcha](https://2captcha.com) account with a small balance
- A Cloudflare account with a domain you can receive mail on
- A rotating HTTP proxy (strongly recommended — see below)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## Quick start

**1. Deploy the mailbox worker**

```bash
cp .env.example .env
# fill in CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, ATRIA_CF_DOMAIN,
# ATRIA_CF_API_BASE and ATRIA_CAPTCHA_KEY

python worker/build.py          # bundle the worker
python worker/deploy.py --all   # D1 database + schema + worker + routing
```

`--all` is idempotent — re-run it any time to redeploy.

**2. Validate everything before spending money**

```bash
python farm_atria.py --check
```

This prints the resolved configuration (secrets masked), warns about anything
suspicious, checks that the mailbox worker answers, and confirms your 2captcha
balance.

**3. Register accounts**

```bash
python farm_atria.py --target 10 --workers 3
```

Results land in `keys.txt` (human-readable) and `keys.jsonl` (machine-readable).
Failures are appended to `keys_failures.txt` with the address and the reason.

**4. Smoke-test a key**

```bash
python farm_atria.py --verify atr_xxxxxxxx
```

---

## Configuration

Everything is read from the environment or a local `.env`. See
[`.env.example`](.env.example) for the full list with comments.

| Variable | Required | Notes |
|---|---|---|
| `ATRIA_CF_API_BASE` | yes | Your deployed worker URL |
| `ATRIA_CF_DOMAIN` | yes | Domain whose catch-all delivers to the worker |
| `ATRIA_CAPTCHA_KEY` | yes | 2captcha API key |
| `ATRIA_PROXY` | recommended | `http://user:pass@host:port` |
| `ATRIA_9ROUTER_PASSWORD` | no | Register keys into a local 9router over HTTP |
| `ATRIA_TARGET` / `ATRIA_WORKERS` | no | Defaults: `100` / `5` |

---

## Why a proxy is not optional

The OTP endpoint throttles per IP:

```
HTTP 429  {"code": "request.ip_message_rate_limited", "data": {"retryAfter": 3600}}
```

An unproxied host gets **three or four registrations per hour** before being
locked out. The throttle is keyed to the *session*, so a fresh session per
account — which the farmer does — gets the most out of each address, and a
rotating proxy removes the ceiling entirely.

Any country works; the platform does not geo-block. Residential rotating
endpoints perform best.

---

## Output format

`keys.txt` — one line per account, append-only:

```
2026-09-17 16:56:18 | fz9663961@example.com | atr_02vj0-s51L9… | 100M_TOKENS | 9router:False
```

`keys.jsonl` — the same records as JSON Lines:

```json
{"email": "fz9663961@example.com", "api_key": "atr_…", "created_at": "…", "injected_9router": false, "quota": "100M_TOKENS"}
```

---

## Safety notes

- **Never commit `.env`.** It is listed in `.gitignore`; keep it that way.
- `--check` masks every secret before printing; the run log never contains a key
  in full.
- The mailbox worker deletes an OTP as soon as it is read, so a code cannot be
  replayed.
- Only register accounts you are authorised to create, and respect the target
  platform's terms of service.

---

## Documentation

- [`docs/SETUP.md`](docs/SETUP.md) — step-by-step deployment, including the exact
  Cloudflare token permissions.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module map and data flow.
- [`docs/FINDINGS.md`](docs/FINDINGS.md) — the platform behaviours that the code
  works around, and how each was verified.

---

## Licence

MIT — see [`LICENSE`](LICENSE).
