# Findings

Everything the implementation works around, and how each item was verified.
All of it was established by observing live traffic — none of it is guesswork.

---

## 1. The captcha region is `cn`, not `sgp`

The sign-in page is a Logto deployment that loads Aliyun Captcha 2.0. The
tenant configuration is embedded in the JS bundle:

```js
window.AliyunCaptchaConfig = { region: "cn", prefix: C.prefix }
```

| Region | 2captcha result | Backend verdict |
|---|---|---|
| `cn` | solved, 40–50 s | **accepted** — `{"success": true}` |
| `sgp` | solved, ~40 s | **rejected** — `{"success": false}` |
| `intl` | `ERROR_CAPTCHA_UNSOLVABLE` | — |

`sgp` is the expensive trap: the token looks perfectly valid, 2captcha reports
success, and the backend refuses it anyway. It is easy to conclude "2captcha
cannot solve this scene" when the real problem is the region.

> Verified by solving the same scene for both regions and posting each token to
> the verification endpoint.

---

## 2. The token must be sent unmodified

The bundle contains a guard that rejects tokens carrying a `failover` field:

```js
U5 = t => {
  const n = JSON.parse(t);
  return "failover" in n && n.failover === "T";   // -> reject
}
```

Aliyun's own documentation agrees: *"Never alter CaptchaVerifyParam — any
modification causes a verification error."*

Serialise the solution object exactly as 2captcha returned it. No re-ordering,
no added keys, no pretty-printing.

---

## 3. Captcha verification is a separate endpoint

The token does **not** belong on the verification-code request:

```
POST /api/experience/verification/verification-code   {captchaToken}  ->  422 session.captcha_required
```

The working sequence is:

```
POST /api/experience/captcha/verify   {"captchaToken": "<json string>"}   -> {"success": true}
PUT  /api/experience                  {"interactionEvent": "Register",
                                       "captchaToken": "<json string>"}   -> 204
POST /api/experience/verification/verification-code
     {"interactionEvent": "Register",
      "identifier": {"type": "email", "value": "…"}}                      -> {"verificationId": "…"}
```

The endpoint names come straight from the bundle (`te.captchaVerification` and
`Vt = (t, n) => PUT prefix {interactionEvent: t, captchaToken: n}`).

---

## 4. Solve without a proxy; register through one

Solving for the `cn` region through a UK or EU exit node fails with
`ERROR_CAPTCHA_UNSOLVABLE`. Registration traffic, by contrast, *needs* a proxy
(see §5). The two paths are therefore deliberately separated: the solver talks
to 2captcha directly, the registration session uses `ATRIA_PROXY`.

The different exit addresses do not matter — the backend never compares them.

---

## 5. The OTP endpoint throttles, and the counter is per session

```
HTTP 429
{"code": "request.ip_message_rate_limited", "data": {"retryAfter": 3600}}
```

Measured behaviour:

| Attempt pattern | Result |
|---|---|
| One session, repeated requests | 3 requests, then `429` for an hour |
| Fresh session per request, rotating proxy | 8/8 and 10/10 succeeded, no `429` |

So the throttle is keyed to the interaction rather than purely to the IP, and a
fresh session per account is what actually keeps a run moving. A rotating proxy
removes the ceiling; without one, an unproxied host manages three or four
registrations per hour.

> Twelve countries were tested against `/console` and `PUT /api/experience`:
> all returned `200` and `204`. There is no geo-blocking.

---

## 6. The e-mail domain must avoid the blocklist

The sign-in payload includes an `emailBlocklistPolicy` with 177 entries,
including wildcard suffixes:

```
@*.biz.id     @*.my.id     @*.web.id     @duckmail.sbs     @catchmail.io     …
```

Any address on a matching domain is refused before the OTP is sent:

```
HTTP 422
{"code": "session.email_blocklist.email_not_allowed",
 "message": "The email address \"…@uberip.com\" is restricted."}
```

Note the timing: the refusal happens **after** the captcha has been solved, so
a blocklisted domain costs a paid captcha solve to discover. Always check the
domain first:

```bash
python farm_atria.py --check-domain example.com
```

Public temp-mail services are the usual casualties — `mail.tm` hands out
`uberip.com`, which is on the list, so its entire temp-email approach is
unusable here.

Query the live list directly with:

```bash
curl -s 'https://auth.atria-asi.ai/sign-in?app_id=bldfnpl1bq5fekc85mcxi' \
  | grep -o '"customBlocklist":\[[^]]*\]'
```

---

## 7. Token acceptance is probabilistic — retry

Roughly half of the structurally valid tokens are refused on the first attempt.
This is not a solver defect: the same token shape succeeds moments later.

Measured over repeated trials: ~50% first-attempt acceptance, and the failure
looks identical to a genuine rejection (`{"success": false}`, HTTP 200).

The consequence for the design: the captcha → interaction → verify sequence
must be a retry loop, and **every retry needs a new session** because the token
is bound to the interaction that requested it.

---

## 8. Account-scoped Cloudflare tokens fail `tokens/verify`

A Cloudflare token restricted to a single account answers:

```
GET /user/tokens/verify  ->  1000 Invalid API Token
GET /user                ->  9109 Valid user-level authentication not found
```

…while every account-scoped endpoint works normally. Validate such a token by
reading the account instead:

```
GET /accounts/{account_id}   ->  success
```

This cost real debugging time; the token was valid the whole time.

---

## 9. Use D1, not KV

Cloudflare's free KV tier allows **1,000 writes per day**. A worker that stores
one row per inbound message exhausts that quickly, and the failure surfaces as:

```
10048: your account has reached the free usage limit for this operation for today
```

D1 allows **100,000 writes per day** on the free plan and is a better fit
anyway: the mailbox is a small relational table, and the one-shot read is a
clean `DELETE … WHERE`.

> An earlier version of the worker also wrote a second `debug_` row per message,
> halving the effective quota. The shipped worker writes once.

---

## 10. Email Routing accepts exactly one action per rule

Attempting to combine delivery to a worker *and* forwarding in a single
catch-all rule is rejected:

```
PUT /zones/{id}/email/routing/rules/catch_all
{"actions": [{"type": "worker", …}, {"type": "forward", …}]}
-> 10000 Authentication error / invalid action combination
```

Use one action. If a backup copy is wanted, forward from inside the worker
instead — which is what the `FORWARD_TO` binding does.

---

## 11. Email Routing writes need an explicit permission

A token with `Zone: Read` can inspect routing rules but not modify them:

```
PUT  /zones/{id}/email/routing/rules/catch_all   ->  10000 Authentication error
POST /zones/{id}/email/routing                   ->  10405 Method not allowed for this authentication scheme
```

Adding **`Email Routing Rules: Edit`** resolves it. The error message points at
authentication rather than authorisation, which is misleading.

---

## 12. Extracting the code from the e-mail

The worker tries four patterns, most specific first:

```js
/(?:verification|verify|confirmation|security|login|one[\s-]?time|otp|kode|verifikasi)\s*code[^0-9]{0,40}(\d{4,8})/i
/otp-code[^>]*>\s*(\d{4,8})\s*</i
// subject: /\b(\d{4,8})\b/
// body fallback: first plausible \b(\d{4,8})\b
```

The last fallback is the risky one — an e-mail footer full of numbers will fool
it. It skips a junk list (`181818`, `666666`, `000000`, …) and zero, and
`GET /raw?email=…` exists so the stored snippet can be inspected whenever a
delivery looks wrong.

---

## 13. 9router rejects its own session cookie over HTTP

The 9router API issues `auth_token` with the `Secure` attribute. Over plain
HTTP that is a silent trap:

```
POST /api/auth/login   -> 200 {"success": true}   + Set-Cookie: auth_token=…; Path=/
GET  /api/providers    -> 401 {"error": "Unauthorized"}      # cookie withheld
GET  /api/providers    -> 200 {"connections": […] }          # cookie passed explicitly
```

`requests` honours the `Secure` flag and simply does not send the cookie back,
so the login appears to succeed and every subsequent call looks unauthorised.
Pass the token explicitly (`cookies={"auth_token": …}`) and it works.

The API also never echoes stored API keys back, so a client cannot detect
duplicates by key — match on the connection name instead.

Relevant endpoints:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/auth/login` | `{password}` → sets `auth_token` |
| `GET` | `/api/provider-nodes` | list provider nodes |
| `POST` | `/api/provider-nodes` | `{name, prefix, baseUrl, apiType}` |
| `GET` | `/api/providers` | list connections |
| `POST` | `/api/providers` | `{provider, apiKey, name}` → 201 |
| `DELETE` | `/api/providers/{id}` | remove a connection |
| `POST` | `/api/providers/validate` | `{provider, apiKey}` → `{valid, error}` |

---

## Reproducing the checks

The probes that established the above are not shipped — they are one-off
scripts. To re-derive any of it, the fastest route is:

```bash
# region behaviour
python - <<'PY'
from atria_farmer.captcha import AliyunSolver, token_from_solution
from atria_farmer.config import Config
cfg = Config.from_env()
solver = AliyunSolver(cfg.captcha_key, scene_id=cfg.captcha_scene,
                      prefix=cfg.captcha_prefix, region="cn",
                      website_url=cfg.signin_url)
print(token_from_solution(solver.solve())[:80])
PY
```

…then post the token to `/api/experience/captcha/verify` and observe
`{"success": …}`. Changing `region` to `sgp` reproduces finding §1.
