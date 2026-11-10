# Agents

You are helping one person contribute one map tile from their own GPU. Read [README.md](README.md) and follow its steps in order. The rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Order

1. `python scripts/redraw_tile.py --preflight`
2. `python scripts/se3_price.py`
3. Check that the source render exists at `inputs/<district>.png`.
4. `python scripts/claim_district.py --list`, then `--district <slug>`
5. `python scripts/redraw_tile.py --district <slug>`
6. `python scripts/publish_png.py --district <slug>`

## Stop and tell your human

- Preflight does not print `preflight: ok`.
- The price check prints `decision: WAIT`, or exits with code 1.
- There is no source render. Do not make one up, and do not claim.
- A claim push is rejected twice.
- Any step asks for a key, a token, or a login you were not given.
- The redraw fails. Report the last lines it printed. Do not change the script to get past it.

## The redraw is long

One tile takes about an hour. The script prints `step x/y window a/b` while it works.

- Run it once and wait. Do not start a second run.
- Do not treat a quiet minute as a hang.
- The first run downloads about 20 GB of weights before it prints any step.

## Never

- Never run `git add -A` or `git add .`. The scripts stage the one file they need.
- Never commit by hand. The only commits are the ones `claim_district.py` and `publish_png.py` make.
- Never edit the scripts, `districts.json`, a claim file, or a file that belongs to another district.
- Never pass `--run-anyway` after a `WAIT`.
- Never claim a second district while one is unfinished.
- Never force push, and never create a branch.
- Never put weights, renders, `.env` files, keys, or tokens in the repo.

## When you cannot finish

Give the district back, then say so:

```bash
python scripts/claim_district.py --release --district <slug>
```

## Report when done

Say which district, the commit, and that the commit holds only `results/<district>.png`.
