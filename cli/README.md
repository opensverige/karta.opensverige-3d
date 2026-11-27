# rutor

`rutor` is the Pixelbygget command. It logs in with GitHub, claims a cell, renders and redraws it, and uploads the PNG. The full install, including CUDA or ROCm, is in the repository [README](../README.md). The service API is in [service/README.md](../service/README.md). The rules are in [CONTRIBUTING](../CONTRIBUTING.md).

Python 3.11 or newer. The client itself uses the standard library. The redraw needs the CUDA or ROCm build of PyTorch and the stack in the repository `requirements.txt`.

## Install

From the repository root, inside the virtualenv from the main README:

```bash
python -m pip install -e cli
rutor --version
```

Or, without installing, from this directory:

```bash
python -m rutor --version
```

## The steps

```bash
rutor preflight --machine
rutor login
rutor preflight --sheet stockholm-norr
rutor claim stockholm-norr --next
rutor render stockholm-norr 74_20
rutor redraw stockholm-norr 74_20
rutor upload stockholm-norr 74_20
```

Replace `74_20` with the cell `rutor claim` prints. `rutor claim --at <lat>,<lon>` takes the cell at a place instead, anywhere in Sweden where Google has 3D tiles and the map is not drawn yet. `stockholm-norr` needs your own Cesium ion token: see the main README. `rutor preflight --machine` checks python, the graphics card and the disk, with no login. `rutor login` prints the terms in [TERMS.md](../TERMS.md). `rutor preflight --sheet stockholm-norr` should be `ok` on every line before `rutor claim`.

`rutor login` prints a code and `url: https://github.com/login/device`. A person approves that code in the browser. The token is stored in `~/.config/pixelbygget/token` (override with `PIXELBYGGET_HOME`), mode `0600` on Linux and macOS. `PIXELBYGGET_URL` points the CLI at a service on this machine, for testing. It accepts no other address. That approval is the step that needs a person.

`rutor render` builds the source render from the sheet's source: open data on `goteborg-norr`, Google's tiles through Cesium ion on `stockholm-norr` (see the main README for the token and `npm ci` in `cli/google-render`). The open-data path needs Pillow and numpy, which are in the repository `requirements.txt` and in the `render` extra (`pip install -e "cli[render]"`). The OpenStreetMap data is pinned to one date per sheet, so every contributor renders the same map.

`rutor redraw` checks its own result before it finishes. A blank, too dark, posterised, shifted or unchanged redraw exits with code 5 and one `check:` line per problem. `rutor upload` refuses a cell until the check has passed. `rutor check <sheet> <col>_<row>` runs the check again on the files in `work/`.

## Reviewing (the maintainer)

```bash
rutor review
```

It opens a page in your browser that shows each waiting cell laid into today's map among its eight neighbours, beside the map as it is today, with two buttons: approve and reject. Approve works only while the cell's own picture is on screen. A rejection needs a reason, which goes into the service's log. A cell you approved can be taken back on the same page until it is on the map. `rutor granska` is the same command. `rutor queue`, `rutor approve` and `rutor reject` do the same from the terminal.

The page runs on your machine only and uses the login from `rutor login`. The service decides who may review: for anyone but the admin it answers `refused` and no page opens.

- It listens on `127.0.0.1`, on a free port, and prints its address as `url:`. The address ends in a long random key. Port and key are new at every start. Whoever has the address can approve and reject while the command runs, so do not paste it anywhere. Ctrl-C closes the page.
- `--no-open` prints the address without opening a browser. Use it on a Linux machine you share: there the browser is started with the address as an argument, which other users of the machine can read.
- An upload is shown only when it is a plain picture: 8-bit RGB with no alpha, animation, gamma or colour profile. A browser and the program that lays the cell into the map would not agree on anything else. Such an upload can only be rejected.
- What the page lists under the builder's own details comes from the builder's machine. Nobody has checked it.
- Today's map comes from the project's tile service, in the edition that is live: cells that have already been put into the map show among the neighbours. `PIXELBYGGET_TILES_URL` points at a local one for testing, and like `PIXELBYGGET_URL` it accepts this machine only.
- A city that has no map yet shows the cell alone.

## Tests

```bash
python -m unittest discover -s tests -v
```

Without Pillow and numpy the renderer tests are skipped, not passed.
