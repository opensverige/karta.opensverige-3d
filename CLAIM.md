# Claims

One district per agent. The lock is a file named `claims/<district>.md`. The only upload after that is `results/<district>.png`.

Districts are the 290 Swedish municipalities in `districts.json`. The slug is the filename (`stockholm`, `goteborg`, `malmo`). Slugs are ASCII so Windows, Linux, and macOS agree.

## Lock

1. `python scripts/se3_price.py`
2. If it prints `decision: WAIT`, stop. Do not claim.
3. `python scripts/claim_district.py --list`
4. Pick one free slug. Do not take the first line just because it is first.
5. `python scripts/claim_district.py --district <slug>`

The script commits only `claims/<slug>.md` and pushes it. If the push is rejected, that district is taken. Run `--list` again and pick a different one.

Do not edit claim files by hand. Do not claim a second district while yours is unfinished.

## Claim file

```
# Claim

district: stockholm
name: Stockholm
code: 0180
status: claimed
claimed_at: 2026-11-09T17:35:00+00:00

One district, one agent. The only result file for this claim is results/stockholm.png.
```

That is the whole file, as the script writes it. The door check rejects a claim with any other content.

The file has no paths, keys, or machine names.

## Finish or give it back

- Finished tile: `python scripts/publish_png.py --district <slug>` commits only `results/<slug>.png`.
- You cannot finish: `python scripts/claim_district.py --release --district <slug>` deletes your claim. This is refused if the PNG already exists.

Leave other agents' claims and PNGs alone.
