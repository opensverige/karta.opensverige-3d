/* The admin's side: approve, reject, trust, block, the queue, and the intake reporting cells
   live. Every action is one line in the event log. */

import { requireAdmin, requireIntake } from './auth.js';
import { logEvent, readCellBody } from './cells.js';
import { error, json, nowIso } from './db.js';

const COMMENT_MAX = 500;
const PURGE_DAYS = 30;

/**
 * ISO time `days` after `now`.
 * @param {string} now
 * @param {number} days
 * @returns {string}
 */
function plusDays(now, days) {
  return new Date(Date.parse(now) + days * 24 * 3600 * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/**
 * The cells as a JSON array for json_each, without duplicates, or null when one repeats.
 * @param {Array<[number, number]>} cells
 * @returns {string|null}
 */
function cellList(cells) {
  const seen = new Set(cells.map(([col, row]) => `${col}_${row}`));
  return seen.size === cells.length ? JSON.stringify(cells) : null;
}

const IN_LIST = "(col, row) IN (SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') FROM json_each(?))";

/**
 * vantar → godkand. The owner's approved_count grows.
 * @param {D1Database} db
 * @param {string} sheet
 * @param {number} col
 * @param {number} row
 * @param {string} now
 * @returns {Promise<{ ok: true } | { ok: false, status: number, code: string }>}
 */
export async function approveCell(db, sheet, col, row, now) {
  const cell = await db.prepare('SELECT state, owner_id FROM cells WHERE sheet = ? AND col = ? AND row = ?').bind(sheet, col, row).first();
  if (!cell || cell.state !== 'vantar') return { ok: false, status: 409, code: 'not_waiting' };
  const moved = await db
    .prepare("UPDATE cells SET state = 'godkand', approved_at = ?, updated_at = ? WHERE sheet = ? AND col = ? AND row = ? AND state = 'vantar'")
    .bind(now, now, sheet, col, row)
    .run();
  // A second approval that lost the race counts nothing and gives no trust.
  if (moved.meta.changes !== 1) return { ok: false, status: 409, code: 'not_waiting' };
  // Trust is never given here. A trusted builder's cells skip review, so a person decides who
  // that is, with POST /trust.
  await db.prepare('UPDATE users SET approved_count = approved_count + 1 WHERE github_id = ?').bind(cell.owner_id).run();
  return { ok: true };
}

/**
 * vantar, godkand or live → fri with a comment. A waiting or approved upload goes to the purge
 * list for 30 days. A live cell keeps its inbox key and is marked removed until the intake has
 * blanked its tiles (POST /blanked); until then nobody may claim it. One event is logged, by
 * the admin when one is given, naming the owner.
 * @param {D1Database} db
 * @param {R2Bucket} inkorg
 * @param {string} sheet
 * @param {number} col
 * @param {number} row
 * @param {string} comment
 * @param {string} now
 * @param {{ github_id: number, login: string }|null} [admin]
 * @returns {Promise<{ ok: true } | { ok: false, status: number, code: string }>}
 */
export async function rejectCell(db, inkorg, sheet, col, row, comment, now, admin = null) {
  const cell = await db.prepare('SELECT state, owner_id, owner_login, inbox_key FROM cells WHERE sheet = ? AND col = ? AND row = ?').bind(sheet, col, row).first();
  if (!cell || !['vantar', 'godkand', 'live'].includes(cell.state)) return { ok: false, status: 409, code: 'nothing_to_reject' };
  const wasLive = cell.state === 'live';
  // Only from the state that was read: a cell the intake just made live must be removed, not freed.
  const moved = await db
    .prepare(
      "UPDATE cells SET state = 'fri', owner_id = NULL, owner_login = NULL, locked_at = NULL, expires_at = NULL, uploaded_at = NULL, approved_at = NULL, live_at = NULL, bytes = NULL, sha256 = NULL, inbox_key = ?, rejected_at = ?, removed_at = ?, comment = ?, updated_at = ? WHERE sheet = ? AND col = ? AND row = ? AND state = ?",
    )
    .bind(wasLive ? cell.inbox_key : null, wasLive ? null : now, wasLive ? now : null, comment, now, sheet, col, row, cell.state)
    .run();
  if (moved.meta.changes !== 1) return { ok: false, status: 409, code: 'changed_meanwhile' };
  if (!wasLive && cell.inbox_key) {
    const due = plusDays(now, PURGE_DAYS);
    await db.batch(['png', 'json'].map((ext) => db.prepare('INSERT OR REPLACE INTO purge (key, due) VALUES (?, ?)').bind(`${cell.inbox_key}.${ext}`, due)));
  }
  await logEvent(db, {
    at: now,
    github_id: admin ? admin.github_id : null,
    login: admin ? admin.login : null,
    action: 'reject',
    sheet,
    col,
    row,
    detail: `${cell.owner_login}: ` + (wasLive ? 'live-removed: ' : '') + comment,
  });
  return { ok: true };
}

/**
 * The one user a trust or block applies to: by github_id, or by login regardless of case.
 * A login two rows share (a rename) must be given as github_id.
 * @param {D1Database} db
 * @param {{ login?: string, github_id?: number }} who
 * @returns {Promise<{ ok: true, github_id: number } | { ok: false, status: number, code: string }>}
 */
async function findUser(db, who) {
  const { results } = Number.isInteger(who.github_id)
    ? await db.prepare('SELECT github_id FROM users WHERE github_id = ?').bind(who.github_id).all()
    : await db.prepare('SELECT github_id FROM users WHERE login = ? COLLATE NOCASE').bind(String(who.login || '')).all();
  if (results.length === 0) return { ok: false, status: 404, code: 'no_user' };
  if (results.length > 1) return { ok: false, status: 409, code: 'ambiguous_login' };
  return { ok: true, github_id: results[0].github_id };
}

/**
 * @param {D1Database} db
 * @param {{ login?: string, github_id?: number }} who
 * @param {0|1} trusted
 */
export async function setTrust(db, who, trusted) {
  const found = await findUser(db, who);
  if (!found.ok) return found;
  await db.prepare('UPDATE users SET trusted = ? WHERE github_id = ?').bind(trusted, found.github_id).run();
  return { ok: true };
}

/**
 * @param {D1Database} db
 * @param {{ login?: string, github_id?: number }} who
 * @param {0|1} blocked
 * @param {string} [now]
 */
export async function setBlock(db, who, blocked, now = nowIso()) {
  const found = await findUser(db, who);
  if (!found.ok) return found;
  const statements = [db.prepare('UPDATE users SET blocked = ? WHERE github_id = ?').bind(blocked, found.github_id)];
  if (blocked) {
    // A blocked builder cannot release, so their open lock would hold a cell for two days.
    // What they have uploaded stays where it is, for the reviewer.
    statements.push(
      db
        .prepare(
          "UPDATE cells SET state = 'fri', owner_id = NULL, owner_login = NULL, locked_at = NULL, expires_at = NULL, updated_at = ? WHERE owner_id = ? AND state = 'last' AND uploaded_at IS NULL",
        )
        .bind(now, found.github_id),
    );
  }
  await db.batch(statements);
  return { ok: true };
}

/**
 * Cells in a state, each with its eight neighbours as [col, row, state]. The state 'removed'
 * lists live cells the admin took down that the intake has not blanked yet.
 * @param {D1Database} db
 * @param {'vantar'|'godkand'|'removed'} state
 * @returns {Promise<Array<import('./db.js').Cell & { neighbours: Array<[number, number, string]> }>>}
 */
export async function listQueue(db, state) {
  const query =
    state === 'removed'
      ? db.prepare('SELECT * FROM cells WHERE removed_at IS NOT NULL ORDER BY removed_at, sheet, row, col')
      : db.prepare('SELECT * FROM cells WHERE state = ? ORDER BY uploaded_at, sheet, row, col').bind(state);
  const { results } = await query.all();
  const around = new Map();
  for (const sheet of new Set(results.map((c) => c.sheet))) {
    const wanted = [];
    for (const c of results.filter((x) => x.sheet === sheet)) {
      for (let dc = -1; dc <= 1; dc += 1) for (let dr = -1; dr <= 1; dr += 1) if (dc || dr) wanted.push([c.col + dc, c.row + dr]);
    }
    const unique = [...new Map(wanted.map((w) => [`${w[0]}_${w[1]}`, w])).values()];
    const { results: found } = await db.prepare(`SELECT col, row, state FROM cells WHERE sheet = ? AND ${IN_LIST}`).bind(sheet, JSON.stringify(unique)).all();
    for (const n of found) around.set(`${sheet}/${n.col}_${n.row}`, n.state);
  }
  return results.map((cell) => {
    const neighbours = [];
    for (let dr = -1; dr <= 1; dr += 1) {
      for (let dc = -1; dc <= 1; dc += 1) {
        if (!dc && !dr) continue;
        const state = around.get(`${cell.sheet}/${cell.col + dc}_${cell.row + dr}`);
        if (state !== undefined) neighbours.push([cell.col + dc, cell.row + dr, state]);
      }
    }
    return { ...cell, neighbours };
  });
}

/**
 * godkand → live for every listed cell, and the sheet's revision, or nothing at all.
 * @param {D1Database} db
 * @param {string} sheet
 * @param {Array<[number, number]>} cells
 * @param {number} rev
 * @param {string} now
 * @returns {Promise<{ ok: true, live: number } | { ok: false, status: number, code: string }>}
 */
export async function markLive(db, sheet, cells, rev, now) {
  const list = cellList(cells);
  if (list === null) return { ok: false, status: 400, code: 'duplicate_cells' };
  const sheetRow = await db.prepare('SELECT rev FROM sheets WHERE id = ?').bind(sheet).first();
  if (!sheetRow) return { ok: false, status: 404, code: 'no_sheet' };
  if (rev <= sheetRow.rev) return { ok: false, status: 409, code: 'stale_rev' };
  // One transaction: the revision moves only when every listed cell is approved at that
  // moment, and the cells move only when the revision did, so it is all or nothing.
  const [bump, moved] = await db.batch([
    db
      .prepare(`UPDATE sheets SET rev = ? WHERE id = ? AND rev < ? AND (SELECT COUNT(*) FROM cells WHERE sheet = ? AND state = 'godkand' AND ${IN_LIST}) = ?`)
      .bind(rev, sheet, rev, sheet, list, cells.length),
    db
      // changes() is the row count of the revision bump just before, in the same transaction:
      // a second call with the same rev moves nothing.
      .prepare(`UPDATE cells SET state = 'live', live_at = ?, updated_at = ? WHERE sheet = ? AND state = 'godkand' AND ${IN_LIST} AND changes() = 1 AND (SELECT rev FROM sheets WHERE id = ?) = ?`)
      .bind(now, now, sheet, list, sheet, rev),
  ]);
  if (bump.meta.changes !== 1) {
    const current = await db.prepare('SELECT rev FROM sheets WHERE id = ?').bind(sheet).first();
    return { ok: false, status: 409, code: current && current.rev >= rev ? 'stale_rev' : 'not_approved' };
  }
  return { ok: true, live: moved.meta.changes };
}

/**
 * The intake has blanked the tiles of removed cells: forget the mark and drop the objects.
 * @param {D1Database} db
 * @param {R2Bucket} inkorg
 * @param {string} sheet
 * @param {Array<[number, number]>} cells
 * @param {string} now
 * @returns {Promise<{ ok: true, blanked: number } | { ok: false, status: number, code: string }>}
 */
export async function markBlanked(db, inkorg, sheet, cells, now) {
  const list = cellList(cells);
  if (list === null) return { ok: false, status: 400, code: 'duplicate_cells' };
  const { results } = await db.prepare(`SELECT col, row, inbox_key FROM cells WHERE sheet = ? AND removed_at IS NOT NULL AND ${IN_LIST}`).bind(sheet, list).all();
  if (results.length === 0) return { ok: true, blanked: 0 };
  const objects = results.filter((c) => c.inbox_key).flatMap((c) => [`${c.inbox_key}.png`, `${c.inbox_key}.json`]);
  if (objects.length) await inkorg.delete(objects);
  await db.batch(
    results.map((c) =>
      db.prepare('UPDATE cells SET removed_at = NULL, inbox_key = NULL, updated_at = ? WHERE sheet = ? AND col = ? AND row = ? AND removed_at IS NOT NULL').bind(now, sheet, c.col, c.row),
    ),
  );
  return { ok: true, blanked: results.length };
}

const MESSAGES = {
  not_waiting: 'That cell is not waiting for review.',
  nothing_to_reject: 'That cell has nothing uploaded to reject.',
  no_user: 'No user with that login or id.',
  ambiguous_login: 'Two users share that login. Send github_id instead.',
  duplicate_cells: 'A cell is listed twice. Nothing was changed.',
  changed_meanwhile: 'That cell changed while you rejected it. Read the queue again.',
  not_approved: 'Every listed cell must be approved. Nothing was marked live.',
  stale_rev: 'That revision is not newer than the sheet’s. Read the sheet again.',
  no_sheet: 'No such sheet.',
};

function fail(r) {
  return error(r.status, r.code, MESSAGES[r.code] || r.code);
}

/** POST /v1/approve */
export const approveRoute = requireAdmin(async (request, env, ctx, params, now, admin) => {
  const body = await readCellBody(request);
  if (!body) return error(400, 'bad_body', 'Send JSON with sheet, col and row.');
  const r = await approveCell(env.DB, body.sheet, body.col, body.row, now);
  if (!r.ok) return fail(r);
  await logEvent(env.DB, { at: now, github_id: admin.github_id, login: admin.login, action: 'approve', ...body });
  return json(200, { ok: true });
});

/** POST /v1/reject */
export const rejectRoute = requireAdmin(async (request, env, ctx, params, now, admin) => {
  const raw = await request.clone().json().catch(() => null);
  const body = await readCellBody(request);
  const comment = raw && typeof raw.comment === 'string' ? raw.comment.trim() : '';
  if (!body || !comment || comment.length > COMMENT_MAX) return error(400, 'bad_body', `Send JSON with sheet, col, row and a comment of 1 to ${COMMENT_MAX} characters.`);
  const r = await rejectCell(env.DB, env.INKORG, body.sheet, body.col, body.row, comment, now, admin);
  if (!r.ok) return fail(r);
  return json(200, { ok: true });
});

/** POST /v1/trust and POST /v1/block */
function flagRoute(field, apply) {
  return requireAdmin(async (request, env, ctx, params, now, admin) => {
    const body = await request.json().catch(() => null);
    const who = body && Number.isInteger(body.github_id) ? { github_id: body.github_id } : body && typeof body.login === 'string' ? { login: body.login } : null;
    if (!who || ![0, 1].includes(body[field])) return error(400, 'bad_body', `Send JSON with login or github_id, and ${field} as 0 or 1.`);
    const r = await apply(env.DB, who, body[field], now);
    if (!r.ok) return fail(r);
    await logEvent(env.DB, { at: now, github_id: admin.github_id, login: admin.login, action: field, detail: `${who.github_id ?? who.login}=${body[field]}` });
    return json(200, { ok: true });
  });
}
export const trustRoute = flagRoute('trusted', setTrust);
export const blockRoute = flagRoute('blocked', setBlock);

/** GET /v1/queue?state=vantar|godkand|removed */
export const queueRoute = requireIntake(async (request, env) => {
  const state = new URL(request.url).searchParams.get('state') || 'vantar';
  if (!['vantar', 'godkand', 'removed'].includes(state)) return error(400, 'bad_state', 'state must be vantar, godkand or removed.');
  return json(200, { state, cells: await listQueue(env.DB, state) }, { 'Cache-Control': 'private, no-store' });
});

/** POST /v1/live */
export const liveRoute = requireIntake(async (request, env, ctx, params, now, admin) => {
  const body = await request.json().catch(() => null);
  const cellsOk = body && Array.isArray(body.cells) && body.cells.every((c) => Array.isArray(c) && c.length === 2 && Number.isInteger(c[0]) && Number.isInteger(c[1]));
  // An empty list would move the revision with no cell, and a revision beyond what a whole
  // number can hold would leave no later one to follow it.
  if (!cellsOk || body.cells.length === 0 || typeof body.sheet !== 'string' || !Number.isSafeInteger(body.rev) || body.rev < 1) {
    return error(400, 'bad_body', 'Send JSON with sheet, at least one cell as [[col,row],...] and rev as a whole number.');
  }
  const r = await markLive(env.DB, body.sheet, body.cells, body.rev, now);
  if (!r.ok) return fail(r);
  await logEvent(env.DB, { at: now, github_id: admin.github_id, login: admin.login, action: 'live', sheet: body.sheet, detail: `${r.live} cells, rev ${body.rev}` });
  return json(200, r);
});

/** POST /v1/blanked */
export const blankedRoute = requireIntake(async (request, env, ctx, params, now, admin) => {
  const body = await request.json().catch(() => null);
  const cellsOk = body && Array.isArray(body.cells) && body.cells.every((c) => Array.isArray(c) && c.length === 2 && Number.isInteger(c[0]) && Number.isInteger(c[1]));
  if (!cellsOk || typeof body.sheet !== 'string') return error(400, 'bad_body', 'Send JSON with sheet and cells as [[col,row],...].');
  const r = await markBlanked(env.DB, env.INKORG, body.sheet, body.cells, now);
  if (!r.ok) return fail(r);
  await logEvent(env.DB, { at: now, github_id: admin.github_id, login: admin.login, action: 'blanked', sheet: body.sheet, detail: `${r.blanked} cells` });
  return json(200, r);
});
