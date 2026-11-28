import { env } from 'cloudflare:test';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { approveCell, listQueue, markBlanked, markLive, rejectCell, setBlock, setTrust } from '../src/admin.js';
import { NOW, call, clearDb, getCell, insertCells, insertSheet, insertUser, setCell } from './fixtures.js';
import { minimalPng } from './png.js';

const LATER = '2026-11-17T13:00:00Z';

async function uploaded(col, row, state = 'vantar', owner = { id: 4242, login: 'klang' }) {
  const key = `goteborg-norr/${col}_${row}.abcabcabcabcabca`;
  await setCell('goteborg-norr', col, row, { state, owner_id: owner.id, owner_login: owner.login, uploaded_at: NOW, sha256: 'abc', bytes: 10, inbox_key: key });
  await env.INKORG.put(`${key}.png`, minimalPng(1024, 1024), { httpMetadata: { contentType: 'image/png' } });
  await env.INKORG.put(`${key}.json`, '{}', { httpMetadata: { contentType: 'application/json' } });
}

async function inboxKeys() {
  return (await env.INKORG.list()).objects.map((o) => o.key).sort();
}

beforeEach(async () => {
  await clearDb();
  await insertSheet();
  await insertCells('goteborg-norr', 4, 3);
  await insertUser();
  const list = await env.INKORG.list();
  await Promise.all(list.objects.map((o) => env.INKORG.delete(o.key)));
});

describe('approveCell', () => {
  it('sets godkand and counts for the owner', async () => {
    await uploaded(1, 1);
    const r = await approveCell(env.DB, 'goteborg-norr', 1, 1, LATER);
    expect(r).toEqual({ ok: true });
    expect(await getCell('goteborg-norr', 1, 1)).toMatchObject({ state: 'godkand', approved_at: LATER, updated_at: LATER });
    const u = await env.DB.prepare('SELECT approved_count, trusted FROM users WHERE github_id = 4242').first();
    expect(u).toEqual({ approved_count: 1, trusted: 0 });
  });

  it('never gives trust by itself: a person decides who skips review', async () => {
    await env.DB.prepare('UPDATE users SET approved_count = 9 WHERE github_id = 4242').run();
    await uploaded(1, 1);
    await approveCell(env.DB, 'goteborg-norr', 1, 1, LATER);
    const u = await env.DB.prepare('SELECT approved_count, trusted FROM users WHERE github_id = 4242').first();
    expect(u).toEqual({ approved_count: 10, trusted: 0 });
  });

  it('does not re-trust a user whose trust was revoked', async () => {
    await env.DB.prepare('UPDATE users SET approved_count = 11, trusted = 0 WHERE github_id = 4242').run();
    await uploaded(1, 1);
    await approveCell(env.DB, 'goteborg-norr', 1, 1, LATER);
    const u = await env.DB.prepare('SELECT approved_count, trusted FROM users WHERE github_id = 4242').first();
    expect(u).toEqual({ approved_count: 12, trusted: 0 });
  });

  it('is 409 when the cell is not waiting', async () => {
    expect(await approveCell(env.DB, 'goteborg-norr', 0, 0, LATER)).toMatchObject({ ok: false, status: 409, code: 'not_waiting' });
  });
});

describe('rejectCell', () => {
  it('frees the cell, keeps the comment and puts the inbox objects on the purge list', async () => {
    await uploaded(1, 1);
    const r = await rejectCell(env.DB, env.INKORG, 'goteborg-norr', 1, 1, 'vattnet saknar kant', LATER);
    expect(r).toEqual({ ok: true });
    expect(await getCell('goteborg-norr', 1, 1)).toMatchObject({ state: 'fri', owner_id: null, owner_login: null, uploaded_at: null, sha256: null, bytes: null, inbox_key: null, rejected_at: LATER, comment: 'vattnet saknar kant' });
    expect(await inboxKeys()).toEqual(['goteborg-norr/1_1.abcabcabcabcabca.json', 'goteborg-norr/1_1.abcabcabcabcabca.png']);
    const { results } = await env.DB.prepare('SELECT key, due FROM purge ORDER BY key').all();
    expect(results).toEqual([
      { key: 'goteborg-norr/1_1.abcabcabcabcabca.json', due: '2026-12-17T13:00:00Z' },
      { key: 'goteborg-norr/1_1.abcabcabcabcabca.png', due: '2026-12-17T13:00:00Z' },
    ]);
  });

  it('marks a live cell removed, keeps its objects and logs live-removed', async () => {
    await uploaded(1, 1, 'live');
    await rejectCell(env.DB, env.INKORG, 'goteborg-norr', 1, 1, 'fel hus', LATER);
    expect(await getCell('goteborg-norr', 1, 1)).toMatchObject({ state: 'fri', removed_at: LATER, rejected_at: null, inbox_key: 'goteborg-norr/1_1.abcabcabcabcabca' });
    expect(await inboxKeys()).toEqual(['goteborg-norr/1_1.abcabcabcabcabca.json', 'goteborg-norr/1_1.abcabcabcabcabca.png']);
    const ev = await env.DB.prepare("SELECT detail FROM events WHERE action = 'reject'").first();
    expect(ev.detail).toContain('live-removed');
    const removed = await listQueue(env.DB, 'removed');
    expect(removed.map((c) => [c.col, c.row])).toEqual([[1, 1]]);
    expect(await markBlanked(env.DB, env.INKORG, 'goteborg-norr', [[1, 1]], '2026-11-17T14:00:00Z')).toEqual({ ok: true, blanked: 1 });
    expect((await getCell('goteborg-norr', 1, 1)).removed_at).toBeNull();
    expect(await inboxKeys()).toEqual([]);
  });

  it('is 409 for a free cell', async () => {
    expect(await rejectCell(env.DB, env.INKORG, 'goteborg-norr', 0, 0, 'x', LATER)).toMatchObject({ ok: false, status: 409, code: 'nothing_to_reject' });
  });
});

describe('trust and block', () => {
  it('sets and clears trust and block by login', async () => {
    expect(await setTrust(env.DB, { login: 'klang' }, 1)).toEqual({ ok: true });
    expect((await env.DB.prepare('SELECT trusted FROM users WHERE login = ?').bind('klang').first()).trusted).toBe(1);
    expect(await setBlock(env.DB, { login: 'klang' }, 1)).toEqual({ ok: true });
    expect((await env.DB.prepare('SELECT blocked FROM users WHERE login = ?').bind('klang').first()).blocked).toBe(1);
    expect(await setTrust(env.DB, { login: 'nobody' }, 1)).toMatchObject({ ok: false, status: 404, code: 'no_user' });
  });
});

describe('listQueue', () => {
  it('lists waiting cells with their eight neighbours', async () => {
    await uploaded(1, 1);
    await setCell('goteborg-norr', 0, 0, { state: 'live' });
    await setCell('goteborg-norr', 2, 2, { state: 'last' });
    const queue = await listQueue(env.DB, 'vantar');
    expect(queue).toHaveLength(1);
    expect(queue[0]).toMatchObject({ sheet: 'goteborg-norr', col: 1, row: 1, state: 'vantar', owner_login: 'klang' });
    expect(queue[0].neighbours).toHaveLength(8);
    expect(queue[0].neighbours).toContainEqual([0, 0, 'live']);
    expect(queue[0].neighbours).toContainEqual([2, 2, 'last']);
    expect(queue[0].neighbours).toContainEqual([1, 0, 'fri']);
  });
});

describe('markLive', () => {
  it('marks all approved cells live and bumps the sheet revision, or nothing', async () => {
    await uploaded(1, 1, 'godkand');
    await uploaded(2, 1, 'godkand');
    const bad = await markLive(env.DB, 'goteborg-norr', [[1, 1], [0, 0]], 3, LATER);
    expect(bad).toMatchObject({ ok: false, status: 409, code: 'not_approved' });
    expect((await getCell('goteborg-norr', 1, 1)).state).toBe('godkand');
    const ok = await markLive(env.DB, 'goteborg-norr', [[1, 1], [2, 1]], 3, LATER);
    expect(ok).toEqual({ ok: true, live: 2 });
    const backwards = await markLive(env.DB, 'goteborg-norr', [], 2, LATER);
    expect(backwards).toMatchObject({ ok: false, status: 409, code: 'stale_rev' });
    expect((await env.DB.prepare("SELECT rev FROM sheets WHERE id = 'goteborg-norr'").first()).rev).toBe(3);
    expect(await getCell('goteborg-norr', 1, 1)).toMatchObject({ state: 'live', live_at: LATER });
    expect(await getCell('goteborg-norr', 2, 1)).toMatchObject({ state: 'live', live_at: LATER });
    expect((await env.DB.prepare("SELECT rev FROM sheets WHERE id = 'goteborg-norr'").first()).rev).toBe(3);
  });
});

describe('admin routes', () => {
  function as(githubId, path, method = 'POST', body = undefined) {
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ id: githubId, login: githubId === 4242 ? 'klang' : 'gustaf' }), { status: 200, headers: { 'content-type': 'application/json' } }));
    const init = { method, headers: { Authorization: 'Bearer t', 'Content-Type': 'application/json' } };
    if (body !== undefined) init.body = JSON.stringify(body);
    return call(new Request(`https://x/v1/${path}`, init), { ADMIN_GITHUB_ID: '1' });
  }

  it('refuses a non-admin on every admin route', async () => {
    for (const [path, method, body] of [
      ['approve', 'POST', { sheet: 'goteborg-norr', col: 1, row: 1 }],
      ['reject', 'POST', { sheet: 'goteborg-norr', col: 1, row: 1, comment: 'x' }],
      ['trust', 'POST', { login: 'klang', trusted: 1 }],
      ['block', 'POST', { login: 'klang', blocked: 1 }],
      ['queue?state=vantar', 'GET'],
      ['live', 'POST', { sheet: 'goteborg-norr', cells: [], rev: 1 }],
    ]) {
      const r = await as(4242, path, method, body);
      expect(r.status, path).toBe(403);
      expect((await r.json()).error).toBe('not_admin');
    }
    vi.unstubAllGlobals();
  });

  it('approve, reject and queue work for the admin and log events', async () => {
    await insertUser({ github_id: 1, login: 'gustaf' });
    await uploaded(1, 1);
    await uploaded(2, 1);
    const q = await as(1, 'queue?state=vantar', 'GET');
    expect(q.status).toBe(200);
    expect((await q.json()).cells).toHaveLength(2);
    const a = await as(1, 'approve', 'POST', { sheet: 'goteborg-norr', col: 1, row: 1 });
    expect(a.status).toBe(200);
    const noComment = await as(1, 'reject', 'POST', { sheet: 'goteborg-norr', col: 2, row: 1, comment: '' });
    expect(noComment.status).toBe(400);
    const rj = await as(1, 'reject', 'POST', { sheet: 'goteborg-norr', col: 2, row: 1, comment: 'suddigt' });
    expect(rj.status).toBe(200);
    const live = await as(1, 'live', 'POST', { sheet: 'goteborg-norr', cells: [[1, 1]], rev: 2 });
    expect(live.status).toBe(200);
    expect(await live.json()).toEqual({ ok: true, live: 1 });
    const { results } = await env.DB.prepare("SELECT action FROM events WHERE github_id = 1 ORDER BY id").all();
    expect(results.map((r) => r.action)).toEqual(['approve', 'reject', 'live']);
    vi.unstubAllGlobals();
  });
});

describe('the intake key', () => {
  const KEY = 'k'.repeat(40);

  function withKey(path, method = 'GET', body = undefined, overrides = {}, key = KEY) {
    // GitHub must not be asked for the key. A non-admin answers when it is, so a wrong key is refused.
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ id: 4242, login: 'klang' }), { status: 200, headers: { 'content-type': 'application/json' } }));
    const init = { method, headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json', 'CF-Connecting-IP': '203.0.113.9' } };
    if (body !== undefined) init.body = JSON.stringify(body);
    return call(new Request(`https://x/v1/${path}`, init), { ADMIN_GITHUB_ID: '1', INTAG_NYCKEL: KEY, ...overrides });
  }

  it('reads the queue and the inbox and marks cells live, in the admin name', async () => {
    await insertUser({ github_id: 1, login: 'gustaf' });
    await uploaded(1, 1, 'godkand');
    const q = await withKey('queue?state=godkand');
    expect(q.status).toBe(200);
    expect((await q.json()).cells).toHaveLength(1);
    const png = await withKey('inkorg/goteborg-norr/1_1.png');
    expect(png.status).toBe(200);
    const live = await withKey('live', 'POST', { sheet: 'goteborg-norr', cells: [[1, 1]], rev: 2 });
    expect(live.status).toBe(200);
    expect(await getCell('goteborg-norr', 1, 1)).toMatchObject({ state: 'live' });
    const { results } = await env.DB.prepare("SELECT action FROM events WHERE github_id = 1 ORDER BY id").all();
    expect(results.map((r) => r.action)).toEqual(['live']);
    vi.unstubAllGlobals();
  });

  it('may not approve, reject, trust or block: those stay with a person', async () => {
    await insertUser({ github_id: 1, login: 'gustaf' });
    await uploaded(1, 1);
    for (const [path, body] of [
      ['approve', { sheet: 'goteborg-norr', col: 1, row: 1 }],
      ['reject', { sheet: 'goteborg-norr', col: 1, row: 1, comment: 'x' }],
      ['trust', { login: 'klang', trusted: 1 }],
      ['block', { login: 'klang', blocked: 1 }],
    ]) {
      const r = await withKey(path, 'POST', body);
      expect(r.status, path).toBe(403);
    }
    expect(await getCell('goteborg-norr', 1, 1)).toMatchObject({ state: 'vantar' });
    vi.unstubAllGlobals();
  });

  it('a wrong key, or a key in the service shorter than 32, is no key', async () => {
    await insertUser({ github_id: 1, login: 'gustaf' });
    expect((await withKey('queue?state=vantar', 'GET', undefined, {}, 'x'.repeat(40))).status).toBe(403);
    expect((await withKey('queue?state=vantar', 'GET', undefined, { INTAG_NYCKEL: 'kort' }, 'kort')).status).toBe(403);
    vi.unstubAllGlobals();
  });

  it('passes the rate limit on its own routes, and only there', async () => {
    await insertUser({ github_id: 1, login: 'gustaf' });
    const over = { TAKT: { limit: async () => ({ success: false }) } };
    expect((await withKey('queue?state=vantar', 'GET', undefined, over)).status).toBe(200);
    expect((await withKey('claim', 'POST', { sheet: 'goteborg-norr', col: 1, row: 1 }, over)).status).toBe(429);
    vi.unstubAllGlobals();
  });

  it('without the admin row the service says it is set up wrongly', async () => {
    const r = await withKey('queue?state=vantar');
    expect(r.status).toBe(503);
    expect((await r.json()).error).toBe('misconfigured');
    vi.unstubAllGlobals();
  });
});
