/**
 * Authorize.net (/an) API reference.
 *
 * The data lives in @shared/api-registry so the server's /api/docs endpoint
 * and this page render the same source and never drift apart.
 */

export {
  API_ENDPOINTS,
  API_LIMITS,
  API_FLOW,
  API_META,
  API_NOTE,
  REASON_CODES,
  STRIPE_FLOW,
  STRIPE_LIMITS,
  STRIPE_REASON_CODES,
  buildCurl,
  type AuthLevel,
  type ApiParam,
  type ApiEndpoint,
  type ApiLimits,
  type ReasonCode,
} from "@shared/api-registry";
