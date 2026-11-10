# Contributing

You lend one GPU run. The map gets one tile, credited to you.

This page holds the rules. The commands and the install are in [README.md](README.md). If an AI agent runs the commands for you, it reads [AGENTS.md](AGENTS.md) first.

## Before you ask for access

- An NVIDIA GPU with CUDA. The door is aimed at 24 GB cards.
- Time. On the maintainer's RTX 5070 one tile takes about an hour.
- About 20 GB of free disk for the model download.
- Windows or Linux. A Mac can read the repo and check the price. It cannot redraw.
- Python 3.11 or newer, git, and a GitHub account.
- A source render of the district. See [Source render](#source-render).

Clone, install, and run the preflight. None of that needs access.

```bash
python scripts/redraw_tile.py --preflight
```

Go on only when it prints `preflight: ok`.

## Get access

Open an [access request](https://github.com/Baltsar/karta.opensverige-3d/issues/new?template=access-request.yml). The owner invites your GitHub account as a collaborator. Accept the invitation.

That invitation is the only access you get. There are no keys, tokens, or other projects to ask for.

## The run

Have the source render ready before you claim, so no district sits locked and waiting.

```bash
python scripts/se3_price.py
python scripts/claim_district.py --list
python scripts/claim_district.py --district <slug>
python scripts/redraw_tile.py --district <slug>
python scripts/publish_png.py --district <slug>
```

Each step is explained in [README.md](README.md). The claim file is explained in [CLAIM.md](CLAIM.md).

## Rules

- One district at a time. Finish it or release it before you take another.
- You add two files, and only through the scripts: `claims/<district>.md` and `results/<district>.png`.
- Never commit `inputs/`, model weights, `.env` files, keys, or tokens. Never use `git add -A`.
- Do not edit the scripts, `districts.json`, or another contributor's claim or tile. Found a bug? Open an issue.
- Obey the price check. `decision: WAIT` means stop.
- Cannot finish? Give the district back: `python scripts/claim_district.py --release --district <slug>`.

## What the door checks

Every push and every pull request runs the `door` check. A contributor change passes only when it does one of these:

- adds a claim exactly as `scripts/claim_district.py` writes it
- adds a real PNG at `results/<district>.png`
- deletes a claim to release a district

Any other file fails the check. So does a hand-edited claim.

## Source render

The redraw repaints a render you already have. It does not create the map view.

- Put it at `inputs/<district>.png`.
- It is a 3D render of the district, seen from above at an angle.
- It is a PNG, at most 4096 pixels on the long side.
- It stays on your machine. `inputs/` is gitignored.

This repo does not ship renders. If you do not have one, say so in your access request, before you claim.

## License and credit

- The PNG you publish is contributed under the MIT license. See [LICENSE](LICENSE).
- By pushing a tile or opening a pull request, you agree that it may be shown on karta.opensverige.se.
- You are credited by the GitHub account that publishes the tile.
- Contribute only a tile you made with this pipeline, from a render you are allowed to use.

The model weights are not part of this repo and keep their own licenses. You download and run them on your own machine, under those licenses:

- Qwen-Image-2.1-Turbo is under the [Qwen Research License](https://huggingface.co/Qwen/Qwen-Image-2.1-Turbo/blob/main/LICENSE). It grants non-commercial use only. Read it before the first run.
- The LoRA `handful/mndf-pixel-qwen-image-2.1` is Apache-2.0.

## Problems

Open an issue. Do not paste keys, tokens, or paths from your machine.
