/**
 * atria-otp — Cloudflare Email Worker for one-time-code delivery.
 *
 * Cloudflare Email Routing hands every message addressed to the configured
 * domain to this worker.  The worker parses the MIME body, extracts the
 * verification code, stores it in D1, and (optionally) forwards the original
 * message to a fallback inbox.
 *
 * Read API
 * --------
 *   GET /?email=<address>    the code, or 404 NOT_FOUND.
 *                            One-shot: the row is deleted when it is read, so a
 *                            stale code can never be replayed.
 *   GET /recent              the 30 most recent rows (debugging).
 *   GET /raw?email=<address> the full stored row including the body snippet.
 *
 * Storage
 * -------
 * D1 rather than KV: the free KV tier allows 1,000 writes per day and this
 * worker writes on every inbound message, while D1 allows 100,000.
 *
 * The `otp` table is created by `deploy.py`.  Expected schema:
 *
 *   CREATE TABLE otp (
 *     email      TEXT PRIMARY KEY,
 *     code       TEXT NOT NULL,
 *     subject    TEXT,
 *     snippet    TEXT,
 *     created_at INTEGER NOT NULL
 *   );
 */

const JUNK_CODES = new Set([
  "181818", "666666", "808080", "999999", "000000",
  "333333", "222222", "111111", "123456", "123123",
]);

/**
 * Pull a verification code out of a message.
 *
 * Tried in order of confidence: an explicit "verification code is N" phrase, an
 * `.otp-code` element, a bare number in the subject, then the first plausible
 * number in the body.  The last step is the riskiest, which is why the junk
 * list and the zero check exist.
 */
function extractCode(subject, body) {
  const head = subject || "";
  const text = body || "";
  let match;

  match = text.match(
    /(?:verification|verify|confirmation|security|login|one[\s-]?time|otp|kode|verifikasi)\s*code[^0-9]{0,40}(\d{4,8})/i
  );
  if (match) return match[1];

  match = text.match(/otp-code[^>]*>\s*(\d{4,8})\s*</i);
  if (match) return match[1];

  match = head.match(/\b(\d{4,8})\b/);
  if (match && !JUNK_CODES.has(match[1])) return match[1];

  for (const candidate of text.matchAll(/\b(\d{4,8})\b/g)) {
    const value = candidate[1];
    if (!JUNK_CODES.has(value) && parseInt(value, 10) !== 0) return value;
  }

  return null;
}

const CORS = {
  "Content-Type": "text/plain",
  "Access-Control-Allow-Origin": "*",
};

export default {
  /** Handle an inbound message delivered by Email Routing. */
  async email(message, env) {
    const to = (message.to || "").toLowerCase();
    let subject = "";
    let body = "";
    let code = null;
    let note = "";

    try {
      const raw = await new Response(message.raw).arrayBuffer();
      const parsed = await PostalMime.parse(raw);
      subject = parsed.subject || "";
      const text = (parsed.text || "").replace(/\s+/g, " ");
      const html = (parsed.html || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
      body = `${text} || ${html}`.slice(0, 4000);
      code = extractCode(subject, body);
      if (!code) note = "NO_CODE_FOUND";
    } catch (error) {
      note = `PARSE_ERROR: ${String(error).slice(0, 200)}`;
    }

    try {
      await env.DB.prepare(
        "INSERT OR REPLACE INTO otp (email, code, subject, snippet, created_at) VALUES (?, ?, ?, ?, ?)"
      )
        .bind(
          to,
          code || "",
          subject.slice(0, 300),
          (note ? `${note} | ` : "") + body.slice(0, 2500),
          Date.now()
        )
        .run();
    } catch (error) {
      // Storage failure must not swallow the message; fall through to the
      // forward below so a human can still recover the code.
      console.error("[atria-otp] store failed:", error);
    }

    if (env.FORWARD_TO) {
      try {
        await message.forward(env.FORWARD_TO);
      } catch (error) {
        console.error("[atria-otp] forward failed:", error);
      }
    }
  },

  /** Serve the read API. */
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/recent") {
      const rows = await env.DB.prepare(
        "SELECT email, code, subject, created_at FROM otp ORDER BY created_at DESC LIMIT 30"
      ).all();
      return new Response(JSON.stringify(rows.results, null, 1), {
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
      });
    }

    if (url.pathname === "/raw") {
      const target = (url.searchParams.get("email") || "").toLowerCase();
      const row = await env.DB.prepare("SELECT * FROM otp WHERE email = ?")
        .bind(target)
        .first();
      return new Response(row ? JSON.stringify(row, null, 1) : "NOT_FOUND", {
        status: row ? 200 : 404,
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
      });
    }

    const email = url.searchParams.get("email");
    if (!email) {
      return new Response("Missing email param", { status: 400, headers: CORS });
    }

    const key = email.toLowerCase();
    const row = await env.DB.prepare("SELECT code FROM otp WHERE email = ?")
      .bind(key)
      .first();

    if (row && row.code) {
      await env.DB.prepare("DELETE FROM otp WHERE email = ?").bind(key).run();
      return new Response(row.code, { headers: CORS });
    }
    return new Response("NOT_FOUND", { status: 404, headers: CORS });
  },
};
