/* Who is calling. The CLI sends the GitHub user token it got from the device flow as a
   Bearer header; we ask GitHub who it belongs to and cache the answer ten minutes in KV under
   a hash of the token, never the token itself. The service has no sessions of its own.

   Whose token: with the secret GITHUB_CLIENT_SECRET set (and GITHUB_CLIENT_ID), GitHub is asked
   through the OAuth app's own check, which only knows tokens that app issued. Without it GitHub
   is asked who the token is, and then any GitHub token at all proves an identity here: one from
   another site's "Sign in with GitHub" too. Set the secret in production. */

import { error, nowIso } from './db.js';

const GITHUB_USER = 'https://api.github.com/user';
const CACHE_TTL = 600;
// A token GitHub refused is remembered here for a minute, by its hash, so that a caller who
// repeats it does not make this service ask GitHub once per request. In memory only: it costs
// no storage writes, and a new isolate simply asks once more.
const BAD_SECONDS = 60;
const BAD_MAX = 5000;
/** @type {Map<string, number>} */
const refused = new Map();

/**
 * @param {string} key
 * @param {string} now
 * @returns {boolean}
 */
function knownBad(key, now) {
  const until = refused.get(key);
  if (until === undefined) return false;
  if (until > Date.parse(now)) return true;
  refused.delete(key);
  return false;
}

/**
 * @param {string} key
 * @param {string} now
 */
function rememberBad(key, now) {
  if (refused.size >= BAD_MAX) refused.clear();
  refused.set(key, Date.parse(now) + BAD_SECONDS * 1000);
}

/**
 * Hex sha256 of a string.
 * @param {string} text
 * @returns {Promise<string>}
 */
async function sha256Hex(text) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * The bearer token on a request, or null.
 * @param {Request} request
 * @returns {string|null}
 */
function bearer(request) {
  const header = request.headers.get('Authorization') || '';
  const found = /^Bearer\s+(\S+)$/i.exec(header.trim());
  return found ? found[1] : null;
}

/**
 * Asks GitHub who the token belongs to.
 * @param {string} token
 * @returns {Promise<{ id: number, login: string } | 'bad' | 'down'>}
 */
async function lookupGithub(token) {
  let response;
  try {
    response = await fetch(GITHUB_USER, {
      headers: {
        Authorization: `Bearer ${token}`,
        'User-Agent': 'pixelbygget',
        Accept: 'application/vnd.github+json',
      },
    });
  } catch {
    return 'down';
  }
  if (response.status === 401) return 'bad';
  // The login asks for no scopes, so reading /user never lacks a right: a 403 is GitHub's rate
  // or abuse limit, and telling the caller to log in again would be wrong.
  if (!response.ok) return 'down';
  let body;
  try {
    body = await response.json();
  } catch {
    return 'down';
  }
  if (!body || typeof body.id !== 'number' || typeof body.login !== 'string') return 'down';
  return { id: body.id, login: body.login };
}

/**
 * Asks GitHub whether our own OAuth app issued the token, and for whom.
 * @param {string} token
 * @param {string} clientId
 * @param {string} secret
 * @returns {Promise<{ id: number, login: string } | 'bad' | 'down'>}
 */
async function lookupOwnApp(token, clientId, secret) {
  let response;
  try {
    response = await fetch(`https://api.github.com/applications/${encodeURIComponent(clientId)}/token`, {
      method: 'POST',
      headers: {
        Authorization: 'Basic ' + btoa(`${clientId}:${secret}`),
        'User-Agent': 'pixelbygget',
        Accept: 'application/vnd.github+json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ access_token: token }),
    });
  } catch {
    return 'down';
  }
  // 404: no such token for this app. 422: not a token at all.
  if (response.status === 404 || response.status === 422) return 'bad';
  if (!response.ok) return 'down';
  let body;
  try {
    body = await response.json();
  } catch {
    return 'down';
  }
  const user = body && body.user;
  if (!user || typeof user.id !== 'number' || typeof user.login !== 'string') return 'down';
  return { id: user.id, login: user.login };
}

/**
 * True when the request carries a token the KV cache already knows as the admin's. It never
 * asks GitHub, so it is cheap enough to run before the rate limit; a token GitHub has not
 * vouched for in the last ten minutes simply does not pass.
 * @param {Request} request
 * @param {import('./db.js').Env} env
 * @returns {Promise<boolean>}
 */
export async function isCachedAdmin(request, env) {
  if (await isIntake(request, env)) return true;
  const token = bearer(request);
  const admin = String(env.ADMIN_GITHUB_ID || '');
  if (!token || !admin || admin === '0') return false;
  try {
    const identity = await env.GH.get('gh:' + (await sha256Hex(token)), 'json');
    return Boolean(identity) && String(identity.id) === admin;
  } catch {
    return false;
  }
}

/**
 * Resolves the caller to a user row, creating it on first sight.
 * Returns a Response when the caller must stop: 401 no_token, 401 bad_token, 403 blocked,
 * 503 github_down.
 * @param {Request} request
 * @param {import('./db.js').Env} env
 * @param {string} now
 * @returns {Promise<{ user: import('./db.js').User, isAdmin: boolean } | Response>}
 */
export async function resolveUser(request, env, now) {
  const token = bearer(request);
  if (!token) return error(401, 'no_token', 'Log in first: rutor login.');
  const key = 'gh:' + (await sha256Hex(token));
  const refusal = () => error(401, 'bad_token', 'GitHub does not accept this token. Log in again: rutor login.');
  if (knownBad(key, now)) return refusal();
  // The cache is a saving, not a gate: when it does not answer, GitHub is asked.
  let identity = await env.GH.get(key, 'json').catch(() => null);
  let fresh = false;
  if (!identity) {
    const secret = String(env.GITHUB_CLIENT_SECRET || '');
    const clientId = String(env.GITHUB_CLIENT_ID || '');
    // The secret means "our own logins only". Without the app's id that cannot be asked, and
    // falling back to "any GitHub token" would quietly undo it.
    if (secret && !clientId) return error(503, 'misconfigured', 'The service is set up wrongly. Try again later.', { 'Retry-After': '300' });
    const looked = secret ? await lookupOwnApp(token, clientId, secret) : await lookupGithub(token);
    if (looked === 'bad') {
      rememberBad(key, now);
      return refusal();
    }
    if (looked === 'down') return error(503, 'github_down', 'GitHub did not answer. Try again in a minute.', { 'Retry-After': '60' });
    identity = looked;
    await env.GH.put(key, JSON.stringify(identity), { expirationTtl: CACHE_TTL }).catch(() => undefined);
    fresh = true;
  }
  // The row is written when GitHub was just asked, or when it is missing: not on every request,
  // so one caller cannot spend the database's daily writes.
  const upsert = () =>
    env.DB.prepare(
      'INSERT INTO users (github_id, login, first_seen) VALUES (?, ?, ?) ON CONFLICT(github_id) DO UPDATE SET login = excluded.login',
    )
      .bind(identity.id, identity.login, now)
      .run();
  if (fresh) await upsert();
  let user = await env.DB.prepare('SELECT * FROM users WHERE github_id = ?').bind(identity.id).first();
  if (!user) {
    await upsert();
    user = await env.DB.prepare('SELECT * FROM users WHERE github_id = ?').bind(identity.id).first();
  }
  if (user.blocked) return error(403, 'blocked', 'This account is blocked from Pixelbygget.');
  const isAdmin = String(user.github_id) === String(env.ADMIN_GITHUB_ID || '');
  return { user, isAdmin };
}

/**
 * Wraps a handler so it runs with a resolved user as its sixth argument.
 * @param {(request: Request, env: import('./db.js').Env, ctx: ExecutionContext, params: Record<string, string>, now: string, user: import('./db.js').User, isAdmin: boolean) => Promise<Response>} handler
 */
export function requireUser(handler) {
  return async (request, env, ctx, params, now = nowIso()) => {
    const resolved = await resolveUser(request, env, now);
    if (resolved instanceof Response) return resolved;
    return handler(request, env, ctx, params, now, resolved.user, resolved.isAdmin);
  };
}

/** The intake key is this long at least. A shorter one is a mistake, and is not let through. */
const INTAKE_MIN = 32;

/**
 * Whether the caller sends the intake key (secret INTAG_NYCKEL). The nightly publication runs on
 * a machine of its own, where a GitHub login cannot be renewed by hand. Compared as digests, so
 * the time taken does not say how much of it was right.
 * @param {Request} request
 * @param {import('./db.js').Env} env
 * @returns {Promise<boolean>}
 */
export async function isIntake(request, env) {
  const key = String(env.INTAG_NYCKEL || '');
  const token = bearer(request);
  if (key.length < INTAKE_MIN || !token) return false;
  const [a, b] = await Promise.all([sha256Hex(token), sha256Hex(key)]);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/**
 * Like requireAdmin, and the intake key may pass too. Only the routes the publication needs are
 * wrapped with this: the queue, the inbox, live and blanked. The key acts as the admin, so the
 * event log names the admin. Approve, reject, trust and block stay with a person.
 * @param {(request: Request, env: import('./db.js').Env, ctx: ExecutionContext, params: Record<string, string>, now: string, user: import('./db.js').User) => Promise<Response>} handler
 */
export function requireIntake(handler) {
  const asAdmin = requireAdmin(handler);
  return async (request, env, ctx, params, now = nowIso()) => {
    if (!(await isIntake(request, env))) return asAdmin(request, env, ctx, params, now);
    const admin = await env.DB.prepare('SELECT * FROM users WHERE github_id = ?').bind(Number(env.ADMIN_GITHUB_ID || 0)).first();
    if (!admin) return error(503, 'misconfigured', 'The service is set up wrongly. Try again later.', { 'Retry-After': '300' });
    return handler(request, env, ctx, params, now, admin);
  };
}

/**
 * Like requireUser, but only the admin may pass: 403 not_admin otherwise.
 * @param {(request: Request, env: import('./db.js').Env, ctx: ExecutionContext, params: Record<string, string>, now: string, user: import('./db.js').User) => Promise<Response>} handler
 */
export function requireAdmin(handler) {
  return requireUser(async (request, env, ctx, params, now, user, isAdmin) => {
    if (!isAdmin) return error(403, 'not_admin', 'Only the admin may do this.');
    return handler(request, env, ctx, params, now, user);
  });
}
