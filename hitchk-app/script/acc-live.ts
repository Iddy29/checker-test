/**
 * Live check: one real run per account checker.
 *
 * Uses deliberately invalid credentials so the expected result is a
 * verdict (FAIL) rather than a hit.  The point is to prove each checker
 * path completes end-to-end and returns a proper verdict -- not a 500,
 * a timeout, or an empty response.
 */

import express from "express";
import session from "express-session";
import MemoryStoreFactory from "memorystore";
import { createServer } from "http";
import { registerRoutes } from "../server/routes";
import { botManager } from "../server/botManager";

const MemoryStore = MemoryStoreFactory(session as any);

const TEST_UID = "1517013110";
const CHECKERS = ["crunchyroll", "xbox", "cyberghost", "duolingo", "hoichoi"];

async function main() {
  (botManager as any).getBotEnvConfig = () => ({
    botToken: "", apiId: "", apiHash: "",
    adminId: TEST_UID,
    groupId: "", groupLink: "", channelLink: "",
  });

  const app = express();
  app.use(express.json());
  app.use(
    session({
      secret: "test-secret",
      resave: false,
      saveUninitialized: false,
      store: new MemoryStore({ checkPeriod: 86400000 }),
    }),
  );
  app.use((req: any, _res: any, next: any) => {
    req.session.userId = TEST_UID;
    req.session.username = "tester";
    req.session.isAdmin = true;
    req.session.adminPinVerified = true;
    next();
  });

  const server = createServer(app);
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const port = (server.address() as any).port;
  const BASE = `http://127.0.0.1:${port}`;
  await registerRoutes(server as any, app as any);

  async function call(method: string, path: string, body?: any) {
    const res = await fetch(BASE + path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    let json: any = text;
    try { json = JSON.parse(text); } catch {}
    return { status: res.status, body: json };
  }

  console.log("=".repeat(76));
  console.log("ENABLED-CHECKER STATUS");
  console.log("=".repeat(76));
  const st = await call("GET", "/api/account-checkers/status");
  console.log(`   ${JSON.stringify(st.body)}`);
  const allOn = CHECKERS.every((c) => st.body?.[c] === true);
  console.log(`   ${allOn ? "ok  " : "FAIL"}  all 5 checkers enabled\n`);

  console.log("=".repeat(76));
  console.log("LIVE RUN PER CHECKER  (invalid credentials -> expect a verdict)");
  console.log("=".repeat(76));

  const results: any[] = [];
  for (const checker of CHECKERS) {
    const t0 = Date.now();
    let status = 0;
    let body: any = null;
    try {
      const r = await call("POST", "/api/tools/account-check", {
        checker,
        combo: `probe.nonexistent.user@example.invalid:ProbePass${Date.now()}!`,
      });
      status = r.status;
      body = r.body;
    } catch (e: any) {
      body = { status: "harness_error", message: String(e?.message || e) };
    }
    const el = ((Date.now() - t0) / 1000).toFixed(1);
    const verdict = body?.status;
    const known = ["HIT", "FREE", "FAIL", "CUSTOM", "2FA", "error", "ERROR"];
    const ok = status === 200 && known.includes(verdict);
    const capKeys = Object.keys(body?.capture || {});
    console.log(
      `   ${ok ? "ok  " : "FAIL"}  ${checker.padEnd(12)} http=${status} status=${String(verdict).padEnd(7)} ${el.padStart(5)}s` +
      (capKeys.length ? `  capture=[${capKeys.join(",")}]` : "") +
      (body?.message ? `  msg="${String(body.message).slice(0, 70)}"` : ""),
    );
    results.push({ checker, ok, status: verdict, el, capKeys, message: body?.message });
  }

  console.log();
  console.log("=".repeat(76));
  console.log("SUMMARY");
  console.log("=".repeat(76));
  for (const r of results) {
    const kind =
      r.status === "FAIL" ? "service rejected the credentials" :
      r.status === "error" ? "request never reached the service" :
      r.status === "ERROR" ? "integration is BROKEN" :
      r.status;
    console.log(
      `   ${r.checker.padEnd(12)} ${r.ok ? "PASS" : "FAIL"}  ${String(r.status).padEnd(7)} ${r.el.padStart(5)}s   ${kind}`,
    );
  }
  const passed = results.filter((r) => r.ok).length;
  const broken = results.filter((r) => r.status === "ERROR");
  const netfail = results.filter((r) => r.status === "error");
  const genuine = results.filter((r) => ["HIT", "FREE", "FAIL", "CUSTOM", "2FA"].includes(r.status));
  console.log();
  console.log(`   ${passed}/${results.length} returned a well-formed status`);
  console.log(`   ${genuine.length}/${results.length} are working checkers (real verdict)`);
  console.log(`   ${broken.length} have a BROKEN integration: ${broken.map((r) => r.checker).join(", ") || "none"}`);
  console.log(`   ${netfail.length} could not reach the service: ${netfail.map((r) => r.checker).join(", ") || "none"}`);

  server.close();
  process.exit(results.every((r) => r.ok) ? 0 : 1);
}

main().catch((e) => {
  console.error("HARNESS ERROR:", e);
  process.exit(2);
});
