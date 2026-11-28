/* The upload: one PNG per cell into the inbox bucket, with its manifest beside it. The Worker
   checks ownership, expiry, size, manifest, PNG chunks and hash before anything is written, so
   a refused upload leaves no object behind and the cell stays locked.

   Every upload has its own inbox key: the cell, the first 16 hex of its sha256 and a random
   part, so two requests never share a key, not even with the same bytes. A request that loses
   its lock half-way deletes only its own key. */

import { requireIntake, requireUser } from './auth.js';
import { logEvent } from './cells.js';
import { error, json } from './db.js';
import { acceptedTerms, termsRefusal } from './terms.js';

export const MAX_UPLOAD = 8388608;
export const ALLOWED_SIDES = [1024, 1536];
const DAILY_UPLOADS_NEW = 20;
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const MANIFEST_KEYS = ['pipeline', 'sha256', 'width', 'height', 'source', 'gpu', 'renderedAt'];

/**
 * @typedef {{ pipeline: string, seed: number|null, sha256: string, width: number, height: number,
 *   source: { provider: string, version: string, sha256?: string }, gpu: string, renderedAt: string,
 *   check: string, source_sha256?: string }} Manifest
 */

const STRING_MAX = 200;

/** True for a string of at most STRING_MAX characters. */
function shortString(value) {
  return typeof value === 'string' && value.length <= STRING_MAX;
}

/**
 * Decodes the X-Manifest header: base64 JSON with every key but seed required. Only the known
 * keys are kept, and every string is at most 200 characters, so the stored JSON stays small.
 * @param {string|null} header
 * @returns {Manifest|null}
 */
export function parseManifest(header) {
  if (!header) return null;
  let parsed;
  try {
    parsed = JSON.parse(atob(header));
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object') return null;
  for (const key of MANIFEST_KEYS) if (!(key in parsed)) return null;
  if (!shortString(parsed.pipeline) || !shortString(parsed.sha256)) return null;
  if (!Number.isInteger(parsed.width) || !Number.isInteger(parsed.height)) return null;
  const source = parsed.source;
  if (!source || typeof source !== 'object' || !shortString(source.provider) || !shortString(source.version)) return null;
  if (source.sha256 !== undefined && !shortString(source.sha256)) return null;
  if (!shortString(parsed.gpu) || !shortString(parsed.renderedAt)) return null;
  if (parsed.check !== undefined && !shortString(parsed.check)) return null;
  if (parsed.source_sha256 !== undefined && !shortString(parsed.source_sha256)) return null;
  /** @type {Manifest} */
  const manifest = {
    pipeline: parsed.pipeline,
    seed: Number.isInteger(parsed.seed) ? parsed.seed : null,
    sha256: parsed.sha256,
    width: parsed.width,
    height: parsed.height,
    source: { provider: source.provider, version: source.version, ...(source.sha256 ? { sha256: source.sha256 } : {}) },
    gpu: parsed.gpu,
    renderedAt: parsed.renderedAt,
    check: typeof parsed.check === 'string' ? parsed.check : '',
  };
  if (typeof parsed.source_sha256 === 'string') manifest.source_sha256 = parsed.source_sha256;
  return manifest;
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

/**
 * CRC-32 as PNG uses it.
 * @param {Uint8Array} bytes
 * @returns {number}
 */
function crc32(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** Bit depths each PNG colour type allows. */
const DEPTHS = { 0: [1, 2, 4, 8, 16], 2: [8, 16], 3: [1, 2, 4, 8], 4: [8, 16], 6: [8, 16] };

/**
 * Width and height of a well-formed PNG, or null. Every chunk is walked: IHDR first with 13
 * bytes and a valid depth and colour type, at least one IDAT, and IEND as the very last bytes,
 * so nothing can hide after the image. Every CRC but the image data's is checked: CRC over a
 * full 8 MB costs about 14 ms of CPU, more than the Workers Free plan allows, and the intake
 * decodes the image data anyway.
 * @param {Uint8Array} bytes
 * @returns {{ width: number, height: number }|null}
 */
export function inspectPng(bytes) {
  if (bytes.length < 8 + 25 + 12) return null;
  for (let i = 0; i < 8; i += 1) if (bytes[i] !== PNG_SIGNATURE[i]) return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let pos = 8;
  let size = null;
  let images = 0;
  while (pos + 12 <= bytes.length) {
    const length = view.getUint32(pos);
    if (length > bytes.length - pos - 12) return null;
    const type = String.fromCharCode(bytes[pos + 4], bytes[pos + 5], bytes[pos + 6], bytes[pos + 7]);
    if (type !== 'IDAT' && crc32(bytes.subarray(pos + 4, pos + 8 + length)) !== view.getUint32(pos + 8 + length)) return null;
    if (size === null) {
      if (type !== 'IHDR' || length !== 13) return null;
      const depth = bytes[pos + 16];
      const colour = bytes[pos + 17];
      if (!(DEPTHS[colour] || []).includes(depth)) return null;
      if (bytes[pos + 18] !== 0 || bytes[pos + 19] !== 0 || bytes[pos + 20] > 1) return null;
      size = { width: view.getUint32(pos + 8), height: view.getUint32(pos + 12) };
    } else if (type === 'IHDR') {
      return null;
    }
    if (type === 'IDAT') images += 1;
    pos += 12 + length;
    if (type === 'IEND') return length === 0 && images > 0 && pos === bytes.length ? size : null;
  }
  return null;
}

/* Parts of a PNG that change no pixel: text, a time, the size in print, and "this is sRGB",
   which is what a file that says nothing is taken to be. */
const PLAIN_CHUNKS = new Set(['IHDR', 'IDAT', 'IEND', 'pHYs', 'tEXt', 'zTXt', 'iTXt', 'tIME', 'sRGB']);

/**
 * Why two programs may show this PNG differently, or null for a plain picture. Call it on bytes
 * inspectPng has passed.
 *
 * A browser plays an animation, lets what is behind show through alpha, and applies a gamma or
 * a colour profile. A program that reads only the pixels may do none of it. The reviewer looks
 * in a browser and the intake reads pixels, so a file with any of that could be approved as one
 * picture and laid into the map as another. The redraw writes 8-bit RGB and nothing else.
 * @param {Uint8Array} bytes
 * @returns {string|null}
 */
export function notPlain(bytes) {
  if (bytes[24] !== 8 || bytes[25] !== 2) return 'is not 8-bit RGB without alpha';
  if (bytes[28] !== 0) return 'is interlaced';
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let pos = 8; pos + 12 <= bytes.length; pos += 12 + view.getUint32(pos)) {
    const type = String.fromCharCode(bytes[pos + 4], bytes[pos + 5], bytes[pos + 6], bytes[pos + 7]);
    if (!PLAIN_CHUNKS.has(type)) return `holds a part named ${/^[A-Za-z]{4}$/.test(type) ? type : 'something unreadable'}`;
  }
  return null;
}

/**
 * The inbox key of one upload: the cell, the first 16 hex of its sha256 and 8 random hex, so
 * every request has a key of its own. The objects are `${key}.png` and `${key}.json`.
 * @param {string} sheet
 * @param {number} col
 * @param {number} row
 * @param {string} sha
 * @param {string} [nonce]
 * @returns {string}
 */
export function inboxKey(sheet, col, row, sha, nonce = crypto.randomUUID().replace(/-/g, '').slice(0, 8)) {
  return `${sheet}/${col}_${row}.${sha.slice(0, 16)}.${nonce}`;
}

/**
 * Hex sha256 of bytes.
 * @param {ArrayBuffer|Uint8Array} bytes
 * @returns {Promise<string>}
 */
export async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * Uploads in the last 24 hours by this user.
 * @param {D1Database} db
 * @param {number} githubId
 * @param {string} now
 * @returns {Promise<number>}
 */
async function uploadsLastDay(db, githubId, now) {
  const since = new Date(Date.parse(now) - 24 * 3600 * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
  const row = await db.prepare("SELECT COUNT(*) AS n FROM events WHERE github_id = ? AND action = 'upload' AND at >= ?").bind(githubId, since).first();
  return row.n;
}

/** PUT /v1/upload/:sheet/:col_:row */
export const uploadRoute = requireUser(async (request, env, ctx, params, now, user) => {
  // Before the body is read: a refusal here costs the caller nothing and keeps their lock.
  const terms = acceptedTerms(request);
  if (!terms) return termsRefusal();
  const sheet = params.sheet;
  const col = Number(params.col);
  const row = Number(params.row);
  const cell = await env.DB.prepare('SELECT state, owner_id, expires_at FROM cells WHERE sheet = ? AND col = ? AND row = ?').bind(sheet, col, row).first();
  if (!cell) return error(404, 'no_cell', 'No such cell.');
  if (cell.owner_id !== user.github_id || cell.state !== 'last') return error(403, 'not_yours', 'You do not hold a lock on that cell.');
  if (!cell.expires_at || cell.expires_at <= now) return error(409, 'lock_expired', 'Your lock on that cell has expired. Claim it again, then upload.');
  const declared = Number(request.headers.get('Content-Length'));
  if (!Number.isInteger(declared) || declared <= 0) return error(411, 'no_length', 'Send Content-Length.');
  if (declared > MAX_UPLOAD) return error(413, 'too_big', `A cell is at most ${MAX_UPLOAD} bytes.`);
  const manifest = parseManifest(request.headers.get('X-Manifest'));
  if (!manifest) return error(400, 'bad_manifest', 'X-Manifest must be base64 JSON with pipeline, sha256, width, height, source, gpu and renderedAt, each string at most 200 characters.');
  if (manifest.check !== 'ok') return error(422, 'not_checked', 'This cell has not passed rutor check. Run it, then upload.');
  const sheetRow = await env.DB.prepare('SELECT pipeline FROM sheets WHERE id = ?').bind(sheet).first();
  if (!sheetRow) return error(404, 'no_sheet', 'No such sheet.');
  if (manifest.pipeline !== sheetRow.pipeline) {
    return error(422, 'wrong_pipeline', `This sheet is drawn with pipeline ${sheetRow.pipeline}; your manifest says ${manifest.pipeline}. Update the template.`);
  }
  if (!user.trusted && (await uploadsLastDay(env.DB, user.github_id, now)) >= DAILY_UPLOADS_NEW) {
    return error(429, 'daily_limit', `New builders may upload ${DAILY_UPLOADS_NEW} cells a day. Try again tomorrow.`, { 'Retry-After': '3600' });
  }
  const bytes = new Uint8Array(await request.arrayBuffer());
  if (bytes.length !== declared) return error(400, 'short_body', `Content-Length said ${declared} bytes, ${bytes.length} arrived.`);
  const png = inspectPng(bytes);
  if (!png) return error(415, 'not_png', 'The body is not a well-formed PNG.');
  const odd = notPlain(bytes);
  if (odd) {
    return error(415, 'not_plain', `A cell is a plain picture: 8-bit RGB with no alpha, animation, gamma or colour profile. This file ${odd}. rutor redraw writes a plain one; do not save the PNG again in another program.`);
  }
  if (png.width !== png.height || !ALLOWED_SIDES.includes(png.width) || png.width !== manifest.width || png.height !== manifest.height) {
    return error(422, 'bad_size', `A cell is a square of ${ALLOWED_SIDES.join(' or ')} px and the manifest must say so.`);
  }
  const sha = await sha256Hex(bytes);
  if (sha !== manifest.sha256) return error(422, 'sha_mismatch', 'The file does not match the sha256 in the manifest.');
  const key = inboxKey(sheet, col, row, sha);
  const state = user.trusted ? 'godkand' : 'vantar';
  try {
    await env.INKORG.put(`${key}.png`, bytes, { httpMetadata: { contentType: 'image/png' } });
    await env.INKORG.put(`${key}.json`, JSON.stringify({ ...manifest, sheet, col, row, login: user.login, github_id: user.github_id, uploadedAt: now }), {
      httpMetadata: { contentType: 'application/json' },
    });
  } catch (e) {
    // No cell points at these objects, so nothing would ever take them away. Only a write
    // that failed is undone here: once the cell is asked to change state, a call that
    // throws may still have changed it, and the cell must not point at nothing.
    await env.INKORG.delete([`${key}.png`, `${key}.json`]).catch(() => undefined);
    throw e;
  }
  // The lock may have expired or been freed while the file was written: the cell changes
  // state only if it is still this user's live lock, otherwise our objects go again.
  const result = await env.DB.prepare(
    "UPDATE cells SET state = ?, uploaded_at = ?, bytes = ?, sha256 = ?, inbox_key = ?, approved_at = ?, updated_at = ? WHERE sheet = ? AND col = ? AND row = ? AND state = 'last' AND owner_id = ? AND expires_at > ?",
  )
    .bind(state, now, bytes.length, sha, key, user.trusted ? now : null, now, sheet, col, row, user.github_id, now)
    .run();
  if (result.meta.changes !== 1) {
    // The key is this request's own, so nobody else's upload can be under it.
    await env.INKORG.delete([`${key}.png`, `${key}.json`]);
    return error(409, 'lock_lost', 'Your lock on that cell ended before the upload finished. Claim it again, then upload.');
  }
  await logEvent(env.DB, { at: now, github_id: user.github_id, login: user.login, action: 'upload', sheet, col, row, detail: `${bytes.length} bytes, ${png.width} px, ${state}, terms ${terms}` });
  return json(201, { state, url: `https://karta.opensverige.se/pixelbygget#${sheet}/${col}_${row}` });
});

/** GET /v1/inkorg/:sheet/:col_:row.(png|json), admin only: the cell's current upload. */
export const inkorgRoute = requireIntake(async (request, env, ctx, params) => {
  const cell = await env.DB.prepare('SELECT inbox_key FROM cells WHERE sheet = ? AND col = ? AND row = ?')
    .bind(params.sheet, Number(params.col), Number(params.row))
    .first();
  const object = cell && cell.inbox_key ? await env.INKORG.get(`${cell.inbox_key}.${params.ext}`) : null;
  if (!object) return error(404, 'not_found', 'Nothing in the inbox for that cell.');
  const headers = new Headers();
  headers.set('Content-Type', object.httpMetadata?.contentType || 'application/octet-stream');
  headers.set('Cache-Control', 'private, no-store');
  headers.set('X-Inbox-Key', cell.inbox_key);
  return new Response(object.body, { status: 200, headers });
});
