# fomo-desk-runner

PAPER-only FOMO meme-coin desk. It **never** places real orders, holds keys, or talks to a swap/signing API. The owner taps a FOMO link in Telegram if they want to buy themselves. Links always include `r=savipww`.

This public repo exists so the desk can run 24/7 on **GitHub Actions** (the previous box froze). GitHub jobs max out around 6 hours, so `desk.yml` stops at ~5h45, checkpoints `desk.db`, and re-dispatches itself. `desk-watchdog.yml` cron (every 20 minutes) starts a new run only if none is already active.

## What is public

Code in this repo is public. The live dashboard already publishes trades and the stack via a public gist. `desk.db` checkpoints go to a **private** gist and to Actions artifacts (public-repo artifacts are downloadable — treat them as public trade data, never secrets).

## Secrets

Never committed. Set as Actions repository secrets: `TG_TOKEN`, `JUPITER_API_KEY`, `APP_TOKEN`, `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `GIST_TOKEN` (a PAT with `gist` + `workflow` + `repo`), `STATE_GIST_ID`.

## Rollback to the box

See `/workspace/desk/RUNNING_ON_ACTIONS.txt` on the original box (written at cutover). Short version: restore `desk.db` from the latest state-gist checkpoint, then start `main.py` and `gist_push.py` with the box env; disable or pause these workflows so you do not run two pollers.

## License / disclaimer

Personal PAPER tool. Not financial advice. Not a live brokerage.
