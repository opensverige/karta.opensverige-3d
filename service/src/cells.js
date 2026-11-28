/* Claim and release: the lock on a cell. The lock is one conditional UPDATE, so two people
   claiming the same cell in the same instant get one winner and one 'taken'. */

import { requireUser } from './auth.js';
import { error, json } from './db.js';
import { acceptedTerms, termsRefusal } from './terms.js';
import { cellAt, cellCentre, gridRow, groundHeight, owns, sheetAt, sheetById } from './tiling.js';

export const LOCK_HOURS = 48;
/** How many uploads of a builder who is not trusted may wait for review at once. */
export const WAITING_LIMIT = 5;

/**
 * How many cells a user may hold locked at once.
 * @param {import('./db.js').User} user
 * @returns {number}
 */
export function lockLimit(user) {
  return user.trusted ? 8 : 1;
}

/**
 * Writes one line to the event log.
 * @param {D1Database} db
 * @param {{ at: string, github_id: number|null, login: string|null, action: string, sheet?: string|null, col?: number|null, row?: number|null, detail?: string|null }} e
 */
export async function logEvent(db, e) {
  await db
    .prepare('INSERT INTO events (at, github_id, login, action, sheet, col, row, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)')
    .bind(e.at, e.github_id ?? null, e.login ?? null, e.action, e.sheet ?? null, e.col ?? null, e.row ?? null, e.detail ?? null)
    .run();
}

/**
 * ISO time `hours` after `now`.
 * @param {string} now
 * @param {number} hours
 * @returns {string}
 */
function plusHours(now, hours) {
  return new Date(Date.parse(now) + hours * 3600 * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/**
 * Makes the rows a first claim on a sheet of the tiling needs: the sheet's, when nobody has
 * claimed there before, and the cell's, as free. Two claims at the same moment both come
 * here; INSERT OR IGNORE lets one write each row. Nothing is written unless the ground height
 * is known: a sheet is never made with a guessed height.
 * @param {D1Database} db
 * @param {import('./db.js').Env} env
 * @param {import('./db.js').User} user
 * @param {import('./tiling.js').Tile} tile
 * @param {boolean} sheetMissing
 * @param {number} col
 * @param {number} row
 * @param {string} now
 * @returns {Promise<{ ok: true } | { ok: false, status: number, code: string, message?: string }>}
 */
async function makeGridCell(db, env, user, tile, sheetMissing, col, row, now) {
  // Where two boxes meet, a cell belongs to the sheet whose box holds its middle.
  if (!owns(tile, col, row)) {
    const m = cellCentre(tile, col, row);
    const other = sheetAt(m.lat, m.lon);
    return { ok: false, status: 409, code: 'other_sheet', message: `That ground belongs to the sheet ${other ? other.id : 'next to it'}. Ask /v1/place for its cell there.` };
  }
  // What is drawn by hand is not gridded again: that ground belongs to its own sheet.
  const centre = cellCentre(tile, col, row);
  const { results: hand } = await db.prepare("SELECT * FROM sheets WHERE made = 'hand'").all();
  const owner = hand.find((sheet) => cellAt(sheet, centre.lat, centre.lon));
  if (owner) return { ok: false, status: 409, code: 'other_sheet', message: `That ground belongs to the sheet ${owner.id}. Ask /v1/place for its cell there.` };
  if (sheetMissing) {
    if (!env.PIPELINE_NOW) return { ok: false, status: 503, code: 'misconfigured' };
    const height = await groundHeight(tile.origin_lat, tile.origin_lon);
    if (height === null) return { ok: false, status: 503, code: 'no_height' };
    const s = gridRow(tile, env.PIPELINE_NOW, height);
    const made = await db
      .prepare(
        'INSERT OR IGNORE INTO sheets (id, city, name, cols, rows, origin_lat, origin_lon, x_meters, y_meters, ground_height, dzi_levels, pipeline, state, queue_order, rev, kind, made) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
      )
      .bind(s.id, s.city, s.name, s.cols, s.rows, s.origin_lat, s.origin_lon, s.x_meters, s.y_meters, s.ground_height, s.dzi_levels, s.pipeline, s.state, s.queue_order, s.rev, s.kind, s.made)
      .run();
    if (made.meta.changes === 1) {
      await logEvent(db, { at: now, github_id: user.github_id, login: user.login, action: 'make_sheet', sheet: s.id, detail: `ground height ${height} m` });
    }
  }
  await db.prepare("INSERT OR IGNORE INTO cells (sheet, col, row, state, updated_at) VALUES (?, ?, ?, 'fri', ?)").bind(tile.id, col, row, now).run();
  return { ok: true };
}

/**
 * Locks a free cell for the user. On a sheet of the tiling (an id sheetById knows) the cell
 * need not have a row, nor the sheet: the first claim there makes them, once every refusal
 * that needs no row has had its say.
 * @param {D1Database} db
 * @param {import('./db.js').User} user
 * @param {string} sheet
 * @param {number} col
 * @param {number} row
 * @param {string} now
 * @param {import('./db.js').Env} [env] read only when a sheet of the tiling gets its row
 * @param {string} [kind] the source the client says it will draw the sheet from
 * @param {string} [terms] the terms version the caller accepted, kept with the claim's event
 * @returns {Promise<{ ok: true, expires_at: string } | { ok: false, status: number, code: string, message?: string }>}
 */
export async function claimCell(db, user, sheet, col, row, now, env = {}, kind = undefined, terms = undefined) {
  const sheetRow = await db.prepare('SELECT id, state, made, kind FROM sheets WHERE id = ?').bind(sheet).first();
  // A row made by hand keeps to its own cells, whatever its id looks like.
  const tile = !sheetRow || sheetRow.made === 'grid' ? sheetById(sheet) : null;
  if (!sheetRow && !tile) return { ok: false, status: 404, code: 'no_sheet' };
  // A client from before the tiling has no source for such a sheet: it would lock the cell
  // and fail at the render. One that knows the sheet says its source with the claim.
  if (tile && kind !== (sheetRow ? sheetRow.kind : 'google')) return { ok: false, status: 426, code: 'update_needed' };
  if (sheetRow && sheetRow.state !== 'open') return { ok: false, status: 403, code: 'sheet_closed' };
  const cell = sheetRow ? await db.prepare('SELECT state, removed_at FROM cells WHERE sheet = ? AND col = ? AND row = ?').bind(sheet, col, row).first() : null;
  if (!cell && !(tile && col < tile.cols && row < tile.rows)) return { ok: false, status: 404, code: 'no_cell' };
  if (cell && cell.state === 'ute') return { ok: false, status: 422, code: 'outside' };
  // A removed live cell waits for the intake to blank its tiles before anyone may draw it again.
  if (cell && cell.removed_at) return { ok: false, status: 409, code: 'being_removed' };
  const limit = lockLimit(user);
  // A lock past its expiry no longer counts and may be taken over, even before the cron frees it.
  const held = await db.prepare("SELECT COUNT(*) AS n FROM cells WHERE owner_id = ? AND state = 'last' AND expires_at > ?").bind(user.github_id, now).first();
  if (held.n >= limit) return { ok: false, status: 403, code: 'too_many_locks' };
  // Uploading frees the lock. Without a limit on what waits, one new account could move 20 cells
  // a day out of everyone's reach and into the reviewer's queue.
  const waitingLimit = user.trusted ? 1e9 : WAITING_LIMIT;
  const waiting = await db.prepare("SELECT COUNT(*) AS n FROM cells WHERE owner_id = ? AND state = 'vantar'").bind(user.github_id).first();
  if (waiting.n >= waitingLimit) return { ok: false, status: 403, code: 'too_many_waiting' };
  if (!cell) {
    const made = await makeGridCell(db, env, user, tile, !sheetRow, col, row, now);
    if (!made.ok) return made;
  }
  const expires_at = plusHours(now, LOCK_HOURS);
  // The lock and the limit are decided by this one statement, so parallel claims cannot
  // slip past the count read above.
  const result = await db
    .prepare(
      "UPDATE cells SET state = 'last', owner_id = ?, owner_login = ?, locked_at = ?, expires_at = ?, rejected_at = NULL, comment = NULL, updated_at = ? WHERE sheet = ? AND col = ? AND row = ? AND removed_at IS NULL AND (state = 'fri' OR (state = 'last' AND expires_at <= ? AND uploaded_at IS NULL)) AND (SELECT COUNT(*) FROM cells WHERE owner_id = ? AND state = 'last' AND expires_at > ?) < ? AND (SELECT COUNT(*) FROM cells WHERE owner_id = ? AND state = 'vantar') < ?",
    )
    .bind(user.github_id, user.login, now, expires_at, now, sheet, col, row, now, user.github_id, now, limit, user.github_id, waitingLimit)
    .run();
  if (result.meta.changes !== 1) {
    const after = await db.prepare('SELECT state, removed_at FROM cells WHERE sheet = ? AND col = ? AND row = ?').bind(sheet, col, row).first();
    if (after && after.removed_at) return { ok: false, status: 409, code: 'being_removed' };
    if (after && after.state === 'fri') return { ok: false, status: 403, code: 'too_many_locks' };
    return { ok: false, status: 409, code: 'taken' };
  }
  await logEvent(db, { at: now, github_id: user.github_id, login: user.login, action: 'claim', sheet, col, row, detail: terms ? `terms ${terms}` : null });
  return { ok: true, expires_at };
}

/**
 * Gives an unfinished lock back.
 * @param {D1Database} db
 * @param {import('./db.js').User} user
 * @param {string} sheet
 * @param {number} col
 * @param {number} row
 * @param {string} now
 * @returns {Promise<{ ok: true } | { ok: false, status: number, code: string }>}
 */
export async function releaseCell(db, user, sheet, col, row, now) {
  const cell = await db.prepare('SELECT state, owner_id, uploaded_at FROM cells WHERE sheet = ? AND col = ? AND row = ?').bind(sheet, col, row).first();
  if (!cell) return { ok: false, status: 404, code: 'no_cell' };
  if (cell.owner_id !== user.github_id) return { ok: false, status: 403, code: 'not_yours' };
  if (cell.state !== 'last' || cell.uploaded_at) return { ok: false, status: 409, code: 'already_uploaded' };
  const r = await db
    .prepare(
      "UPDATE cells SET state = 'fri', owner_id = NULL, owner_login = NULL, locked_at = NULL, expires_at = NULL, updated_at = ? WHERE sheet = ? AND col = ? AND row = ? AND state = 'last' AND owner_id = ? AND uploaded_at IS NULL",
    )
    .bind(now, sheet, col, row, user.github_id)
    .run();
  if (r.meta.changes !== 1) return { ok: false, status: 409, code: 'already_uploaded' };
  await logEvent(db, { at: now, github_id: user.github_id, login: user.login, action: 'release', sheet, col, row });
  return { ok: true };
}

const MESSAGES = {
  no_sheet: 'No such sheet.',
  no_cell: 'No such cell.',
  sheet_closed: 'That sheet is not open for claims.',
  outside: 'That cell lies outside the city and is not drawn.',
  too_many_locks: 'You already hold as many cells as you may. Finish or release one first.',
  too_many_waiting: 'Five of your cells are waiting for review. Take another when one has been reviewed.',
  taken: 'That cell is taken: it is locked, waits for review or is on the map. Pick another.',
  being_removed: 'That cell is being taken off the map. Try again in an hour.',
  other_sheet: 'That ground belongs to another sheet.',
  no_height: 'The ground height of that place could not be read just now, so its sheet was not made. Try again in a minute.',
  misconfigured: 'The service is not set up to make new sheets yet.',
  update_needed: 'That sheet needs a newer rutor than this one. Update it, then claim again.',
  not_yours: 'That cell is not yours.',
  already_uploaded: 'That cell is already uploaded and cannot be released.',
};

/**
 * Reads { sheet, col, row } from a JSON body, and `kind` when the client says which source
 * it will draw the sheet from.
 * @param {Request} request
 * @returns {Promise<{ sheet: string, col: number, row: number, kind?: string } | null>}
 */
export async function readCellBody(request) {
  let body;
  try {
    body = await request.json();
  } catch {
    return null;
  }
  if (!body || typeof body.sheet !== 'string' || !/^[a-z0-9-]+$/.test(body.sheet)) return null;
  if (!Number.isInteger(body.col) || !Number.isInteger(body.row) || body.col < 0 || body.row < 0) return null;
  if (body.kind !== undefined && body.kind !== 'open' && body.kind !== 'google') return null;
  const cell = { sheet: body.sheet, col: body.col, row: body.row };
  return body.kind === undefined ? cell : { ...cell, kind: body.kind };
}

/** POST /v1/claim */
export const claimRoute = requireUser(async (request, env, ctx, params, now, user) => {
  const terms = acceptedTerms(request);
  if (!terms) return termsRefusal();
  const body = await readCellBody(request);
  if (!body) return error(400, 'bad_body', 'Send JSON with sheet, col and row (integers from 0), and kind as open or google when you send it.');
  const r = await claimCell(env.DB, user, body.sheet, body.col, body.row, now, env, body.kind, terms);
  if (!r.ok) return error(r.status, r.code, r.message || MESSAGES[r.code], r.status === 503 ? { 'Retry-After': '60' } : {});
  return json(201, { sheet: body.sheet, col: body.col, row: body.row, expires_at: r.expires_at });
});

/** POST /v1/release */
export const releaseRoute = requireUser(async (request, env, ctx, params, now, user) => {
  const body = await readCellBody(request);
  if (!body) return error(400, 'bad_body', 'Send JSON with sheet, col and row (integers from 0).');
  const r = await releaseCell(env.DB, user, body.sheet, body.col, body.row, now);
  if (!r.ok) return error(r.status, r.code, MESSAGES[r.code]);
  return json(200, { ok: true });
});
