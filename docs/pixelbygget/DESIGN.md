# Pixelbygget design

How the parts fit together. The commands are in [README.md](../../README.md), the rules in [CONTRIBUTING.md](../../CONTRIBUTING.md), the service API in [service/README.md](../../service/README.md). What we measured and why things are the way they are is in [LEARNINGS.md](LEARNINGS.md).

## The idea

The pixel cities on karta.opensverige.se are drawn cell by cell. Anyone with an NVIDIA card, or an AMD card on Linux, claims a cell, renders it from the sheet's source, redraws it as pixel art on their own machine and uploads the result. An admin reviews it, and the cell lights up on the map with the builder's GitHub login.

## Sheets and cells

- A city is one or more **sheets**. A sheet has an origin (latitude, longitude), a size in cells and a fixed camera. Today: `goteborg-norr` (130 by 118 cells) and `stockholm-norr` (144 by 70).
- A **cell** is 1024 px at 0.25 m per pixel: 256 m east-west and 362 m north-south (the camera looks down at 45 degrees).
- The render and the redraw work on a **1536 px window**: the cell plus 256 px on each side. Neighbouring windows overlap by 512 px.
- **Volunteers draw what is missing.** A cell the live map already shows is `live` from the start, credited to `opensverige`, and cannot be taken. A `fri` cell is built-up ground the map has not drawn: at least 5 percent of the cell lies inside an SCB 2023 urban area plus 500 m, and none of it is on the map.
- Every other cell is `ute` and is not offered: ground outside the built-up area, and built-up ground the sheet has no source for yet. On `goteborg-norr` that is all of the missing ground, since it lies beyond the city's own orthophoto.

## The parts

| Part | Where | What it does |
| --- | --- | --- |
| `rutor` | `cli/` in this repo | Login, claim, render, redraw, check, upload, release. |
| Service | `service/` in this repo, a Cloudflare Worker | Locks, uploads, the review queue, the grid. |
| Intake | private, run by the maintainer | Takes approved cells, blends their seams, cuts map tiles and publishes them. |
| Map | karta.opensverige.se | Shows the live tiles and the build page. |

Contributors never get write access to a repo, a bucket or a key. The only thing that leaves a contributor's machine is the redrawn PNG and its manifest.

## A cell's life

`fri` → `last` (locked, 48 hours) → `vantar` (uploaded, waiting for review) → `godkand` (approved) → `live` (on the map).

- A new contributor holds one lock at a time and uploads at most 20 cells a day.
- A person reviews every upload of a contributor who is not trusted, and at most five of theirs wait at once.
- Trust is given by the maintainer by hand (`POST /trust`), never by a count: up to eight locks, and uploads go straight to `godkand`.
- A rejected cell goes back to `fri` with a comment. Its upload is kept for 30 days, then purged.
- A live cell the admin takes down is `removed` until the intake has blanked its tiles. Nobody can claim it before that.

## The render

Each sheet has one source, set in `SHEET_KINDS` in `cli/rutor/renderer/sources.py`, so neighbouring cells never mix sources.

### Open data (`goteborg-norr`)

`rutor render` builds the 1536 px source window from open data only:

- **Orthophoto** from a WMS, fetched as shared 1024 px tiles on the sheet's 0.25 m grid, so every cell cuts its photo from the same pixels. Göteborgs Stad (CC0) today. Lidingö and Lantmäteriet are open issues.
- **Ground heights** from the city's terrain model, Göteborgs Stad Höjdmodell 2022 (CC0, laser scanned, 0.5 m, RH 2000). Each 1 km tile is read out of the city's 10 km zip with HTTP range requests, about 7 MB, and cached for the whole sheet. Lantmäteriet's national model needs a login, so it is not used.
- **OpenStreetMap** from Overpass, pinned to one date per sheet, for buildings, roads, water and trees. Single trees come from their own query: today's trees whose current version is older than the pin, because Overpass runs out of memory on them at a pinned date.
- Every photo pixel is placed by the live map's own camera (`cli/rutor/frame.py`): its latitude and longitude, at the ground height under it above the ellipsoid (RH 2000 plus a geoid height of 36.7 m), land on one map pixel. Buildings and trees rise from that ground by their own height. Facades, windows and tree crowns come from rules keyed to world position, never to the cell.

The rule that matters: **two neighbouring cells draw identical pixels in their 512 px overlap, on any machine.** `test_neighbour_cells_agree_in_their_overlap` pins it with zero differing pixels, on hilly ground steep enough to leave gaps between photo rows. A change to the renderer must keep that test green. What keeps it: positions are computed in map pixels from the photo grid's global indices, floored, then moved by whole pixels; trigonometry runs in Python's `math` per row and per column, the per-pixel work is only `+`, `-` and `*`; ground heights are read on a lattice every 2 m counted from the origin; and the canvas reaches 32 px past the window, so gaps are filled from the same neighbours in both cells.

### Google (`stockholm-norr`)

`cli/google-render/render.mjs` opens Cesium in headless Chrome, loads Google Photorealistic 3D Tiles (Cesium ion asset 2275207) with the contributor's own token, and captures one orthographic 1536 px frame.

- The camera is the one that drew the live map: one orthographic camera per sheet, fixed in the earth-centred frame at the sheet origin, looking along the heading and 45 degrees down, moved sideways in its picture plane to each cell. A point lands on the map pixel the site's own projection gives it, terrain included, because Google's tiles carry their heights. `test_the_pose_is_the_one_that_drew_the_live_map` pins it to numbers from that renderer (`grid-config.ts`).
- The token goes to node in its environment and to the page in memory, never on a command line, in a file or in a logged URL.
- Streamed tiles are not pixel-identical between machines, so the overlap test does not apply. The intake blends the seams.
- Google's terms bind the token. `rutor render` prints that on every Google cell, and the manifest names the source, so those cells can be found and taken down if Google asks.
- What the live map shows of this sheet, 5 036 cells, is `live` from the start. The free cells are the built-up ground around it that the map has not drawn: 2 378 cells. The sheet is extended, not redrawn.

## The redraw

`rutor redraw` runs the pinned diffusion pipeline over the window in four 1024 px tiles, then crops the centre 1024 px. One cell takes about two minutes on a 12 GB card. Each phase puts only its own parts on the GPU, so the peak is about 11 GB.

Redraws are not pixel-identical across GPUs and library versions. That is fine: the intake blends neighbours over their overlap, so no seam shows.

## The quality check

`rutor check` compares the redraw with its source. It catches a blank, too dark, posterised, shifted or unchanged redraw, and a redraw of the wrong cell. It was calibrated on 100 redraws. A cell that fails exits with code 5 and cannot be uploaded. The service also refuses a manifest that does not say the check passed.

## Uploads and the inbox

- An upload is one PUT with the PNG as the body and the manifest in a header.
- The service checks the lock, its expiry, the size, the manifest, every PNG chunk and the sha256 before it writes anything.
- It takes a plain picture only: 8-bit RGB, not interlaced, with no part in the file but the picture, text, a time, the print size and the sRGB mark. A browser plays an animation, lets what is behind show through alpha and applies a gamma or a colour profile; a program that reads only the pixels does none of that. The reviewer looks in a browser and the intake reads pixels, so any other file could be approved as one picture and published as another. `rutor redraw` writes a plain one. The answer is 415 `not_plain`, and the lock stays.
- Each upload is stored under its own key (the cell, the start of its sha256 and a random part). A request that loses its lock can only delete its own objects, never someone else's.

## The review page

`rutor review` is how the maintainer looks at a cell before it goes on the map. It is a small server in the CLI (`cli/rutor/review.py`, standard library only) and a page of plain HTML, CSS and JavaScript (`cli/rutor/review_page/`).

What it shows: a view three cells wide. The nine cells of today's map are nine tiles: a cell is 1024 px of the full picture and a tile is 512 px, so on the level below full size one tile is exactly one cell and has the cell's own column and row. The upload is laid over the middle. A 1536 px upload covers the middle half of the view, and it fades into its neighbours over the 256 px that lie outside its own cell. That fade is a preview of the blending the intake does, not the intake's own result.

The map's tiles never change under their address, so a sheet that has had cells put into it gets an edition, and its tiles are asked for as `<sheet>@<edition>_files/...`. The page reads the live edition from the tile service's `karta-utgava.json`, at most once a minute, and shows the map in it. A sheet without an edition is shown as the base.

The page can approve and reject, so it answers one browser tab and nothing else:

- The server listens on 127.0.0.1, on a free port that is new at every start. Every address starts with a random key made at start; without it every request gets 404.
- The Host header must be the printed address, `127.0.0.1` and the port. A page on another site that points its own name at 127.0.0.1 is refused, and so is the name `localhost`, which can also mean `::1` where this server does not listen.
- A POST must be JSON and must not carry another page's Origin.
- The page is served with a policy that lets it load only its own files. It has no inline script or style, and it shows what the service says as text.
- The login stays in the server process. It is sent to the service and nowhere else: not to the page, and not to the tile service.
- Requests are not logged, because the address holds the key. A fault inside the server prints its kind and place, never its message.

Two rules are about the reviewer seeing the right thing:

- An upload is passed to the page only when it is a plain picture: 8-bit RGB, not interlaced, and no part in the file but the picture, text, a time, the print size and the sRGB mark. A browser plays an animation, lets the map show through alpha and applies a gamma or a colour profile; a program that reads only the pixels does none of that. The service refuses such files at upload since 2026-11-23. The page keeps its own check, for uploads from before that day and in case the two rules ever drift apart.
- Approve is off until the upload of the cell on screen has loaded, and while the queue is read after a decision. A late click cannot approve the next cell unseen.

The manifest is written by the builder's machine. The page shows it under that heading and not as something checked.

The tile service answers only pages it knows, which it tells by the Referer header. Since 2026-11-23 it knows pages on `localhost` and `127.0.0.1`, so the review server says what it is: `http://127.0.0.1/`, with no port and no path, so the key never travels. It fetches nine tiles a cell and keeps them while it runs, since a tile never changes under its address. When the intake starts publishing revisions of a map, this page has to read which revision is live.

## What is open and what is not

Open, in this repo: the CLI, the renderer, the redraw call, the quality check and the service. Not open: the intake (blending, tile cutting, publishing to the live bucket) and the maintainer's machines.
