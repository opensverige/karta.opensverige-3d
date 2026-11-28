/* Regression tests for the 2026-11-21 review: uploads keep their own inbox keys, removed and
   expired cells cannot destroy a newer upload, PNGs are walked chunk by chunk, and the admin
   paths and the public reads behave under load. */

import { env } from 'cloudflare:test';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { approveCell, markBlanked, markLive, rejectCell, setTrust } from '../src/admin.js';
import { claimCell } from '../src/cells.js';
import { nowIso } from '../src/db.js';
import { expireLocks, purgeDue } from '../src/cron.js';
import { clearPublicCache } from '../src/index.js';
import { inboxKey, inspectPng } from '../src/upload.js';
import { NOW, call, clearDb, getCell, insertCells, insertSheet, insertUser, setCell } from './fixtures.js';
import { manifestHeader, minimalPng, pngChunk, sha256Hex } from './png.js';
import { TERMS_VERSION } from '../src/terms.js';

const user = { github_id: 4242, login: 'klang', first_seen: NOW, approved_count: 0, trusted: 0, blocked: 0 };
const other = { github_id: 5151, login: 'vind', first_seen: NOW, approved_count: 0, trusted: 0, blocked: 0 };
const deny = { limit: async () => ({ success: false }) };

function loginAs(u) {
  vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ id: u.github_id, login: u.login }), { status: 200, headers: { 'content-type': 'application/json' } }));
}

function uploadRequest(bytes, manifest, headers = {}) {
  return new Request('https://x/v1/upload/goteborg-norr/1_1', {
    method: 'PUT',
    headers: {
      Authorization: 'Bearer t',
      'X-Terms-Version': TERMS_VERSION,
      'Content-Type': 'image/png',
      'Content-Length': String(bytes.length),
      'X-Manifest': manifest,
      ...headers,
    },
    body: bytes,
  });
}

async function put(u, bytes, overrides = {}, extra = {}) {
  loginAs(u);
  const sha = await sha256Hex(bytes);
  const r = await call(uploadRequest(bytes, manifestHeader({ sha256: sha, ...overrides })), extra);
  vi.unstubAllGlobals();
  return r;
}

/** An inbox wrapper that runs `before` once, right before the first object is written. */
function inboxWith(before) {
  let done = false;
  return {
    put: async (key, value, options) => {
      if (!done) {
        done = true;
        await before();
      }
      return env.INKORG.put(key, value, options);
    },
    get: (key) => env.INKORG.get(key),
    delete: (keys) => env.INKORG.delete(keys),
    list: (o) => env.INKORG.list(o),
  };
}

async function keys() {
  return (await env.INKORG.list()).objects.map((o) => o.key).sort();
}

async function tokenKey(token) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
  return 'gh:' + [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

beforeEach(async () => {
  await clearDb();
  await insertSheet();
  await insertCells('goteborg-norr', 4, 3);
  await insertUser(user);
  await insertUser(other);
  const list = await env.INKORG.list();
  await Promise.all(list.objects.map((o) => env.INKORG.delete(o.key)));
  clearPublicCache();
});

describe('per-upload inbox keys', () => {
  it('stores the upload under a key with its sha and records it on the cell', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const bytes = minimalPng(1024, 1024);
    const sha = await sha256Hex(bytes);
    const r = await put(user, bytes);
    expect(r.status).toBe(201);
    const key = (await getCell('goteborg-norr', 1, 1)).inbox_key;
    expect(key).toMatch(new RegExp(`^goteborg-norr/1_1\\.${sha.slice(0, 16)}\\.[0-9a-f]{8}$`));
    expect(await keys()).toEqual([`${key}.json`, `${key}.png`]);
    expect(inboxKey('goteborg-norr', 1, 1, sha)).not.toBe(inboxKey('goteborg-norr', 1, 1, sha));
  });

  it('keeps the winner of a twin upload with the same bytes', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const bytes = minimalPng(1024, 1024);
    const sha = await sha256Hex(bytes);
    // The twin has the same bytes but a key of its own.
    const key = inboxKey('goteborg-norr', 1, 1, sha, 'aaaa0000');
    // The twin request finishes first: same user, same bytes, same key.
    const inkorg = inboxWith(async () => {
      await env.INKORG.put(`${key}.png`, bytes);
      await env.INKORG.put(`${key}.json`, '{}');
      await setCell('goteborg-norr', 1, 1, { state: 'vantar', uploaded_at: NOW, sha256: sha, bytes: bytes.length, inbox_key: key });
    });
    const r = await put(user, bytes, {}, { INKORG: inkorg });
    expect(r.status).toBe(409);
    expect((await r.json()).error).toBe('lock_lost');
    expect(await keys()).toEqual([`${key}.json`, `${key}.png`]);
    expect(await getCell('goteborg-norr', 1, 1)).toMatchObject({ state: 'vantar', inbox_key: key });
  });

  it('never deletes the upload of whoever took the cell during a slow upload', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const theirs = 'goteborg-norr/1_1.0123456789abcdef';
    const inkorg = inboxWith(async () => {
      await env.INKORG.put(`${theirs}.png`, minimalPng(1024, 1024));
      await env.INKORG.put(`${theirs}.json`, '{}');
      await setCell('goteborg-norr', 1, 1, { state: 'vantar', owner_id: other.github_id, owner_login: other.login, uploaded_at: NOW, inbox_key: theirs });
    });
    const r = await put(user, minimalPng(1024, 1024, 64), {}, { INKORG: inkorg });
    expect(r.status).toBe(409);
    expect(await keys()).toEqual([`${theirs}.json`, `${theirs}.png`]);
  });

  it('serves the current upload of a cell from the inbox route', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    expect((await put(user, minimalPng(1024, 1024))).status).toBe(201);
    loginAs(user);
    const ok = await call(new Request('https://x/v1/inkorg/goteborg-norr/1_1.json', { headers: { Authorization: 'Bearer t' } }), { ADMIN_GITHUB_ID: '4242' });
    vi.unstubAllGlobals();
    expect(ok.status).toBe(200);
    expect(JSON.parse(await ok.text())).toMatchObject({ login: 'klang', check: 'ok' });
  });
});

describe('upload guards', () => {
  it('refuses an upload after the lock expired, before the cron frees it', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    await setCell('goteborg-norr', 1, 1, { expires_at: '2026-01-01T00:00:00Z' });
    const r = await put(user, minimalPng(1024, 1024));
    expect(r.status).toBe(409);
    expect((await r.json()).error).toBe('lock_expired');
    expect(await keys()).toEqual([]);
  });

  it('refuses a manifest that has not passed the check', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const r = await put(user, minimalPng(1024, 1024), { check: 'failed' });
    expect(r.status).toBe(422);
    expect((await r.json()).error).toBe('not_checked');
    expect(await keys()).toEqual([]);
  });

  it('refuses over-long manifest strings and drops unknown keys', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const long = await put(user, minimalPng(1024, 1024), { gpu: 'x'.repeat(201) });
    expect(long.status).toBe(400);
    expect((await long.json()).error).toBe('bad_manifest');
    const r = await put(user, minimalPng(1024, 1024), { extra: 'y'.repeat(5000) });
    expect(r.status).toBe(201);
    const key = (await getCell('goteborg-norr', 1, 1)).inbox_key;
    const stored = JSON.parse(await (await env.INKORG.get(`${key}.json`)).text());
    expect(stored).not.toHaveProperty('extra');
  });

  it('checks the daily limit before it reads the body', async () => {
    const statements = [];
    const recent = new Date(Date.now() - 3600 * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
    for (let i = 0; i < 20; i += 1) {
      statements.push(env.DB.prepare("INSERT INTO events (at, github_id, login, action) VALUES (?, 4242, 'klang', 'upload')").bind(recent));
    }
    await env.DB.batch(statements);
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    // Not a PNG at all: a 429 proves the limit was checked before the body was looked at.
    const r = await put(user, new Uint8Array(40).fill(7));
    expect(r.status).toBe(429);
  });
});

describe('inspectPng walks every chunk', () => {
  it('accepts a well-formed PNG with an ancillary chunk', () => {
    expect(inspectPng(minimalPng(1024, 1024, 100))).toEqual({ width: 1024, height: 1024 });
  });

  it('refuses trailing bytes after IEND', () => {
    const good = minimalPng(1024, 1024);
    const tail = new Uint8Array(good.length + 5);
    tail.set(good);
    expect(inspectPng(tail)).toBeNull();
  });

  it('refuses a broken CRC', () => {
    const bad = minimalPng(1024, 1024);
    bad[29] ^= 0xff;
    expect(inspectPng(bad)).toBeNull();
  });

  it('refuses a PNG without image data or with a bad IHDR', () => {
    const sig = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
    const ihdr = (depth, colour) => pngChunk('IHDR', [0, 0, 4, 0, 0, 0, 4, 0, depth, colour, 0, 0, 0]);
    const iend = pngChunk('IEND', []);
    const idat = pngChunk('IDAT', [0x78, 0x9c, 0x03, 0x00, 0x00, 0x00, 0x00, 0x01]);
    expect(inspectPng(new Uint8Array([...sig, ...ihdr(8, 6), ...iend]))).toBeNull();
    expect(inspectPng(new Uint8Array([...sig, ...ihdr(3, 6), ...idat, ...iend]))).toBeNull();
    expect(inspectPng(new Uint8Array([...sig, ...ihdr(8, 6), ...idat, ...iend]))).toEqual({ width: 1024, height: 1024 });
  });
});

describe('removed cells', () => {
  it('cannot be claimed until the intake has blanked them', async () => {
    await setCell('goteborg-norr', 1, 1, { state: 'fri', removed_at: NOW, inbox_key: 'goteborg-norr/1_1.aaaaaaaaaaaaaaaa' });
    expect(await claimCell(env.DB, other, 'goteborg-norr', 1, 1, nowIso())).toMatchObject({ ok: false, status: 409, code: 'being_removed' });
  });

  it('blanking deletes the removed upload and frees the cell for a new claim', async () => {
    const key = 'goteborg-norr/1_1.aaaaaaaaaaaaaaaa';
    await setCell('goteborg-norr', 1, 1, { state: 'live', owner_id: 4242, owner_login: 'klang', uploaded_at: NOW, live_at: NOW, inbox_key: key });
    await env.INKORG.put(`${key}.png`, minimalPng(1024, 1024));
    await env.INKORG.put(`${key}.json`, '{}');
    await rejectCell(env.DB, env.INKORG, 'goteborg-norr', 1, 1, 'fel hus', NOW);
    expect(await getCell('goteborg-norr', 1, 1)).toMatchObject({ state: 'fri', removed_at: NOW, inbox_key: key });
    expect(await markBlanked(env.DB, env.INKORG, 'goteborg-norr', [[1, 1]], NOW)).toEqual({ ok: true, blanked: 1 });
    expect(await keys()).toEqual([]);
    expect(await getCell('goteborg-norr', 1, 1)).toMatchObject({ removed_at: null, inbox_key: null });
    expect(await claimCell(env.DB, other, 'goteborg-norr', 1, 1, nowIso())).toMatchObject({ ok: true });
  });
});

describe('rejected uploads are purged by key', () => {
  it('purges the rejected upload after 30 days and never a newer one', async () => {
    const old = 'goteborg-norr/1_1.aaaaaaaaaaaaaaaa';
    const fresh = 'goteborg-norr/1_1.bbbbbbbbbbbbbbbb';
    await setCell('goteborg-norr', 1, 1, { state: 'vantar', owner_id: 4242, owner_login: 'klang', uploaded_at: NOW, inbox_key: old });
    for (const k of [old, fresh]) {
      await env.INKORG.put(`${k}.png`, minimalPng(1024, 1024));
      await env.INKORG.put(`${k}.json`, '{}');
    }
    await rejectCell(env.DB, env.INKORG, 'goteborg-norr', 1, 1, 'suddigt', NOW);
    expect((await getCell('goteborg-norr', 1, 1)).inbox_key).toBeNull();
    await setCell('goteborg-norr', 1, 1, { state: 'vantar', owner_id: 5151, owner_login: 'vind', uploaded_at: NOW, inbox_key: fresh });
    expect(await purgeDue(env.DB, env.INKORG, '2026-12-01T00:00:00Z')).toBe(0);
    expect(await purgeDue(env.DB, env.INKORG, '2026-12-18T00:00:00Z')).toBe(1);
    expect(await keys()).toEqual([`${fresh}.json`, `${fresh}.png`]);
  });
});

describe('races the second review found', () => {
  it('never purges a key a cell points at again', async () => {
    const key = 'goteborg-norr/1_1.aaaaaaaaaaaaaaaa.00000000';
    await env.INKORG.put(`${key}.png`, minimalPng(1024, 1024));
    await env.INKORG.put(`${key}.json`, '{}');
    await env.DB.batch([
      env.DB.prepare('INSERT INTO purge (key, due) VALUES (?, ?)').bind(`${key}.png`, '2026-01-01T00:00:00Z'),
      env.DB.prepare('INSERT INTO purge (key, due) VALUES (?, ?)').bind(`${key}.json`, '2026-01-01T00:00:00Z'),
    ]);
    await setCell('goteborg-norr', 1, 1, { state: 'vantar', owner_id: 4242, owner_login: 'klang', uploaded_at: NOW, inbox_key: key });
    expect(await purgeDue(env.DB, env.INKORG, NOW)).toBe(0);
    expect(await keys()).toEqual([`${key}.json`, `${key}.png`]);
  });

  it('the cron does not free a lock someone took over after it looked', async () => {
    await setCell('goteborg-norr', 1, 1, { state: 'last', owner_id: 4242, owner_login: 'klang', locked_at: '2026-01-01T00:00:00Z', expires_at: '2026-01-03T00:00:00Z' });
    const db = {
      prepare: (sql) => {
        const real = env.DB.prepare(sql);
        if (!sql.startsWith('SELECT sheet, col, row, owner_id')) return real;
        return {
          bind: (...args) => ({
            all: async () => {
              const out = await real.bind(...args).all();
              // The takeover lands between the cron's read and its write.
              await claimCell(env.DB, other, 'goteborg-norr', 1, 1, nowIso());
              return out;
            },
          }),
        };
      },
    };
    await expireLocks(db, nowIso());
    expect(await getCell('goteborg-norr', 1, 1)).toMatchObject({ state: 'last', owner_id: 5151 });
  });

  it('a second /live with the same rev moves nothing', async () => {
    await setCell('goteborg-norr', 1, 1, { state: 'godkand', owner_id: 4242, owner_login: 'klang' });
    expect(await markLive(env.DB, 'goteborg-norr', [[1, 1]], 3, NOW)).toEqual({ ok: true, live: 1 });
    await setCell('goteborg-norr', 2, 1, { state: 'godkand', owner_id: 4242, owner_login: 'klang' });
    // The second call read the sheet before the first one bumped it.
    const stale = {
      prepare: (sql) => (sql.startsWith('SELECT rev FROM sheets') ? { bind: () => ({ first: async () => ({ rev: 0 }) }) } : env.DB.prepare(sql)),
      batch: (s) => env.DB.batch(s),
    };
    expect(await markLive(stale, 'goteborg-norr', [[2, 1]], 3, NOW)).toMatchObject({ ok: false, status: 409 });
    expect((await getCell('goteborg-norr', 2, 1)).state).toBe('godkand');
  });

  it('a reject that read an approved cell does not free it once it went live', async () => {
    await setCell('goteborg-norr', 1, 1, { state: 'live', owner_id: 4242, owner_login: 'klang', live_at: NOW, inbox_key: 'goteborg-norr/1_1.aaaaaaaaaaaaaaaa.00000000' });
    const stale = {
      prepare: (sql) =>
        sql.startsWith('SELECT state, owner_id, owner_login, inbox_key')
          ? { bind: () => ({ first: async () => ({ state: 'godkand', owner_id: 4242, owner_login: 'klang', inbox_key: 'goteborg-norr/1_1.aaaaaaaaaaaaaaaa.00000000' }) }) }
          : env.DB.prepare(sql),
      batch: (s) => env.DB.batch(s),
    };
    expect(await rejectCell(stale, env.INKORG, 'goteborg-norr', 1, 1, 'fel', NOW)).toMatchObject({ ok: false, status: 409, code: 'changed_meanwhile' });
    expect(await getCell('goteborg-norr', 1, 1)).toMatchObject({ state: 'live', removed_at: null });
    expect((await env.DB.prepare('SELECT COUNT(*) AS n FROM purge').first()).n).toBe(0);
  });
});

describe('expired locks', () => {
  it('do not count against the limit and can be taken by someone else', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 0, 0, nowIso());
    await setCell('goteborg-norr', 0, 0, { expires_at: '2026-01-01T00:00:00Z' });
    expect(await claimCell(env.DB, user, 'goteborg-norr', 2, 2, nowIso())).toMatchObject({ ok: true });
    expect(await claimCell(env.DB, other, 'goteborg-norr', 0, 0, nowIso())).toMatchObject({ ok: true });
    expect(await getCell('goteborg-norr', 0, 0)).toMatchObject({ state: 'last', owner_id: 5151 });
  });
});

describe('admin actions', () => {
  it('markLive refuses duplicate cells and changes nothing', async () => {
    await setCell('goteborg-norr', 1, 1, { state: 'godkand', owner_id: 4242, owner_login: 'klang' });
    expect(await markLive(env.DB, 'goteborg-norr', [[1, 1], [1, 1]], 3, NOW)).toMatchObject({ ok: false, status: 400, code: 'duplicate_cells' });
    expect((await getCell('goteborg-norr', 1, 1)).state).toBe('godkand');
    expect((await env.DB.prepare("SELECT rev FROM sheets WHERE id = 'goteborg-norr'").first()).rev).toBe(0);
  });

  it('counts an approval once even when two approvals race', async () => {
    await setCell('goteborg-norr', 1, 1, { state: 'godkand', owner_id: 4242, owner_login: 'klang' });
    // The read says waiting, as it would for the loser of a race; the write must not count.
    const db = {
      prepare: (sql) => (sql.startsWith('SELECT state, owner_id FROM cells') ? { bind: () => ({ first: async () => ({ state: 'vantar', owner_id: 4242 }) }) } : env.DB.prepare(sql)),
      batch: (s) => env.DB.batch(s),
    };
    expect(await approveCell(db, 'goteborg-norr', 1, 1, NOW)).toMatchObject({ ok: false, code: 'not_waiting' });
    expect((await env.DB.prepare('SELECT approved_count FROM users WHERE github_id = 4242').first()).approved_count).toBe(0);
  });

  it('logs a rejection once', async () => {
    await setCell('goteborg-norr', 1, 1, { state: 'vantar', owner_id: 4242, owner_login: 'klang', uploaded_at: NOW });
    loginAs(user);
    const r = await call(
      new Request('https://x/v1/reject', { method: 'POST', headers: { Authorization: 'Bearer t' }, body: JSON.stringify({ sheet: 'goteborg-norr', col: 1, row: 1, comment: 'suddigt' }) }),
      { ADMIN_GITHUB_ID: '4242' },
    );
    vi.unstubAllGlobals();
    expect(r.status).toBe(200);
    const { results } = await env.DB.prepare("SELECT * FROM events WHERE action = 'reject'").all();
    expect(results).toHaveLength(1);
    expect(results[0].detail).toContain('klang');
  });

  it('trust matches a login regardless of case and refuses an ambiguous one', async () => {
    expect(await setTrust(env.DB, { login: 'KLANG' }, 1)).toEqual({ ok: true });
    expect((await env.DB.prepare('SELECT trusted FROM users WHERE github_id = 4242').first()).trusted).toBe(1);
    await insertUser({ github_id: 9999, login: 'Klang' });
    expect(await setTrust(env.DB, { login: 'klang' }, 0)).toMatchObject({ ok: false, status: 409, code: 'ambiguous_login' });
    expect(await setTrust(env.DB, { github_id: 9999 }, 1)).toEqual({ ok: true });
  });
});

describe('rate limit and public reads', () => {
  it('lets the admin past the rate limit and nobody else', async () => {
    await env.GH.put(await tokenKey('admintoken'), JSON.stringify({ id: 4242, login: 'klang' }));
    const admin = await call(new Request('https://x/v1/queue', { headers: { Authorization: 'Bearer admintoken', 'CF-Connecting-IP': '203.0.113.7' } }), { TAKT: deny, ADMIN_GITHUB_ID: '4242' });
    expect(admin.status).toBe(200);
    const stranger = await call(new Request('https://x/v1/queue', { headers: { Authorization: 'Bearer someoneelse', 'CF-Connecting-IP': '203.0.113.8' } }), { TAKT: deny, ADMIN_GITHUB_ID: '4242' });
    expect(stranger.status).toBe(429);
  });

  it('serves the grid from memory for the cache window', async () => {
    const first = await call(new Request('https://x/v1/grid/goteborg-norr'), { PUBLIC_CACHE_SECONDS: '60' });
    expect(first.status).toBe(200);
    await setCell('goteborg-norr', 0, 0, { state: 'live' });
    const cached = await (await call(new Request('https://x/v1/grid/goteborg-norr'), { PUBLIC_CACHE_SECONDS: '60' })).json();
    expect(cached.cells.find((c) => c[0] === 0 && c[1] === 0)[2]).toBe('fri');
    const fresh = await (await call(new Request('https://x/v1/grid/goteborg-norr'), { PUBLIC_CACHE_SECONDS: '0' })).json();
    expect(fresh.cells.find((c) => c[0] === 0 && c[1] === 0)[2]).toBe('live');
  });
});
