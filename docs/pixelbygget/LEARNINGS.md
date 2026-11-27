# Pixelbygget learnings

What we tried, what we measured and what we changed. Newest first. One entry per finding, with the number that decided it. Images, renders and raw logs stay outside the repository; this file keeps the conclusions.

## 2026-11-27

### Ubuntu 24.04 keeps the renderer's Chrome from its sandbox
The first Google render on the Strix Halo machine below, started by hand from a terminal, failed at once with `the Google render failed: TROUBLESHOOTING: https://pptr.dev/troubleshooting`.
- **The cause was in a line rutor did not show.** Puppeteer's launch error has many lines, and rutor printed the last. Further up Chrome said `No usable sandbox!`. Ubuntu since 23.10 sets `kernel.apparmor_restrict_unprivileged_userns = 1`: a program makes user namespaces only when an AppArmor profile allows it, and the Chrome that `npm ci` fetches into `~/.cache/puppeteer` has none. `rutor render` now says what it means and points to the README.
- **It worked from some shells and not others.** Started by a program that runs under a profile of its own that allows user namespaces, Chrome built its sandbox; started from the desktop's terminal, which is `unconfined`, it did not. `systemd-run --user` starts a process the way that terminal does, and reproduced the failure without a person.
- **The fix is a profile for that one binary,** as Chromium's `apparmor-userns-restrictions.md` describes, shaped like Ubuntu's own `/etc/apparmor.d/chrome`. After it was loaded Chrome started from the user session, and `render.mjs` reached Cesium ion (a made-up token was refused with 401, as it should be). The README has the commands; they need sudo, so AGENTS.md hands them to the person. The path is in the home folder, so any program that runs as that user can put a file there and get user namespaces too. Chromium's page says so; the README says it as well.
- **The renderer draws on the GPU here.** With the renderer's own flags (`--use-angle=gl-egl`) Chrome reported `ANGLE (AMD, Radeon 8060S Graphics (radeonsi strix_halo ACO), OpenGL ES 3.2)`. Without them, SwiftShader.
- **npm 11.16 warns that Puppeteer's install script is not covered by `allowScripts`, and runs it anyway.** Into an empty `PUPPETEER_CACHE_DIR`, `npm ci` fetched Chrome 156.0.8078.47, the version Puppeteer 25.16.0 names. The warning needs nothing.
- **Not tried here:** a whole Google render with a real token. That is the person's token, and it was theirs to run.

### An AMD APU draws a cell through ROCm
On an AMD Ryzen AI Max+ 395 (Strix Halo, Radeon 8060S, `gfx1151`, 64 GB, Ubuntu with Linux 7.0, Python 3.14), from main at 09d2e67. `rutor preflight --machine` said `check: gpu missing` there before this change, and the README said AMD does not work.
- **The redraw ran unchanged.** On the ROCm build PyTorch answers to `cuda`, so `pipe.to("cuda")`, the phase placement and `torch.cuda.empty_cache` need no edit. What changed is the machine check, the messages and the docs.
- **PyTorch 2.14.1+rocm7.2 lists `gfx1151` in `torch.cuda.get_arch_list()`.** It saw 28.7 GiB, did 17 TFLOPS in a bf16 matmul, and ran flash and memory-efficient attention. No ROCm was installed on the system: the wheel carries its libraries, and the kernel's `amdgpu` driver was enough.
- **bitsandbytes 0.50.2 from PyPI has `libbitsandbytes_rocm72.so` in it and loaded it.** NF4 on the GPU matched a dequantised reference within 0.3 %. It runs `rocminfo` at import to learn the architecture, prints two error lines when that is missing, and uses the answer nowhere.
- **The card is found in the kernel's KFD table,** `/sys/class/kfd/kfd/topology/nodes`, which ROCm itself reads. It needs no PyTorch, so `--machine` stays light. On this APU the GPU node's memory bank is the GTT size, 30 867 931 136 bytes, the same 28.7 GiB PyTorch sees. The 4 GB the firmware sets aside as VRAM is not the number that counts.
- **The table is readable by every user, `/dev/kfd` is not.** Here the desktop seat gave the account an ACL on it, without the `render` group. An SSH login would have seen the card and no GPU in PyTorch, 15 GB later. `--machine` now asks.
- **A whole cell through a local service** (`service/scripts/local.sh`): `goteborg-norr 63_51`, claimed, rendered from open data, redrawn, `check: ok`, uploaded, `vantar`. The manifest says `"gpu": "Radeon 8060S Graphics"`, and the service took it.
- **6 min 30 s for the redraw with the model cached:** about 25 s to start and load, 17 s to encode four windows, 42 s for each of the eight steps, about 10 s to decode. Peak resident memory 14.5 GB. The first run, which fetched the weights at about 125 MB/s, took 15 min, and its encode 2 min 40 s: the first use of each kernel. An RTX 5070 takes about two minutes.
- **Two runs of the same cell gave the same PNG,** sha256 `6cb602ab…`, on this machine. Against an NVIDIA run of the same seed it has not been compared.
- **A ROCm build can see a card it has no kernels for.** `is_available()` still says yes, and the first sign would be `invalid device function` mid-redraw, after the lock and the downloads. `check: torch` now holds `gcnArchName` (up to the colon) against the build's own `torch.cuda.get_arch_list()`. The rocm7.2 wheel lists gfx900, 906, 908, 90a, 942, 950, 1030, 1100–1103, 1150, 1151, 1200 and 1201: the 780M (gfx1103) is in it, the RX 6700 XT (gfx1031, 12 GB) is not. Raised in review.
- **Not tried:** a discrete AMD card, any other `gfx` target, Windows (AMD is asked only on Linux), a machine with 32 GB or less, where an APU's GPU memory and the redraw's own share one pool, and an upload to the real service.

### A third run: the same place asked for again
From a fresh clone of main at 4396626 on the test PC, with "build a cell in Lund" once more, now that Lund's cell waits for review.
- **A place name gives the same cell every time.** `rutor claim --plats Lund` answered `place: se-n5572-e1312 47_30` and was refused `taken`. Nothing was locked. So the second person to ask for a town meets a refusal, not a cell.
- **"Someone else holds that cell" was said about the asker's own cell.** The service says one sentence for locked, waiting, approved and live. The grid is public and has the state and the login, so `rutor claim` now reads it and says `se-n5572-e1312 47_30 is built and waits for review, from <login>`, and names `rutor claim <sheet> --next`. The service's sentence is reworded too, which shows after the next deploy.
- **AGENTS.md had no line for a place whose cell is taken.** "If your human named it, tell them and ask" was written for a named cell. For a named place the agent now takes a free cell beside it and says which.
- **`--next` on a sheet with one cell took the one up and to the left,** 46_29 beside 47_30: the station beside the cathedral. Built, checked and uploaded, 43 lines of output.
- **A stored login had stopped working on two machines.** The Mac's from 2026-11-23 and the test PC's from before were refused `bad_token`. GitHub's rule for an OAuth app is ten tokens for one person, app and scope: an eleventh revokes an older one, first the oldest that was never used (docs.github.com, "Token expiration and revocation"). The test logins of one week were more than ten. AGENTS.md says so. Whether that is what ended these two was not shown.
- **The login is per user on the machine, not per clone:** a fresh clone was logged in already.
- **Not run here:** the service's tests, after the one changed sentence. No test names it.

### The same test again, from main with the fixes in
An agent built from a fresh clone of main at 695d98c on the test PC, twice: a named cell beside the flat one, and "build a cell in Lund".
- **The flat cell was stopped and given back.** `se-n5557-e1412 33_4`: claimed, `flat: 0%` at the render, released as the line said, `locks: 0`. Nothing was uploaded.
- **A place name was enough.** `rutor claim --plats Lund` said `found: Lund, Lunds kommun, Skåne län, Sverige` and took `se-n5572-e1312 47_30`. It passed the flat check and came out drawn, the cathedral in it. The human answered nothing between the sentence and the upload: the login from the day before still held, in a new clone.
- **A cell that was given back is counted.** The sheet said `0/2 taken, 2912 more can be claimed` with one cell taken and one released. The service's `total` is the cells with a row. The lines now say `claimed so far, of 2914 cells`, and the note on `rutor grid` names the cells listed as `fri`.
- **torch has a second line that reads as a fault,** `Redirects are currently not supported in Windows or MacOs`, from `torch.distributed.elastic.multiprocessing.redirects`. Quieted the same way as the first.
- **Through a pipe the redraw printed hundreds of lines of bars for loading the model.** They are diffusers' and transformers' own; both have `disable_progress_bar`. They are off when standard error is not a terminal. The redraw of the same cell then printed 39 lines: the pin line, the windows, `redrawn:` and `check: ok`. The bars of the first download are huggingface_hub's and were left.
- **Not tried, still:** a machine without Node, a Cesium token and the weights.

## 2026-11-26

### A flat cell is measured and stopped at the render
The cell below came out as a photo on flat ground. The same day the renderer learned to see that. Measured on the test PC on nine cells: two near that town, two in Helsingborg, five in Stockholm.
- **Google's tile tree says nothing.** Every cell, flat or not, selected tiles down to depth 25 with a smallest geometric error of 2.006. Where Google has no buildings the tiles are as deep, with a photo on smooth ground.
- **The height of the surface says it.** On 24 x 24 points of the drawn frame, the share that lies more than 3 m above the lowest point within two steps (32 m on the screen): 38, 59, 64, 77 and 80 % where buildings and trees are shapes; 0, 0, 0 and 3 % for the flat town, a field beside it, open sea at Helsingborg and open water in Stockholm. The limit is 10 %.
- **Open water measures as the flat town does.** So the render does not decide: on a sheet of the tiling it stops with `flat:` and exit 5, keeps the picture, and `--flat-ok` draws the cell after a person has looked. A hand-made sheet is not asked, since its cells were picked from a drawn map.
- **`Cesium3DTileset.pick` gave heights 15 km under the ground in Helsingborg.** All 576 rays there hit one plane, with a selected tile of geometric error 262 979 in the set. In the other eight cells the same call was right. `scene.pickPosition`, which reads the depth of the frame, was right in all nine and took 0.17 s for the 576 points; `scene.sampleHeight` agreed and took 1.2 s.
- **A steep hill counts as rising.** More than 3 m in 32 m is about 9 % of slope. That errs towards drawing the cell.
- **The headless Chrome draws on the PC's built-in AMD card,** not the NVIDIA one: the `gl` line says so. The render still takes under 20 seconds.
- **Tried for real:** the flat cell is refused with `flat: 0%`, and Helsingborg 47_16 on a sheet of the tiling renders as before.
- **Not solved:** nothing says it before the lock. The lock is held for the 20 seconds the render takes and then given back by hand. And the flat cell that was uploaded before this still waits for review.

### An agent given one sentence built a cell from a fresh clone
On 2026-11-26 an agent got only this: clone the repository, read AGENTS.md, build `se-n5557-e1412 32_4`. It ran on the test PC (RTX 5070, 12 GB) in a new folder, from main at 1dd86b0, and followed AGENTS.md step by step. The cell was uploaded and waits for review.
- **The procedure held.** No step needed a guess or a way around. About ten minutes from clone to upload: the render under 20 seconds, the redraw 2.5 minutes, the rest installs.
- **The picture came out flat.** The source render looks like an aerial photo seen straight down, with no height on the houses, and the redraw is a smoothed, greener photo more than pixel art. `check: ok` let it through. This fits the guess in the entry of 2026-11-25 that a place without Google 3D buildings gives flat ground with a photo on it. Whether Google has no 3D buildings there was not looked up.
- **The check cannot tell a flat cell from a drawn one.** Nothing between the claim and the review says so either. The reviewer is the only stop.
- **A sheet nobody has drawn on looks closed before the claim.** `rutor grid <sheet>` prints nothing and exits 0, `--free` says `free: none`, and `rutor cities` does not list the sheet. The claim of the named cell then works. A careful agent could stop at `free: none` and tell its human the cell cannot be taken.
- **The one sentence still means three answers from the human:** the choice about Google's tiles, the terms, and the code at GitHub. That is as intended, but a text that hands people the sentence should say so.
- **An expired login says what to do.** The PC had a login from days before: `refused: bad_token ... Log in again: rutor login`. Nothing else was needed.
- **Two printed lines read as faults and are not:** `pin: unset diffusers_version` at the start of the redraw, and torch's `triton not found` warning in the preflight and the redraw.
- **Small things in AGENTS.md.** Step 5 says `rutor preflight --sheet stockholm-norr` also for another sheet; the sheet's own id works there. `Activate.ps1` does nothing for an agent whose every command is a new shell; it called `.venv\Scripts\rutor` by path.
- **Fixed in the same branch:** `rutor cities` and `rutor grid` say that every other cell of such a sheet can be claimed; the pin line says the redraw goes on; torch's triton warning is silenced by its logger's level, set before torch is imported; AGENTS.md names the sheet in the preflight, says how to call the programs without activating, and that a named cell takes three answers.
- **Not tried:** a machine without Node, without a Cesium token and without the weights. All three were already on the PC, so the 16 GB of downloads, the Cesium account and `setx` with a restart of the program were not walked through.

## 2026-11-25

### Draw anywhere: the service and the CLI for a cell in any place
Built on 2026-11-25 from the plan "Rita var du vill", parts A and B: the tiling, the place lookup, the first claim that makes a sheet, the source on the sheet's row, `rutor claim --at`, and the app page's code field. Nothing is deployed and no migration has run. Publishing, the tile Worker and the build page are other parts.
- **A sheet of the tiling is 47 cells high everywhere and 41 to 61 wide.** 0.15 degrees of latitude is 16.7 km, and a cell covers 362 m north to south (256 m seen 45 degrees down), so 46.1 rows, rounded up. 0.25 degrees of longitude is 15.6 km at 56 degrees north and 10.4 km at 68, so 61 and 41 cells of 256 m.
- **The plan's example id cannot come out of the rule.** `se-n5607-e1262` has no 0.15 degree step behind it. Boxes lie on whole steps from 0 and the id is the origin's hundredths cut, so Helsingborg is `se-n5602-e1262`: a real id is 7 more than a multiple of 15, and 12 more than a multiple of 25. Nothing outside the service should read the numbers.
- **The ground height moves a point up the picture by height / 1.41.** The sheet's origin is on the ellipsoid and the camera looks 45 degrees down. In a town at 300 m plus 30 m of geoid that is 233 picture metres, 0.9 of a cell: the plan's flat formula would have answered the neighbour to the south. So the place lookup uses the renderer's own frame (`cli/rutor/frame.py`, checked against it on 4 points to the cell), and a sheet of the tiling stores `y_meters` as rows x 128 + height / 1.41. The box then stays in the middle of the picture whatever the height, and the grid does not move on the ground when the row is made: the same Helsingborg point is cell 47_16 before the row exists (height 0) and after (height 68).
- **A sheet with no row answers height 0 with the matching picture top, not a guess.** The two describe the same grid on the ground. The height is asked only when the row is made.
- **The limits are asked before anything is written.** A builder at the lock limit who claims on a new sheet makes no sheet row and no elevation request. The plan had the rows inserted first.
- **A claim by id could have gridded Stockholm again.** The place lookup sends a point inside a hand-made sheet to that sheet, but `rutor claim se-n5932-e1812 30_20` asks no place. A first claim on a cell whose middle lies inside a hand-made sheet's picture is refused 409 `other_sheet`. This also means the `ute` cells of a hand-made sheet cannot be drawn through the tiling.
- **Open-Meteo answers `{"elevation": [38.0]}`** for `/v1/elevation?latitude=..&longitude=..`, from the Copernicus DEM at 90 m, and asks to be named together with Copernicus where the data is used. Read from their documentation; the API itself was never called. The service takes a whole number between -100 and 2500 m and adds 30.
- **`/v1/place` is not kept in the isolate's memory.** That cache holds 64 answers keyed by path; a point per key would push the grids out. It carries the 60 second header.
- **The source of a sheet was decided in three places in the CLI and two in the app.** All go through `sources.kind_of(sheet)` now: the row's `kind`, else the two lines for the two known sheets, else a refusal before any claim. `rutor claim` reads the sheet's row before every claim, one grid read more than before.
- **`--next` has nothing to list on a sheet of the tiling:** a free cell there has no row. It takes a free cell beside one that has a row, beside a live one first, and refuses with "name a cell, or use --at" on a sheet nobody has drawn on.
- **The app page keeps the sheet of a pasted code until "Ta nästa lediga" is pressed.** The page looks for an open sheet every 60 seconds, and would have swapped the chosen sheet for Stockholm under a person about to press Rita. Seen in a browser against the stand-in service at 800 and 375 px wide: a bad code is refused under the field, a good one goes to step 7, and the small map shows the chosen cell with its free neighbours.
- **A code is taken apart by shape before anything is asked, and never said back.** 20 malformed codes and 4 with markup each answer 400 `bad_code` with no request to the service.
- **A fresh reader of the whole branch found that the renderer could be handed the sheet from before its row existed.** `rutor claim` reads the grid, the claim makes the row and moves the picture's top by height / 1.41, and `rutor render` reads the grid again seconds later. The service kept the first answer in memory for 60 seconds, so the camera would have framed the cell 48 m off at Helsingborg's 68 m: 192 px. The tests ran with that memory off and could not see it. A sheet with no row is now answered with `exists: false` and `no-store`, is never kept, and `rutor render` refuses it. One test runs with the memory on.
- **The same answer, kept, let anyone push the real grids out of memory:** about 4900 ids answered 200 into 64 places. Fixed by the same change.
- **A claim on a sheet of the tiling says the source it will draw from, or is refused 426 `update_needed`.** An installed CLI from before would otherwise lock a cell on a new sheet and fail at the render with the lock left for up to 48 hours. It sends no `kind`, so the service can tell, with no version number involved. Nothing is locked and no row is made.
- **`outside` means two things.** The place lookup says it for a point outside Sweden, the claim for a cell a hand-made sheet does not offer. AGENTS.md said only the first. An agent reads the sentence after the word now.
- **The migration has to be applied before the code.** Every claim selects `made` and `kind`, so the new code on the old table answers 500 on every sheet, Stockholm too. The README says so.
- **Not solved: a builder can make sheet rows without drawing.** Claim, release, claim on the next sheet: one row and one elevation request each, at most 60 a minute. There are about 4900 boxes. The limits were to stay as they are.
- **Not solved: the page cannot say before the lock that a place has no 3D buildings.** The proposal asked for that. Nothing here can tell without rendering. Where Google has no 3D buildings its tiles are probably flat ground with a photo on it, not empty, so the render would not fail either: not tried.
- **The box around Sweden holds Copenhagen and parts of Norway and Finland.** 55.2 to 69.1 north, 10.9 to 24.2 east. A sheet is in when the middle of its box is.
- **Left as it is, from the same reading:** a point on the far side of the earth can resolve to a hand-made sheet (the frame has no near-side test); `rutor cities` shows a new sheet as `1/1` and `rutor grid --free` as none, though about 2860 cells of it are free; the app's "next free" would take a released cell on a new sheet once Stockholm is full; the place lookup says `fri` for a few cells the claim then refuses; `--at` takes digits that are not ASCII and then fails; neighbouring sheets overlap by about 320 m north to south (47 rows cover 17.0 km, the box is 16.7) and up to a column east to west, so edge cells of two sheets show the same ground; whether Open-Meteo answers a Worker at all is not known.
- **Not tried:** anything against the deployed service; the migration on real rows (its UPDATE was run on test rows); a real `rutor claim --at`; a render of a sheet of the tiling; the elevation API; the page on Windows.

### Three reviewers read the whole contribution path
Three independent readers went through the repository on 2026-11-25: one as a newcomer on each of the three ways in (the app, an agent, the terminal), one hunting bugs in the CLI, the app page and the installer, one in the service. They found 52 things. What was fixed is in the same change as this entry.
- **A lock could be left with nothing running, in four ways.** An upload whose answer was lost; an app that was ended in the middle of a cell; a claim that crashed (exit 1 was read as "certainly no lock"); a claim and its release both cut off by the same dead network. A new builder may hold one cell, so each of them locked the person out for up to 48 hours with nothing on the page to press.
- **A release the service refuses with `already_uploaded` is a cell that went in.** The page now says done. `not_yours` and `no_cell` mean there is no lock of ours, and are not asked about again.
- **"Try again" was said for things trying again cannot fix.** The daily limit of 20, a pipeline the sheet no longer takes, a used-up Cesium quota and a token refused in the middle of a render each have a sentence now. A person who is told to try again tries again.
- **An old pipeline was found out at upload, after the whole drawing.** The page refuses Rita at once.
- **"Lediga rutor saknas på kartan" reads as "no cell is free".** It meant "the map lacks them". It stood above a map of free cells.
- **The terms were in English only on a Swedish page.** Four lines in Swedish stand above the ten, the choice about Google's tiles among them. The English text is the one that applies.
- **A live cell the admin took down was shown as free and could not be claimed.** `pick_next` is the same for everyone, so every `--next` picked that cell and was refused until the tiles were blanked. The grid shows it as locked.
- **429 `rate_limited` and every 5xx exited 2, "refused".** AGENTS.md tells an agent to report a refusal and to retry exit 4. They exit 4 now. 429 `daily_limit` is the person's own limit and stays a refusal.
- **The secret without the app's id let any GitHub token in.** `secret && clientId` fell back to "any token" when the id was empty. It answers 503 now.
- **pip and npm ran under the person's own settings.** A `pip.ini` with `user = true`, `PIP_REQUIRE_VIRTUALENV`, or an `.npmrc` with `ignore-scripts` would each have ended the fetch somewhere the app never looks. Not seen on a machine: found by reading.
- **The machine check asked for 25 GB free after 9 of the 16 were already on the disk.** A fetch that was stopped halfway could not go on. An app install is asked for what is left plus the headroom.
- **A fourth reader went through the fixes and found two holes in them.** A lost upload answer followed by a release nobody answered still ended as "taken" at the next press: the release's `already_uploaded` was thrown away and the same cell claimed again. And a stored login was asked about with the command's 30 second timeout, with the pause counted from before the ask, so a slow service held every look at the page for a minute. The ask is 5 seconds now and the pause is counted from its end.
- **The page was told "failed" and then "done".** After a failed upload the lock is asked back while the job is still the upload, so the page hears one answer.
- **A stop during the upload waits for the upload.** Up to 45 seconds. The page says "Skickar in klart först", and the cell then waits for review. Ending the upload halfway left a cell the service might have and the page did not know about.
- **A lock from before is named, and is given back only for the cell that was refused.** `/v1/me` lists every lock, on any sheet, and one of them may be a cell the person is drawing in a terminal. Choosing another cell forgets it.
- **Only a failed write takes an uploaded picture away again.** The first fix also deleted it when the database call threw, which could leave a cell pointing at nothing if the change had gone through.
- **A test that could not fail was taken out:** `expireLocks` returned the same number before and after its fix in the one case the test made.
- **Not fixed, and why:** the builder never sees the comment on a rejected cell (needs a column); the same builder can hold a cell for ever by releasing and claiming again; one account can use up the database's daily writes; approve names a cell, not an upload. Each needs a decision or a migration.
- **Not tried:** any of the new sentences on a real machine; pip and npm with the new settings on Windows.

## 2026-11-24

### The installer, built and tried on GitHub
The Windows installer of the app page (Tech Embassy Pixel to a person, Pixelbygget in here) is built by `installer/build.ps1` and tried by the workflow `installer` on a Windows that has never seen it. The first run, 2026-11-24, was green in every step.
- **The installer is 35.8 MB.** It holds a Python of its own (CPython 3.11.18 from python-build-standalone), the `rutor` code and one wheel of diffusers. The build refuses anything over 50 MB, since this is what a person fetches before the page has said whether their machine can draw.
- **The page fetches 15.3 GB after that, and says 16.** The model stack is 2.86 GB (52 wheels), Node 36 MB, the renderer with its Chrome 0.92 GB and the model 11.47 GB. The model's folder on the test PC is 11.4 GB, which agrees with the Hugging Face API's number. README and AGENTS.md still say 20 GB.
- **The lock is the install that draws today.** `requirements.txt` pins 10 packages and leaves 41 to whatever is newest, so the lock was made from the `pip freeze` of the working install on the test PC, with `pip download` into a folder that was removed afterwards. Nothing was installed there.
- **pip takes the whole lock with hashes, and builds nothing.** `pip install --dry-run --require-hashes --only-binary :all:` on the runner ends with 53 packages, the diffusers wheel from the installer among them. It fetches torch's 2.75 GB wheel to check its hash, which takes 77 seconds of the run's 3 min 17 s.
- **No index has a wheel of the pinned diffusers commit.** The build makes one from the commit's zip, pinned by SHA-256, and ships it. Nothing is built on a person's machine.
- **A machine without a card is told no, and nothing is fetched to it.** On the runner the page answers `no_card`, and `api/install` answers 409 `not_ready`.
- **A profile named "Åsa Öberg" works.** Install, start, a second start, close, the shortcut, `rutor app --quit` and uninstall all ran under a folder with a space and Swedish letters. After the uninstall no process ran from the app's folder, and both the app's folder and the login folder were gone.
- **The oldest NVIDIA driver the page accepts is 528.33.** That is the minimum for CUDA 12.x on Windows in NVIDIA's CUDA 12.8 release notes. The same notes name 570.65 for 12.8 GA; a driver between the two is let through and has not been tried.
- **GitHub starts a hand run only for a workflow that `main` has.** `gh workflow run` on the branch answered 404, "not found on the default branch". Until the merge the workflow runs on each push to its branch.
- **The Python archive has 830 compiled files of its own.** The build adds none: the count in the installer is the count in the archive.
- **The runner's image already had Inno Setup 6.7.1.** The Chocolatey step changed nothing.
- **`actions/checkout@v4` and `actions/upload-artifact@v4` are warned about.** They target Node 20, which the runners force to Node 24. The repository's other workflows use the same versions; they move together, not here.
- **Not tried:** a machine with a card; the fetch for real (pip against the lock, `npm ci` through the app's own Node, the model); whether a black window ever shows; what Windows says about an unsigned installer; a newer installer run over an older install. `build.ps1` and the Inno Setup script have run on the runner only.

### A cell built from a page
`rutor app` serves a page on the builder's own machine that runs claim, render, redraw and upload as child processes. The first cell built from it was stockholm-norr 82_24, on the test PC (Windows, RTX 5070, 32 GB) on 2026-11-24.
- **Rita to waiting for review took about 2 min 45 s.** The render took 13 seconds, the redraw about 2 min 20 s, the claim and the upload the rest. No command was typed after `rutor app`.
- **The redraw holds 12.6 GiB of ordinary memory at its peak.** That is the redraw child's peak working set. It had asked the system for 22.8 GiB. Free memory on the machine fell from 17.2 GB to 4.6 GB, and the card peaked at 11.2 GB. The render's Chrome held about 1.2 GB for its 13 seconds.
- **So the page warns below 17 GB and does not refuse.** 13 GB for the redraw and 4 for everything else. A machine with 16 GB has not been tried, so a no would be a guess.
- **The page's own process is 50 MB.** PyTorch and bitsandbytes are asked about in a child that ends. Imported in the page's process they would have stayed in memory beside the redraw for as long as the page was open, and a reinstalled PyTorch would not have been seen until a restart.
- **Windows has no time zone database.** `ZoneInfo("Europe/Stockholm")` fails there unless the tzdata package is installed, so the price check could never be loaded on that machine, and once the page loaded it at start every `rutor` command crashed. Found by the first test run on Windows. The price check now knows Swedish time by its rule (last Sunday of March to last Sunday of October, compared with the database every half hour of 2026 and 2027), and nothing about the price is loaded before a price is asked for.
- **Avbryt during the render left nothing behind.** `taskkill /T /F` on the render took Node and nine Chrome processes with it within six seconds. The card was back at 800 MiB, `rutor status` said `locks: 0`, and the cell (75_20) was free on the service.
- **A child that is stopped on Windows exits 1.** That is also `rutor`'s code for bad input. So the exit of a stopped claim says nothing about whether the service gave the lock, and a stopped claim is released once in case it did.
- **`rutor claim` reads the Cesium token itself,** before it locks. The page hands a pasted token to claim and render, and to no other command.
- **A token pasted into the page wins over `CESIUM_ION_TOKEN`.** Otherwise a variable that has gone stale could never be put right from the page.
- **A lock is remembered with its cell.** A lock that could not be given back is asked back by its own name before another cell is claimed. A new builder may hold one cell, so the next claim would be refused with nothing on the page to say why.
- **A hidden tab asks once a minute.** Chrome and Edge slow the timers of a tab that has been hidden for five minutes. The page counts as gone after 150 seconds without a request, not 30, or switching tab would stop the job.
- **An SSH session that ends does not end `rutor app` on Windows.** The process stayed after the connection was closed and had to be ended with `taskkill`. "Nothing left running" is checked in the process list, not assumed.
- **The progress line shows whatever the redraw prints,** a PyTorch warning and the loading bars included, as one line of text. It is honest and not pretty.
- **Three older tests assume a machine that cannot draw.** `test_preflight_lines`, `test_preflight_checks_render_libraries_not_chrome` and `test_redraw_writes_png_and_manifest` in `tests/test_render.py` expect no PyTorch and no card, and fail on the test PC. The other 373 pass there.
- **Not settled: a closed tab against a short job.** A cell like this one takes about 2 min 45 s and the page counts as gone after 2 min 30 s. A tab closed just after Rita therefore mostly lets the cell finish and go in. Stopping at once when the tab closes would also stop a job on a reload.
- **Not tried:** a machine with 16 GB; closing the tab for real during a job (the chain from timer to release is tested with a fake child); the first run's 20 GB download from the page; a person who has never used a terminal.

### The token is asked about before a cell is locked
The first real cell was locked for 48 hours with a token Cesium ion refused, because the check only looked for a value. `rutor preflight --sheet` and `rutor claim` now ask Cesium ion.
- **One request tells a good token from a bad one.** `GET https://api.cesium.com/v1/assets/2275207/endpoint` with the token as a Bearer header answers 200 for a working token and 401 `INVALID_TOKEN` for a made-up one. Both were tried against Cesium ion itself, the working one on the builder's own machine with only the status printed.
- **The token goes in a header, never in the address,** and a redirect is not followed. Cesium's own code is passed on only when it is one plain word; any other text from outside is dropped.
- **A token pasted twice is told apart by its dots.** A token has two. On a 401 with any other number the message says so. The token itself is never part of a message.
- **Cesium ion being down is not a bad token.** It ends as a network failure (exit 4) with nothing locked, where a refused token is exit 3. Preflight prints either as one line.
- **What the machine lacks is still named first,** without the network: a missing token, Node or renderer is reported before Cesium ion is asked.
- **A patch stopped out of order stays.** The fake service patches the card check while it runs. A test that patched the same check on top and stopped later left a mock behind, and four machine tests in another file failed. Do not patch what the fake service already patches.
- **Not tried:** what Cesium ion answers for a real token that lacks the asset or the scope. 403 and 404 are treated as that, with a message that says what to look at.

## 2026-11-23

### The first volunteer cell is on the map
stockholm-norr 74_20 went from approved to visible on karta.opensverige.se on 2026-11-23, as edition 1 of the sheet.
- **One cell is 51 uploads and 34 seconds.** The source picture, 46 tiles over 19 levels, the edge picture, the island picture, the sheet's list and the switch. Sixteen of the tiles are at full scale.
- **The site needed no change.** It read the new addresses out of karta.json, which the tile service cached for its five minutes before the cell showed.
- **Everything else answered as before.** A tile nobody changed came back byte for byte the base's, and the two other cities were not touched.
- **The build page and the review page showed the base picture,** not the edition, so the lit cell was dark there. Both now read the live edition from the tile service's own `karta-utgava.json`, not from the service's revision: the tile service is what knows which addresses exist.

### The base map is drawn further out than the city shows
Found while building the step that puts an approved cell into the map, tried on stockholm-norr 74_20 without publishing anything.
- **The edge picture hides real pictures.** The city fades out at its edge picture (8 pixels per cell, alpha), and its half-way line lies up to a cell and a half inside what the tiles hold. Of 40 cells with only a faint alpha, 25 were drawn all over and none was empty. Of 40 empty-alpha cells next to them, 20 held a picture too. Drawn ground ends on a 512 px grid, half a cell.
- **So a free cell is often half drawn already.** 74_20 was offered as missing, and its lower half is in the tiles, hidden. The rule for "missing" reads the edge picture, not the tiles.
- **A new cell would have been an island.** Opening the edge over the cell alone leaves a see-through band between the cell and the city, because the cells between them count as on the map and are never offered. The publishing step opens hidden, drawn ground between a new cell and the city, at most two cells wide, and never opens over empty ground.
- **The margin has two jobs.** Against drawn ground it fades in over its 256 px, as the review page shows. Over empty ground it is kept whole, so that the cell's edge has a picture to fade out over. Empty ground is told from drawn by being flat and dark; a block next to a drawn one needs 16 px of its rim left out, since the compression spills over.
- **The city's edge is a blur of 3.1 edge pixels,** 400 px on the map, measured on Stockholm. A new cell shows whole to its own edge and is gone where its margin ends.
- **Tiles are cached for a year,** so a changed tile cannot keep its address. A sheet gets an edition number in the address, changed tiles lie beside the base, and the base is never written. Taking every cell away gives back the map exactly.
- **An address with an edition has to give the same picture every time.** The first build kept one copy of each changed tile and let every edition read it. An independent review showed what that meant from the second edition on: a run that stopped half way had already changed what visitors saw, and a cell withdrawn after that stayed in caches for a year. Changed tiles are now written once per edition, and a list per sheet says which edition each tile belongs to. Nothing a run uploads can be seen before the list is written.
- **The same review found a cell could slip in after a rejection.** A cell rejected while a run was under way was published and never taken out, because a rejected cell is free again and stands in no queue. The run now asks once more right before the switch, and takes out any cell on the map that the service no longer counts as lit or approved.
- **A picture comparison that compared nothing.** `ImageChops.difference(a, b).getbbox()` on RGBA pictures looks at alpha only, so the tests for "same picture" passed whatever the colours were. A deliberate fault in the paste order showed it. They compare bytes now.

### The first cell went the whole way, from a fresh clone to the review page
A Windows PC with an RTX 5070 (12 GB) was driven over SSH, the way an agent would: a fresh clone of `main`, the light install, `rutor preflight --machine`, the model stack, `npm ci`, the login by device code, `rutor claim stockholm-norr --next`, render, redraw, upload. The cell (74_20) came up on the review page among its neighbours, with the check at `ok`. Nothing in the chain had to be worked around.
- **A wrong Cesium token locked the cell before anyone knew.** The token had been pasted twice into the variable: 583 characters and four dots where a token has two. `rutor preflight --sheet` and `rutor claim` said `cesium token ok`, because they only look for a value. Cesium ion answered 401 at `rutor render`, with the cell already held for 48 hours. The check should ask Cesium ion before a lock is taken.
- **A variable set with `setx` reaches a new SSH session at once.** No restart of the SSH service was needed. A program that was already open still has to be reopened, as the instructions say.
- **The times were short.** The redraw was done within three minutes of the render, with the model weights already on the disk. The render was not timed; it ended well inside the 15 minutes the instructions allow.
- **The water came out darker than the map's.** The new cell's sea is navy where the three drawn neighbours have a lighter blue, so the cell's edge shows in open water. The land joins without a visible seam. The reviewer decides; nothing was changed.

### Two fresh agents followed the instructions, and the login never reached them
After the change to "draw what is missing", two agents that knew nothing of the project were sent in: one ran the instructions for real on a Mac, one traced a Windows PC with a 12 GB card on paper against the code.
- **`rutor login` printed its code into a buffer.** Through a pipe, which is how an agent runs a command, Python holds output back until the program ends. The login prints a code and then waits for a person to approve it, so the code was never shown and the wait never ended. `rutor` now writes every line when it is printed, as UTF-8, and the login says that it is waiting.
- **The order asked for the account last.** Today's free cells need Node and the human's own Cesium ion account, and the instructions raised that after the terms, the 20 GB install and the GitHub login. It now comes right after the machine check.
- **A cell could be locked that the machine could not render.** Without Node, `npm ci` or the token, `rutor render` failed with the cell already held for 48 hours. `rutor claim` now refuses on a Google sheet until all three are in place, and `rutor preflight --sheet` shows them ahead.
- **No step installed Node or ran `npm ci`,** and nothing said how the human sets the token on Windows so that the agent's terminal sees it.
- **Stale lines sent agents the old way:** Göteborg offered as the open-data alternative, Stockholm described as "redrawn, not extended", "their first ten cells are reviewed", 20 GB of disk against the code's 25, and `rutor status` named as the place to see an uploaded cell, which it does not list.

### Volunteers draw what is missing, not what is there
Both sheets offered exactly the cells the live map already showed: 3 071 on goteborg-norr and 5 036 on stockholm-norr, every one of them drawn. A volunteer's cell would have replaced a finished one, and a cell from open data is plainly lighter and thinner than the map around it. The maintainer saw it on the build page: the lit city was offered and the grey ground around it was not.
- **Changed on the service the same day.** What the map shows is `live`, credited to `opensverige`: 8 156 cells on goteborg-norr, 5 036 on stockholm-norr. Built-up ground with nothing drawn is `fri`: 2 378 cells on stockholm-norr. Nobody held a lock, so no one's work was touched.
- **Göteborg has 1 971 missing built-up cells and none of them can be drawn.** The city's orthophoto stops at the municipal border, and all of the missing ground lies beyond it. Checked cell by cell: 0 of them have photo over the whole cell. They stay `ute` until the renderer reads a photo that covers the country.
- **Lantmäteriet's orthophoto is that photo.** It is open, it covers Sweden, and its download service asks for a free account of the user's own (basic auth or OAuth), which is why the earlier try answered 401. It becomes a step in onboarding, like the Cesium account.
- **"Built-up" is the old rule:** an SCB 2023 urban area plus 500 m, a cell counted when at least 5 percent of it is inside. "Not drawn" is the map's own edge picture: no pixel of the cell on the island.
- **The service had no way to change which cells a sheet offers.** The change was made in the database, with every statement guarded by the state it expects. A route for it is still to be written.

### The service takes a plain picture only
The review page stopped PNGs with alpha, animation or a gamma, but a trusted builder's upload never passes the page. The upload now refuses them with 415 `not_plain` and keeps the lock.
- **Before the change a trusted builder's RGBA upload went straight to approved.** That was the failing test. After it, the same builder's plain upload still does.
- **Run for real:** a see-through copy of a redrawn cell was refused, and the plain original went through on the same lock.
- **The test helper was itself not plain.** `minimalPng` wrote RGBA and padded with an invented chunk, so every upload test used a file the redraw never writes. It writes RGB with a text chunk now.
- **Pillow does not write a gamma when asked.** `save(path, gamma=0.3)` gave a file with IHDR, IDAT and IEND only. A test file meant to carry a trick has to be checked for the trick before its result means anything.
- **A local `.dev.vars` reaches the tests.** With `ALLOWED_ORIGINS` set there for a local run, four CORS tests failed. They pass without the file, and CI has none.

### The review page: one map tile is one cell
The maintainer reviewed uploads by downloading PNGs and looking at them alone, which shows nothing about seams or placement. `rutor review` now shows the upload laid into today's map.
- **No image work is needed to place it.** A cell is 1024 px of the full map picture and a tile is 512 px, so on the level below full size the tile `col_row` is the cell `col_row`. Nine tiles and one PNG, placed with CSS, are the whole view. For goteborg-norr that level is 17.
- **A misplaced upload is obvious at once.** The same redrawn picture uploaded to its own cell (64_58) and to a cell where it does not belong (70_40): the second shows housing blocks on top of an industrial estate. A cell rendered from open data is also plainly lighter and thinner than the Google-drawn map around it, which is the thing a reviewer has to judge.
- **A local page that can approve is a target for every other page in the browser.** So it answers only under a random key in the address, checks the Host header, takes JSON only and loads nothing from outside. Each of those rules was switched off in turn in a copy of the code, and a test failed every time (14 of 14 after one test was sharpened: a body over the size limit that was also not JSON proved nothing about the limit).
- **A fresh reader found what the tests could not.** A security review of the finished page found no way past the key, the Host check or the page policy. It found three things about what the reviewer sees. The service accepts a PNG with alpha, animation or a gamma, which a browser and a pixel reader show differently: a see-through test upload passed the service and would have shown today's map through its hole. The approve button came back before the next cell's picture, so a second click could approve a cell nobody had seen. And the manifest's `check: ok` was shown in green under "what the computer has checked", though it is only what the uploader's machine says. All three are fixed in the page.
- **The page says who it is to the tile service.** The first version fetched tiles with the map site's address as Referer, because the tile service answered that site only. The maintainer chose the other way: the tile service now lets in `localhost` and `127.0.0.1`, and the review server sends `http://127.0.0.1/`. The cost is that any page someone runs on their own machine can show the tiles. Checked after the deploy: 200 for 127.0.0.1, localhost and the site, 403 for another site, for `127.0.0.1.evil.example` and for no Referer.
- **The first screenshot lied.** Four seconds after loading, the bottom row of tiles was still on its way and looked like missing map. The pictures were complete a moment later. Check `img.complete` before judging a view.

### Safe for anyone's agent: a security review and a newcomer's dry run
Two fresh readers went through the repository: one looking for ways to misuse a volunteer's machine, login or the service, one playing an agent told only "help out here" on a Mac. What they found and what changed:
- **Any GitHub token was a login.** The service asked GitHub "whose token is this", never "which app issued it", so a token from any other site's "Sign in with GitHub" worked here, the admin's too. The service now asks through the OAuth app's own check when `GITHUB_CLIENT_SECRET` is set. Until the maintainer sets that secret, the old behaviour stands.
- **The weights could be swapped.** The model and the LoRA followed a branch. Both are now pinned to a commit and loaded as safetensors only. Both repositories hold safetensors files only, and the cache on the test PC already had exactly these commits.
- **The Python packages floated.** `requirements.txt` now pins every package to the version the redraw runs with. sentencepiece and protobuf were listed but not installed on the working machine, so they are gone.
- **The redraw does not load without torchvision**, and the README installed only torch. Blocking the import on the test PC reproduces the ImportError a volunteer met.
- **`PIXELBYGGET_URL` sent the login anywhere.** It may now only point at this machine, and no redirect is followed with the login.
- **Uploads could park cells.** An upload frees the lock, so one new account could move 20 cells a day into the review queue. At most five uploads of a builder who is not trusted wait at once.
- **Trust came by itself after ten approvals** and then skipped review. It is now given by hand only.
- **The machine was checked after the consent.** `rutor preflight --machine` asks the driver (`nvidia-smi`) for the card and its memory and checks the disk, with nothing installed and no login. A Mac is told in the first two lines. `rutor claim` refuses on a machine that cannot build.
- **`rutor login --accept-terms` never showed the terms.** They are always printed, and they are ten lines in `TERMS.md`: unpaid, own cost, public login, the model's research license, and what Google's policy says about the Google sheet.
- Smaller: sheet ids are checked before they become folders, a terrain tile over 64 MB is not unpacked, a building height from the map data stops at 400 m, text from the service reaches the terminal as one printable line, and `rutor cities` and `rutor grid` work before login.
- Checked on a real machine: a fresh folder and virtualenv on the RTX 5070 PC, installed exactly as the README says (torch 2.11.0+cu128, torchvision 0.26.0+cu128), against a local service. `preflight --machine`, the full preflight, `claim --next`, render (170 s), redraw (133 s, `check: ok`), upload (`state vantar`) and a second claim all worked, and the card was back at 800 MiB afterwards.
- Not changed: the service still sees the GitHub login token on each request (it has no scopes and is not stored), headless Chrome still runs with `--disable-gpu-sandbox` on Linux for Google sheets, and a per-isolate grid cache can be made to rescan a sheet once a minute.

### A new city without a terrain model
- After PR 17 an open sheet needed a city terrain model, so a new city like Eslöv stopped with "no open terrain model". A sheet without one is now drawn on flat ground at its `ground_height`, and its manifest says `terrain flat`. A new city has no older map to line up with, so flat ground only costs hills their height.

### The open renderer is placed by the live map's camera, on real ground
- `rutor/frame.py` holds the live frame for both sources. Its points match `grid-config.ts` to 1e-6 m at the origin, at the sheet's corners and on a 110 m hill.
- Before: `camera_for` mapped degrees to metres with one cos(origin_lat), and `draw.py` put all ground 34.7 m above a flat plane. Over Göteborg's 3 071 masked cells that missed the live map at sea level by a median 40 px and at most 174 px, mostly sideways. Higher ground missed by 2.8 px more per metre. 20 km from the origin, curvature alone drops the ground 31 m.
- Ground heights come from Göteborgs Stad Höjdmodell 2022 (CC0, 0.5 m, RH 2000). It covers all 3 071 masked cells. Lantmäteriet's national model answers 401 without a login. The city ships it as 10 km zips of up to 700 MB, but a zip has its directory at the end: six HTTP range requests in 1.2 s fetch one 1 km tile, about 7 MB. Pillow reads the LZW float32 GeoTIFF, so nothing new is installed.
- RH 2000 to the ellipsoid: SWEN17_RH2000 is 36.49 m and 36.92 m at Lantmäteriet's two nearest control points. A plane through three points gives 36.7 m at the origin, within about 0.5 m (1.5 px) over the sheet. The old renderer's comment already said "sea level in Gothenburg about 36 m".
- The SWEREF 99 TM formula is checked against Lantmäteriet's STAC box for tile 640_32 to 1e-7 degrees.
- Compared with the live tiles by correlating blurred brightness in boxes around strong ground features. Edge correlation over a whole cell is useless against pixel art (score 0.05).
  - Cell 64_52, the canals by the origin, rendered from real data in 168 s: 0 to 8 px off in four boxes (correlation 0.58 to 0.73).
  - Cell 63_19, a forested hill, rendered in 78 s: 14 to 46 px off in three boxes (0.56 to 0.68), 0 px over the whole cell (0.40). The old renderer was 34 to 120 px off in the same boxes. Google's mesh over a forest is the canopy, not the ground, which may explain part of the rest.
- The new boxes sit where the cells really are, so the mask's photo cover rule moves with them. Rerun with the sheet's ground height, 9 of the 3 071 masked cells no longer have full photo cover: 36_55, 40_55, 58_95, 62_62, 72_59, 74_59, 77_60, 78_60 and 84_42.
- Keeping neighbours identical on slopes: positions are floored in map pixels and moved by whole pixels. Trigonometry runs in Python per row and per column, so per-pixel work is only `+`, `-` and `*`. Ground is read on a 2 m lattice counted from the origin. The canvas reaches 32 px past the window, so gaps on slopes steeper than 22 degrees are filled from the same neighbours in both cells. The overlap test now runs on a ridge that is 58 degrees at its steepest and still finds 0 differing pixels.
- Real neighbours 63_19 and 64_19, each centred on its own ground with its own photo box and terrain tiles, drew identical pixels in all 786 432 of their overlap.
- The source manifest's version now ends in `terrain hojdmodell_2022`. The service does not check it, so a render from an older checkout can only be caught in review.
- A photo row lands 0.7 px below the row north of it and paints over it, so the southern row of a mark can hide under the next row. A test of where a pixel lands must look at a row that something nearer cannot cover.

### Single trees at a pinned date break Overpass
- `node["natural"="tree"]` with `[date:...]` runs out of memory (2 GB, `node_tags_global.bin`) on overpass-api.de and maps.mail.ru, even for one cell and even with the box first. kumi.systems and private.coffee timed out. Tree areas and relations at the same date answer in about 33 s.
- Single trees now come from their own query without the date: today's trees whose current version is older than the pin (`(if: timestamp() < "...")`). For 63_19 that is all 159 of today's trees. A tree someone edits or deletes later drops out of later renders, and neighbours rendered on either side of that edit can differ by that tree. The intake blends seams.
- A friend's agent hit the same failure on an Eslöv cell and fixed it in their own way. That fix is not in this repo.

## 2026-11-22

### The Google camera is the live map's camera
- The live tiles were rendered by `renderer/grid-config.ts` on the PC: one orthographic camera per sheet, fixed at the origin's tangent plane, moved sideways per shot. The site's `geometri.ts` projects with the same frame. Copying that pose into `google.py` matches the old code to 5e-10 m.
- The first Google camera reused the open renderer's shortcut, a target at `ground_height - lift_m` with local axes. Against the live pose it missed by 16 to 65 px vertically, and by up to 352 px across at the sheet's corners.
- The open renderer has the same two gaps against the live map. `camera_for` maps degrees to metres with one cos(origin_lat): Göteborg's masked cells sit a median 41 px and at most 176 px (44 m) from the live frame, mostly across. And it draws all ground at one height: cell 63_19 lies 110 m above the sea, about 300 px higher on the live map, and an edge match put it about 230 px off.
- The Overpass tree query for 63_19 failed five times: out of memory once, then HTTP 502.

### Stockholm's mask is the live island
- Stockholm norr is already on the map, so it is redrawn cell for cell. Its mask is every cell the live island covers: 5 036 of 10 080, read from the map's edge picture (`oar/norr-kant.png`, 8 px per cell).
- The urban area rule gives 6 247 cells here, 2 250 of them off the island, and it misses 1 039 cells that are on it. Lidingö is its own urban area but sits on the island. A cell off the island would also need a new edge picture.
- Google covers the whole sheet, so the photo cover rule does not apply.

### A Google source per sheet
- Google stopped serving Photorealistic 3D Tiles to EEA-billed Map Tiles projects after 2025-07-08, but Cesium ion still streams them as asset 2275207, and a free ion account works from Sweden: about 1 000 root loads a month.
- A wrong token comes back from ion as 401 on `/v1/assets/2275207/endpoint` within five seconds, before any tile loads.
- Chrome, Cesium and the Chrome download come to about 750 MB, kept out of the Python package in `cli/google-render`.

### The service is live
- `pixelbygget.gustaf-garnow.workers.dev` runs with D1, R2 and KV in Western Europe. Only `goteborg-norr` is open; `stockholm-norr` opened on 2026-11-22 with its Google source and 5 036 cells.
- A municipal orthophoto stops at the municipal border. Göteborg's is pure white over Partille, Mölndal and Kungälv, which lie inside the Göteborg urban area. A sheet's mask therefore needs both rules: the urban area plus 500 m, and full photo cover over the cell's whole photo box. Göteborg norr: 5 349 cells by area, 3 071 with photo.
- overpass-api.de answers 406 to curl's default User-Agent. With `pixelbygget-rutor/0.1` a small query took 28 s, and 42 s with the pinned date: history queries are slower.

## 2026-11-21

### Review before opening
Four reviews read the code without running the pipeline. What they found and what changed:
- **The overlap was not exact.** Tree crown tables were float32 and held each cell's own photo x, so 7 pixels differed between east-west neighbours. The test allowed 0.1 percent (786 pixels). Fix: float64, and the test now demands zero.
- **Uploads shared one inbox key per cell.** A retry, or someone taking the cell during a slow upload, could make the loser delete the winner's image. Fix: one key per upload (cell plus sha256 prefix); a request deletes only its own key, and only when the cell does not point at it.
- **A removed live cell could be claimed again** before the intake blanked it, and the blanking then deleted the new owner's upload. Fix: no claims while removed.
- **Uploads after the lock's expiry were accepted** until the hourly cron ran. Fix: the service checks the expiry, and expired locks no longer count against the limit.
- **Forests mapped as multipolygon relations were never drawn**, and a way Overpass sent twice could lose its tags.
- **Network timeouts crashed with a traceback** instead of exit 4, and an Overpass error answer with status 200 was cached forever.
- Docs told contributors to run preflight before login, which can never pass, and the login terms still mentioned a Google key.
- A second, fresh review of the service fixes found four more races in them: a purged key could be a re-upload with the same bytes (the seed is fixed per cell), the cron could free a lock taken over after it looked, two `/live` calls with the same rev could both move cells, and a reject could free a cell that had just gone live. Each write now names the exact state it read, keys carry a random part, and each race has a test that fails on the code before it. Review your own fixes with fresh eyes too.
- CRC32 in JavaScript costs about 14 ms of CPU for 8 MB, over the Workers Free limit of 10 ms. The upload checks every chunk's CRC except the image data's.

### Opening the repository
- The full history was checked before going public: no images, no keys, no env files, 29 KB packed. The Google-era plate pipeline only exists on the branch `cursor/plate-pipeline-7eb5`, as code.
- A service test seeded uploads at a fixed date while the route counts the last 24 hours from the real clock, so it failed from 2026-11-18. Tests that meet the real clock must seed relative to it.
- Pull requests now run both test suites in GitHub Actions (`.github/workflows/tests.yml`). The CLI tests need only Pillow and numpy, no GPU and no model.

## 2026-11-20

### First end-to-end contributor run
- The whole volunteer flow ran on a Windows gaming PC (RTX 5070, 12 GB) against the service running locally with `wrangler dev`: preflight, claim `--next`, render from open data, redraw, quality check, upload, then the admin's queue, inbox download (same sha256) and approve. Every step worked without code changes.
- Login without a GitHub app: the service caches who a token belongs to in KV under `gh:<sha256 of token>`, so a local test seeds two fake users with `wrangler kv key put --binding GH --local` and uses made-up tokens. GitHub is never asked and no real token is used. The admin id comes from `service/.dev.vars` (`ADMIN_GITHUB_ID`), which is now ignored by git.
- Reaching a service on one machine from another: when a firewall blocks the port, a reverse SSH tunnel (`ssh -N -R 8787:localhost:8787 <gpu-host>`) works.
- RTX 5070 with 12 GB is enough: one full `rutor redraw` took 133 s including model load and the check.
- The same cell and seed is not pixel-identical across GPUs and library versions (RTX 5070 with diffusers 0.42.1 against an L4 with the pinned commit: mean difference 23). The look is the same; blending covers the seams, but a cell cannot be reproduced exactly on another machine.
- `rutor queue --state godkand` printed approved cells as "waiting"; fixed.
- On Windows, run PowerShell through script files (`powershell -File`); nested quoting over SSH breaks. Tar archives from macOS carry `._*` files unless `COPYFILE_DISABLE=1` is set.

## 2026-11-19

### Lidingö against the Google map (16 cells, west Lidingö)
- The chain ran clean: 130 s per cell on an L4, all 16 cells passed the check, no visible seams after blending, and the render is identical on the Mac and on the box.
- It looks clearly worse than the Google-based map in three ways, each with a known cause:
  - **Villas drawn as five-storey blocks.** 89 percent of Lidingö's OSM buildings have no height or levels (Göteborg: 65 percent), and the renderer falls back to 18 m. A height from footprint size and building type, or laser heights from Lantmäteriet, fixes it.
  - **Forest drawn as lawn.** The 2019 photo is from April with bare trees, and OSM has few forest polygons on the slopes. Land cover data (Nationella Marktäckedata, open) or laser crown heights would place the trees.
  - **Ghost houses in the water.** Lidingö's photo stops at the municipal border, so cells reaching the Stockholm side draw OSM buildings on an empty photo. A cell needs full photo coverage, or several sources stitched together.
- Granite cliffs and rocky shores are missing: no data source in the chain describes them yet.

### Open orthophotos around Stockholm
- **Lidingö** publishes its 2019 orthophoto as CC0: WMS `https://karta.lidingo.se/wms?servicename=wms_ortofoto_2019_oppendata`, layer `theme-ortofoto2019`, `EPSG:4326` (latitude first in WMS 1.3.0). Sharper than Göteborg's.
- **Stockholms stad** orthophoto WMS (`WMS_STHLM_ORTOFOTO_2020`) says all rights reserved and no use without written consent. Do not use it.
- **Solna, Sundbyberg, Danderyd** have no open orthophoto on the EU data portal. North Stockholm needs Lantmäteriet (Geotorget account).
- The renderer now takes a CRS per source and endpoints that already carry a query string.

### The live Google cities do not cover everything
- The Stockholm sheet's rendered "island" leaves out most of Lidingö, including Lidingö centrum. Check the sheet's level-10 overview before choosing cells to compare.

### Lift on the Stockholm sheet
- Scanning lift heights against the live map on two Lidingö cells scored best at 35 and 40 m. Göteborg measured 34.7 m. One constant (34.7 m) serves both sheets.

### Redraw on an L4 (24 GB)
- **Out of memory, cause 1:** the window encode ran with autograd on. One 1024 px window held over 21 GB. Fix: `torch.inference_mode` around the whole redraw.
- **Out of memory, cause 2:** all parts stayed on the GPU. Fix: per phase only the parts that phase uses (encode: text encoder and VAE, denoise: transformer, decode: VAE). Peak is now 10.7 GB.
- **Measured time:** 100 cells, mean 133.3 s, standard deviation 1.4 s, with the model loaded once. The first cell takes about 144 s.
- **Cost:** about 2.60 USD for 100 cells on Vast.ai (0.47 USD per hour).

### Seams after the redraw
- Each cell is redrawn alone, so cells cut at the cell edge show a step in tone and colour (seam step about 2x an ordinary pixel step).
- Cross-fading neighbours over their shared 512 px (128 m) removes it (seam step 0.7 to 1.2, the same as inside a cell). This belongs in the step that assembles the map, not with the person who redraws.

### Quality check calibration
- Over 100 L4 redraws, correct pairs score a match peak of 0.088 and up; a redraw of the wrong cell scores 0.011 to 0.018. The limit is 0.04.
- Open water has almost no edges to align against. A source with under 3 percent clear edges skips the alignment test; the other checks still apply.

### Running on rented boxes
- A slow home uplink (about 20 kB/s) makes uploads to rented boxes the bottleneck. Render sources on the box from the open WMS, upload only code and OSM extracts (a few MB, gzipped).
- The `pytorch/pytorch:2.8.0` image ships old `brotli` and `zstandard`; Hugging Face downloads then crash in `httpx2` with `process() takes no keyword arguments`. Fix: `pip install -U brotli zstandard`.
- Start long jobs with `setsid nohup ... &` and `ssh -n`, or the job dies with the SSH session. Vast boxes can refuse the direct port for minutes while they install SSH; the `sshN.vast.ai` proxy address may also lag.
- `pgrep -f pattern` over SSH matches the SSH command line itself. Grep the process list for the script name instead.
- On the Mac the shell is zsh, which does not split `$var` into words: `set -- $addr` keeps "host port" as one argument and every SSH attempt fails with "Bad port". A wait loop built that way reported a box as down for 15 minutes while it was up. Pass host and port as separate variables.
- Never type an API key for someone. Vast keys go in through a terminal script that reads them without echo.

## 2026-11-18

### Seams in the source renders
Neighbouring 1536 px windows overlap by 512 px and must draw identical pixels there. Four causes were found and fixed, each pinned by `test_neighbour_cells_agree_in_their_overlap`:
- The WMS resamples every request on its own, so two overlapping requests differed in 13 percent of pixels. Fix: fetch the photo as shared 1024 px tiles on the sheet's 0.25 m grid (PNG) and cut every cell from them.
- With the box snapped to the grid, photo pixel centres land exactly between screen columns and rounding follows floating-point noise. Fix: floor, and one longitude scale per sheet.
- Tree randomness came from one sequential generator, so the same tree looked different in each cell. Fix: randomness keyed to world position.
- Window bays counted from the window's column, and wall shading used the tallest wall in the row (which reaches outside the window). Fix: count from the sheet column, shade each wall by its own height.
- Smaller ones: `int(ppm)` gave 7 in one cell and 8 in the next, float32 running sums lost precision with distance, crown texture used each crop's own mean.

### Data fetching
- Overpass answers 502/504 often. Ask once for a whole block and copy the result into each cell's cache instead of two queries per cell.

### GPU estimate method
- Cells per city: SCB tätort 2023 polygon plus 500 m, rasterised to cells (256 m east-west by 362 m north-south). This matches the existing Malmö sheet (1 355 computed, 1 348 actual).
- The free model (Qwen-Image-Edit-2511) has an Apache 2.0 four-step distillation, `lightx2v/Qwen-Image-Edit-2511-Lightning`.

## 2026-11-17

### Google is closed
- Google Photorealistic 3D Tiles are not served to EEA-billed projects created after 2025-07-08, and the terms forbid derived works. The pixel LoRA (`handful/mndf-pixel-qwen-image-2.1`) is trained on Google renders with Codex-painted targets, and Qwen-Image-2.1 is under a research licence. New cities come from open data; the style model must be replaced before an open release.

### What open data gives
- OpenStreetMap alone gives blocky renders that redraw poorly.
- Göteborg's CC0 orthophoto draped on OSM footprints, plus procedural facades, OSM and photo-detected trees and roof colour grading, redraws close to the live Google-based map.
- `camera_for` was missing the cos 45 north-south factor. A fixed 34.7 m lift aligns our renders with the live sheet.
- Redraw on an L40S: 57 s per cell with the model loaded.
