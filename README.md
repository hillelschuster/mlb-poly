# mlb-poly

Paper-only Polymarket MLB game-moneyline bot. It has exactly one entry thesis:

> Buy a team only when a current multi-book no-vig probability is materially above that exact team's Polymarket token's executable all-in cost.

This is a price-discovery bot, not a static wallet-copy bot and not a sports-prediction model. It has no private key, no order-signing dependency, and no live-order code.

## The actual bet

For every active Polymarket MLB **game moneyline** market, the bot:

1. Pulls the current `h2h` MLB odds feed from The Odds API.
2. Converts each bookmaker's two-sided decimal odds to a no-vig team probability:

   `p_team = (1 / decimal_team) / ((1 / decimal_team) + (1 / decimal_opponent))`

3. Uses the median across at least three valid books as `fair_probability`.
4. Reads the exact team-token CLOB asks, then walks the book with a 30% depth haircut, the market's published taker-fee rate, and 0.3¢/share paper friction.
5. Opens a paper position only if the filled all-in price clears `fair_probability - MIN_NET_EDGE` (`3.5¢` by default). It holds to settlement.

The fair-price edge is calculated on the *filled* price, not the screen midpoint or last trade. That is the whole strategy.

The bot also records recent large public Polymarket BUY flow on the same token—dollar amount and distinct-wallet count—but it does **not** buy because a wallet bought. That distinction is deliberate: in the existing VPS history, headline MLB paper results are positive, yet the wallet samples are too small and too selective to promote a fixed “top wallet” list honestly. Flow is a useful future segmentation variable, not the alpha claim.

## Why this is the only sensible initial lane

The reconciled in-house record says three things:

- Hermes' true MLB-moneyline paper slice was positive overall (`45` resolved, `+$52.56` on `$555.32`), but it also had a losing closed trade and its September sample deteriorated.
- The best past price band (`.65–.72`) did better than higher prices, but that is an observation—not a portable reason to buy that band.
- The apparent multi-wallet consensus result was only three games. A static 5–20 wallet copier would be post-hoc selection masquerading as a strategy.

The only candidate with a mechanism that can recur across MLB seasons is a lag between a liquid sportsbook consensus and a thin or slow Polymarket book. It is not guaranteed to exist, and it will disappear when Polymarket is already efficient. The bot therefore logs the edge at the time it was actually executable, so the paper ledger can distinguish a real edge from a pretty backtest.

It also refuses futures, World Series, props, spreads, totals, run lines, innings, and in-play/near-start games. Those are different pricing regimes and would contaminate the sample.

## Quick start

```bash
git clone https://github.com/hillelschuster/mlb-poly.git
cd mlb-poly
cp .env.example .env
# Put a The Odds API key in .env
set -a; . ./.env; set +a

# One real paper cycle
python3 mlb_poly.py --once

# Continuous paper collection
python3 mlb_poly.py

# PnL / resolved-trade ledger only
python3 mlb_poly.py --stats
```

The Odds API key is the sole required credential. It provides MLB `h2h` pricing and per-book timestamps; use a plan with enough quota for your polling cadence. `ODDS_REFRESH_SECONDS=120` intentionally caches the external request while the bot can inspect Polymarket books every 30 seconds.

## What the SQLite ledger preserves

- `observations`: five-minute price snapshots for matched markets, including fair probability, book count, data age, executable price, net edge, public-flow context, and the decision.
- `positions`: exact simulated fills and resolved net PnL after fee and paper friction.

The practical promotion decision is not win rate. It is whether resolved positions remain profitable *after* the ledger's all-in costs, while the recorded pregame edge predicts results across independent game dates. If that fails, the strategy fails; do not rescue it with a wallet list or extra rules.

## Intentionally absent

- No private key or live execution path
- No machine-learning model
- No hard-coded team ratings or team map
- No static copied-wallet allow-list
- No deployment, Docker, dashboard, CI, or broad test suite

Polymarket's public market discovery/CLOB endpoints and The Odds API are used directly. Check the official provider terms and your local availability before running.
