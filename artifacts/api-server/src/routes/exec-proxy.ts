import express, { Router, type IRouter } from "express";

/**
 * Passthrough proxy: /api/exec/* → http://127.0.0.1:5000/*
 *
 * The Python execution engine (tc-execution-engine) listens on port 5000 but
 * is not registered as a Replit artifact, so it is not reachable from outside
 * this Repl.  This router exposes it through the api-server (which IS routed
 * publicly at /api) so other Repls (e.g. Target Capital) can hit it over
 * HTTPS.
 *
 * Critical: the engine verifies an HMAC over the RAW request bytes.  We use
 * express.raw() and mount this router BEFORE express.json() in app.ts so the
 * body is preserved byte-for-byte.
 */
const router: IRouter = Router();

const ENGINE_URL = process.env.EXEC_ENGINE_URL ?? "http://127.0.0.1:5000";

const HOP_BY_HOP = new Set([
  "host",
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "content-length",
]);

// Capture raw bytes for every content type (signed body must round-trip exactly).
router.use(express.raw({ type: "*/*", limit: "5mb" }));

router.all(/.*/, async (req, res) => {
  // req.originalUrl looks like /api/exec/v1/orders?foo=bar — strip the prefix.
  const upstreamPath = req.originalUrl.replace(/^\/api\/exec/, "") || "/";
  const upstreamUrl = `${ENGINE_URL}${upstreamPath}`;

  const headers: Record<string, string> = {};
  for (const [k, v] of Object.entries(req.headers)) {
    if (HOP_BY_HOP.has(k.toLowerCase())) continue;
    if (Array.isArray(v)) headers[k] = v.join(", ");
    else if (typeof v === "string") headers[k] = v;
  }

  const hasBody = !["GET", "HEAD", "OPTIONS"].includes(req.method);
  // express.raw gives req.body as Buffer when content-type matches, otherwise {}
  const body = hasBody && Buffer.isBuffer(req.body) ? req.body : undefined;

  try {
    const upstream = await fetch(upstreamUrl, {
      method: req.method,
      headers,
      body,
    });

    res.status(upstream.status);
    upstream.headers.forEach((value, key) => {
      // skip hop-by-hop; let express set content-length from buffer
      if (HOP_BY_HOP.has(key.toLowerCase())) return;
      res.setHeader(key, value);
    });
    const buf = Buffer.from(await upstream.arrayBuffer());
    res.send(buf);
  } catch (err) {
    req.log?.error({ err, upstreamUrl }, "exec-proxy upstream failed");
    res.status(502).json({
      error: "engine_unreachable",
      message: err instanceof Error ? err.message : String(err),
    });
  }
});

export default router;
