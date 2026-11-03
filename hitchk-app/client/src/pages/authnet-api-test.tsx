import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Loader2, Play, CheckCircle2, XCircle, AlertCircle, Terminal } from "lucide-react";
import { PageTransition } from "@/components/page-transition";
import { apiRequest } from "@/lib/queryClient";
import { ThemeToggle } from "@/components/theme-toggle";

interface CheckResponse {
  status: string;
  response: string;
  gateway?: string;
  card?: string;
  amount?: string | number | null;
  site?: string;
}

const SAMPLE_CARDS = [
  { label: "Visa (valid format)", value: "4111111111111111|12|29|123" },
  { label: "Mastercard", value: "5187251978425315|08|2029|746" },
  { label: "Luhn fail", value: "4111111111111112|12|29|123" },
  { label: "Expired", value: "5500000000000004|01|20|123" },
];

function statusVariant(status: string): "default" | "secondary" | "destructive" | "outline" {
  if (status === "charged" || status === "approved") return "default";
  if (status === "declined") return "destructive";
  if (status === "error") return "secondary";
  return "outline";
}

function StatusIcon({ status }: { status: string }) {
  if (status === "charged" || status === "approved") return <CheckCircle2 className="w-4 h-4" />;
  if (status === "declined") return <XCircle className="w-4 h-4" />;
  return <AlertCircle className="w-4 h-4" />;
}

export default function AuthnetApiTestPage() {
  const [card, setCard] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<CheckResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState<number | null>(null);

  async function runCheck() {
    const value = card.trim();
    if (!value) {
      setError("Enter a card in CC|MM|YY|CVV format");
      return;
    }

    setLoading(true);
    setError(null);
    setResult(null);
    setElapsed(null);

    const started = Date.now();
    try {
      const res = await apiRequest("POST", "/api/authnet/check", { card: value });
      const data = (await res.json()) as CheckResponse;
      setResult(data);
    } catch (e: any) {
      setError(e?.message || "Request failed");
    } finally {
      setElapsed((Date.now() - started) / 1000);
      setLoading(false);
    }
  }

  const curlBody = JSON.stringify({ card: card.trim() || "4111111111111111|12|29|123" }, null, 2);

  return (
    <PageTransition>
      <div className="min-h-screen bg-background">
        <div className="container mx-auto px-4 py-8 max-w-4xl">
          <div className="flex items-center justify-between mb-6">
            <div>
              <h1 className="text-2xl font-bold flex items-center gap-2">
                <Terminal className="w-6 h-6" />
                Authorize.net API Test
              </h1>
              <p className="text-sm text-muted-foreground mt-1">
                POST /api/authnet/check — live gateway call
              </p>
            </div>
            <ThemeToggle />
          </div>

          <Card className="mb-6">
            <CardHeader>
              <CardTitle>Request</CardTitle>
              <CardDescription>
                Runs a real $0.01 authorization against the merchant and returns the issuer verdict.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="card">Card</Label>
                <div className="flex gap-2">
                  <Input
                    id="card"
                    value={card}
                    onChange={(e) => setCard(e.target.value)}
                    placeholder="4111111111111111|12|29|123"
                    onKeyDown={(e) => { if (e.key === "Enter" && !loading) runCheck(); }}
                  />
                  <Button onClick={runCheck} disabled={loading}>
                    {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
                    {loading ? "Checking" : "Send"}
                  </Button>
                </div>
              </div>

              <div className="flex flex-wrap gap-2">
                <span className="text-xs text-muted-foreground self-center">Samples:</span>
                {SAMPLE_CARDS.map((s) => (
                  <Button
                    key={s.label}
                    variant="outline"
                    size="sm"
                    onClick={() => setCard(s.value)}
                    disabled={loading}
                  >
                    {s.label}
                  </Button>
                ))}
              </div>
            </CardContent>
          </Card>

          {error && (
            <Alert variant="destructive" className="mb-6">
              <AlertCircle className="w-4 h-4" />
              <AlertTitle>Request failed</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          {result && (
            <Card className="mb-6">
              <CardHeader>
                <CardTitle>Response</CardTitle>
                <CardDescription>
                  {elapsed !== null && `Completed in ${elapsed.toFixed(1)}s`}
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="flex items-center gap-2 mb-4">
                  <Badge variant={statusVariant(result.status)} className="gap-1">
                    <StatusIcon status={result.status} />
                    {result.status.toUpperCase()}
                  </Badge>
                  {result.gateway && <Badge variant="outline">{result.gateway}</Badge>}
                  {result.amount && <Badge variant="outline">{result.amount}</Badge>}
                </div>
                <p className="font-mono text-sm">{result.response}</p>
                {result.card && (
                  <p className="font-mono text-xs text-muted-foreground mt-3">card: {result.card}</p>
                )}
                {result.site && (
                  <p className="font-mono text-xs text-muted-foreground">site: {result.site}</p>
                )}
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader>
              <CardTitle>curl</CardTitle>
              <CardDescription>Same request from the command line.</CardDescription>
            </CardHeader>
            <CardContent>
              <pre className="bg-muted p-4 rounded-md overflow-x-auto text-xs font-mono">
{`curl -X POST http://localhost:5000/api/authnet/check \\
  -H "Content-Type: application/json" \\
  -b cookies.txt \\
  -d '${curlBody}'`}
              </pre>
            </CardContent>
          </Card>
        </div>
      </div>
    </PageTransition>
  );
}
