import { env } from 'cloudflare:test';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LOCK_HOURS, claimCell, lockLimit, releaseCell } from '../src/cells.js';
import { getGrid } from '../src/sheets.js';
import { nearestCell, sheetAt } from '../src/tiling.js';
import { NOW, call, clearDb, getCell, insertCells, insertSheet, insertUser, rowCounts, setCell, stubOutside } from './fixtures.js';
import { TERMS_VERSION } from '../src/terms.js';

const user = { github_id: 4242, login: 'klang', first_seen: NOW, approved_count: 0, trusted: 0, blocked: 0 };
const other = { github_id: 7, login: 'ebba', first_seen: NOW, approved_count: 12, trusted: 1, blocked: 0 };

beforeEach(async () => {
  await clearDb();
  await insertSheet();
  await insertCells('goteborg-norr', 4, 3, (col, row) => (col === 3 && row === 2 ? 'ute' : 'fri'));
  await insertUser(user);
  await insertUser(other);
});

describe('claimCell', () => {
  it('locks a free cell for 48 hours', async () => {
    const r = await claimCell(env.DB, user, 'goteborg-norr', 1, 1, NOW);
    expect(LOCK_HOURS).toBe(48);
    expect(r).toEqual({ ok: true, expires_at: '2026-11-19T12:00:00Z' });
    const cell = await getCell('goteborg-norr', 1, 1);
    expect(cell).toMatchObject({ state: 'last', owner_id: 4242, owner_login: 'klang', locked_at: NOW, expires_at: '2026-11-19T12:00:00Z', updated_at: NOW });
  });

  it('is taken when the cell is not free', async () => {
    await setCell('goteborg-norr', 1, 1, { state: 'last', owner_id: 7, owner_login: 'ebba' });
    const r = await claimCell(env.DB, user, 'goteborg-norr', 1, 1, NOW);
    expect(r).toMatchObject({ ok: false, status: 409, code: 'taken' });
  });

  it('refuses a cell outside the sheet mask', async () => {
    const r = await claimCell(env.DB, user, 'goteborg-norr', 3, 2, NOW);
    expect(r).toMatchObject({ ok: false, status: 422, code: 'outside' });
  });

  it('refuses a closed sheet and an unknown sheet', async () => {
    await insertSheet({ id: 'uppsala-norr', city: 'uppsala', name: 'Uppsala', state: 'queued', queue_order: 2 });
    await insertCells('uppsala-norr', 1, 1);
    expect(await claimCell(env.DB, user, 'uppsala-norr', 0, 0, NOW)).toMatchObject({ ok: false, status: 403, code: 'sheet_closed' });
    expect(await claimCell(env.DB, user, 'nope', 0, 0, NOW)).toMatchObject({ ok: false, status: 404, code: 'no_sheet' });
  });

  it('limits locks: 1 for a new user, 8 for a trusted one', async () => {
    expect(lockLimit(user)).toBe(1);
    expect(lockLimit(other)).toBe(8);
    expect((await claimCell(env.DB, user, 'goteborg-norr', 0, 0, NOW)).ok).toBe(true);
    expect(await claimCell(env.DB, user, 'goteborg-norr', 1, 0, NOW)).toMatchObject({ ok: false, status: 403, code: 'too_many_locks' });
    const eight = [[0, 1], [1, 1], [2, 1], [3, 1], [0, 2], [1, 2], [2, 2], [1, 0]];
    for (const [col, row] of eight) {
      expect((await claimCell(env.DB, other, 'goteborg-norr', col, row, NOW)).ok).toBe(true);
    }
    expect(await claimCell(env.DB, other, 'goteborg-norr', 2, 0, NOW)).toMatchObject({ ok: false, status: 403, code: 'too_many_locks' });
  });

  it('stops a new user who already has five cells waiting for review', async () => {
    // Uploading frees the lock, so without this one account could park 20 cells a day in the queue.
    const waiting = [[0, 1], [1, 1], [2, 1], [3, 1], [0, 2]];
    for (const [col, row] of waiting) {
      await env.DB.prepare("UPDATE cells SET state = 'vantar', owner_id = ?, owner_login = ?, uploaded_at = ? WHERE sheet = 'goteborg-norr' AND col = ? AND row = ?")
        .bind(user.github_id, user.login, NOW, col, row)
        .run();
    }
    expect(await claimCell(env.DB, user, 'goteborg-norr', 0, 0, NOW)).toMatchObject({ ok: false, status: 403, code: 'too_many_waiting' });
    // One of them is reviewed: room again.
    await env.DB.prepare("UPDATE cells SET state = 'godkand' WHERE sheet = 'goteborg-norr' AND col = 0 AND row = 1").run();
    expect((await claimCell(env.DB, user, 'goteborg-norr', 0, 0, NOW)).ok).toBe(true);
  });

  it('does not hold a trusted user to the waiting limit', async () => {
    for (const [col, row] of [[0, 1], [1, 1], [2, 1], [3, 1], [0, 2], [1, 2]]) {
      await env.DB.prepare("UPDATE cells SET state = 'vantar', owner_id = ?, owner_login = ?, uploaded_at = ? WHERE sheet = 'goteborg-norr' AND col = ? AND row = ?")
        .bind(other.github_id, other.login, NOW, col, row)
        .run();
    }
    expect((await claimCell(env.DB, other, 'goteborg-norr', 0, 0, NOW)).ok).toBe(true);
  });

  it('holds the lock limit under parallel claims on different cells', async () => {
    const results = await Promise.all([
      claimCell(env.DB, user, 'goteborg-norr', 0, 1, NOW),
      claimCell(env.DB, user, 'goteborg-norr', 1, 1, NOW),
      claimCell(env.DB, user, 'goteborg-norr', 2, 1, NOW),
    ]);
    expect(results.filter((r) => r.ok)).toHaveLength(1);
    const held = await env.DB.prepare("SELECT COUNT(*) AS n FROM cells WHERE owner_id = 4242 AND state = 'last'").first();
    expect(held.n).toBe(1);
  });

  it('clears an old rejection when the cell is claimed again', async () => {
    await setCell('goteborg-norr', 0, 1, { rejected_at: '2026-11-01T00:00:00Z', comment: 'gammalt' });
    await claimCell(env.DB, user, 'goteborg-norr', 0, 1, NOW);
    expect(await getCell('goteborg-norr', 0, 1)).toMatchObject({ state: 'last', rejected_at: null, comment: null });
  });

  it('lets exactly one of two simultaneous claims win', async () => {
    const [a, b] = await Promise.all([
      claimCell(env.DB, user, 'goteborg-norr', 2, 1, NOW),
      claimCell(env.DB, other, 'goteborg-norr', 2, 1, NOW),
    ]);
    expect([a.ok, b.ok].filter(Boolean)).toHaveLength(1);
    const cell = await getCell('goteborg-norr', 2, 1);
    expect(cell.state).toBe('last');
    expect([4242, 7]).toContain(cell.owner_id);
  });

  it('logs an event', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 0, 1, NOW);
    const row = await env.DB.prepare("SELECT * FROM events WHERE action = 'claim'").first();
    expect(row).toMatchObject({ at: NOW, github_id: 4242, login: 'klang', sheet: 'goteborg-norr', col: 0, row: 1 });
  });
});

describe('releaseCell', () => {
  it('frees your own lock', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 0, 0, NOW);
    expect(await releaseCell(env.DB, user, 'goteborg-norr', 0, 0, '2026-11-17T13:00:00Z')).toEqual({ ok: true });
    const cell = await getCell('goteborg-norr', 0, 0);
    expect(cell).toMatchObject({ state: 'fri', owner_id: null, owner_login: null, locked_at: null, expires_at: null, updated_at: '2026-11-17T13:00:00Z' });
  });

  it('refuses another user’s lock', async () => {
    await claimCell(env.DB, other, 'goteborg-norr', 0, 0, NOW);
    expect(await releaseCell(env.DB, user, 'goteborg-norr', 0, 0, NOW)).toMatchObject({ ok: false, status: 403, code: 'not_yours' });
  });

  it('refuses after upload', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 0, 0, NOW);
    await setCell('goteborg-norr', 0, 0, { state: 'vantar', uploaded_at: NOW });
    expect(await releaseCell(env.DB, user, 'goteborg-norr', 0, 0, NOW)).toMatchObject({ ok: false, status: 409, code: 'already_uploaded' });
  });
});

describe('routes', () => {
  function asUser(path, body) {
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ id: 4242, login: 'klang' }), { status: 200, headers: { 'content-type': 'application/json' } }));
    return call(new Request(`https://x/v1/${path}`, { method: 'POST', headers: { Authorization: 'Bearer t', 'X-Terms-Version': TERMS_VERSION, 'Content-Type': 'application/json' }, body: JSON.stringify(body) }));
  }

  it('POST /v1/claim answers 201 with expires_at and validates the body', async () => {
    const r = await asUser('claim', { sheet: 'goteborg-norr', col: 1, row: 0 });
    expect(r.status).toBe(201);
    expect(await r.json()).toMatchObject({ sheet: 'goteborg-norr', col: 1, row: 0, expires_at: expect.stringMatching(/Z$/) });
    const bad = await asUser('claim', { sheet: 'goteborg-norr', col: -1, row: 'x' });
    expect(bad.status).toBe(400);
    expect((await bad.json()).error).toBe('bad_body');
    vi.unstubAllGlobals();
  });

  it('POST /v1/release answers ok', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 0, NOW);
    const r = await asUser('release', { sheet: 'goteborg-norr', col: 1, row: 0 });
    expect(r.status).toBe(200);
    expect(await r.json()).toEqual({ ok: true });
    vi.unstubAllGlobals();
  });
});

describe('a cell on a sheet of the tiling', () => {
  // Helsingborg. Nobody has opened this sheet: its row is made by the first claim there.
  const HBG = 'se-n5602-e1262';
  const sheetRow = () => env.DB.prepare('SELECT * FROM sheets WHERE id = ?').bind(HBG).first();

  afterEach(() => vi.unstubAllGlobals());

  it('is claimed, and the claim makes the sheet and the cell', async () => {
    stubOutside(38);
    const r = await claimCell(env.DB, user, HBG, 47, 16, NOW, env, 'google');
    expect(r).toEqual({ ok: true, expires_at: '2026-11-19T12:00:00Z' });
    const sheet = await sheetRow();
    expect(sheet).toMatchObject({
      id: HBG,
      city: 'se',
      name: 'N 56,02 E 12,62',
      cols: 61,
      rows: 47,
      origin_lat: 56.025,
      origin_lon: 12.625,
      x_meters: -7808,
      ground_height: 68,
      dzi_levels: 17,
      pipeline: env.PIPELINE_NOW,
      state: 'open',
      queue_order: 100,
      rev: 0,
      kind: 'google',
      made: 'grid',
    });
    // The picture's top follows the ground height, so the box stays in the middle of it.
    expect(sheet.y_meters).toBeCloseTo(6016 + 68 * Math.SQRT1_2, 9);
    expect(await getCell(HBG, 47, 16)).toMatchObject({ state: 'last', owner_id: 4242, owner_login: 'klang' });
    expect((await env.DB.prepare('SELECT COUNT(*) AS n FROM cells WHERE sheet = ?').bind(HBG).first()).n).toBe(1);
  });

  it('asks for the ground height once, with the origin of the sheet and nothing about the builder', async () => {
    const asked = stubOutside(38);
    await claimCell(env.DB, user, HBG, 47, 16, NOW, env, 'google');
    await claimCell(env.DB, other, HBG, 48, 16, NOW, env, 'google');
    expect(asked).toHaveLength(1);
    expect(asked[0].url).toBe('https://api.open-meteo.com/v1/elevation?latitude=56.025&longitude=12.625');
    const sent = JSON.stringify(asked[0].init ?? {}) + JSON.stringify([...new Headers(asked[0].init?.headers ?? {}).entries()]);
    expect(sent).not.toMatch(/klang|4242|authorization|cookie/i);
    expect(asked[0].init?.method ?? 'GET').toBe('GET');
    expect(asked[0].init?.body).toBeUndefined();
  });

  it('writes down who made the sheet', async () => {
    stubOutside(38);
    await claimCell(env.DB, user, HBG, 47, 16, NOW, env, 'google');
    const made = await env.DB.prepare("SELECT * FROM events WHERE action = 'make_sheet'").all();
    expect(made.results).toHaveLength(1);
    expect(made.results[0]).toMatchObject({ at: NOW, github_id: 4242, login: 'klang', sheet: HBG, detail: 'ground height 68 m' });
  });

  it('is refused 503 when the elevation service does not answer, and nothing is written', async () => {
    for (const answer of ['down', 500, 'not json', '{"elevation":[]}', '{"elevation":["38"]}', '{"elevation":[99999]}', '{"elevation":[null]}']) {
      const asked = stubOutside(answer);
      const before = await rowCounts();
      const r = await claimCell(env.DB, user, HBG, 47, 16, NOW, env, 'google');
      expect(r, String(answer)).toMatchObject({ ok: false, status: 503, code: 'no_height' });
      expect(asked).toHaveLength(1);
      expect(await rowCounts()).toEqual(before);
      expect((await env.DB.prepare('SELECT COUNT(*) AS n FROM events').first()).n).toBe(0);
    }
  });

  it('is refused 503 when the service does not know which pipeline a new sheet is drawn with', async () => {
    const asked = stubOutside(38);
    const before = await rowCounts();
    expect(await claimCell(env.DB, user, HBG, 47, 16, NOW, { ...env, PIPELINE_NOW: '' }, 'google')).toMatchObject({ ok: false, status: 503, code: 'misconfigured' });
    expect(await claimCell(env.DB, user, HBG, 47, 16, NOW, undefined, 'google')).toMatchObject({ ok: false, status: 503, code: 'misconfigured' });
    expect(asked).toHaveLength(0);
    expect(await rowCounts()).toEqual(before);
  });

  it('holds the lock limit across a hand-made sheet and a sheet of the tiling', async () => {
    const asked = stubOutside(38);
    expect((await claimCell(env.DB, user, 'goteborg-norr', 0, 0, NOW, env)).ok).toBe(true);
    const before = await rowCounts();
    expect(await claimCell(env.DB, user, HBG, 47, 16, NOW, env, 'google')).toMatchObject({ ok: false, status: 403, code: 'too_many_locks' });
    // A claim that could not be had makes no sheet and asks nobody for a height.
    expect(await rowCounts()).toEqual(before);
    expect(asked).toHaveLength(0);
    // And the other way round.
    await releaseCell(env.DB, user, 'goteborg-norr', 0, 0, NOW);
    expect((await claimCell(env.DB, user, HBG, 47, 16, NOW, env, 'google')).ok).toBe(true);
    expect(await claimCell(env.DB, user, 'goteborg-norr', 0, 0, NOW, env)).toMatchObject({ ok: false, status: 403, code: 'too_many_locks' });
  });

  it('holds the waiting limit before a sheet is made', async () => {
    const asked = stubOutside(38);
    for (const [col, row] of [[0, 1], [1, 1], [2, 1], [3, 1], [0, 2]]) {
      await setCell('goteborg-norr', col, row, { state: 'vantar', owner_id: user.github_id, owner_login: user.login, uploaded_at: NOW });
    }
    expect(await claimCell(env.DB, user, HBG, 47, 16, NOW, env, 'google')).toMatchObject({ ok: false, status: 403, code: 'too_many_waiting' });
    expect(await sheetRow()).toBeNull();
    expect(asked).toHaveLength(0);
  });

  it('stays a row and is free again when it is released', async () => {
    stubOutside(38);
    await claimCell(env.DB, user, HBG, 47, 16, NOW, env, 'google');
    expect(await releaseCell(env.DB, user, HBG, 47, 16, NOW)).toEqual({ ok: true });
    expect(await getCell(HBG, 47, 16)).toMatchObject({ state: 'fri', owner_id: null });
    expect((await claimCell(env.DB, other, HBG, 47, 16, NOW, env, 'google')).ok).toBe(true);
  });

  it('is taken when someone else holds it', async () => {
    stubOutside(38);
    await claimCell(env.DB, other, HBG, 47, 16, NOW, env, 'google');
    expect(await claimCell(env.DB, user, HBG, 47, 16, NOW, env, 'google')).toMatchObject({ ok: false, status: 409, code: 'taken' });
  });

  it('cannot be claimed on a sheet the admin has closed', async () => {
    stubOutside(38);
    await claimCell(env.DB, other, HBG, 47, 16, NOW, env, 'google');
    await env.DB.prepare("UPDATE sheets SET state = 'done' WHERE id = ?").bind(HBG).run();
    const before = await rowCounts();
    expect(await claimCell(env.DB, user, HBG, 10, 10, NOW, env, 'google')).toMatchObject({ ok: false, status: 403, code: 'sheet_closed' });
    expect(await rowCounts()).toEqual(before);
  });

  it('is refused where a hand-made sheet already has the ground, and nothing is written', async () => {
    const asked = stubOutside(38);
    await insertSheet({ id: 'stockholm-norr', city: 'stockholm', name: 'Stockholm', cols: 64, rows: 52, origin_lat: 59.33, origin_lon: 18.06, x_meters: -8192, y_meters: 6656, ground_height: 30, kind: 'google', queue_order: 0 });
    const tile = sheetAt(59.3326, 18.0649);
    const inside = nearestCell(tile, 59.3326, 18.0649);
    const before = await rowCounts();
    const r = await claimCell(env.DB, user, tile.id, inside.col, inside.row, NOW, env, 'google');
    expect(r).toMatchObject({ ok: false, status: 409, code: 'other_sheet' });
    expect(r.message).toContain('stockholm-norr');
    expect(await rowCounts()).toEqual(before);
    expect(asked).toHaveLength(0);
    // The same sheet of the tiling, beyond the hand-made picture, is free ground.
    const beyond = nearestCell(tile, 59.26, 18.24);
    expect((await claimCell(env.DB, user, tile.id, beyond.col, beyond.row, NOW, env, 'google')).ok).toBe(true);
  });

  it('shows in the grid: a computed sheet with no cells before the first claim, one locked cell after', async () => {
    stubOutside(38);
    const before = await getGrid(env.DB, HBG, env.PIPELINE_NOW);
    expect(before.cells).toEqual([]);
    expect(before.sheet).toMatchObject({ id: HBG, cols: 61, rows: 47, kind: 'google', made: 'grid', state: 'open', ground_height: 0, y_meters: 6016, pipeline: env.PIPELINE_NOW });
    expect(await sheetRow()).toBeNull();
    const claimed = await call(new Request('https://x/v1/claim', { method: 'POST', headers: { Authorization: 'Bearer t', 'X-Terms-Version': TERMS_VERSION, 'Content-Type': 'application/json' }, body: JSON.stringify({ sheet: HBG, col: 47, row: 16, kind: 'google' }) }));
    expect(claimed.status).toBe(201);
    expect(await claimed.json()).toMatchObject({ sheet: HBG, col: 47, row: 16, expires_at: expect.stringMatching(/Z$/) });
    const grid = await call(new Request(`https://x/v1/grid/${HBG}`));
    expect(grid.status).toBe(200);
    const body = await grid.json();
    expect(body.sheet).toMatchObject({ id: HBG, ground_height: 68, made: 'grid' });
    expect(body.cells).toEqual([[47, 16, 'last', 'klang', expect.stringMatching(/Z$/)]]);
  });

  it('answers 503 with a time to try again when the height cannot be had', async () => {
    stubOutside('down');
    const r = await call(new Request('https://x/v1/claim', { method: 'POST', headers: { Authorization: 'Bearer t', 'X-Terms-Version': TERMS_VERSION, 'Content-Type': 'application/json' }, body: JSON.stringify({ sheet: HBG, col: 47, row: 16, kind: 'google' }) }));
    expect(r.status).toBe(503);
    expect(r.headers.get('Retry-After')).toBe('60');
    expect((await r.json()).error).toBe('no_height');
  });

  it('the grid of an id the tiling does not give out is still 404', async () => {
    for (const id of ['se-n5601-e1263', 'se-n5002-e1262', 'nope']) {
      const r = await call(new Request(`https://x/v1/grid/${id}`));
      expect(r.status, id).toBe(404);
    }
  });
});
