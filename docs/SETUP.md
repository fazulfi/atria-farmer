# Setup

Two things have to exist before the farmer can run: a mailbox worker that can
receive OTP e-mails, and a captcha solver balance. This document walks through
both, plus the exact Cloudflare token permissions.

---

## 1. Cloudflare token

Create an API token at **dash.cloudflare.com → My Profile → API Tokens →
Create Token → Custom token** with these permissions:

| Scope | Resource | Permission |
|---|---|---|
| Account | Workers Scripts | Edit |
| Account | D1 | Edit |
| Zone | Zone | Read |
| Zone | Email Routing Rules | Edit |

> **Note:** the token is *account-scoped*. `GET /user/tokens/verify` will answer
> `Invalid API Token` even when the token is perfectly valid — that endpoint
> requires user-scoped credentials. `worker/deploy.py` validates the token by
> reading the account instead, which is the reliable check.

Set the token and account id in `.env`:

```
CLOUDFLARE_API_TOKEN=…
CLOUDFLARE_ACCOUNT_ID=…
```

---

## 2. Domain and Email Routing

1. Add the domain to Cloudflare and let it become **active**.
2. Go to **Email → Email Routing** and enable it. Cloudflare will offer to add
   the required `MX` and `TXT` records — accept.
3. Pick a domain that is **not** on the platform's e-mail blocklist. The list is
   served with the sign-in page as `emailBlocklistPolicy.customBlocklist`
   (177 entries at the time of writing) and it blocks whole suffixes:

   ```
   @*.biz.id    @*.my.id    @*.web.id    @duckmail.sbs    @catchmail.io   …
   ```

   Common domains such as `gmail.com`, `outlook.com` and plain `.com`/`.dev`
   domains are fine. Check before you commit to one:

   ```bash
   curl -s 'https://auth.atria-asi.ai/sign-in?app_id=bldfnpl1bq5fekc85mcxi' \
     | grep -o '"customBlocklist":\[[^]]*\]'
   ```

---

## 3. Build and deploy the worker

```bash
python worker/build.py            # fetch postal-mime, bundle to worker/dist/
python worker/deploy.py --all     # database + schema + worker + routing
```

`--all` performs, in order:

1. create (or reuse) the D1 database `atria-otp`;
2. apply the `otp` table schema;
3. upload the worker with the D1 binding and enable its `workers.dev` route;
4. point the domain's catch-all Email Routing rule at the worker.

Verify:

```bash
curl 'https://atria-otp.<subdomain>.workers.dev/?email=probe@example.com'
# -> 404 NOT_FOUND      (worker is alive, mailbox empty)

curl 'https://atria-otp.<subdomain>.workers.dev/recent'
# -> []
```

Check the current state at any time:

```bash
python worker/deploy.py --show
```

---

## 4. Captcha solver

Register at [2captcha.com](https://2captcha.com), top up, and copy the API key
into `.env` as `ATRIA_CAPTCHA_KEY`.

Budget roughly **$0.004 per account**: one solve costs about $0.002 and roughly
half of the returned tokens are rejected on the first try, so plan for ~2
solves per account.

Check the balance at any time:

```bash
python farm_atria.py --check
```

---

## 5. Proxy

The OTP endpoint throttles per IP — three or four requests, then a one-hour
lockout. Configure a rotating proxy:

```
ATRIA_PROXY=http://user:password@host:port
```

Any country works. Rotating residential endpoints are ideal because each
request leaves from a different address, so the per-IP counter never builds up.

---

## 6. Validate and run

```bash
python farm_atria.py --check                    # config + connectivity
python farm_atria.py --target 10 --workers 3    # register 10 accounts
```

To resume a previous run without duplicating addresses:

```bash
python farm_atria.py --target 100 --resume
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `422 session.captcha_required` | captcha token missing or sent to the wrong endpoint | the farmer already handles this; if it persists, check `ATRIA_CAPTCHA_REGION=cn` |
| `{"success": false}` from `captcha/verify` | token solved for the wrong region | must be `cn`; `sgp` solves but is rejected |
| `ERROR_CAPTCHA_UNSOLVABLE` | solving through a proxy | the farmer always solves directly — check that `ATRIA_PROXY` is not leaking into the solver |
| `429 request.ip_message_rate_limited` | no proxy, or a shared IP | configure `ATRIA_PROXY` |
| OTP never arrives | domain blocklisted, or routing not pointed at the worker | check the blocklist and `worker/deploy.py --show` |
| `10048 reached the free usage limit` | KV, not D1 | the shipped worker uses D1; make sure you deployed `worker/dist/atria-otp.js` |
| Worker returns `NO_CODE_FOUND` | the e-mail format changed | inspect `GET /raw?email=…` and extend `extractCode()` in `worker/src/index.js` |

---

## Uninstalling

```bash
# remove the worker
curl -X DELETE "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/workers/scripts/atria-otp" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"

# drop the database
curl -X DELETE "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/d1/database/<uuid>" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"
```

Then restore the domain's catch-all rule in the Cloudflare dashboard.
