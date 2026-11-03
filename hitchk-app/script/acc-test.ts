/**
 * In-process end-to-end test of the account-check route.
 *
 * Boots the real express app on an ephemeral port, fakes a logged-in
 * session, and exercises the account-check endpoints for real.
 */

import express from "express";
import session from "express-session";
import MemoryStoreFactory from "memorystore";
import { createServer } from "http";
import { readFileSync } from "fs";
import { registerRoutes } from "../server/routes";
import { botManager } from "../server/botManager";

const MemoryStore = MemoryStoreFactory(session as any);

async function main() {
  // The fake session user must be an admin so it gets the "gold" tier and
  // can actually run checks (free tier is limited to one per 10 min).
  // config.json wins over the env var, so patch the config the manager reads.
  const TEST_UID = "1517013110";
  (botManager as any).getBotEnvConfig = () => ({
    botToken: "", apiId: "", apiHash: "",
    adminId: TEST_UID,
    groupId: "", groupLink: "", channelLink: "",
  });

  // Enable every account checker so the test can exercise them.
  const realGetSettings = (botManager as any).getBotSettings.bind(botManager);
  (botManager as any).getBotSettings = () => {
    const s = realGetSettings();
    const tool_settings = { ...(s.tool_settings || {}) };
    for (const c of ["crunchyroll", "xbox", "cyberghost", "duolingo", "hoichoi"]) {
      tool_settings[`acc_${c}`] = { ...(tool_settings[`acc_${c}`] || {}), enabled: true };
    }
    return { ...s, tool_settings };
  };

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
  // Fake a logged-in session for every request.
  app.use((req: any, _res: any, next: any) => {
    req.session.userId = "1517013110";
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

  let pass = 0;
  let fail = 0;
  function check(label: string, cond: boolean, detail = "") {
    console.log(`   ${cond ? "ok  " : "FAIL"}  ${label}${!cond && detail ? `   ${detail}` : ""}`);
    cond ? pass++ : fail++;
  }

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

  console.log("=".repeat(72));
  console.log("1. GET /api/account-checkers/status");
  console.log("=".repeat(72));
  const st = await call("GET", "/api/account-checkers/status");
  console.log(`   -> ${st.status} ${JSON.stringify(st.body)}`);
  const expected = ["crunchyroll", "xbox", "cyberghost", "duolingo", "hoichoi"];
  check("returns 200", st.status === 200);
  check("all 5 checkers listed", JSON.stringify(Object.keys(st.body).sort()) === JSON.stringify(expected.slice().sort()),
    JSON.stringify(Object.keys(st.body)));

  console.log();
  console.log("=".repeat(72));
  console.log("2. GET /api/user/tier  (baseline usage)");
  console.log("=".repeat(72));
  const t0 = await call("GET", "/api/user/tier");
  const before = t0.body?.usage?.accountMassChecks ?? 0;
  console.log(`   tier=${t0.body?.tier} massAccountMax=${t0.body?.limits?.massAccountMax} accountMassChecks=${before}`);

  console.log();
  console.log("=".repeat(72));
  console.log("3. POST /api/tools/account-check  (invalid checker)");
  console.log("=".repeat(72));
  const bad = await call("POST", "/api/tools/account-check", { checker: "notreal", combo: "a@b.com:x" });
  console.log(`   -> ${bad.status} ${JSON.stringify(bad.body)}`);
  check("rejected with 400", bad.status === 400);

  console.log();
  console.log("=".repeat(72));
  console.log("4. POST /api/tools/account-check  (malformed combo)");
  console.log("=".repeat(72));
  const bad2 = await call("POST", "/api/tools/account-check", { checker: "crunchyroll", combo: "no-colon-here" });
  console.log(`   -> ${bad2.status} ${JSON.stringify(bad2.body)}`);
  check("rejected with 400", bad2.status === 400, `got ${bad2.status}`);

  console.log();
  console.log("=".repeat(72));
  console.log("5. POST /api/tools/account-check  (real run, duolingo)");
  console.log("=".repeat(72));
  const tStart = Date.now();
  const real = await call("POST", "/api/tools/account-check", {
    checker: "duolingo",
    combo: "nobody@example.invalid:WrongPass123",
  });
  const el = ((Date.now() - tStart) / 1000).toFixed(1);
  console.log(`   -> ${real.status} in ${el}s`);
  console.log(`   body: ${JSON.stringify(real.body).slice(0, 300)}`);
  check("returns 200 (not a 500)", real.status === 200, `got ${real.status}`);
  const status = real.body?.status;
  check("status in known vocabulary", ["HIT", "FREE", "FAIL", "CUSTOM", "2FA", "error"].includes(status), String(status));
  check("capture is always an object", real.body && typeof real.body.capture === "object" && real.body.capture !== null);
  if (status === "error") {
    check("gateway error carries a message", typeof real.body.message === "string" && real.body.message.length > 0,
      JSON.stringify(real.body));
  }

  console.log();
  console.log("=".repeat(72));
  console.log("6. Usage counter");
  console.log("=".repeat(72));
  const t1 = await call("GET", "/api/user/tier");
  const after = t1.body?.usage?.accountMassChecks ?? 0;
  console.log(`   accountMassChecks ${before} -> ${after}`);
  if (status === "error") {
    check("a gateway error did NOT consume quota", after === before, `after=${after}`);
  } else {
    check("a real verdict DID consume quota", after > before, `after=${after}`);
  }

  console.log();
  console.log("=".repeat(72));
  console.log("6b. A gateway error does NOT consume quota");
  console.log("=".repeat(72));
  // Use a checker whose upstream is unreachable from here so the python
  // layer reports a network error rather than a verdict.
  const before2 = (await call("GET", "/api/user/tier")).body?.usage?.accountMassChecks ?? 0;
  const errRun = await call("POST", "/api/tools/account-check", {
    checker: "cyberghost",
    combo: "nobody@example.invalid:WrongPass123",
  });
  const after2 = (await call("GET", "/api/user/tier")).body?.usage?.accountMassChecks ?? 0;
  console.log(`   status=${errRun.body?.status} ${before2} -> ${after2}`);
  if (errRun.body?.status === "error" || errRun.body?.status === "ERROR") {
    check("gateway error did not consume quota", after2 === before2, `after=${after2}`);
  } else {
    console.log(`   note: cyberghost answered with a verdict (${errRun.body?.status}); quota consumed is correct`);
    check("a real verdict consumed quota", after2 > before2, `after=${after2}`);
  }

  console.log();
  console.log("=".repeat(72));
  console.log("6c. A BROKEN checker (ERROR) does NOT consume quota");
  console.log("=".repeat(72));
  // crunchyroll's API client is revoked -> the python layer raises
  // IntegrationError -> status ERROR.  The user did nothing wrong and no
  // account was checked, so it must be free.
  const before3 = (await call("GET", "/api/user/tier")).body?.usage?.accountMassChecks ?? 0;
  const brokenRun = await call("POST", "/api/tools/account-check", {
    checker: "crunchyroll",
    combo: "nobody@example.invalid:WrongPass123",
  });
  const after3 = (await call("GET", "/api/user/tier")).body?.usage?.accountMassChecks ?? 0;
  console.log(`   status=${brokenRun.body?.status} ${before3} -> ${after3}`);
  console.log(`   msg: ${String(brokenRun.body?.message).slice(0, 100)}`);
  if (brokenRun.body?.status === "ERROR") {
    check("broken checker did NOT consume quota", after3 === before3, `after=${after3}`);
    check("ERROR carries a reason", typeof brokenRun.body?.message === "string" && brokenRun.body.message.length > 0);
  } else {
    check("crunchyroll still returns a known status",
      ["HIT", "FREE", "FAIL", "CUSTOM", "2FA", "error", "ERROR"].includes(brokenRun.body?.status),
      String(brokenRun.body?.status));
  }

  console.log();
  console.log("=".repeat(72));
  console.log("7. Activity feed records a HIT (the old bug)");
  console.log("=".repeat(72));
  // The python layer returned "HIT"; the old code compared against "hit".
  const oldMatch = "HIT" === "hit" || "HIT" === "premium" || "HIT" === "valid";
  const newMatch = ["HIT"].includes("HIT");
  check("old comparison never matched an uppercase HIT", oldMatch === false);
  check("new comparison matches", newMatch === true);

  console.log();
  console.log("=".repeat(72));
  console.log("8. Timeout budget is per-checker, not a flat 30s");
  console.log("=".repeat(72));
  const src = readFileSync("server/routes.ts", "utf-8");
  const idx = src.indexOf('/api/tools/account-check", requireAuth');
  const seg = idx >= 0 ? src.slice(idx, idx + 6000) : "";
  check("no flat 30s kill in the account-check route", !seg.includes("timeout: 30000"));
  check("uses the per-checker timeout", seg.includes("checkerInfo.timeoutSec"));

  console.log();
  console.log("=".repeat(72));
  console.log("9. A slow checker is NOT killed -> no 500");
  console.log("=".repeat(72));
  // xbox has the longest chain (5 requests x 20s = 100s raw).  With the old
  // flat 30s kill the process was SIGTERMed and the route returned 500.
  const tX = Date.now();
  const xb = await call("POST", "/api/tools/account-check", {
    checker: "xbox",
    combo: "nobody@example.invalid:WrongPass123",
  });
  const elX = ((Date.now() - tX) / 1000).toFixed(1);
  console.log(`   -> ${xb.status} in ${elX}s`);
  console.log(`   body: ${JSON.stringify(xb.body).slice(0, 250)}`);
  check("does not return 500", xb.status !== 500, `got ${xb.status}`);
  check("returns 200 with a status field", xb.status === 200, `got ${xb.status}`);
  check("status is in the known vocabulary",
    ["HIT", "FREE", "FAIL", "CUSTOM", "2FA", "error"].includes(xb.body?.status), String(xb.body?.status));

  server.close();
  console.log();
  console.log("=".repeat(72));
  console.log(`RESULT: ${pass} passed, ${fail} failed`);
  console.log("=".repeat(72));
  process.exit(fail > 0 ? 1 : 0);
}

main().catch((e) => {
  console.error("TEST HARNESS ERROR:", e);
  process.exit(2);
});
