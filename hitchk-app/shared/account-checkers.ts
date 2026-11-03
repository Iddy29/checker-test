/**
 * Account checker contract -- single source of truth.
 *
 * The python layer (bot/gates/account_checkers.py) returns UPPERCASE
 * statuses.  The server and the client both consume them, so the allowed
 * values and the "does this count as a hit" rule live here.
 *
 * The key distinction:
 *   FAIL  -- the service looked at these credentials and rejected them.
 *   ERROR -- the integration is broken.  The endpoint moved, the API
 *            credentials were revoked, the response shape changed, or the
 *            request never reached the service.
 *
 * Collapsing ERROR into FAIL hides broken integrations behind "invalid
 * credentials" and makes a dead checker look like a working one that
 * keeps finding invalid accounts.
 */

export type AccountStatus =
  | "HIT"
  | "FREE"
  | "FAIL"
  | "CUSTOM"
  | "2FA"
  | "ERROR"
  | "error";

export interface AccountCheckerInfo {
  id: string;
  label: string;
  /** The bot tool_settings key that toggles this checker on/off. */
  toolKey: string;
  /** Worst-case wall time for this checker, in seconds. */
  timeoutSec: number;
}

/**
 * Every supported checker.
 *
 * The timeout must cover the checker's own worst case (login plus every
 * follow-up request) plus one retry -- the python layer retries once on a
 * network error, which doubles the budget.
 */
export const ACCOUNT_CHECKERS: AccountCheckerInfo[] = [
  { id: "crunchyroll", label: "Crunchyroll", toolKey: "acc_crunchyroll", timeoutSec: 200 },
  { id: "xbox", label: "Xbox Game Pass", toolKey: "acc_xbox", timeoutSec: 230 },
  { id: "cyberghost", label: "CyberGhost VPN", toolKey: "acc_cyberghost", timeoutSec: 80 },
  { id: "duolingo", label: "Duolingo", toolKey: "acc_duolingo", timeoutSec: 80 },
  { id: "hoichoi", label: "Hoichoi", toolKey: "acc_hoichoi", timeoutSec: 80 },
];

export const ACCOUNT_CHECKER_IDS: string[] = ACCOUNT_CHECKERS.map((c) => c.id);

export const ACCOUNT_STATUSES: AccountStatus[] = [
  "HIT",
  "FREE",
  "FAIL",
  "CUSTOM",
  "2FA",
  "ERROR",
  "error",
];

/**
 * Statuses that mean the checker reached the service and got a verdict
 * about the account.
 *
 * HIT    -- paid/active subscription
 * FREE   -- logs in but has no paid plan
 * FAIL   -- the service rejected the credentials (invalid account)
 * CUSTOM -- logs in but in an odd state (locked, max activations, reset needed)
 * 2FA    -- logs in but needs a second factor
 */
export const ACCOUNT_RESULT_STATUSES: AccountStatus[] = [
  "HIT",
  "FREE",
  "FAIL",
  "CUSTOM",
  "2FA",
];

/**
 * Statuses that mean "the account exists and is worth keeping".
 *
 * FAIL is excluded -- invalid credentials are not worth exporting.
 */
export const ACCOUNT_USABLE_STATUSES: AccountStatus[] = [
  "HIT",
  "FREE",
  "CUSTOM",
  "2FA",
];

/** Statuses that count as a subscription hit for the activity feed. */
export const ACCOUNT_HIT_STATUSES: AccountStatus[] = ["HIT"];

/**
 * Statuses that mean the integration is broken, not the account.
 *
 * These must not consume the user's daily quota -- the user did nothing
 * wrong and no account was actually checked.
 */
export const ACCOUNT_ERROR_STATUSES: AccountStatus[] = ["ERROR", "error"];

/** True when the status is a real account verdict (not a gateway failure). */
export function isAccountVerdict(status: string): boolean {
  return ACCOUNT_RESULT_STATUSES.includes(status as AccountStatus);
}

/** True when the status means the account is worth keeping. */
export function isAccountUsable(status: string): boolean {
  return ACCOUNT_USABLE_STATUSES.includes(status as AccountStatus);
}

/** True when the status means the integration is broken, not the account. */
export function isAccountError(status: string): boolean {
  return ACCOUNT_ERROR_STATUSES.includes(status as AccountStatus);
}

/**
 * Normalize whatever the checker returned into the known vocabulary.
 * Unknown values become "error" so a broken checker can never be read as
 * a pass.
 *
 * Case is significant for the two error states and is preserved:
 *   "error" -- the request never reached the service (network, rate limit)
 *   "ERROR" -- the service was reached but the integration is broken
 *              (endpoint moved, API client revoked, response shape changed)
 */
export function normalizeAccountStatus(raw: unknown): AccountStatus {
  const s = String(raw ?? "").trim();
  if ((ACCOUNT_STATUSES as string[]).includes(s)) return s as AccountStatus;
  const upper = s.toUpperCase();
  if ((ACCOUNT_STATUSES as string[]).includes(upper)) return upper as AccountStatus;
  return "error";
}
