# SimCity AI-karta

Public contributor door for the OpenSverige map. This repository is the only place you work.

You do not receive API keys, tokens, SSH keys, or any other project. You clone this repo, claim one Swedish municipality, redraw that tile on your own GPU, and commit the finished PNG. Nothing else is uploaded.

Districts are the 290 municipalities in `districts.json`. One district per claim. The result path is `results/<district>.png`.

The owner invites your GitHub account as a collaborator. That invitation is the only access. You need write access so the claim can be pushed. Ask for it with an [access request](https://github.com/Baltsar/karta.opensverige-3d/issues/new?template=access-request.yml).

The rules, the license, and how you are credited are in [CONTRIBUTING.md](CONTRIBUTING.md). An AI agent reads [AGENTS.md](AGENTS.md) before it runs anything.

Model weights are not in this repo. The redraw downloads them into the Hugging Face cache on your machine.

## 0. Check the machine

NVIDIA CUDA is required for the redraw. The 4-bit checkpoint is a bitsandbytes quant. The door is aimed at 24 GB cards. On the maintainer's RTX 5070 one tile takes about an hour.

```bash
python scripts/redraw_tile.py --preflight
```

- **Windows and Linux** with an NVIDIA GPU: preflight should print `preflight: ok`.
- **macOS**: clone, install, the price check, and `--list` all work. Preflight exits if there is no CUDA device. Do not claim a district on a Mac that fails preflight.

## 1. Price

Swedish bidding zone SE3, from the public Elpriset just nu files. No key. Prices are SEK/kWh excluding VAT.

```bash
python scripts/se3_price.py
```

Obey the `decision` line:

- `decision: RUN` — claim one district and redraw.
- `decision: WAIT` — stop. Do not claim. Do not redraw. The `action` line says when SE3 is cheapest. Run this script again then.
- Exit code 1 — the price could not be read. Do not redraw.

Do not pass `--run-anyway` to the redraw when the decision is WAIT.

## 2. Claim

```bash
python scripts/claim_district.py --list
python scripts/claim_district.py --district stockholm
```

Have your source render ready first (step 3), so the district does not sit locked and waiting. Pick any free slug. Do not all take the first line. The script commits only `claims/<district>.md` and pushes it. If the push is rejected, that district is gone: `--list` again and choose another. Details are in `CLAIM.md`.

## 3. Source image

Put your own render at `inputs/<district>.png`. That folder is gitignored. Do not commit it. This repo does not ship source imagery.

## 4. Redraw

```bash
python scripts/redraw_tile.py --district stockholm
```

One tile. Windows are 1024 pixels, stride 512. Overlapping noise predictions are averaged at every denoising step, then the scheduler steps once (MultiDiffusion). The script writes `results/<district>.png` and nothing else.

Weights, downloaded only into your Hugging Face cache:

- `addlabsviral/Qwen-Image-2.1-Turbo-4bit`
- LoRA `handful/mndf-pixel-qwen-image-2.1`

The Qwen Research License applies to those weights. They are not copied into this repo. The first run is a large download, on the order of 20 GB.

## 5. Publish

```bash
python scripts/publish_png.py --district stockholm
```

That commit contains only `results/<district>.png`. Do not use `git add -A`.

If you cannot finish, give the district back:

```bash
python scripts/claim_district.py --release --district stockholm
```

## Setup

Use Python 3.11 or newer. Set your own git identity inside the clone (not `--global` unless that is already how you work):

```bash
git config user.name "Your Name"
git config user.email "you@example.com"
```

Install a CUDA build of PyTorch first, then the rest, so a later install does not replace it with a CPU wheel. If the CUDA 12.8 index does not match your driver, use the current index from <https://pytorch.org>.

### Windows (PowerShell)

```powershell
git clone https://github.com/Baltsar/karta.opensverige-3d
cd karta.opensverige-3d
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install torch --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r requirements.txt
python scripts/redraw_tile.py --preflight
python scripts/se3_price.py
```

If activation is blocked for the current window only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### Linux

```bash
git clone https://github.com/Baltsar/karta.opensverige-3d
cd karta.opensverige-3d
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install torch --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r requirements.txt
python scripts/redraw_tile.py --preflight
python scripts/se3_price.py
```

### macOS

```bash
git clone https://github.com/Baltsar/karta.opensverige-3d
cd karta.opensverige-3d
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install torch
python -m pip install -r requirements.txt
python scripts/redraw_tile.py --preflight
python scripts/se3_price.py
```

The price check and the claim list do not need a GPU. The redraw does not run until preflight reports CUDA.

## What never goes in a commit

Environment files, private keys, tokens, model weights, and files under `inputs/`. A pull request may only add `claims/<district>.md` or `results/<district>.png`, or delete a claim when a district is released.

PNG files you publish are contributed to this repository under the MIT license, and may be shown on karta.opensverige.se, credited to your GitHub account. The model weights stay under their own licenses. The Qwen Research License grants non-commercial use only. Details are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Local checks

This does not download the model:

```bash
python -m unittest tests/test_door.py
```

Door test line from the owner. This pull request will be closed.
