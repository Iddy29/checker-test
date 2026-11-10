/**
 * Authorize.net (/an) API reference -- single source of truth.
 *
 * The server's GET /api/docs and the client's /docs page both render this
 * data, so the two can never drift apart.
 */

export type AuthLevel = "public" | "auth" | "admin";

export interface ApiParam {
  name: string;
  in: "path" | "query" | "body";
  type: string;
  required: boolean;
  description: string;
  example?: string;
}

export interface ApiEndpoint {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  auth: AuthLevel;
  summary: string;
  description?: string;
  params?: ApiParam[];
  body?: ApiParam[];
  responseExample?: string;
  /** Machine-readable twin of responseExample, served by GET /api/docs. */
  responseExampleJson?: unknown;
  errors?: { code: number; when: string }[];
  notes?: string[];
}

export interface ApiLimits {
  globalPerMinute: number;
  globalPerMinuteAdmin: number;
  checksPerMinute: number;
  concurrentChecks: number;
}

export interface ReasonCode {
  code: string;
  meaning: string;
  verdict: string;
}

export const API_ENDPOINTS: ApiEndpoint[] = [
  {
    method: "POST",
    path: "/api/stripe/check",
    auth: "auth",
    summary: "Stripe $0 auth check",
    description:
      "Runs a $0 card verification against a live merchant's Stripe account and returns the issuer's verdict. No money moves and no card is charged.",
    body: [
      {
        name: "card",
        in: "body",
        type: "string",
        required: true,
        description:
          "Card in CC|MM|YY|CVV format. 13-19 digits. MM is 1-2 digits, YY may be 2 or 4 digits. The card is sent to the issuing bank, so a Luhn-valid card can still come back declined.",
        example: "5187251978425315|08|2029|746",
      },
    ],
    responseExample: `{
  "status": "approved",
  "response": "Card verified by the issuer (live card)",
  "gateway": "Stripe Auth $0",
  "card": "5187251978425315|08|29|746"
}`,
    responseExampleJson: {
      status: "approved",
      response: "Card verified by the issuer (live card)",
      gateway: "Stripe Auth $0",
      card: "5187251978425315|08|29|746",
    },
    errors: [
      { code: 400, when: "Missing card, card too long, or malformed format" },
      { code: 401, when: "Not logged in" },
      { code: 403, when: "Daily check limit reached for the tier" },
      { code: 429, when: "Rate limit exceeded" },
      { code: 503, when: "Server busy" },
    ],
  },
  {
    method: "POST",
    path: "/api/check/batch",
    auth: "auth",
    summary: "Batch check",
    description:
      "Runs many cards through one gateway and returns a job you poll. Use gateway \"st\" for the Stripe $0 check.",
    body: [
      {
        name: "gateway",
        in: "body",
        type: "string",
        required: true,
        description: "Gateway id. \"st\" for the Stripe $0 check, \"shp\" for Shopify.",
        example: "st",
      },
      {
        name: "cards",
        in: "body",
        type: "string[]",
        required: true,
        description:
          "Array of cards in CC|MM|YY|CVV format. The batch size is capped by the tier limit.",
        example: ["5187251978425315|08|2029|746", "4038893270181361|02|2030|483"],
      },
    ],
    responseExample: `{
  "jobId": "9f2c1a4b7d3e5f60",
  "status": "running",
  "totalCards": 2
}`,
    responseExampleJson: {
      jobId: "9f2c1a4b7d3e5f60",
      status: "running",
      totalCards: 2,
    },
    errors: [
      { code: 400, when: "Missing gateway, no valid cards, or the batch is over the tier limit" },
      { code: 401, when: "Not logged in" },
      { code: 403, when: "Daily check limit reached for the tier" },
      { code: 409, when: "A batch is already running for this user" },
    ],
    notes: [
      "Poll GET /api/check/batch/:jobId for the results.",
      "Stop it with DELETE /api/check/batch/:jobId.",
    ],
  },
  {
    method: "POST",
    path: "/api/authnet/check",
    auth: "auth",
    summary: "Authorize.net check",
    description:
      "Runs the card through a live Authorize.net merchant checkout and returns the issuer's verdict. Performs a real $0.01 transaction on approval.",
    body: [
      {
        name: "card",
        in: "body",
        type: "string",
        required: true,
        description:
          "Card in CC|MM|YY|CVV format. 13-19 digits. MM is 1-2 digits, YY may be 2 or 4 digits. The card is sent to the issuing bank, so a Luhn-valid card can still come back declined.",
        example: "5187251978425315|08|2029|746",
      },
    ],
    responseExample: `{
  "status": "charged",
  "response": "Charged - Transaction Approved",
  "gateway": "Authorize.net Auth",
  "card": "5187251978425315|08|29|746",
  "amount": "$0.01",
  "site": "bethlehemdental.dentist"
}`,
    responseExampleJson: {
      status: "charged",
      response: "Charged - Transaction Approved",
      gateway: "Authorize.net Auth",
      card: "5187251978425315|08|29|746",
      amount: "$0.01",
      site: "bethlehemdental.dentist",
    },
    errors: [
      { code: 400, when: "Missing card, card too long, or malformed format" },
      { code: 401, when: "Not logged in" },
      { code: 403, when: "Daily check limit reached for the tier" },
      { code: 429, when: "Rate limit exceeded (10 checks/min per IP)" },
      { code: 503, when: "All check slots busy" },
      { code: 500, when: "Checker process failed" },
    ],
    notes: [
      "Response time is 10-40s. The captcha solve dominates; a cached token makes it faster.",
      "status=charged means the issuer approved and the $0.01 moved.",
      "status=approved means the issuer declined the sale but the card reached the bank (live/CCN).",
      "status=declined means the issuer rejected the card (invalid number, expired, etc.).",
      "status=error means the gateway itself failed - the site could not be visited, the captcha was not solved, or a proxy died. It is NOT a card verdict; retry.",
    ],
  },
];

/** Global limits applied to the /an endpoint. */
export const API_LIMITS: ApiLimits = {
  globalPerMinute: 120,
  globalPerMinuteAdmin: 300,
  checksPerMinute: 10,
  concurrentChecks: 5,
};

/** Limits applied to the Stripe $0 check (/st) endpoint. */
export const STRIPE_LIMITS: ApiLimits = {
  globalPerMinute: 120,
  globalPerMinuteAdmin: 300,
  checksPerMinute: 10,
  concurrentChecks: 5,
};

/** The issuer reason codes the merchant relays back. */
export const REASON_CODES: ReasonCode[] = [
  { code: "2", meaning: "Transaction declined", verdict: "approved (live / CCN)" },
  { code: "6", meaning: "Card number invalid", verdict: "declined" },
  { code: "8", meaning: "Card expired", verdict: "declined" },
  { code: "14", meaning: "Invalid card number", verdict: "declined" },
  { code: "15", meaning: "Expired card", verdict: "declined" },
  { code: "27", meaning: "CVV mismatch", verdict: "approved (live / CCN)" },
  { code: "41", meaning: "Insufficient funds", verdict: "approved (live / CCN)" },
  { code: "54", meaning: "Card expired", verdict: "declined" },
  { code: "any other", meaning: "Issuer responded", verdict: "approved (live / CCN)" },
];

/** The reason codes the Stripe $0 check (/st) reports. */
export const STRIPE_REASON_CODES: ReasonCode[] = [
  { code: "succeeded", meaning: "The issuer verified the card", verdict: "approved (live card)" },
  { code: "requires_action", meaning: "The issuer wants 3DS to finish the check", verdict: "approved (live card)" },
  { code: "insufficient_funds", meaning: "The issuer refused: no balance", verdict: "approved (live card)" },
  { code: "do_not_honor", meaning: "The issuer refused", verdict: "approved (live card)" },
  { code: "lost_card", meaning: "The issuer refused: card reported lost", verdict: "approved (live card)" },
  { code: "stolen_card", meaning: "The issuer refused: card reported stolen", verdict: "approved (live card)" },
  { code: "restricted_card", meaning: "The issuer refused: card restricted", verdict: "approved (live card)" },
  { code: "expired_card", meaning: "The card's expiry date has passed", verdict: "declined" },
  { code: "incorrect_number", meaning: "The card number does not exist", verdict: "declined" },
  { code: "incorrect_cvc", meaning: "The security code is wrong", verdict: "declined" },
  { code: "invalid_expiry_month", meaning: "The expiry month is not valid", verdict: "declined" },
  { code: "invalid_expiry_year", meaning: "The expiry year is not valid", verdict: "declined" },
  { code: "live_mode_test_card", meaning: "A Stripe test card, not a real card", verdict: "error" },
  { code: "fraudulent", meaning: "The merchant's risk system blocked the request", verdict: "declined" },
  { code: "generic_decline", meaning: "The issuer refused without naming a reason", verdict: "declined" },
];

/** The order of operations the Stripe $0 check runs. */
export const STRIPE_FLOW: string[] = [
  "The card is checked locally: a Luhn failure or an expiry date in the past is declined without any request leaving the bot.",
  "The merchant's account page is read for the live publishable key and the setup-intent nonce.",
  "Stripe's account-range table is queried for the card's BIN. An empty table means Stripe does not know the BIN, so the card cannot exist.",
  "The card is tokenized with the publishable key. This validates the number, the expiry format and the BIN only - it does NOT contact the bank.",
  "The store confirms a setup intent with its own secret key. That is a $0 card verification, so the issuing bank answers without any money moving.",
  "The issuer's answer becomes the verdict: a verified card is approved, a refusal is declined.",
];

/** The order of operations the gateway runs, shown on the docs page. */
export const API_FLOW: string[] = [
  "The card is tokenized against Authorize.net - this validates format only and does NOT contact the bank.",
  "The merchant's Cloudflare Turnstile gate is solved.",
  "The form is submitted to the merchant, which calls Authorize.net with its secret transaction key. That call is what reaches the issuing bank.",
  "The issuer's response code comes back and is mapped to a verdict.",
];

export const API_NOTE =
  "On approval a real $0.01 transaction is made. A Luhn-valid card can still come back declined - only the issuer decides.";

export const API_META = {
  name: "Authorize.net API",
  version: "1.0",
  gateway: "an",
  baseUrl: "/api",
  auth: {
    login: "POST /api/auth/request-otp -> POST /api/auth/verify-otp",
    admin: "POST /api/admin/verify-pin",
    note: "A session cookie is set on a successful OTP verify.",
  },
};

/** Build a copy-pasteable curl command. */
export function buildCurl(endpoint: ApiEndpoint, baseUrl = ""): string {
  const parts: string[] = [`curl -X ${endpoint.method} ${baseUrl}${endpoint.path}`];
  if (endpoint.body?.length) {
    const body: Record<string, string> = {};
    for (const p of endpoint.body) body[p.name] = p.example ?? "";
    parts.push(`  -H "Content-Type: application/json"`);
    parts.push(`  -b cookies.txt`);
    parts.push(`  -d '${JSON.stringify(body)}'`);
  }
  return parts.join(" \\\n");
}
