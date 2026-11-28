# Rut-tjänsten (`pixelbygget`)

The service behind Pixelbygget: one Cloudflare Worker that locks grid cells, takes finished cells into an inbox bucket, serves the grid to the build page and the CLI, and lets the admin review. It is the single source of truth for who holds which cell. The design is in [docs/pixelbygget/DESIGN.md](../docs/pixelbygget/DESIGN.md).

State lives in D1 (`sheets`, `cells`, `users`, `events`, `purge`), uploads in the R2 bucket `pixelbygget-inkorg`, GitHub user lookups in KV for ten minutes. The caller sends the GitHub user token from the device flow as a Bearer header; the service asks GitHub who it is and keeps no sessions. With the secret `GITHUB_CLIENT_SECRET` set (the "Pixelbygget" OAuth app's client secret, beside the variable `GITHUB_CLIENT_ID`), GitHub is asked through the app's own token check, so only a token from `rutor login` is accepted. Without it any GitHub token proves an identity here, one from another site's "Sign in with GitHub" too: set it in production (`npx wrangler secret put GITHUB_CLIENT_SECRET`). The one admin is the GitHub id in `ADMIN_GITHUB_ID`. The secret `INTAG_NYCKEL` (32 characters or more) is a key for the nightly publication, which runs on a machine of its own where a GitHub login cannot be renewed by hand: sent as the Bearer token it may read `/queue` and `/inkorg`, and post `/live` and `/blanked`, in the admin's name, past the rate limit. It cannot approve, reject, trust or block. Without the secret no key is let through. Each upload is stored under its own inbox key, the cell plus the first 16 hex of its sha256 and 8 random hex, and the cell records that key. A cron runs every hour: locks past their 48 hours go back to free, and rejected uploads whose 30 days are up are deleted from the `purge` list. A live cell the admin removes is marked `removed` until the intake reports it blanked (`POST /blanked`), which also drops its objects; it cannot be claimed before that. Public reads are kept in memory per isolate for `PUBLIC_CACHE_SECONDS` (60 by default). A claim and an upload need the header `X-Terms-Version`: the version of TERMS.md the caller's rutor says its person accepted, a date no older than `TERMS_VERSION` in `src/terms.js`. Without it they answer 428 `terms_needed` and change nothing. The version is written into the claim's and the upload's line in `events`, the receipt for that cell.

With the secret `GITHUB_CLIENT_SECRET` set and `GITHUB_CLIENT_ID` empty, every logged-in route answers 503 `misconfigured`: "our own logins only" cannot be asked without the app's id, and nobody is let in until both are there.

## Create once (the maintainer)

```bash
npx wrangler@4.163.0 d1 create pixelbygget
```

```bash
npx wrangler@4.163.0 r2 bucket create pixelbygget-inkorg --location weur
```

```bash
npx wrangler@4.163.0 kv namespace create GH
```

Put the printed `database_id` and KV `id` into `wrangler.jsonc`. Then set the admin: your GitHub id (`gh api user --jq .id`) as `ADMIN_GITHUB_ID` in `wrangler.jsonc` or under Settings → Variables.

The CLI needs a GitHub OAuth App named "Pixelbygget" under the organisation with "Enable Device Flow" ticked; its client id is `GITHUB_CLIENT_ID` in `cli/rutor/config.py`, and the same value is the variable `GITHUB_CLIENT_ID` in `wrangler.jsonc` (a test in `cli/tests` holds them together).

## Deploy

Node 22 or newer, logged in with `npx wrangler login`.

```bash
npx wrangler@4.163.0 d1 migrations apply pixelbygget --remote
```

```bash
npx wrangler@4.163.0 deploy
```

`npx wrangler@4.163.0 deploy --dry-run` builds and lists the bindings without uploading.

The migration comes first, always. The code reads columns the migrations add (`made` and `kind` since `0004`), so code deployed before its migration answers 500 to every claim, on every sheet. A migration applied before its code is safe: the older code does not read the new columns.

## Test

Node 22 or newer.

```bash
npm ci
```

```bash
npm test
```

Runs the suite in the Workers runtime with a fresh D1, R2 and KV per file. GitHub is stubbed.

Against a deployed service, `scripts/smoke.sh` walks health, sheets, grid, `/me`, claim, upload and release:

```bash
BASE=https://pixelbygget.<account>.workers.dev GH_TOKEN=<token> scripts/smoke.sh goteborg-norr 64_59
```

## Close

Set `PIXELBYGGET_STANGD` to `"1"` (Settings → Variables, or `wrangler.jsonc` and deploy). Every request but GET gets 503; the grid stays readable.

## Endpoints

All under `/v1`. Errors are `{ "error": "<code>", "message": "..." }`.

| Method | Path | Who | Answers |
| --- | --- | --- | --- |
| GET | `/health` | anyone | 200 |
| GET | `/sheets` | anyone | sheets with `done` and `total` |
| GET | `/cities` | anyone | sheets grouped by city |
| GET | `/grid/{sheet}` | anyone | sheet and cells `[col, row, state, login, updated]`; 404 `no_sheet`. A live cell the admin took down is shown as `last` until its tiles are blanked: it cannot be claimed. A sheet of the tiling lists only the cells that have a row; one nobody has claimed on yet is answered as the tiling computes it, with no cells, `exists: false` on the sheet and `Cache-Control: no-store`: its height and picture top are stand-ins, and nothing is drawn from it |
| GET | `/place?lat=..&lon=..` | anyone | `{sheet, col, row, state}`: the sheet and cell a point on the earth belongs to. A point inside a hand-made sheet's picture belongs to that sheet. Anywhere else in Sweden the tiling decides, and `sheet.exists` is false until someone has claimed a cell there; a cell with no row is `fri`. 404 `outside` outside Sweden, 400 `bad_query`. Writes nothing |
| GET | `/me` | user | login, counts, locks |
| POST | `/claim` `{sheet, col, row}`, and `kind` on a sheet of the tiling | user | 201 `expires_at`; 404 `no_sheet` `no_cell`, 426 `update_needed` (a claim on a sheet of the tiling must say the sheet's `kind`: a client from before the tiling says none, and is refused before anything is locked), 409 `taken` `being_removed` `other_sheet` (the ground belongs to a hand-made sheet), 403 `sheet_closed` `too_many_locks` `too_many_waiting` (five uploads of a builder who is not trusted already wait for review), 422 `outside`, 503 `no_height` `misconfigured` (a new sheet could not be made; nothing was written). A lock past its expiry can be taken. On a sheet of the tiling the first claim makes the sheet's row and the cell's |
| POST | `/release` `{sheet, col, row}` | user | 200; 403 `not_yours`, 409 `already_uploaded` |
| PUT | `/upload/{sheet}/{col}_{row}` | user | body PNG, header `X-Manifest`; 201 `{state}`; 404 `no_cell`, 403 `not_yours`, 411 `no_length`, 413 `too_big`, 400 `bad_manifest` `short_body`, 422 `not_checked` `wrong_pipeline` `bad_size` `sha_mismatch`, 415 `not_png` `not_plain` (the PNG is not 8-bit RGB, is interlaced, or holds alpha, animation, a gamma, a colour profile or any part but the picture, text, a time, the print size and the sRGB mark), 429 `daily_limit`, 409 `lock_expired` `lock_lost` (the lock ended during the upload; nothing kept). The manifest must have `check: "ok"`; strings are at most 200 characters and unknown keys are dropped |
| GET | `/inkorg/{sheet}/{col}_{row}.png` · `.json` | admin | the cell's current upload; header `X-Inbox-Key` |
| POST | `/approve` `{sheet, col, row}` | admin | 200; 409 `not_waiting` |
| POST | `/reject` `{sheet, col, row, comment}` | admin | 200; 409 `nothing_to_reject` `changed_meanwhile`. The cell goes back to free; its upload is purged after 30 days (a live cell: kept until `/blanked`) |
| POST | `/trust` `{login or github_id, trusted}` · `/block` `{login or github_id, blocked}` | admin | 200; 404 `no_user`, 409 `ambiguous_login` (two rows share it: send `github_id`). Login matches regardless of case. A block frees the cell that builder holds; what they uploaded stays in the queue |
| GET | `/queue?state=vantar\|godkand\|removed` | admin | cells in that state with their eight neighbours; `removed` = live cells taken down, for the intake to blank |
| POST | `/live` `{sheet, cells, rev}` | admin (the intake) | 200 `{live}`; all or nothing: 409 `not_approved` (a listed cell was not approved; nothing changed), 409 `stale_rev` (rev must be greater than the sheet's), 400 `duplicate_cells`, 400 `bad_body` (no cells, or `rev` not a whole number from 1) |
| POST | `/blanked` `{sheet, cells}` | admin (the intake) | 200 `{blanked}`: removed cells whose tiles are blank again; drops their inbox objects |
| POST | `/sheets` (sheet fields + `mask`, and `kind` when the sheet is not `open`) | admin | 201 (`resumed: true` when a half-written sheet was completed); 409 `exists`, 400 `bad_body` |
| POST | `/seed` `{sheet, cells, login, github_id}` | admin | 200; 422 `seed_outside_mask` |

Everything answers 503 `closed` when the service is closed (GET excepted), 429 `rate_limited` over 60 requests a minute per client, and 500 `internal` when storage fails. The admin's token, once GitHub has vouched for it, passes the rate limit on admin routes.

A sheet row says which source it is drawn from in `kind`: `open` (orthophoto and OpenStreetMap) or `google` (Google's 3D tiles through the builder's own Cesium ion account). The CLI reads it from `/grid/{sheet}`, so a new sheet needs no line in the drawing code. `made` says how the row came to be: `hand` by the admin through `POST /sheets`, `grid` by the tiling.

## The tiling

Nobody opens a sheet for a new town. The country is cut into fixed sheets by a rule in `src/tiling.js`: a sheet is a box of 0.15° of latitude by 0.25° of longitude, on whole steps, with its origin in the middle, 47 cells high and 41 to 61 wide. Its id is `se-n<lat>-e<lon>`, the origin's hundredths of a degree (`se-n5602-e1262` is Helsingborg). `GET /place` turns a point into a sheet and a cell.

A sheet of the tiling has no row until someone claims a cell on it, and a cell has no row until someone claims it: a cell with no row is free. The first claim writes the sheet's row (`made` `grid`, `kind` `google`, pipeline from the variable `PIPELINE_NOW`) and the cell's, and then locks the cell as any other. The limits are the same as on a hand-made sheet, and they are asked before anything is written.

The ground height of a new sheet is asked of the [Open-Meteo elevation API](https://open-meteo.com/en/docs/elevation-api) once, when the row is made: the sheet's origin, two numbers, and nothing about who claimed. 30 m is added for the geoid. When the answer does not come, the claim is refused with 503 `no_height` and nothing is written. The heights are from the Copernicus DEM (GLO-90) through Open-Meteo, and both ask to be named where the data is used.

The ground inside a hand-made sheet's picture belongs to that sheet. `GET /place` answers the hand-made sheet there, and a claim on a cell of the tiling whose middle lies there is refused with `other_sheet`.

### A city that is on the map and not open

A city the map already shows whole, and that volunteers do not draw in, is registered as a hand-made sheet in the state `done` with an empty mask. Every cell is then `ute`, a click there resolves to that sheet with nothing free, and the tiling does not grid the ground again. Malmö is such a city. The maintainer sends this once, as the admin:

```bash
curl -sS -X POST "$BASE/v1/sheets" -H "Authorization: Bearer $GH_TOKEN" -H "Content-Type: application/json" -d @malmo-norr.json
```

with `malmo-norr.json`:

```json
{
  "id": "malmo-norr",
  "city": "malmo",
  "name": "Malmö",
  "cols": 62,
  "rows": 36,
  "origin_lat": 55.605,
  "origin_lon": 13,
  "x_meters": -7680,
  "y_meters": 3584,
  "ground_height": 0,
  "dzi_levels": 17,
  "pipeline": "2026.11-a",
  "state": "done",
  "queue_order": 2,
  "kind": "google",
  "mask": []
}
```

The picture is 63488 by 36864 px, so 62 by 36 cells. Before sending, put Malmö's `groundHeight` from the live `karta.json` in `ground_height`: the 0 above is a placeholder, and a wrong height moves the picture's edge north or south by 0.7 m per metre. The service keeps no heading, so this holds for a city whose `heading` in `karta.json` is 0. The answer is `201 {"ok": true, "cells": 2232, "fri": 0}`.

`PIPELINE_NOW` in `wrangler.jsonc` has to be the pipeline the CLI ships (`cli/rutor/pipeline.py`); a test in `cli/tests` holds them together. Move both in the same change.

States of a cell: `ute` (not offered: outside the built-up area, or no source to draw it from yet), `fri`, `last` (locked), `vantar` (uploaded, waiting for review), `godkand`, `live`. A live cell credited to `opensverige` is one the map showed before Pixelbygget.
