#!/bin/bash
# One-liner you can paste. It will:
# 1. Create the public GitHub repo `ballpark-stats` under your account.
# 2. Push the committed server code.
#
# Requires `gh auth login` once beforehand. Run `gh auth login` in
# your terminal and follow the browser flow first. Then run this.

cd /Users/jack/ballpark-stats

# Create the public repo (won't fail if it already exists with
# the same name; will fail loudly if it does — that's fine, just
# push to the existing one).
gh repo create ballpark-stats --public --source=. --remote=origin --description="Anonymous aggregate stats for the Close Enough iOS app" --push 2>&1

# If gh repo create succeeded, the push is already done. If you
# need to push to an existing repo instead:
#   git remote add origin https://github.com/jackgreening1995-creator/ballpark-stats.git
#   git push -u origin main
