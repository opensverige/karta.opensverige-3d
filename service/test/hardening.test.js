/* What a review of the service found, each as the request that went wrong. */

import { env } from 'cloudflare:test';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setBlock } from '../src/admin.js';
import { resolveUser } from '../src/auth.js';
import { clearPublicCache } from '../src/index.js';
import { claimCell } from '../src/cells.js';
import { getGrid } from '../src/sheets.js';
import { NOW, call, clearDb, getCell, insertCells, insertSheet, insertUser, rowCounts, setCell, stubOutside } from './fixtures.js';
import { manifestHeader, minimalPng } from './png.js';
import { TERMS_VERSION } from '../src/terms.js';

const LATER = '2026-11-17T13:00:00Z';

async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function withToken(token) {
  return new Request('https://x/v1/me', { headers: { Authorization: `Bearer ${token}` } });
}

async function emptyInbox() {
  const list = await env.INKORG.list();
  await Promise.all(list.objects.map((o) => env.INKORG.delete(o.key)));
}

beforeEach(async () => {
  await clearDb();
  await emptyInbox();
});

afterEach(() => vi.unstubAllGlobals());

describe('a live cell the admin took down', () => {
  it('is not shown as free while nobody may claim it', async () => {
    await insertSheet({ cols: 2, rows: 1 });
    await insertCells('goteborg-norr', 2, 1);
    await setCell('goteborg-norr', 1, 0, { state: 'fri', removed_at: NOW, inbox_key: 'goteborg-norr/1_0.abc' });
    const grid = await getGrid(env.DB, 'goteborg-norr');
    expect(grid.cells).toEqual([
      [0, 0, 'fri', null, NOW],
      [1, 0, 'last', null, NOW],
    ]);
  });
});

describe('a token GitHub refused', () => {
  it('is not asked about again within the minute', async () => {
    let asked = 0;
    vi.stubGlobal('fetch', async () => {
      asked += 1;
      return new Response('{}', { status: 401 });
    });
    const first = await resolveUser(withToken('tok-bad-once'), env, NOW);
    const second = await resolveUser(withToken('tok-bad-once'), env, NOW);
    expect(first.status).toBe(401);
    expect(second.status).toBe(401);
    expect(asked).toBe(1);
  });

  it('is asked about again when the minute has passed', async () => {
    let asked = 0;
    vi.stubGlobal('fetch', async () => {
      asked += 1;
      return new Response('{}', { status: 401 });
    });
    await resolveUser(withToken('tok-bad-later'), env, NOW);
    await resolveUser(withToken('tok-bad-later'), env, '2026-11-17T12:02:00Z');
    expect(asked).toBe(2);
  });
});

describe('the login cache', () => {
  it('failing to answer sends the question to GitHub, not a 500', async () => {
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ id: 4242, login: 'klang' }), { status: 200 }));
    const broken = {
      get: async () => {
        throw new Error('kv down');
      },
      put: async () => {
        throw new Error('kv down');
      },
    };
    const resolved = await resolveUser(withToken('tok-kv'), { ...env, GH: broken }, NOW);
    expect(resolved.user.login).toBe('klang');
  });
});

describe('own logins only', () => {
  it('with the secret set and no client id, nobody is let in and GitHub is not asked', async () => {
    let asked = 0;
    vi.stubGlobal('fetch', async () => {
      asked += 1;
      return new Response(JSON.stringify({ id: 4242, login: 'klang' }), { status: 200 });
    });
    const resolved = await resolveUser(withToken('tok-any'), { ...env, GITHUB_CLIENT_SECRET: 's', GITHUB_CLIENT_ID: '' }, NOW);
    expect(resolved.status).toBe(503);
    expect((await resolved.json()).error).toBe('misconfigured');
    expect(asked).toBe(0);
  });
});

describe('blocking a builder', () => {
  it('frees the cell they hold', async () => {
    await insertSheet();
    await insertCells('goteborg-norr', 4, 3);
    await insertUser();
    await setCell('goteborg-norr', 1, 1, { state: 'last', owner_id: 4242, owner_login: 'klang', locked_at: NOW, expires_at: '2026-11-19T12:00:00Z' });
    expect(await setBlock(env.DB, { login: 'klang' }, 1, LATER)).toEqual({ ok: true });
    expect(await getCell('goteborg-norr', 1, 1)).toMatchObject({ state: 'fri', owner_id: null, owner_login: null, expires_at: null });
  });

  it('leaves their waiting upload for the reviewer', async () => {
    await insertSheet();
    await insertCells('goteborg-norr', 4, 3);
    await insertUser();
    await setCell('goteborg-norr', 2, 1, { state: 'vantar', owner_id: 4242, owner_login: 'klang', uploaded_at: NOW });
    await setBlock(env.DB, { login: 'klang' }, 1, LATER);
    expect(await getCell('goteborg-norr', 2, 1)).toMatchObject({ state: 'vantar', owner_id: 4242 });
  });
});

describe('POST /live', () => {
  async function asAdmin(body) {
    await insertUser({ github_id: 1, login: 'admin' });
    await env.GH.put('gh:' + (await sha256Hex(new TextEncoder().encode('tok-admin'))), JSON.stringify({ id: 1, login: 'admin' }));
    return call(
      new Request('https://x/v1/live', { method: 'POST', headers: { Authorization: 'Bearer tok-admin', 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
      { ADMIN_GITHUB_ID: '1' },
    );
  }

  it('refuses an empty list, so a revision cannot move with no cell', async () => {
    await insertSheet();
    const response = await asAdmin({ sheet: 'goteborg-norr', cells: [], rev: 5 });
    expect(response.status).toBe(400);
    expect((await env.DB.prepare('SELECT rev FROM sheets WHERE id = ?').bind('goteborg-norr').first()).rev).toBe(0);
  });

  it('refuses a revision no later one could follow', async () => {
    await insertSheet();
    await insertCells('goteborg-norr', 4, 3);
    await setCell('goteborg-norr', 0, 0, { state: 'godkand' });
    const response = await asAdmin({ sheet: 'goteborg-norr', cells: [[0, 0]], rev: 1e300 });
    expect(response.status).toBe(400);
    expect((await getCell('goteborg-norr', 0, 0)).state).toBe('godkand');
  });
});

describe('an upload that fails after the picture is stored', () => {
  it('takes its own picture away again', async () => {
    await insertSheet();
    await insertCells('goteborg-norr', 4, 3);
    await insertUser();
    await env.GH.put('gh:' + (await sha256Hex(new TextEncoder().encode('tok-up'))), JSON.stringify({ id: 4242, login: 'klang' }));
    await setCell('goteborg-norr', 1, 1, { state: 'last', owner_id: 4242, owner_login: 'klang', locked_at: NOW, expires_at: '2099-01-01T00:00:00Z' });
    const png = minimalPng(1024, 1024);
    const real = env.INKORG;
    const inbox = {
      put: async (key, value, options) => {
        if (key.endsWith('.json')) throw new Error('r2 down');
        return real.put(key, value, options);
      },
      delete: (keys) => real.delete(keys),
      get: (key) => real.get(key),
      list: () => real.list(),
    };
    const request = new Request('https://x/v1/upload/goteborg-norr/1_1', {
      method: 'PUT',
      headers: { Authorization: 'Bearer tok-up', 'X-Terms-Version': TERMS_VERSION, 'Content-Length': String(png.length), 'X-Manifest': manifestHeader({ sha256: await sha256Hex(png) }) },
      body: png,
    });
    const response = await call(request, { INKORG: inbox });
    expect(response.status).toBe(500);
    expect((await real.list()).objects.map((o) => o.key)).toEqual([]);
    expect((await getCell('goteborg-norr', 1, 1)).state).toBe('last');
  });
});

describe('unblocking a builder', () => {
  it('changes nothing but the block', async () => {
    await insertSheet();
    await insertCells('goteborg-norr', 4, 3);
    await insertUser({ blocked: 1 });
    await setCell('goteborg-norr', 2, 1, { state: 'vantar', owner_id: 4242, owner_login: 'klang', uploaded_at: NOW });
    expect(await setBlock(env.DB, { login: 'klang' }, 0, LATER)).toEqual({ ok: true });
    expect((await env.DB.prepare('SELECT blocked FROM users WHERE github_id = 4242').first()).blocked).toBe(0);
    expect(await getCell('goteborg-norr', 2, 1)).toMatchObject({ state: 'vantar', owner_id: 4242 });
  });
});

describe('a claim on a sheet of the tiling', () => {
  const HBG = 'se-n5602-e1262';
  const klang = { github_id: 4242, login: 'klang', first_seen: NOW, approved_count: 0, trusted: 0, blocked: 0 };
  const ebba = { github_id: 7, login: 'ebba', first_seen: NOW, approved_count: 0, trusted: 0, blocked: 0 };

  it('is refused for an id that has the shape but lies outside Sweden or off the steps, and makes no row', async () => {
    const asked = stubOutside(38);
    for (const id of ['se-n5002-e1262', 'se-n5602-e0512', 'se-n5601-e1263', 'se-n5602-e1263', 'se-n9999-e9999']) {
      expect(await claimCell(env.DB, klang, id, 0, 0, NOW, env, 'google'), id).toMatchObject({ ok: false, status: 404, code: 'no_sheet' });
    }
    expect(await rowCounts()).toEqual({ sheets: 0, cells: 0 });
    expect(asked).toHaveLength(0);
  });

  it('two first claims at the same moment make one sheet, and both cells are claimed', async () => {
    stubOutside(38);
    const [a, b] = await Promise.all([claimCell(env.DB, klang, HBG, 47, 16, NOW, env, 'google'), claimCell(env.DB, ebba, HBG, 48, 16, NOW, env, 'google')]);
    expect([a.ok, b.ok]).toEqual([true, true]);
    expect(await rowCounts()).toEqual({ sheets: 1, cells: 2 });
    expect((await getCell(HBG, 47, 16)).owner_login).toBe('klang');
    expect((await getCell(HBG, 48, 16)).owner_login).toBe('ebba');
  });

  it('two first claims on the same cell at the same moment have one winner', async () => {
    stubOutside(38);
    const [a, b] = await Promise.all([claimCell(env.DB, klang, HBG, 47, 16, NOW, env, 'google'), claimCell(env.DB, ebba, HBG, 47, 16, NOW, env, 'google')]);
    expect([a.ok, b.ok].filter(Boolean)).toHaveLength(1);
    expect([a, b].find((r) => !r.ok)).toMatchObject({ status: 409, code: 'taken' });
    expect(await rowCounts()).toEqual({ sheets: 1, cells: 1 });
  });

  it('is refused for a cell outside the size of the sheet, and makes no row', async () => {
    const asked = stubOutside(38);
    for (const [col, row] of [[61, 0], [0, 47], [9999, 9999]]) {
      expect(await claimCell(env.DB, klang, HBG, col, row, NOW, env, 'google')).toMatchObject({ ok: false, status: 404, code: 'no_cell' });
    }
    expect(await rowCounts()).toEqual({ sheets: 0, cells: 0 });
    expect(asked).toHaveLength(0);
    // The same when the sheet has its row.
    expect((await claimCell(env.DB, ebba, HBG, 60, 46, NOW, env, 'google')).ok).toBe(true);
    expect(await claimCell(env.DB, klang, HBG, 61, 46, NOW, env, 'google')).toMatchObject({ ok: false, status: 404, code: 'no_cell' });
    expect(await rowCounts()).toEqual({ sheets: 1, cells: 1 });
  });

  it('a hand-made sheet with missing cell rows is not filled in by a claim', async () => {
    await insertSheet({ cols: 2, rows: 1 });
    expect(await claimCell(env.DB, klang, 'goteborg-norr', 1, 0, NOW, env)).toMatchObject({ ok: false, status: 404, code: 'no_cell' });
    expect(await rowCounts()).toEqual({ sheets: 1, cells: 0 });
  });
});

describe('the memory of public reads and a sheet nobody has made yet', () => {
  const HBG = 'se-n5602-e1262';
  const KEEP = { PUBLIC_CACHE_SECONDS: '60' };
  const claim = (body) =>
    call(new Request('https://x/v1/claim', { method: 'POST', headers: { Authorization: 'Bearer t', 'X-Terms-Version': TERMS_VERSION, 'Content-Type': 'application/json' }, body: JSON.stringify(body) }), KEEP);

  beforeEach(() => clearPublicCache());

  it('is not shown the sheet as it was before the claim that made it', async () => {
    // rutor claim reads the grid, claims, and rutor render reads the grid again seconds later.
    // The picture's top moved with the ground height when the row was made: the renderer must
    // get the row, not what the tiling computed before there was one.
    stubOutside(38);
    const before = await (await call(new Request(`https://x/v1/grid/${HBG}`), KEEP)).json();
    expect(before.sheet).toMatchObject({ ground_height: 0, y_meters: 6016, exists: false });
    expect((await claim({ sheet: HBG, col: 47, row: 16, kind: 'google' })).status).toBe(201);
    const after = await (await call(new Request(`https://x/v1/grid/${HBG}`), KEEP)).json();
    expect(after.sheet.ground_height).toBe(68);
    expect(after.sheet.y_meters).toBeCloseTo(6016 + 68 * Math.SQRT1_2, 9);
    expect(after.sheet.exists).not.toBe(false);
    expect(after.cells).toHaveLength(1);
  });

  it('tells every cache between here and the reader not to keep a sheet that has no row', async () => {
    const computed = await call(new Request(`https://x/v1/grid/${HBG}`), KEEP);
    expect(computed.headers.get('Cache-Control')).toBe('no-store');
    await insertSheet({ cols: 1, rows: 1 });
    const stored = await call(new Request('https://x/v1/grid/goteborg-norr'), KEEP);
    expect(stored.headers.get('Cache-Control')).toBe('public, max-age=60');
  });

  it('does not let a stream of sheets nobody has made push a real grid out', async () => {
    await insertSheet({ cols: 1, rows: 1 });
    await insertCells('goteborg-norr', 1, 1);
    const first = await (await call(new Request('https://x/v1/grid/goteborg-norr'), KEEP)).json();
    expect(first.cells[0][2]).toBe('fri');
    // The table changes; an answer still kept in memory does not.
    await setCell('goteborg-norr', 0, 0, { state: 'live' });
    for (let n = 0; n < 70; n += 1) {
      const id = `se-n${5602 + 15 * (n % 50)}-e${1262 + 25 * Math.floor(n / 50)}`;
      expect((await call(new Request(`https://x/v1/grid/${id}`), KEEP)).status, id).toBe(200);
    }
    const again = await (await call(new Request('https://x/v1/grid/goteborg-norr'), KEEP)).json();
    expect(again.cells[0][2]).toBe('fri');
  });
});

describe('a client from before the tiling', () => {
  const HBG = 'se-n5602-e1262';
  const klang = { github_id: 4242, login: 'klang', first_seen: NOW, approved_count: 0, trusted: 0, blocked: 0 };

  it('is told to update, and locks nothing, on a sheet of the tiling', async () => {
    // An installed rutor from before this does not know where a new sheet's picture comes
    // from. It would lock the cell and then fail at the render. A claim there has to say the
    // source the client will draw from; a client that says none is refused before any row.
    const asked = stubOutside(38);
    expect(await claimCell(env.DB, klang, HBG, 47, 16, NOW, env)).toMatchObject({ ok: false, status: 426, code: 'update_needed' });
    expect(await claimCell(env.DB, klang, HBG, 47, 16, NOW, env, 'open')).toMatchObject({ ok: false, status: 426, code: 'update_needed' });
    expect(await rowCounts()).toEqual({ sheets: 0, cells: 0 });
    expect(asked).toHaveLength(0);
    // The same once the sheet has its row.
    expect((await claimCell(env.DB, klang, HBG, 47, 16, NOW, env, 'google')).ok).toBe(true);
    const ebba = { ...klang, github_id: 7, login: 'ebba' };
    expect(await claimCell(env.DB, ebba, HBG, 48, 16, NOW, env)).toMatchObject({ ok: false, status: 426, code: 'update_needed' });
    expect(await rowCounts()).toEqual({ sheets: 1, cells: 1 });
  });

  it('claims on a hand-made sheet as it always did', async () => {
    await insertSheet({ cols: 1, rows: 1 });
    await insertCells('goteborg-norr', 1, 1);
    expect((await claimCell(env.DB, klang, 'goteborg-norr', 0, 0, NOW)).ok).toBe(true);
  });

  it('the route reads the source from the body, and refuses one that is not a source', async () => {
    stubOutside(38);
    const post = (body) => call(new Request('https://x/v1/claim', { method: 'POST', headers: { Authorization: 'Bearer t', 'X-Terms-Version': TERMS_VERSION, 'Content-Type': 'application/json' }, body: JSON.stringify(body) }));
    const old = await post({ sheet: HBG, col: 47, row: 16 });
    expect(old.status).toBe(426);
    expect((await old.json()).error).toBe('update_needed');
    const bad = await post({ sheet: HBG, col: 47, row: 16, kind: 'bing' });
    expect(bad.status).toBe(400);
    expect((await post({ sheet: HBG, col: 47, row: 16, kind: 'google' })).status).toBe(201);
  });
});
