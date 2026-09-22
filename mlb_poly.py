#!/usr/bin/env python3
"""Paper-only MLB Polymarket price-discovery bot.

One thesis, no live-order path: buy an MLB game-moneyline team token only when
a fresh, no-vig sportsbook consensus prices that team materially above the
*executable* Polymarket all-in cost.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
ODDS = "https://api.the-odds-api.com/v4"
LOG = logging.getLogger("mlb-poly")

NOT_GAME_MONEYLINE = re.compile(
    r"world series|champion|division|playoff|postseason|wild card|"
    r"total|over|under|spread|runline|run line|inning|first 5|f5|"
    r"player|prop|home run|strikeout|rbi|hits?", re.I
)


def f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except json.JSONDecodeError:
            return []
    return []


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def epoch(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        value = float(value)
        return value / 1000 if value > 10_000_000_000 else value
    text = str(value).strip()
    try:
        value = float(text)
        return value / 1000 if value > 10_000_000_000 else value
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def normal(value: str) -> str:
    """Mechanical name matcher; no team-specific alpha or static team list."""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    db_path: str
    odds_api_key: str
    poll_seconds: float
    odds_refresh_seconds: float
    odds_regions: str
    odds_bookmakers: str
    min_books: int
    max_odds_age_seconds: float
    min_net_edge: float
    min_minutes_to_start: float
    max_hours_to_start: float
    paper_trade_usd: float
    paper_bankroll_usd: float
    min_fill_fraction: float
    depth_haircut: float
    friction_per_share: float
    flow_min_notional: float
    flow_lookback_seconds: float
    request_timeout: float

    @classmethod
    def load(cls) -> "Config":
        return cls(
            db_path=os.getenv("DB_PATH", "./mlb_poly.sqlite"),
            odds_api_key=os.getenv("ODDS_API_KEY", "").strip(),
            poll_seconds=env_float("POLL_SECONDS", 30),
            odds_refresh_seconds=env_float("ODDS_REFRESH_SECONDS", 120),
            odds_regions=os.getenv("ODDS_REGIONS", "us").strip(),
            odds_bookmakers=os.getenv("ODDS_BOOKMAKERS", "").strip(),
            min_books=env_int("MIN_BOOKS", 3),
            max_odds_age_seconds=env_float("MAX_ODDS_AGE_SECONDS", 300),
            min_net_edge=env_float("MIN_NET_EDGE", 0.035),
            min_minutes_to_start=env_float("MIN_MINUTES_TO_START", 12),
            max_hours_to_start=env_float("MAX_HOURS_TO_START", 48),
            paper_trade_usd=env_float("PAPER_TRADE_USD", 25),
            paper_bankroll_usd=env_float("PAPER_BANKROLL_USD", 1_000),
            min_fill_fraction=env_float("MIN_FILL_FRACTION", 0.80),
            depth_haircut=env_float("PAPER_DEPTH_HAIRCUT", 0.30),
            friction_per_share=env_float("PAPER_FRICTION_PER_SHARE", 0.003),
            flow_min_notional=env_float("FLOW_MIN_NOTIONAL", 2_000),
            flow_lookback_seconds=env_float("FLOW_LOOKBACK_SECONDS", 600),
            request_timeout=env_float("REQUEST_TIMEOUT", 12),
        )


class Http:
    def __init__(self, config: Config):
        self.timeout = config.request_timeout
        self.headers = {
            "Accept": "application/json",
            "User-Agent": os.getenv("USER_AGENT", "mlb-poly/0.1 paper-research"),
        }

    def get(self, base: str, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        url = f"{base}{path}"
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})
        request = Request(url, headers=self.headers)
        with urlopen(request, timeout=self.timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"GET {url}: HTTP {response.status}")
            return json.load(response)


class OddsFeed:
    """Current multi-book h2h odds, cached to respect the provider's quota."""
    def __init__(self, config: Config, http: Http):
        self.config, self.http = config, http
        self.last_fetch = 0.0
        self.events: list[dict[str, Any]] = []

    def refresh(self) -> list[dict[str, Any]]:
        if self.events and time.time() - self.last_fetch < self.config.odds_refresh_seconds:
            return self.events
        if not self.config.odds_api_key:
            raise RuntimeError("ODDS_API_KEY is required for price discovery")
        params: dict[str, Any] = {
            "apiKey": self.config.odds_api_key,
            "regions": self.config.odds_regions,
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if self.config.odds_bookmakers:
            params["bookmakers"] = self.config.odds_bookmakers
        payload = self.http.get(ODDS, "/sports/baseball_mlb/odds", params)
        self.events = [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
        self.last_fetch = time.time()
        return self.events

    def fair_probability(self, event: dict[str, Any], team: str) -> tuple[Optional[float], int, Optional[float]]:
        """Median no-vig probability across valid bookmaker pairs for one team."""
        target = normal(team)
        home, away = normal(str(event.get("home_team", ""))), normal(str(event.get("away_team", "")))
        if target not in {home, away}:
            return None, 0, None
        counterpart = away if target == home else home
        values: list[float] = []
        freshest: Optional[float] = None
        for bookmaker in event.get("bookmakers", []):
            if not isinstance(bookmaker, dict):
                continue
            seen: dict[str, float] = {}
            for market in bookmaker.get("markets", []):
                if not isinstance(market, dict) or market.get("key") != "h2h":
                    continue
                for outcome in market.get("outcomes", []):
                    if not isinstance(outcome, dict):
                        continue
                    price = f(outcome.get("price"))
                    if price > 1.0:
                        seen[normal(str(outcome.get("name", "")))] = price
            if target not in seen or counterpart not in seen:
                continue
            implied_target, implied_other = 1 / seen[target], 1 / seen[counterpart]
            values.append(implied_target / (implied_target + implied_other))
            updated = epoch(bookmaker.get("last_update"))
            if updated is not None:
                freshest = max(freshest or updated, updated)
        if len(values) < self.config.min_books:
            return None, len(values), freshest
        return statistics.median(values), len(values), freshest


def token_ids(market: dict[str, Any]) -> list[str]:
    return [str(item) for item in json_list(market.get("clobTokenIds"))]


def outcomes(market: dict[str, Any]) -> list[str]:
    return [str(item) for item in json_list(market.get("outcomes"))]


def is_game_moneyline(market: dict[str, Any]) -> bool:
    question = str(market.get("question", ""))
    if NOT_GAME_MONEYLINE.search(question):
        return False
    return (
        str(market.get("sportsMarketType", "")).lower() == "moneyline"
        and " vs. " in question.lower()
        and len(outcomes(market)) == 2
        and len(token_ids(market)) == 2
        and bool(market.get("enableOrderBook", True))
    )


def match_event(market: dict[str, Any], events: Iterable[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Match an exact two-team Polymarket game to the external feed."""
    market_teams = {normal(name) for name in outcomes(market)}
    if len(market_teams) != 2 or not all(market_teams):
        return None
    candidates: list[dict[str, Any]] = []
    for event in events:
        home, away = str(event.get("home_team", "")), str(event.get("away_team", ""))
        if {normal(home), normal(away)} != market_teams:
            continue
        candidates.append(event)
    if len(candidates) == 1:
        return candidates[0]
    start = epoch(market.get("gameStartTime"))
    if start is not None:
        close = [event for event in candidates if (event_start := epoch(event.get("commence_time"))) is not None and abs(event_start - start) < 12 * 3600]
        if len(close) == 1:
            return close[0]
    return None


def orderbook_levels(book: dict[str, Any], side: str) -> list[tuple[float, float]]:
    levels = []
    for item in book.get(side, []) if isinstance(book, dict) else []:
        if isinstance(item, dict):
            price, size = f(item.get("price")), f(item.get("size"))
            if price > 0 and size > 0:
                levels.append((price, size))
    return levels


def fee_rate(market: dict[str, Any]) -> float:
    schedule = market.get("feeSchedule")
    if isinstance(schedule, dict) and f(schedule.get("rate")) > 0:
        return f(schedule.get("rate"))
    return 0.05  # Polymarket's documented sports taker fee when no field is returned.


class Ledger:
    def __init__(self, path: str):
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS observations (
              observation_key TEXT PRIMARY KEY,
              observed_at TEXT NOT NULL,
              market_id TEXT NOT NULL,
              condition_id TEXT NOT NULL,
              external_event_id TEXT NOT NULL,
              team TEXT NOT NULL,
              fair_probability REAL NOT NULL,
              books INTEGER NOT NULL,
              odds_age_seconds REAL,
              best_bid REAL,
              best_ask REAL,
              all_in_price REAL,
              net_edge REAL,
              flow_notional REAL NOT NULL,
              flow_wallets INTEGER NOT NULL,
              decision TEXT NOT NULL,
              reason TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS observations_market_time ON observations(market_id, observed_at);
            CREATE TABLE IF NOT EXISTS positions (
              position_id TEXT PRIMARY KEY,
              opened_at TEXT NOT NULL,
              market_id TEXT NOT NULL,
              condition_id TEXT NOT NULL,
              external_event_id TEXT NOT NULL UNIQUE,
              market_slug TEXT NOT NULL,
              team TEXT NOT NULL,
              token_id TEXT NOT NULL,
              fair_probability REAL NOT NULL,
              books INTEGER NOT NULL,
              best_bid REAL NOT NULL,
              entry_vwap REAL NOT NULL,
              all_in_price REAL NOT NULL,
              net_edge REAL NOT NULL,
              shares REAL NOT NULL,
              cash_used REAL NOT NULL,
              fee_usd REAL NOT NULL,
              friction_usd REAL NOT NULL,
              flow_notional REAL NOT NULL,
              flow_wallets INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'OPEN',
              resolved_at TEXT,
              payout_usd REAL,
              pnl_usd REAL
            );
            """
        )

    def close(self) -> None:
        self.conn.close()

    def has_position(self, external_event_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM positions WHERE external_event_id = ?", (external_event_id,)).fetchone() is not None

    def open_exposure(self) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(cash_used), 0) FROM positions WHERE status = 'OPEN'").fetchone()
        return f(row[0] if row else 0)

    def open_positions(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM positions WHERE status = 'OPEN' ORDER BY opened_at").fetchall()

    def observe(self, record: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO observations VALUES
            (:observation_key,:observed_at,:market_id,:condition_id,:external_event_id,:team,
             :fair_probability,:books,:odds_age_seconds,:best_bid,:best_ask,:all_in_price,
             :net_edge,:flow_notional,:flow_wallets,:decision,:reason)
            """, record
        )
        self.conn.commit()

    def open_position(self, position: dict[str, Any]) -> None:
        columns = ", ".join(position)
        placeholders = ", ".join(f":{key}" for key in position)
        with self.conn:
            self.conn.execute(f"INSERT INTO positions ({columns}) VALUES ({placeholders})", position)

    def resolve(self, position: sqlite3.Row, payout_multiple: float) -> None:
        payout = f(position["shares"]) * payout_multiple
        pnl = payout - f(position["cash_used"])
        with self.conn:
            self.conn.execute(
                "UPDATE positions SET status='RESOLVED', resolved_at=?, payout_usd=?, pnl_usd=? WHERE position_id=?",
                (iso_now(), payout, pnl, position["position_id"]),
            )

    def stats(self) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT COUNT(*) total,
              COALESCE(SUM(status='OPEN'),0) open_count,
              COALESCE(SUM(status='RESOLVED'),0) resolved,
              COALESCE(SUM(CASE WHEN status='OPEN' THEN cash_used END),0) open_cash,
              COALESCE(SUM(CASE WHEN status='RESOLVED' THEN cash_used END),0) resolved_cash,
              COALESCE(SUM(CASE WHEN status='RESOLVED' THEN pnl_usd END),0) pnl,
              COALESCE(SUM(CASE WHEN status='RESOLVED' AND pnl_usd>0 THEN 1 ELSE 0 END),0) wins,
              COALESCE(SUM(CASE WHEN status='RESOLVED' AND pnl_usd<=0 THEN 1 ELSE 0 END),0) losses
            FROM positions
            """
        ).fetchone()
        result = dict(row) if row else {}
        result["roi"] = f(result.get("pnl")) / f(result.get("resolved_cash")) if f(result.get("resolved_cash")) else 0.0
        return result


class Bot:
    def __init__(self, config: Config, ledger: Ledger):
        self.config, self.ledger = config, ledger
        self.http = Http(config)
        self.odds = OddsFeed(config, self.http)
        self.stop = False

    def request_stop(self, *_: Any) -> None:
        self.stop = True

    def mlb_markets(self) -> list[dict[str, Any]]:
        tag = self.http.get(GAMMA, "/tags/slug/mlb")
        tag_id = str(tag.get("id", "")) if isinstance(tag, dict) else ""
        if not tag_id:
            raise RuntimeError("could not resolve Polymarket MLB tag")
        payload = self.http.get(GAMMA, "/markets", {
            "tag_id": tag_id, "sports_market_types": "moneyline", "active": "true", "closed": "false", "limit": 500,
        })
        return [item for item in payload if isinstance(item, dict) and is_game_moneyline(item)] if isinstance(payload, list) else []

    def flow(self) -> dict[str, tuple[float, int]]:
        """Public flow is a context field, never an entry condition."""
        try:
            rows = self.http.get(DATA, "/trades", {
                "limit": 500, "filterType": "CASH", "filterAmount": int(self.config.flow_min_notional), "_": int(time.time() * 1000)
            })
        except Exception as exc:
            LOG.warning("flow tape unavailable: %s", exc)
            return {}
        result: dict[str, tuple[float, set[str]]] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or str(row.get("side", "")).upper() != "BUY":
                continue
            age = time.time() - (epoch(row.get("timestamp")) or 0)
            if age < 0 or age > self.config.flow_lookback_seconds:
                continue
            token = str(row.get("asset", ""))
            price, size = f(row.get("price")), f(row.get("size"))
            wallet = str(row.get("proxyWallet", ""))
            amount, wallets = result.get(token, (0.0, set()))
            wallets.add(wallet) if wallet else None
            result[token] = (amount + price * size, wallets)
        return {token: (round(amount, 6), len(wallets)) for token, (amount, wallets) in result.items()}

    def fill(self, asks: list[tuple[float, float]], budget: float, rate: float, max_all_in: float) -> Optional[dict[str, float]]:
        remaining = budget
        shares = gross = fees = friction = 0.0
        for price, displayed in sorted(asks):
            available = displayed * (1 - self.config.depth_haircut)
            per_share_fee = rate * price * (1 - price)
            all_in = price + per_share_fee + self.config.friction_per_share
            if all_in > max_all_in:
                break
            quantity = min(available, remaining / all_in)
            if quantity <= 0:
                continue
            shares += quantity
            gross += quantity * price
            fees += quantity * per_share_fee
            friction += quantity * self.config.friction_per_share
            remaining -= quantity * all_in
            if remaining < 0.001:
                break
        used = gross + fees + friction
        if shares <= 0 or used < budget * self.config.min_fill_fraction:
            return None
        return {
            "shares": shares, "cash_used": used, "entry_vwap": gross / shares,
            "all_in_price": used / shares, "fee_usd": fees, "friction_usd": friction,
        }

    def resolution(self, position: sqlite3.Row) -> Optional[float]:
        try:
            market = self.http.get(GAMMA, f"/markets/{position['market_id']}")
        except Exception as exc:
            LOG.warning("resolution fetch failed for %s: %s", position["market_id"], exc)
            return None
        closed = bool(market.get("closed")) or bool(market.get("resolved"))
        prices = [f(item, -1) for item in json_list(market.get("outcomePrices"))]
        ids = token_ids(market)
        if not closed or len(prices) != len(ids) or position["token_id"] not in ids:
            return None
        settled = prices[ids.index(position["token_id"])]
        return settled if 0 <= settled <= 1 else None

    def resolve_open(self) -> int:
        resolved = 0
        for position in self.ledger.open_positions():
            payout_multiple = self.resolution(position)
            if payout_multiple is None:
                continue
            self.ledger.resolve(position, payout_multiple)
            refreshed = self.ledger.conn.execute("SELECT * FROM positions WHERE position_id=?", (position["position_id"],)).fetchone()
            result = "WIN" if payout_multiple >= .99 else "LOSS" if payout_multiple <= .01 else "PARTIAL"
            LOG.info("RESOLVED %s | %s | PnL $%+.2f", result, position["team"], f(refreshed["pnl_usd"]))
            resolved += 1
        return resolved

    def observation(self, market: dict[str, Any], event: dict[str, Any], team: str, token: str, fair: float, books: int, odds_age: Optional[float], flow: tuple[float, int], **extra: Any) -> dict[str, Any]:
        bucket = int(time.time() // 300)
        return {
            "observation_key": f"{market.get('id')}:{token}:{bucket}", "observed_at": iso_now(),
            "market_id": str(market.get("id", "")), "condition_id": str(market.get("conditionId", "")),
            "external_event_id": str(event.get("id", "")), "team": team,
            "fair_probability": fair, "books": books, "odds_age_seconds": odds_age,
            "best_bid": extra.get("best_bid"), "best_ask": extra.get("best_ask"),
            "all_in_price": extra.get("all_in_price"), "net_edge": extra.get("net_edge"),
            "flow_notional": flow[0], "flow_wallets": flow[1],
            "decision": extra.get("decision", "SKIP"), "reason": extra.get("reason", "unknown"),
        }

    def scan(self) -> int:
        events = self.odds.refresh()
        markets = self.mlb_markets()
        flow = self.flow()
        taken = 0
        available = self.config.paper_bankroll_usd - self.ledger.open_exposure()
        now = time.time()
        for market in markets:
            event = match_event(market, events)
            if not event or not event.get("id"):
                continue
            if self.ledger.has_position(str(event["id"])):
                continue
            start = epoch(event.get("commence_time"))
            minutes = (start - now) / 60 if start else None
            if minutes is None or minutes < self.config.min_minutes_to_start or minutes > self.config.max_hours_to_start * 60:
                continue
            budget = min(self.config.paper_trade_usd, available)
            if budget < self.config.paper_trade_usd * self.config.min_fill_fraction:
                break
            candidates: list[dict[str, Any]] = []
            for team, token in zip(outcomes(market), token_ids(market)):
                fair, books, updated = self.odds.fair_probability(event, team)
                if fair is None:
                    continue
                odds_age = max(0.0, now - updated) if updated is not None else None
                token_flow = flow.get(token, (0.0, 0))
                if odds_age is None or odds_age > self.config.max_odds_age_seconds:
                    self.ledger.observe(self.observation(market, event, team, token, fair, books, odds_age, token_flow, reason="stale_odds"))
                    continue
                try:
                    book = self.http.get(CLOB, "/book", {"token_id": token})
                except Exception as exc:
                    LOG.warning("book fetch failed for %s: %s", market.get("id"), exc)
                    continue
                bids, asks = orderbook_levels(book, "bids"), orderbook_levels(book, "asks")
                if not bids or not asks:
                    self.ledger.observe(self.observation(market, event, team, token, fair, books, odds_age, token_flow, reason="empty_book"))
                    continue
                best_bid, best_ask = max(price for price, _ in bids), min(price for price, _ in asks)
                if best_ask <= best_bid:
                    self.ledger.observe(self.observation(market, event, team, token, fair, books, odds_age, token_flow, best_bid=best_bid, best_ask=best_ask, reason="crossed_book"))
                    continue
                filled = self.fill(asks, budget, fee_rate(market), fair - self.config.min_net_edge)
                if filled is None:
                    self.ledger.observe(self.observation(market, event, team, token, fair, books, odds_age, token_flow, best_bid=best_bid, best_ask=best_ask, reason="not_executable_at_edge"))
                    continue
                edge = fair - filled["all_in_price"]
                candidates.append({
                    "team": team, "token": token, "fair": fair, "books": books, "odds_age": odds_age,
                    "flow": token_flow, "best_bid": best_bid, "best_ask": best_ask, "filled": filled, "edge": edge,
                })
            if not candidates:
                continue
            choice = max(candidates, key=lambda item: item["edge"])
            for item in candidates:
                reason = "paper_fill" if item is choice else "lesser_edge_same_game"
                decision = "TAKEN" if item is choice else "SKIP"
                self.ledger.observe(self.observation(
                    market, event, item["team"], item["token"], item["fair"], item["books"], item["odds_age"], item["flow"],
                    best_bid=item["best_bid"], best_ask=item["best_ask"], all_in_price=item["filled"]["all_in_price"],
                    net_edge=item["edge"], decision=decision, reason=reason,
                ))
            position = {
                "position_id": hashlib.sha1(f"{event['id']}:{choice['token']}".encode()).hexdigest()[:20],
                "opened_at": iso_now(), "market_id": str(market.get("id")), "condition_id": str(market.get("conditionId")),
                "external_event_id": str(event["id"]), "market_slug": str(market.get("slug", "")), "team": choice["team"],
                "token_id": choice["token"], "fair_probability": choice["fair"], "books": choice["books"], "best_bid": choice["best_bid"],
                "entry_vwap": choice["filled"]["entry_vwap"], "all_in_price": choice["filled"]["all_in_price"], "net_edge": choice["edge"],
                "shares": choice["filled"]["shares"], "cash_used": choice["filled"]["cash_used"], "fee_usd": choice["filled"]["fee_usd"],
                "friction_usd": choice["filled"]["friction_usd"], "flow_notional": choice["flow"][0], "flow_wallets": choice["flow"][1],
            }
            try:
                self.ledger.open_position(position)
            except sqlite3.IntegrityError:
                continue
            available -= choice["filled"]["cash_used"]
            taken += 1
            LOG.info(
                "PAPER BUY %s | fair %.3f | all-in %.3f | edge %.3f | %d books | flow $%.0f/%d wallets | $%.2f",
                choice["team"], choice["fair"], choice["filled"]["all_in_price"], choice["edge"], choice["books"], choice["flow"][0], choice["flow"][1], choice["filled"]["cash_used"],
            )
        return taken

    def cycle(self) -> tuple[int, int]:
        resolved = self.resolve_open()
        taken = self.scan()
        stats = self.ledger.stats()
        LOG.info("ledger total=%d open=%d resolved=%d pnl=$%+.2f roi=%.2f%%", stats.get("total", 0), stats.get("open_count", 0), stats.get("resolved", 0), f(stats.get("pnl")), 100 * f(stats.get("roi")))
        return resolved, taken

    def run(self, once: bool) -> bool:
        successful = True
        while not self.stop:
            try:
                self.cycle()
            except Exception as exc:
                successful = False
                LOG.error("cycle failed: %s", exc)
            if once:
                return successful
            time.sleep(max(self.config.poll_seconds, 1))
        return successful


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-only MLB Polymarket price-discovery bot")
    parser.add_argument("--once", action="store_true", help="resolve and scan once")
    parser.add_argument("--stats", action="store_true", help="read the local ledger only")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
    config = Config.load()
    ledger = Ledger(config.db_path)
    if args.stats:
        print(json.dumps(ledger.stats(), indent=2, sort_keys=True))
        return 0
    if not config.odds_api_key:
        print("ODDS_API_KEY is required. Copy .env.example, set the key, and export the file.", file=sys.stderr)
        return 2
    bot = Bot(config, ledger)
    signal.signal(signal.SIGINT, bot.request_stop)
    signal.signal(signal.SIGTERM, bot.request_stop)
    try:
        return 0 if bot.run(args.once) else 1
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
