import { env } from 'cloudflare:test';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { claimCell } from '../src/cells.js';
import { nowIso } from '../src/db.js';
import { ALLOWED_SIDES, MAX_UPLOAD, inspectPng, notPlain, parseManifest } from '../src/upload.js';
import { NOW, call, clearDb, getCell, insertCells, insertSheet, insertUser, setCell } from './fixtures.js';
import { manifestHeader, minimalPng, pngOfKind, sha256Hex } from './png.js';
import { TERMS_VERSION } from '../src/terms.js';

const user = { github_id: 4242, login: 'klang', first_seen: NOW, approved_count: 0, trusted: 0, blocked: 0 };
const trusted = { github_id: 7, login: 'ebba', first_seen: NOW, approved_count: 12, trusted: 1, blocked: 0 };

function loginAs(u) {
  vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ id: u.github_id, login: u.login }), { status: 200, headers: { 'content-type': 'application/json' } }));
}

/** PUTs bytes as the given user, with a manifest built from overrides. */
async function put(u, bytes, manifest = {}, headers = {}) {
  loginAs(u);
  const sha = await sha256Hex(bytes);
  const r = await call(
    new Request('https://x/v1/upload/goteborg-norr/1_1', {
      method: 'PUT',
      headers: {
        Authorization: 'Bearer t',
        'X-Terms-Version': TERMS_VERSION,
        'Content-Type': 'image/png',
        'Content-Length': String(bytes.length),
        'X-Manifest': manifestHeader({ sha256: sha, ...manifest }),
        ...headers,
      },
      body: bytes,
    }),
  );
  vi.unstubAllGlobals();
  return r;
}

beforeEach(async () => {
  await clearDb();
  await insertSheet();
  await insertCells('goteborg-norr', 4, 3);
  await insertUser(user);
  await insertUser(trusted);
  const list = await env.INKORG.list();
  await Promise.all(list.objects.map((o) => env.INKORG.delete(o.key)));
});

describe('helpers', () => {
  it('manifest round-trips and rejects a missing key', () => {
    const m = parseManifest(manifestHeader({ sha256: 'abc' }));
    expect(m).toMatchObject({ pipeline: '2026.11-a', seed: 7, sha256: 'abc', width: 1024, source: { provider: 'opengeodata.goteborg.se orto_2025' } });
    expect(parseManifest(null)).toBeNull();
    expect(parseManifest(btoa('{"pipeline":"x"}'))).toBeNull();
    expect(parseManifest('not base64 json')).toBeNull();
  });

  it('reads width and height out of IHDR', () => {
    expect(inspectPng(minimalPng(1536, 1536))).toEqual({ width: 1536, height: 1536 });
    expect(inspectPng(new Uint8Array([1, 2, 3]))).toBeNull();
    expect(MAX_UPLOAD).toBe(8388608);
    expect(ALLOWED_SIDES).toEqual([1024, 1536]);
  });
});

describe('PUT /v1/upload', () => {
  it('writes png and json and sets vantar', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const bytes = minimalPng(1024, 1024);
    const r = await put(user, bytes);
    expect(r.status).toBe(201);
    const body = await r.json();
    expect(body).toMatchObject({ state: 'vantar', url: 'https://karta.opensverige.se/pixelbygget#goteborg-norr/1_1' });
    const key = (await getCell('goteborg-norr', 1, 1)).inbox_key;
    const png = await env.INKORG.get(`${key}.png`);
    expect(png.httpMetadata.contentType).toBe('image/png');
    expect(new Uint8Array(await png.arrayBuffer())).toEqual(bytes);
    const json = await env.INKORG.get(`${key}.json`);
    expect(JSON.parse(await json.text()).pipeline).toBe('2026.11-a');
    const cell = await getCell('goteborg-norr', 1, 1);
    expect(cell).toMatchObject({ state: 'vantar', bytes: bytes.length, sha256: await sha256Hex(bytes) });
    expect(cell.uploaded_at).toMatch(/Z$/);
    const ev = await env.DB.prepare("SELECT * FROM events WHERE action = 'upload'").first();
    expect(ev).toMatchObject({ github_id: 4242, sheet: 'goteborg-norr', col: 1, row: 1 });
  });

  it('sends a trusted user straight to godkand', async () => {
    await claimCell(env.DB, trusted, 'goteborg-norr', 1, 1, nowIso());
    const r = await put(trusted, minimalPng(1536, 1536), { width: 1536, height: 1536 });
    expect(r.status).toBe(201);
    expect((await r.json()).state).toBe('godkand');
    expect((await getCell('goteborg-norr', 1, 1)).state).toBe('godkand');
  });

  it('refuses a cell that is not yours and writes nothing', async () => {
    await claimCell(env.DB, trusted, 'goteborg-norr', 1, 1, nowIso());
    const r = await put(user, minimalPng(1024, 1024));
    expect(r.status).toBe(403);
    expect((await r.json()).error).toBe('not_yours');
    expect((await env.INKORG.list()).objects).toHaveLength(0);
  });

  it('refuses the wrong pipeline version', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const r = await put(user, minimalPng(1024, 1024), { pipeline: '2025.01-z' });
    expect(r.status).toBe(422);
    expect((await r.json()).error).toBe('wrong_pipeline');
  });

  it('leaves the cell locked when the body is shorter than Content-Length', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const bytes = minimalPng(1024, 1024);
    const r = await put(user, bytes, {}, { 'Content-Length': String(bytes.length + 50) });
    expect(r.status).toBe(400);
    expect((await r.json()).error).toBe('short_body');
    expect((await env.INKORG.list()).objects).toHaveLength(0);
    expect((await getCell('goteborg-norr', 1, 1)).state).toBe('last');
  });

  it('refuses a sha that does not match the manifest', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const r = await put(user, minimalPng(1024, 1024), { sha256: 'deadbeef' });
    expect(r.status).toBe(422);
    expect((await r.json()).error).toBe('sha_mismatch');
  });

  it('refuses a png that is not a square of an allowed side', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const r = await put(user, minimalPng(1024, 512), { height: 512 });
    expect(r.status).toBe(422);
    expect((await r.json()).error).toBe('bad_size');
    const notPng = await put(user, new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8, 9]));
    expect(notPng.status).toBe(415);
    expect((await notPng.json()).error).toBe('not_png');
  });

  it('refuses over 8 MB before reading the body', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const r = await put(user, minimalPng(1024, 1024), {}, { 'Content-Length': '8388609' });
    expect(r.status).toBe(413);
    expect((await r.json()).error).toBe('too_big');
  });

  it('limits a new user to 20 uploads a day', async () => {
    // The route counts from the real clock, so the uploads must be recent in real time.
    const anHourAgo = new Date(Date.now() - 3600 * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
    const statements = [];
    for (let i = 0; i < 20; i += 1) {
      statements.push(env.DB.prepare("INSERT INTO events (at, github_id, login, action, sheet, col, row) VALUES (?, 4242, 'klang', 'upload', 'goteborg-norr', 0, 0)").bind(anHourAgo));
    }
    await env.DB.batch(statements);
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const r = await put(user, minimalPng(1024, 1024));
    expect(r.status).toBe(429);
    expect((await r.json()).error).toBe('daily_limit');
    expect((await getCell('goteborg-norr', 1, 1)).state).toBe('last');
  });
});

describe('PUT /v1/upload, lock lost mid-flight', () => {
  it('answers 409 lock_lost, removes what it wrote and leaves the cell as it found it', async () => {
    await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
    const bytes = minimalPng(1024, 1024);
    const sha = await sha256Hex(bytes);
    let flipped = false;
    const inkorg = {
      put: async (key, value, options) => {
        if (!flipped) {
          flipped = true;
          await setCell('goteborg-norr', 1, 1, { state: 'fri', owner_id: null, owner_login: null, locked_at: null, expires_at: null });
        }
        return env.INKORG.put(key, value, options);
      },
      get: (key) => env.INKORG.get(key),
      delete: (keys) => env.INKORG.delete(keys),
      list: (o) => env.INKORG.list(o),
    };
    loginAs(user);
    const r = await call(
      new Request('https://x/v1/upload/goteborg-norr/1_1', {
        method: 'PUT',
        headers: { Authorization: 'Bearer t', 'X-Terms-Version': TERMS_VERSION, 'Content-Type': 'image/png', 'Content-Length': String(bytes.length), 'X-Manifest': manifestHeader({ sha256: sha }) },
        body: bytes,
      }),
      { INKORG: inkorg },
    );
    vi.unstubAllGlobals();
    expect(r.status).toBe(409);
    expect((await r.json()).error).toBe('lock_lost');
    expect((await env.INKORG.list()).objects).toHaveLength(0);
    expect(await getCell('goteborg-norr', 1, 1)).toMatchObject({ state: 'fri', owner_id: null, sha256: null, uploaded_at: null });
  });
});

describe('GET /v1/inkorg', () => {
  it('serves the object to the admin only', async () => {
    await setCell('goteborg-norr', 1, 1, { state: 'vantar', owner_id: 4242, owner_login: 'klang', inbox_key: 'goteborg-norr/1_1.abcabcabcabcabca' });
    await env.INKORG.put('goteborg-norr/1_1.abcabcabcabcabca.png', minimalPng(1024, 1024), { httpMetadata: { contentType: 'image/png' } });
    loginAs(user);
    const denied = await call(new Request('https://x/v1/inkorg/goteborg-norr/1_1.png', { headers: { Authorization: 'Bearer t' } }));
    expect(denied.status).toBe(403);
    const ok = await call(new Request('https://x/v1/inkorg/goteborg-norr/1_1.png', { headers: { Authorization: 'Bearer t' } }), { ADMIN_GITHUB_ID: '4242' });
    expect(ok.status).toBe(200);
    expect(ok.headers.get('Content-Type')).toBe('image/png');
    const missing = await call(new Request('https://x/v1/inkorg/goteborg-norr/2_2.png', { headers: { Authorization: 'Bearer t' } }), { ADMIN_GITHUB_ID: '4242' });
    expect(missing.status).toBe(404);
    vi.unstubAllGlobals();
  });
});

/* A browser plays an animation, lets what is behind show through alpha and applies a gamma or a
   colour profile. A program that reads only the pixels does none of it. So a file with any of
   that could be approved as one picture and laid into the map as another. The redraw writes
   8-bit RGB and nothing else, and that is all the service takes. */
describe('only a plain picture is taken', () => {
  const odd = {
    'see-through': { colour: 6 },
    'grey with alpha': { colour: 4 },
    grey: { colour: 0 },
    palette: { colour: 3, extra: [['PLTE', [0, 0, 0]]] },
    'sixteen bit': { depth: 16 },
    interlaced: { interlace: 1 },
    animated: { extra: [['acTL', [0, 0, 0, 2, 0, 0, 0, 0]]] },
    'transparent colour': { extra: [['tRNS', [0, 0, 0, 0, 0, 0]]] },
    gamma: { extra: [['gAMA', [0, 0, 0, 1]]] },
    'colour profile': { extra: [['iCCP', [120, 0, 0, 120]]] },
    'primaries': { extra: [['cHRM', new Array(32).fill(0)]] },
    'a part nobody knows': { extra: [['ruTa', [1]]] },
  };

  it('says why a file is not plain, and nothing for one that is', () => {
    for (const [name, kind] of Object.entries(odd)) {
      expect(inspectPng(pngOfKind(kind)), name).toEqual({ width: 1024, height: 1024 });
      expect(notPlain(pngOfKind(kind)), name).toEqual(expect.any(String));
    }
    expect(notPlain(pngOfKind())).toBeNull();
    // Parts that change no pixel: text, a time, the size in print, and "this is sRGB".
    const harmless = [['tEXt', [83, 0, 114]], ['tIME', [7, 234, 11, 23, 2, 8, 11]], ['pHYs', [0, 0, 11, 19, 0, 0, 11, 19, 1]], ['sRGB', [0]]];
    expect(notPlain(pngOfKind({ extra: harmless }))).toBeNull();
    expect(notPlain(pngOfKind({ extra: [['acTL', [0, 0, 0, 2, 0, 0, 0, 0]]] }))).toContain('acTL');
  });

  it('refuses such an upload and keeps the lock', async () => {
    for (const [name, kind] of Object.entries(odd)) {
      await clearDb();
      await insertSheet();
      await insertCells('goteborg-norr', 4, 3);
      await insertUser(user);
      await claimCell(env.DB, user, 'goteborg-norr', 1, 1, nowIso());
      const r = await put(user, pngOfKind(kind));
      expect(r.status, name).toBe(415);
      const body = await r.json();
      expect(body.error, name).toBe('not_plain');
      expect(body.message, name).toContain('8-bit RGB');
      expect((await getCell('goteborg-norr', 1, 1)).state, name).toBe('last');
    }
    expect((await env.INKORG.list()).objects).toEqual([]);
  });

  it('takes a plain one from a trusted builder and an untrusted one alike', async () => {
    await claimCell(env.DB, trusted, 'goteborg-norr', 1, 1, nowIso());
    expect((await put(trusted, pngOfKind({ colour: 6 }))).status).toBe(415);
    const r = await put(trusted, pngOfKind());
    expect(r.status).toBe(201);
    expect((await r.json()).state).toBe('godkand');
  });
});
