import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { BookOpen, Copy, Check, Terminal, ShieldCheck, AlertCircle, Play, Loader2 } from "lucide-react";
import { PageTransition } from "@/components/page-transition";
import { ThemeToggle } from "@/components/theme-toggle";
import {
  API_ENDPOINTS,
  API_FLOW,
  API_LIMITS,
  API_META,
  API_NOTE,
  REASON_CODES,
  STRIPE_FLOW,
  STRIPE_LIMITS,
  STRIPE_REASON_CODES,
  buildCurl,
  type ApiEndpoint,
} from "@/lib/api-registry";

const METHOD_STYLE: Record<string, string> = {
  GET: "bg-blue-500/15 text-blue-600 border-blue-500/30",
  POST: "bg-green-500/15 text-green-600 border-green-500/30",
  PUT: "bg-amber-500/15 text-amber-600 border-amber-500/30",
  PATCH: "bg-purple-500/15 text-purple-600 border-purple-500/30",
  DELETE: "bg-red-500/15 text-red-600 border-red-500/30",
};

function MethodBadge({ method }: { method: string }) {
  return (
    <Badge variant="outline" className={`font-mono text-[10px] font-bold ${METHOD_STYLE[method] ?? ""}`}>
      {method}
    </Badge>
  );
}

function AuthBadge({ auth }: { auth: string }) {
  if (auth === "admin") {
    return <Badge variant="outline" className="text-[10px] border-red-500/40 text-red-600">ADMIN</Badge>;
  }
  if (auth === "auth") {
    return <Badge variant="outline" className="text-[10px] border-amber-500/40 text-amber-600">AUTH</Badge>;
  }
  return <Badge variant="secondary" className="text-[10px]">PUBLIC</Badge>;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard?.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
    >
      {copied ? <Check className="w-3 h-3 text-green-500" /> : <Copy className="w-3 h-3" />}
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function ParamTable({ title, params }: { title: string; params: NonNullable<ApiEndpoint["body"]> }) {
  return (
    <div className="mt-3">
      <p className="text-xs font-semibold text-muted-foreground mb-2">{title}</p>
      <div className="border rounded-md overflow-hidden">
        <table className="w-full text-xs">
          <thead className="bg-muted/50">
            <tr>
              <th className="text-left px-3 py-2 font-medium">Name</th>
              <th className="text-left px-3 py-2 font-medium">Type</th>
              <th className="text-left px-3 py-2 font-medium">Required</th>
              <th className="text-left px-3 py-2 font-medium">Description</th>
            </tr>
          </thead>
          <tbody>
            {params.map((p) => (
              <tr key={p.name} className="border-t">
                <td className="px-3 py-2 font-mono font-semibold">{p.name}</td>
                <td className="px-3 py-2 font-mono text-muted-foreground">{p.type}</td>
                <td className="px-3 py-2">
                  {p.required ? (
                    <span className="text-red-500">yes</span>
                  ) : (
                    <span className="text-muted-foreground">no</span>
                  )}
                </td>
                <td className="px-3 py-2 text-muted-foreground">
                  {p.description}
                  {p.example && <code className="block mt-1 text-[11px] text-foreground/70">{p.example}</code>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function EndpointCard({ endpoint }: { endpoint: ApiEndpoint }) {
  const curl = buildCurl(endpoint);

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start gap-2 flex-wrap">
          <MethodBadge method={endpoint.method} />
          <code className="text-sm font-mono font-semibold break-all">{endpoint.path}</code>
          <AuthBadge auth={endpoint.auth} />
        </div>
        <CardTitle className="text-sm mt-2">{endpoint.summary}</CardTitle>
        {endpoint.description && (
          <CardDescription className="text-xs leading-relaxed">{endpoint.description}</CardDescription>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        {endpoint.params && <ParamTable title="Path parameters" params={endpoint.params} />}
        {endpoint.body && <ParamTable title="Body parameters" params={endpoint.body} />}

        {endpoint.responseExample && (
          <div>
            <p className="text-xs font-semibold text-muted-foreground mb-2">Response</p>
            <pre className="bg-muted p-3 rounded-md overflow-x-auto text-[11px] font-mono">
{endpoint.responseExample}
            </pre>
          </div>
        )}

        {endpoint.errors && endpoint.errors.length > 0 && (
          <div>
            <p className="text-xs font-semibold text-muted-foreground mb-2">Errors</p>
            <div className="border rounded-md overflow-hidden">
              <table className="w-full text-xs">
                <tbody>
                  {endpoint.errors.map((e) => (
                    <tr key={e.code} className="border-t first:border-t-0">
                      <td className="px-3 py-2 font-mono font-semibold w-20">{e.code}</td>
                      <td className="px-3 py-2 text-muted-foreground">{e.when}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {endpoint.notes && endpoint.notes.length > 0 && (
          <ul className="text-xs text-muted-foreground space-y-1 list-disc list-inside">
            {endpoint.notes.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        )}

        <div>
          <div className="flex items-center justify-between mb-2">
            <p className="text-xs font-semibold text-muted-foreground">curl</p>
            <CopyButton text={curl} />
          </div>
          <pre className="bg-muted p-3 rounded-md overflow-x-auto text-[11px] font-mono">
{curl}
          </pre>
        </div>
      </CardContent>
    </Card>
  );
}

function TryIt({ endpoint }: { endpoint: string }) {
  const [card, setCard] = useState("5187251978425315|08|2029|746");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ card: card.trim() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.response || data.message || `HTTP ${res.status}`);
      setResult(data);
    } catch (e: any) {
      setError(e?.message || "Request failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm flex items-center gap-2">
          <Play className="w-3.5 h-3.5" />
          Try it
        </CardTitle>
        <CardDescription className="text-xs">
          {endpoint === "/api/stripe/check"
            ? "Runs a $0 card verification against a live merchant. No money moves."
            : "Runs a live $0.01 authorization against the merchant."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex gap-2">
          <Input value={card} onChange={(e) => setCard(e.target.value)} className="font-mono text-sm" />
          <Button onClick={run} disabled={loading}>
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
            {loading ? "Checking" : "Send"}
          </Button>
        </div>

        {error && (
          <Alert variant="destructive">
            <AlertCircle className="w-4 h-4" />
            <AlertTitle>Request failed</AlertTitle>
            <AlertDescription className="text-xs">{error}</AlertDescription>
          </Alert>
        )}

        {result && (
          <div className="border rounded-md p-3 space-y-2">
            <div className="flex items-center gap-2">
              <Badge
                variant={
                  result.status === "charged" || result.status === "approved" ? "default" : "destructive"
                }
                className="text-[10px]"
              >
                {String(result.status).toUpperCase()}
              </Badge>
              {result.gateway && <Badge variant="outline" className="text-[10px]">{result.gateway}</Badge>}
              {result.amount && <Badge variant="outline" className="text-[10px]">{result.amount}</Badge>}
            </div>
            <p className="font-mono text-xs">{result.response}</p>
            {result.card && (
              <p className="font-mono text-[11px] text-muted-foreground">card: {result.card}</p>
            )}
            {result.site && !/shopify/i.test(result.gateway || "") && (
              <p className="font-mono text-[11px] text-muted-foreground">site: {result.site}</p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function ApiDocsPage() {
  const [gateway, setGateway] = useState<"st" | "an">("st");
  const isStripe = gateway === "st";
  const endpoints = isStripe
    ? API_ENDPOINTS.filter((e) => e.path.startsWith("/api/stripe/"))
    : API_ENDPOINTS.filter((e) => e.path.startsWith("/api/authnet/"));
  const flow = isStripe ? STRIPE_FLOW : API_FLOW;
  const reasonCodes = isStripe ? STRIPE_REASON_CODES : REASON_CODES;
  const limits = isStripe ? STRIPE_LIMITS : API_LIMITS;

  return (
    <PageTransition>
      <div className="min-h-screen bg-background">
        <div className="container mx-auto px-4 py-8 max-w-4xl">
          <div className="flex items-center justify-between mb-6">
            <div>
              <h1 className="text-2xl font-bold flex items-center gap-2">
                <BookOpen className="w-6 h-6" />
                {isStripe ? "Stripe $0 API" : API_META.name}
              </h1>
              <p className="text-sm text-muted-foreground mt-1">
                Reference for the{" "}
                <code className="text-xs">/{isStripe ? "st" : API_META.gateway}</code>{" "}
                gateway
              </p>
            </div>
            <ThemeToggle />
          </div>

          <div className="flex gap-2 mb-6">
            {(["st", "an"] as const).map((g) => (
              <button
                key={g}
                type="button"
                onClick={() => setGateway(g)}
                className={
                  "px-3 py-1.5 rounded-md text-xs font-mono border transition-colors " +
                  (gateway === g
                    ? "bg-primary text-primary-foreground border-primary"
                    : "bg-muted/50 text-muted-foreground border-border hover:bg-muted")
                }
              >
                /{g}
              </button>
            ))}
          </div>

          <Alert className="mb-6">
            <ShieldCheck className="w-4 h-4" />
            <AlertTitle>Authentication</AlertTitle>
            <AlertDescription className="text-xs">
              This endpoint needs a session cookie. Log in with{" "}
              <code className="text-[11px]">POST /api/auth/request-otp</code> then{" "}
              <code className="text-[11px]">POST /api/auth/verify-otp</code>.
            </AlertDescription>
          </Alert>

          <Card className="mb-6">
            <CardHeader className="pb-3">
              <CardTitle className="text-sm">How it works</CardTitle>
            </CardHeader>
            <CardContent>
              <ol className="text-xs text-muted-foreground space-y-2 list-decimal list-inside">
                {flow.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
              <p className="text-xs text-muted-foreground mt-3">
                {isStripe
                  ? "No money moves and the card is never charged. The card is sent to the issuing bank for a $0 verification, so a Luhn-valid card can still come back declined."
                  : API_NOTE}
              </p>
            </CardContent>
          </Card>

          {endpoints.map((e) => (
            <div key={`${e.method} ${e.path}`} className="mb-4">
              <EndpointCard endpoint={e} />
            </div>
          ))}

          <TryIt endpoint={endpoints[0]?.path ?? "/api/stripe/check"} />

          <Card className="mt-6">
            <CardHeader className="pb-3">
              <CardTitle className="text-sm">
                {isStripe ? "Stripe reason codes" : "Issuer reason codes"}
              </CardTitle>
              <CardDescription className="text-xs">
                {isStripe
                  ? "The code in the response string is the reason the issuer or Stripe gave."
                  : "The numeric prefix on the error string is the issuer's response code."}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="border rounded-md overflow-hidden">
                <table className="w-full text-xs">
                  <thead className="bg-muted/50">
                    <tr>
                      <th className="text-left px-3 py-2 font-medium">Code</th>
                      <th className="text-left px-3 py-2 font-medium">Meaning</th>
                      <th className="text-left px-3 py-2 font-medium">Verdict</th>
                    </tr>
                  </thead>
                  <tbody>
                    {reasonCodes.map((r) => (
                      <tr key={r.code} className="border-t">
                        <td className="px-3 py-2 font-mono font-semibold whitespace-nowrap">{r.code}</td>
                        <td className="px-3 py-2 text-muted-foreground">{r.meaning}</td>
                        <td className="px-3 py-2 text-muted-foreground">{r.verdict}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>

          <Card className="mt-6">
            <CardHeader className="pb-3">
              <CardTitle className="text-sm">Rate limits</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-center">
                <div className="border rounded-md p-3">
                  <div className="text-xl font-bold">{limits.globalPerMinute}</div>
                  <div className="text-[11px] text-muted-foreground">req/min per IP</div>
                </div>
                <div className="border rounded-md p-3">
                  <div className="text-xl font-bold">{limits.globalPerMinuteAdmin}</div>
                  <div className="text-[11px] text-muted-foreground">req/min (admin)</div>
                </div>
                <div className="border rounded-md p-3">
                  <div className="text-xl font-bold">{limits.checksPerMinute}</div>
                  <div className="text-[11px] text-muted-foreground">checks/min per IP</div>
                </div>
                <div className="border rounded-md p-3">
                  <div className="text-xl font-bold">{limits.concurrentChecks}</div>
                  <div className="text-[11px] text-muted-foreground">concurrent checks</div>
                </div>
              </div>
              <p className="text-[11px] text-muted-foreground mt-3">
                Daily limits also apply per tier. Exceeding a limit returns 429 with a{" "}
                <code className="text-[11px]">Retry-After</code> header.
              </p>
            </CardContent>
          </Card>

          <p className="text-center text-xs text-muted-foreground mt-10 pb-6">
            <Terminal className="w-3 h-3 inline mr-1" />
            Base URL: <code className="text-[11px]">http://localhost:5000</code> in dev, your domain in production.
          </p>
        </div>
      </div>
    </PageTransition>
  );
}
