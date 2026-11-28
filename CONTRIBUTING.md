# Contributing

Two ways to help. A code change is open now. A GPU run puts one cell on the map, credited to your GitHub login.

This page holds the rules. The commands and the install are in [README.md](README.md). If an agent runs the commands for you, it reads [AGENTS.md](AGENTS.md) first.

## Before you start

A code change needs none of the list below: any machine with Python 3.11 will do, and Node 22 for the service's tests. A GPU run needs all of it. The full list is under "What you need" in [README.md](README.md).

- An NVIDIA GPU with CUDA and 12 GB of memory or more, or on Linux an AMD GPU that ROCm supports, with 12 GB the GPU can use.
- Time. One cell takes about two minutes on an RTX 5070 or an L4, and six and a half on an AMD Strix Halo.
- 25 GB of free disk, and about 1 GB more for `stockholm-norr`. The downloads are about 16 GB in all, 12 of them the model.
- Windows or Linux for the redraw. A Mac can change the code and run the tests. It cannot redraw, so do not claim a cell on a Mac.
- Python 3.11 or newer, and a GitHub account.
- The source render comes from the sheet's source. Today every free cell is on `stockholm-norr`. `goteborg-norr` is open data: Göteborgs Stad orthophoto and terrain model (both CC0) and OpenStreetMap (ODbL). `stockholm-norr` is Google's tiles through your own Cesium ion token, Node 22 and `npm ci` in `cli/google-render`. Google's terms bind that token, and Google can ask for those cells to be taken down.

Clone, install the CLI and run `rutor preflight --machine` first: it needs no login and says whether this machine can build a cell. Then install the rest, log in, and run `rutor preflight --sheet stockholm-norr`. Go on only when every line says `ok`.

## The run

```bash
rutor preflight --machine
rutor login        # prints the terms and asks you to accept them
rutor preflight --sheet stockholm-norr
rutor claim <sheet> --next
rutor render <sheet> <col>_<row>
rutor redraw <sheet> <col>_<row>
rutor upload <sheet> <col>_<row>
```

Each step is explained in [README.md](README.md).

## Rules

- A lock lasts 48 hours. `rutor status` shows yours.
- One cell at a time until you are trusted. Finish it or release it before you take another. A second open lock is refused, and so is a new one while five of your cells wait for review.
- A person reviews your cells. `rutor upload` leaves the cell waiting, and an admin approves or rejects it. Nothing goes on the map without that.
- Trust is given by the maintainer, by hand, to builders whose cells have held up. A trusted builder may hold eight locks, and their cells go straight through.
- A new contributor uploads at most 20 cells a day.
- Do not send the source render to anyone. `work/<sheet>/<col>_<row>.src.png` stays on your machine. Only the redrawn PNG is uploaded.
- During a GPU run, do not edit `cli/rutor/pipeline.py`, the redraw or the manifest, and never touch another contributor's cell. A change to the redraw is a code change: open a pull request.
- Cannot finish? Give the cell back: `rutor release <sheet> <col>_<row>`.

## Code contributions

Code contributions and GPU runs are both open. GPU runs need an NVIDIA card, or an AMD card on Linux.

- Pick an issue, or open one before a large change.
- Branch from `main` and open a pull request against `main`.
- Make a virtualenv (`python3 -m venv .venv`, then activate it) and install with `python -m pip install -e "cli[render]"`. No GPU is needed.
- Run the tests before you push, from the repository root:
  - `cd cli && python -m unittest discover -s tests` (the renderer tests are skipped without Pillow and numpy)
  - `cd service && npm ci && npm test` (Node 22 or newer)
- To try the CLI against a real service, run one on your machine with `service/scripts/local.sh`.
- A source render must stay identical between neighbouring cells: `test_neighbour_cells_agree_in_their_overlap` allows zero differing pixels. Add a test when you change `cli/rutor/renderer/`.
- Do not loosen the quality check in `cli/rutor/check.py` to make a test pass.
- Write what you learned in `docs/pixelbygget/LEARNINGS.md`, newest first.
- Never commit images, model weights or keys.

## How to report

Open an issue on <https://github.com/opensverige/karta.opensverige-3d/issues>. Include the command, the exit code and the `error:` line. Do not paste your GitHub token.

## Terms, licence and credit

The ten lines in [TERMS.md](TERMS.md) are what you accept at `rutor login`. In short:

- It is voluntary and unpaid, on your own machine at your own cost and risk.
- A person reviews your cells before they go on the map, and a cell can be taken down later.
- You are credited by the GitHub login you used in `rutor login`. It is shown publicly.
- Each claim and upload carries the version of the terms you accepted. When the version changes, run `rutor terms` and say yes again.
- The model weights and the sources keep their own licenses. TERMS.md names each one.
