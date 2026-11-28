/* The terms a claim and an upload carry: refused without today's, and kept in the event log. */

import { env } from 'cloudflare:test';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { claimCell } from '../src/cells.js';
import { nowIso } from '../src/db.js';
import { TERMS_VERSION, acceptedTerms } from '../src/terms.js';
import { NOW, call, clearDb, getCell, insertCells, insertSheet, insertUser } from './fixtures.js';
import { manifestHeader, minimalPng, sha256Hex } from './png.js';

const user = { github_id: 4242, login: 'klang', first_seen: NOW, approved_count: 0, trusted: 0, blocked: 0 };

/** The request headers with the terms header set to `terms`, or left out when it is null. */
function withTerms(terms, headers) {
  return terms === null ? headers : { ...headers, 'X-Terms-Version': terms };
}

function claim(terms, cell = { sheet: 'goteborg-norr', col: 1, row: 1 }) {
  return call(
    new Request('https://x/v1/claim', {
      method: 'POST',
      headers: withTerms(terms, { Authorization: 'Bearer t', 'Content-Type': 'application/json' }),
      body: JSON.stringify(cell),
    }),
  );
}

async function upload(terms) {
  const bytes = minimalPng(1024, 1024);
  return call(
    new Request('https://x/v1/upload/goteborg-norr/1_1', {
      method: 'PUT',
      headers: withTerms(terms, {
        Authorization: 'Bearer t',
        'Content-Type': 'image/png',
        'Content-Length': String(bytes.length),
        'X-Manifest': manifestHeader({ sha256: await sha256Hex(bytes) }),
      }),
      body: bytes,
    }),
  );
}

async function events(action) {
  const { results } = await env.DB.prepare('SELECT * FROM events WHERE action = ? ORDER BY id').bind(action).all();
  return results;
}

beforeEach(async () => {
  await clearDb();
  await insertSheet();
  await insertCells('goteborg-norr', 4, 3);
  await insertUser(user);
  vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ id: 4242, login: 'klang' }), { status: 200, headers: { 'content-type': 'application/json' } }));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('the terms header', () => {
  it('passes today’s version and a newer one, and nothing else', () => {
    const asked = (value) => acceptedTerms(new Request('https://x/', { headers: value === undefined ? {} : { 'X-Terms-Version': value } }));
    expect(asked(TERMS_VERSION)).toBe(TERMS_VERSION);
    expect(asked(` ${TERMS_VERSION} `)).toBe(TERMS_VERSION);
    expect(asked('2999-01-01')).toBe('2999-01-01');
    expect(asked('2026-11-23')).toBeNull();
    expect(asked(undefined)).toBeNull();
    expect(asked('')).toBeNull();
    expect(asked('9999-99-99; DROP TABLE events')).toBeNull();
    expect(asked('yes')).toBeNull();
  });
});

describe('POST /v1/claim and the terms', () => {
  it('refuses a claim without the terms, says what to do, and locks nothing', async () => {
    const r = await claim(null);
    expect(r.status).toBe(428);
    const body = await r.json();
    expect(body.error).toBe('terms_needed');
    // A rutor from before prints only this line, so it has to say all of it.
    expect(body.message).toContain('rutor terms');
    expect(body.message).toContain('Update rutor');
    expect(body.message).toContain('lock you hold is kept');
    expect((await getCell('goteborg-norr', 1, 1)).state).toBe('fri');
    expect(await events('claim')).toHaveLength(0);
  });

  it('refuses an older version and one of another shape', async () => {
    expect((await claim('2026-11-23')).status).toBe(428);
    expect((await claim('accepted')).status).toBe(428);
    expect((await getCell('goteborg-norr', 1, 1)).state).toBe('fri');
  });

  it('takes the claim with today’s version and writes it into the claim’s event', async () => {
    const r = await claim(TERMS_VERSION);
    expect(r.status).toBe(201);
    const [event] = await events('claim');
    expect(event).toMatchObject({ github_id: 4242, login: 'klang', sheet: 'goteborg-norr', col: 1, row: 1, detail: `terms ${TERMS_VERSION}` });
  });

  it('lets a release through without the terms', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const r = await call(
      new Request('https://x/v1/release', {
        method: 'POST',
        headers: { Authorization: 'Bearer t', 'Content-Type': 'application/json' },
        body: JSON.stringify({ sheet: 'goteborg-norr', col: 1, row: 1 }),
      }),
    );
    expect(r.status).toBe(200);
    expect((await getCell('goteborg-norr', 1, 1)).state).toBe('fri');
  });
});

describe('PUT /v1/upload and the terms', () => {
  it('refuses an upload without the terms, keeps the lock and writes nothing', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const r = await upload(null);
    expect(r.status).toBe(428);
    expect((await r.json()).error).toBe('terms_needed');
    const cell = await getCell('goteborg-norr', 1, 1);
    expect(cell.state).toBe('last');
    expect(cell.owner_id).toBe(4242);
    expect((await env.INKORG.list()).objects.filter((o) => o.key.startsWith('goteborg-norr/1_1.'))).toHaveLength(0);
    expect(await events('upload')).toHaveLength(0);
  });

  it('writes the version into the upload’s event, the receipt for the cell', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const r = await upload(TERMS_VERSION);
    expect(r.status).toBe(201);
    const [event] = await events('upload');
    expect(event.detail).toMatch(new RegExp(`, terms ${TERMS_VERSION}$`));
  });
});
