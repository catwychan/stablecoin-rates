#!/usr/bin/env python3
"""Fetch stablecoin lending rates -> rates.json and history.json (same folder).

Sources (all free, no key):
  Aave V3 Ethereum : api.v3.aave.com/graphql
  Aave V4 Ethereum : api.aave.com/graphql
  Morpho (Ethereum + Base) : api.morpho.org/graphql
  Venus BNB core pool : api.venus.io
  SparkLend Ethereum (Sky ecosystem) : spark-api.blockanalitica.com
  Kamino (Solana) : api.kamino.finance
  Fluid (Ethereum) : api.fluid.instadapp.io
  Jupiter Lend (Solana) : lite-api.jup.ag
  Binance VIP Loan : www.binance.com public bapi (optional source: skipped, not FAILED, when unreachable)
  Sky Savings Rate / stUSDS : info-sky.blockanalitica.com (+ yields.llama.fi/chart for SSR history)

rates.json   one row per market, `group` =
    totals      single markets by borrowed volume (pools/hubs >= $10M, Morpho BTC-collateral markets >= $50M), for the volume table
    totals_other  Morpho markets >= $50M with non-BTC collateral, for the second volume table
    aave_v3 / aave_v4 / spark / sky_savings / morpho_btc / morpho_top / venus   the per-platform tables
history.json one row per series per day: 90 days of daily average borrow APY.

If one source fails, its rows from the previous files are kept (with their old fetched_at).
"""
import json, os, sys, time, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT, HIST = os.path.join(HERE, "rates.json"), os.path.join(HERE, "history.json")
NOW_TS = int(time.time())
NOW = datetime.fromtimestamp(NOW_TS, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
DAYS = 90
START_DAY = datetime.fromtimestamp(NOW_TS - DAYS * 86400, timezone.utc).strftime("%Y-%m-%d")
UA = {"user-agent": "curl/8.4", "accept": "application/json"}

STABLES = {"USDT", "USDC", "DAI", "USDS", "GHO", "PYUSD", "RLUSD", "USDE", "USDG", "FRXUSD", "USD1",
           "FDUSD", "USDTB", "LUSD", "CRVUSD", "USD0", "USDA", "AUSD", "USDF", "DOLA", "FRAX", "USDP",
           "USR", "EUSD", "BOLD", "MUSD", "USDO", "USDT0", "USD₮0", "USD₮", "USDX", "USDD", "USDAT", "APXUSD", "JUPUSD"}
BTC_COLLATERAL = {"WBTC", "CBBTC", "KBTC", "LBTC", "TBTC", "XBTC", "ZBTC", "BTC"}
MIN_TABLE_USD = 1_000_000     # a market shows in a platform table above this borrowed amount
MIN_TOTAL_USD = 10_000_000    # a pool / hub shows in the volume table above this
MIN_MORPHO_TOTAL_USD = 50_000_000  # an isolated market with non-BTC collateral (Morpho, Jupiter vault, Kamino side market) shows above this
MIN_BTC_MARKET_USD = 5_000_000     # an isolated market with BTC collateral (Morpho, Jupiter vault) shows above this
VENUS_CORE = "0xfd36e2c2a6789db23113685031d7f16329158384"


def is_stable(sym):
    return sym.upper() in STABLES


def nice(sym):
    return {"USDT": "USDT", "USD₮": "USDT", "USD₮0": "USDT0"}.get(sym.upper(), sym)


def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90))


def gql(url, query):
    req = urllib.request.Request(url, data=json.dumps({"query": query}).encode(),
                                 headers={**UA, "content-type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=90))
    if r.get("errors"):
        raise RuntimeError(str(r["errors"])[:300])
    return r["data"]


def r2(v, n=2):
    return None if v is None else round(float(v), n)


def row(group, protocol, chain, market, asset, collateral, borrow, supply, borrowed, liquidity,
        util, source, borrow7d=None, lltv=None, headline=0, series=None):
    return {
        "id": f"{group}:{protocol}:{chain}:{market}".lower().replace(" ", "-"),
        "group": group, "protocol": protocol, "chain": chain, "market": market,
        "asset": asset, "collateral": collateral,
        "borrow_apy": r2(borrow), "supply_apy": r2(supply), "borrow_apy_7d": r2(borrow7d),
        "borrowed_musd": r2(None if borrowed is None else borrowed / 1e6, 1),
        "liquidity_musd": r2(None if liquidity is None else liquidity / 1e6, 1),
        "utilization_pct": r2(util, 1), "lltv_pct": r2(lltv, 1), "tvl_musd": None,
        "headline": headline, "series": series, "source": source, "fetched_at": NOW,
    }


def vol_row(platform, chain, asset, venue, rate, borrowed, liq, util, source, series, min_usd=None):
    """One market (one real borrow rate) for the volume table; None if it is too small."""
    if borrowed < (MIN_TOTAL_USD if min_usd is None else min_usd):
        return None
    r = row("totals", platform, chain, venue, asset, venue, rate, None, borrowed, liq, util, source, series=series)
    r["id"] = "totals:" + series
    return r


def hist_rows(series, label, platform, borrowed, points):
    """points: iterable of (YYYY-MM-DD, pct). Keeps the last DAYS days, one point per day."""
    by_day = {}
    for d, v in points:
        if v is not None and d >= START_DAY:
            by_day[d] = float(v)
    return [{"series": series, "label": label, "platform": platform, "borrowed_musd": r2(borrowed / 1e6, 1),
             "date": d, "borrow_apy": r2(v), "fetched_at": NOW} for d, v in sorted(by_day.items())]


def day(ts):
    return datetime.fromtimestamp(int(float(ts)), timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- Aave V3
def aave_v3():
    url = "https://api.v3.aave.com/graphql"
    q = """{ markets(request:{chainIds:[1]}){ name address reserves{ isFrozen isPaused
      underlyingToken{symbol address} supplyInfo{apy{value}}
      borrowInfo{ apy{value} total{usd} availableLiquidity{usd} utilizationRate{value} } } } }"""
    table, totals, hist = [], [], []
    for m in gql(url, q)["markets"]:
        inst = m["name"].replace("AaveV3Ethereum", "").replace("Lido", "Prime")
        platform = "Aave V3" + (f" {inst}" if inst else "")
        for x in m["reserves"]:
            sym, bi = x["underlyingToken"]["symbol"], x["borrowInfo"]
            if not bi or not is_stable(sym):
                continue
            sym = nice(sym)
            borrowed, liq = float(bi["total"]["usd"]), float(bi["availableLiquidity"]["usd"])
            rate, util = float(bi["apy"]["value"]) * 100, float(bi["utilizationRate"]["value"]) * 100
            series = f"{platform}:{sym}".lower().replace(" ", "-")
            if not inst and borrowed >= MIN_TABLE_USD:
                table.append(row("aave_v3", "Aave V3", "Ethereum", sym, sym, "池内任意抵押品", rate,
                                 float(x["supplyInfo"]["apy"]["value"]) * 100, borrowed, liq, util, "Aave API",
                                 headline=int(sym in ("USDT", "USDC")), series=series))
            t = vol_row(platform, "Ethereum", sym, f"Aave V3 · {inst or 'Core'}", rate, borrowed, liq, util,
                        "Aave API", series)
            if t:
                totals.append(t)
                hq = '{ borrowAPYHistory(request:{chainId:1, market:"%s", underlyingToken:"%s", window:LAST_SIX_MONTHS}){ date avgRate{value} } }' % (
                    m["address"], x["underlyingToken"]["address"])
                try:
                    pts = [(p["date"][:10], float(p["avgRate"]["value"]) * 100) for p in gql(url, hq)["borrowAPYHistory"]]
                    hist += hist_rows(series, f"{platform} · {sym}", platform, borrowed, pts)
                except Exception as e:
                    print(f"  history failed {series}: {e}")
    table.sort(key=lambda r: -(r["borrowed_musd"] or 0))
    if not table:
        raise RuntimeError("Aave V3 API returned no stablecoin reserves")
    return {"aave_v3": table}, totals, hist


# ---------------------------------------------------------------- Aave V4
def aave_v4():
    url = "https://api.aave.com/graphql"
    q = """{ reserves(request:{query:{chainIds:[1]}, filter:ALL, orderBy:{borrowApy:DESC}}){
      canBorrow asset{ id hub{name} underlying{info{symbol}}
        summary{ borrowApy{normalized} supplyApy{normalized} utilizationRate{normalized}
          borrowed{exchange(currency:USD){value}} availableLiquidity{exchange(currency:USD){value}} } } } }"""
    seen, table, totals, hist = set(), [], [], []
    for x in gql(url, q)["reserves"]:
        a = x["asset"]; sym = a["underlying"]["info"]["symbol"]; hub = a["hub"]["name"]
        if not x["canBorrow"] or (hub, sym) in seen or not is_stable(sym):
            continue
        seen.add((hub, sym))
        sym = nice(sym)
        s = a["summary"]
        borrowed, liq = float(s["borrowed"]["exchange"]["value"]), float(s["availableLiquidity"]["exchange"]["value"])
        rate, util = float(s["borrowApy"]["normalized"]), float(s["utilizationRate"]["normalized"])
        series = f"aave-v4:{hub}:{sym}".lower().replace(" ", "-")
        if borrowed >= MIN_TABLE_USD:
            table.append(row("aave_v4", "Aave V4", "Ethereum", f"{hub} · {sym}", sym, f"{hub} Hub", rate,
                             s["supplyApy"]["normalized"], borrowed, liq, util,
                             "Aave API", headline=int(hub == "Core" and sym in ("USDT", "USDC")), series=series))
        t = vol_row("Aave V4", "Ethereum", sym, f"Aave V4 · {hub}", rate, borrowed, liq, util, "Aave API", series)
        if t:
            totals.append(t)
            hq = '{ borrowApyHistory(request:{hubAsset:"%s", window:LAST_THREE_MONTHS, includeRewards:false}){ date avgRate{normalized} } }' % a["id"]
            try:
                pts = [(p["date"][:10], p["avgRate"]["normalized"]) for p in gql(url, hq)["borrowApyHistory"]]
                hist += hist_rows(series, f"Aave V4 · {hub} · {sym}", "Aave V4", borrowed, pts)
            except Exception as e:
                print(f"  history failed {series}: {e}")
    table.sort(key=lambda r: -(r["borrowed_musd"] or 0))
    if not table:
        raise RuntimeError("Aave V4 API returned no stablecoin reserves")
    return {"aave_v4": table}, totals, hist


# ---------------------------------------------------------------- Morpho
def morpho():
    url = "https://api.morpho.org/graphql"
    items, skip = [], 0
    while skip < 1000:
        q = """{ markets(first: 100, skip: %d, orderBy: BorrowAssetsUsd, orderDirection: Desc,
          where: {chainId_in: [1, 8453], listed: true}) { items { marketId lltv chain { id network }
            loanAsset { symbol } collateralAsset { symbol }
            state { borrowApy supplyApy weeklyBorrowApy borrowAssetsUsd liquidityAssetsUsd utilization } } } }""" % skip
        page = gql(url, q)["markets"]["items"]
        items += page
        if len(page) < 100 or ((page[-1]["state"] or {}).get("borrowAssetsUsd") or 0) < MIN_TABLE_USD:
            break
        skip += 100
    btc, other, seen, totals, want = [], [], set(), [], {}
    for m in items:
        s, loan, col = m["state"], m["loanAsset"]["symbol"], (m["collateralAsset"] or {}).get("symbol")
        if not s or not col or not is_stable(loan) or (s["borrowAssetsUsd"] or 0) < MIN_TABLE_USD:
            continue
        loan = nice(loan)
        chain = "Ethereum" if m["chain"]["id"] == 1 else "Base"
        name = f"{col} / {loan}"
        series = f"morpho:{m['chain']['id']}:{m['marketId'][:12]}"
        info = (s["borrowApy"] * 100, s["borrowAssetsUsd"], s["liquidityAssetsUsd"] or 0, name, chain, m, series)
        t = vol_row("Morpho", chain, loan, f"Morpho · {col} 抵押 · {chain}", info[0], info[1], info[2],
                    s["utilization"] * 100, "Morpho API", series,
                    MIN_BTC_MARKET_USD if col.upper() in BTC_COLLATERAL else MIN_MORPHO_TOTAL_USD)
        if t and (chain, name) not in seen:   # same pair again (other oracle / LLTV): keep only the largest
            if col.upper() not in BTC_COLLATERAL:
                t["group"] = "totals_other"   # non-BTC collateral: shown in its own volume table
            totals.append(t)
            want[series] = info       # markets that get a 90-day history
        if (chain, name) in seen:  # same pair again (other oracle / LLTV): the table keeps only the largest
            continue
        seen.add((chain, name))
        is_btc = col.upper() in BTC_COLLATERAL
        r = row("morpho_btc" if is_btc else "morpho_top", "Morpho", chain, name, loan, col,
                s["borrowApy"] * 100, s["supplyApy"] * 100, s["borrowAssetsUsd"], s["liquidityAssetsUsd"],
                s["utilization"] * 100, "Morpho API",
                borrow7d=None if s["weeklyBorrowApy"] is None else s["weeklyBorrowApy"] * 100,
                lltv=int(m["lltv"]) / 1e16, series=series)
        (btc if is_btc else other).append((r, info))
    if not btc:
        raise RuntimeError("Morpho API returned no BTC-backed markets")
    btc, other = btc[:10], other[:8]
    for r, info in btc[:4]:
        r["headline"] = 1
        want[info[6]] = info
    hist = []
    for series, info in want.items():
        m = info[5]
        hq = """{ markets(first: 1, where: {uniqueKey_in: ["%s"], chainId_in: [%d]}) { items { marketId
          historicalState { dailyBorrowApy(options:{startTimestamp:%d, endTimestamp:%d, interval:DAY}){x y} } } } }""" % (
            m["marketId"], m["chain"]["id"], NOW_TS - DAYS * 86400, NOW_TS)
        try:
            it = gql(url, hq)["markets"]["items"]
            if not it or it[0]["marketId"] != m["marketId"]:
                raise RuntimeError("market not returned")
            pts = [(day(p["x"]), None if p["y"] is None else p["y"] * 100) for p in it[0]["historicalState"]["dailyBorrowApy"]]
            hist += hist_rows(series, f"Morpho · {info[3]}" + ("" if info[4] == "Ethereum" else f"（{info[4]}）"),
                              "Morpho", info[1], pts)
        except Exception as e:
            print(f"  history failed {series}: {e}")
    return {"morpho_btc": [r for r, _ in btc], "morpho_top": [r for r, _ in other]}, totals, hist


# ---------------------------------------------------------------- Venus
def venus():
    table, totals, hist = [], [], []
    for x in get("https://api.venus.io/markets?chainId=56&limit=300")["result"]:
        sym = x["underlyingSymbol"]
        if (x.get("poolComptrollerAddress") or "").lower() != VENUS_CORE or not is_stable(sym):
            continue
        rate, borrowed, liq = float(x["borrowApy"]), float(x["totalBorrowCents"]) / 100, float(x["liquidityCents"]) / 100
        if rate <= 0 or borrowed < MIN_TABLE_USD:
            continue
        supplied = float(x["totalSupplyUnderlyingCents"]) / 100
        series = f"venus:{sym}".lower()
        table.append(row("venus", "Venus", "BNB Chain", sym, sym, "池内任意抵押品", rate, float(x["supplyApy"]),
                         borrowed, liq, 100 * borrowed / supplied if supplied else None, "Venus API",
                         headline=int(sym in ("USDT", "USDC")), series=series))
        t = vol_row("Venus", "BNB Chain", sym, "Venus · Core", rate, borrowed, liq,
                    100 * borrowed / supplied if supplied else None, "Venus API", series)
        if t:
            totals.append(t)
            try:
                data = get(f"https://api.venus.io/markets/history?asset={x['address']}&chainId=56")["result"]["data"]
                pts = [(day(p["blockTimestamp"]), float(p["borrowApy"])) for p in data]
                hist += hist_rows(series, f"Venus · {sym}", "Venus", borrowed, pts)
            except Exception as e:
                print(f"  history failed {series}: {e}")
    table.sort(key=lambda r: -(r["borrowed_musd"] or 0))
    if not table:
        raise RuntimeError("Venus API returned no stablecoin markets")
    return {"venus": table}, totals, hist


# ---------------------------------------------------------------- Sky + SparkLend
SSR_LLAMA_POOL = "d8c4eff5-c8a9-46fc-a888-057c4c668e72"   # sUSDS (Sky Savings Rate) on DefiLlama yields


def sky():
    base = "https://spark-api.blockanalitica.com/v1/ethereum/markets/"
    table, totals, hist = [], [], []
    for m in get(base + "?format=json"):
        sym = m["symbol"]
        borrowed = float(m["total_borrow_usd"] or 0)
        if not is_stable(sym) or not m.get("borrowing_enabled") or borrowed < MIN_TABLE_USD:
            continue
        sym_n = nice(sym)
        rate, liq = float(m["borrow_variable_apy"]) * 100, float(m["tvl_usd"] or 0)
        util = float(m["utilization_rate"]) * 100
        series = f"spark:{sym_n}".lower()
        table.append(row("spark", "Spark", "Ethereum", sym_n, sym_n, "池内任意抵押品", rate,
                         float(m["supply_apy"]) * 100, borrowed, liq, util, "Spark API (Block Analitica)",
                         headline=int(sym_n in ("USDT", "USDC", "USDS")), series=series))
        t = vol_row("Spark", "Ethereum", sym_n, "Spark · SparkLend", rate, borrowed, liq, util,
                    "Spark API (Block Analitica)", series)
        if t:
            totals.append(t)
            try:
                data = get(f"{base}{sym}/historic-details/?days_ago={DAYS}&format=json")
                pts = [(p["dt"][:10], float(p["borrow_apy"]) * 100) for p in data]
                hist += hist_rows(series, f"Spark · {sym_n}", "Spark", borrowed, pts)
            except Exception as e:
                print(f"  history failed {series}: {e}")
    table.sort(key=lambda r: -(r["borrowed_musd"] or 0))
    if not table:
        raise RuntimeError("Spark API returned no stablecoin markets")

    o = get("https://info-sky.blockanalitica.com/api/v1/overall/?format=json")[0]
    savings = []
    for name, rate, tvl, note in (
            ("Sky Savings Rate（sUSDS）", o["sky_savings_rate_apy"], o["sky_savings_rate_tvl"], "存入 USDS 拿 sUSDS"),
            ("stUSDS", o.get("stusds_rate"), o.get("stusds_tvl"), "为 SKY 质押借款提供资金，风险更高")):
        if rate is None:
            continue
        r = row("sky_savings", "Sky", "Ethereum", name, "USDS", note, None, float(rate) * 100, None, None, None,
                "Sky API (Block Analitica)")
        r["tvl_musd"] = r2(float(tvl) / 1e6, 1)
        r["id"] = "sky_savings:" + ("ssr" if name.startswith("Sky") else "stusds")
        savings.append(r)
    try:
        data = get(f"https://yields.llama.fi/chart/{SSR_LLAMA_POOL}")["data"]
        pts = [(p["timestamp"][:10], p["apyBase"] if p.get("apyBase") is not None else p.get("apy")) for p in data]
        hist += hist_rows("sky:ssr", "Sky 储蓄利率 SSR（存款利率）", "Sky", float(o["sky_savings_rate_tvl"]), pts)
    except Exception as e:
        print(f"  history failed sky:ssr: {e}")
    return {"spark": table, "sky_savings": savings}, totals, hist


# ---------------------------------------------------------------- Kamino (Solana)
KAMINO_BTC_MARKETS = ("SOL/BTC Market", "Bitcoin Market", "Zeus Bitcoin Market")   # pools that take BTC collateral


def kamino():
    base = "https://api.kamino.finance"
    table, totals, hist = [], [], []
    for mk in get(f"{base}/v2/kamino-market"):
        name, addr = mk["name"], mk["lendingMarket"]
        try:
            reserves = get(f"{base}/kamino-market/{addr}/reserves/metrics?env=mainnet-beta")
        except Exception as e:
            if mk.get("isPrimary"):
                raise
            print(f"  kamino market skipped {name}: {e}")
            continue
        best = {}
        for x in reserves:                       # a market can hold hidden duplicates of a token: keep the largest
            sym = x["liquidityToken"]
            if is_stable(sym) and float(x["totalBorrowUsd"] or 0) > float(best.get(sym, {}).get("totalBorrowUsd") or 0):
                best[sym] = x
        for sym, x in best.items():
            borrowed, supplied = float(x["totalBorrowUsd"]), float(x["totalSupplyUsd"] or 0)
            if borrowed < MIN_TABLE_USD:
                continue
            sym_n, rate = nice(sym), float(x["borrowApy"]) * 100
            liq, util = max(supplied - borrowed, 0), 100 * borrowed / supplied if supplied else None
            series = f"kamino:{addr[:6]}:{sym_n}".lower()
            is_btc = name in KAMINO_BTC_MARKETS
            table.append(row("kamino", "Kamino", "Solana", f"{sym_n} · {name}", sym_n, name, rate,
                             float(x["supplyApy"]) * 100, borrowed, liq, util, "Kamino API",
                             headline=int(bool(mk.get("isPrimary")) and sym_n == "USDC"), series=series))
            t = vol_row("Kamino", "Solana", sym_n, f"Kamino · {name}", rate, borrowed, liq, util, "Kamino API", series,
                        MIN_TOTAL_USD if is_btc else MIN_MORPHO_TOTAL_USD)
            if t:
                if not is_btc:
                    t["group"] = "totals_other"
                totals.append(t)
                try:
                    h = get(f"{base}/kamino-market/{addr}/reserves/{x['reserve']}/metrics/history?env=mainnet-beta"
                            f"&start={START_DAY}&end={NOW[:10]}&frequency=day")["history"]
                    pts = []
                    for p in h:
                        m = p.get("metrics") or {}
                        v = m.get("borrowInterestAPY", m.get("borrowApy"))
                        pts.append((p["timestamp"][:10], None if v is None else float(v) * 100))
                    hist += hist_rows(series, f"Kamino · {sym_n} · {name}", "Kamino", borrowed, pts)
                except Exception as e:
                    print(f"  history failed {series}: {e}")
    table.sort(key=lambda r: -(r["borrowed_musd"] or 0))
    if not table:
        raise RuntimeError("Kamino API returned no stablecoin reserves")
    return {"kamino": table[:14]}, totals, hist


# ---------------------------------------------------------------- Fluid (Ethereum)
def fluid():
    """Fluid sets one borrow rate per token at its Liquidity Layer; every vault borrowing that token pays it."""
    table, totals, hist = [], [], []
    vaults = get("https://api.fluid.instadapp.io/v2/1/vaults")
    cols = {}
    for v in vaults:
        b, sp = v["borrowToken"], v["supplyToken"]
        if b["token1"].get("symbol"):
            continue
        c = sp["token0"]["symbol"] + ("-" + sp["token1"]["symbol"] if sp["token1"].get("symbol") else "")
        amt = int(v["totalBorrow"]) / 10 ** int(b["token0"]["decimals"]) * float(b["token0"]["price"])
        cols.setdefault(b["token0"]["symbol"], []).append((amt, c))
    for t in get("https://api.fluid.instadapp.io/v2/liquidity/1/tokens"):
        sym = t["symbol"]
        if not is_stable(sym):
            continue
        scale = 10 ** int(t["decimals"]); price = float(t["price"])
        borrowed, supplied = int(t["totalBorrow"]) / scale * price, int(t["totalSupply"]) / scale * price
        if borrowed < MIN_TABLE_USD:
            continue
        sym_n, rate = nice(sym), int(t["borrowRate"]) / 100
        liq, util = max(supplied - borrowed, 0), int(t["lastStoredUtilization"]) / 100
        top = "、".join(c for _, c in sorted(cols.get(sym, []), reverse=True)[:4])
        series = f"fluid:{sym_n}".lower()
        table.append(row("fluid", "Fluid", "Ethereum", sym_n, sym_n, f"主要抵押品：{top}" if top else "各 Vault 共用",
                         rate, int(t["supplyRate"]) / 100, borrowed, liq, util, "Fluid API",
                         headline=int(sym_n in ("USDT", "USDC")), series=series))
        v = vol_row("Fluid", "Ethereum", sym_n, "Fluid · Ethereum", rate, borrowed, liq, util, "Fluid API", series)
        if v:
            totals.append(v)
            try:   # hourly points for a year -> daily averages
                data = get(f"https://api.fluid.instadapp.io/v2/liquidity/1/tokens/{t['address']}/apr-history?range=1y")
                days = {}
                for p in data:
                    days.setdefault(day(p["timestamp"]), []).append(int(p["borrowApr"]) / 100)
                pts = [(d, sum(v2) / len(v2)) for d, v2 in days.items()]
                hist += hist_rows(series, f"Fluid · {sym_n}", "Fluid", borrowed, pts)
            except Exception as e:
                print(f"  history failed {series}: {e}")
    table.sort(key=lambda r: -(r["borrowed_musd"] or 0))
    if not table:
        raise RuntimeError("Fluid API returned no stablecoin tokens")
    return {"fluid": table}, totals, hist


# ---------------------------------------------------------------- Jupiter Lend (Solana)
def jupiter():
    table, totals = [], []
    for v in get("https://lite-api.jup.ag/lend/v1/borrow/vaults"):
        b, sp = v["borrowToken"], v["supplyToken"]
        sym = b["symbol"]
        if not is_stable(sym):
            continue
        scale = 10 ** int(b["decimals"]); price = float(b["price"])
        borrowed = int(v["totalBorrow"]) / scale * price
        if borrowed < MIN_TABLE_USD:
            continue
        col = sp.get("uiSymbol") or sp["symbol"]
        sym_n, rate = nice(b.get("uiSymbol") or sym), int(v["borrowRate"]) / 100
        liq = int(v["borrowable"]) / scale * price
        series = f"jupiter:{v['id']}"
        is_btc = sp["symbol"].upper() in BTC_COLLATERAL
        r = row("jupiter", "Jupiter Lend", "Solana", f"{col} / {sym_n}", sym_n, col, rate, None, borrowed, liq, None,
                "Jupiter Lend API", lltv=int(v["liquidationThreshold"]) / 10, series=series)
        r["id"] = f"jupiter:{v['id']}"
        table.append(r)
        t = vol_row("Jupiter Lend", "Solana", sym_n, f"Jupiter Lend · {col} 抵押", rate, borrowed, liq, None,
                    "Jupiter Lend API", series, MIN_BTC_MARKET_USD if is_btc else MIN_MORPHO_TOTAL_USD)
        if t:
            if not is_btc:
                t["group"] = "totals_other"
            totals.append(t)
    table.sort(key=lambda r: -(r["borrowed_musd"] or 0))
    if not table:
        raise RuntimeError("Jupiter Lend API returned no stablecoin vaults")
    return {"jupiter": table[:14]}, totals, []


# ---------------------------------------------------------------- Binance VIP Loan (CeFi)
BINANCE_COINS = ("USDT", "USDC", "USD1", "FDUSD")
BINANCE_MAIN = ("USDT", "USDC")          # shown in the volume table and the 90-day chart


def binance():
    """Published flexible VIP Loan rate (simple APR, accrues hourly). Binance does not publish loan volumes."""
    url = "https://www.binance.com/bapi/margin/v1/public/collateral/vip-loan/hourlyInterestRates"
    hdr = {"content-type": "application/json", "accept": "application/json", "clienttype": "web",
           "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
    table, totals, hist = [], [], []
    for coin in BINANCE_COINS:
        body = json.dumps({"coin": coin, "startTime": (NOW_TS - (DAYS + 1) * 86400) * 1000, "endTime": NOW_TS * 1000}).encode()
        r = json.load(urllib.request.urlopen(urllib.request.Request(url, data=body, headers=hdr), timeout=60))
        data = r.get("data") or []
        if not data:
            if coin in BINANCE_MAIN:
                raise RuntimeError(f"no data for {coin}: {str(r)[:120]}")
            continue
        pts = sorted((int(p["dateTimestamp"]), float(p["annualInterestRate"]) * 100) for p in data)
        rate = pts[-1][1]
        week = [v for t, v in pts if t >= (NOW_TS - 7 * 86400) * 1000]
        series = f"binance:{coin}".lower()
        main = coin in BINANCE_MAIN
        r0 = row("binance", "Binance VIP Loan", "CeFi", coin, coin, "浮动利率，可用 BTC、ETH 等抵押，仅 VIP 用户", rate, None,
                 None, None, None, "Binance", borrow7d=sum(week) / len(week) if week else None,
                 headline=int(main), series=series)
        table.append(r0)
        if main:
            t = dict(r0, group="totals", market="Binance VIP Loan", id="totals:" + series, headline=0)
            totals.append(t)
            days = {}
            for ts, v in pts:
                days.setdefault(day(ts / 1000), []).append(v)
            hist += hist_rows(series, f"Binance VIP Loan · {coin}", "Binance VIP Loan", 0,
                              [(d, sum(v) / len(v)) for d, v in days.items()])
    return {"binance": table}, totals, hist


OPTIONAL_SOURCES = {"Binance VIP Loan"}   # unreachable from some networks; never blocks a refresh

PLATFORM_OF = {"Aave V3": "Aave V3", "Aave V4": "Aave V4", "Morpho": "Morpho", "Venus": "Venus"}


def source_of(protocol):
    if protocol in ("Spark", "Sky"):
        return "Sky"
    for k in PLATFORM_OF:
        if protocol == k or (k == "Aave V3" and protocol.startswith("Aave V3")):
            return k
    return protocol


def load(path):
    try:
        return json.load(open(path))
    except Exception:
        return []


def main():
    old, old_hist = load(OUT), load(HIST)
    groups, totals, hist, failed, skipped = {}, [], [], [], []
    for name, fn, table_groups in (("Aave V3", aave_v3, ["aave_v3"]), ("Aave V4", aave_v4, ["aave_v4"]),
                                   ("Morpho", morpho, ["morpho_btc", "morpho_top"]), ("Venus", venus, ["venus"]), ("Sky", sky, ["spark", "sky_savings"]), ("Kamino", kamino, ["kamino"]),
                                   ("Fluid", fluid, ["fluid"]), ("Jupiter Lend", jupiter, ["jupiter"]),
                                   ("Binance VIP Loan", binance, ["binance"])):
        try:
            g, t, h = fn()
            groups.update(g)
        except Exception as e:  # keep the previous rows for this source
            (skipped if name in OPTIONAL_SOURCES else failed).append(f"{name}: {e}")
            for tg in table_groups:
                groups[tg] = [r for r in old if r.get("group") == tg]
            t = [r for r in old if r.get("group") in ("totals", "totals_other") and source_of(r.get("protocol", "")) == name]
            h = []
        if not h:  # history call(s) failed: keep what we had for this platform
            h = [r for r in old_hist if source_of(r.get("platform", "")) == name]
        totals += t
        hist += h
    totals.sort(key=lambda r: -(r["borrowed_musd"] or 0))
    rows = totals + sum((groups.get(g, []) for g in ["aave_v3", "aave_v4", "spark", "sky_savings", "morpho_btc", "morpho_top", "venus",
                                                                 "kamino", "fluid", "jupiter", "binance"]), [])
    if not rows:
        sys.exit("all sources failed: " + "; ".join(failed))
    # history panels go in order of borrowed volume
    size = {}
    for r in hist:
        size[r["series"]] = max(size.get(r["series"], 0), r["borrowed_musd"] or 0)
    rank = {s: i + 1 for i, s in enumerate(sorted(size, key=lambda s: -size[s]))}
    for r in hist:
        r["order"] = rank[r["series"]]
    hist.sort(key=lambda r: (r["order"], r["date"]))
    json.dump(rows, open(OUT, "w"), ensure_ascii=False, indent=1)
    json.dump(hist, open(HIST, "w"), ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {len(rows)} rows to {OUT} and {len(hist)} rows ({len(rank)} series) to {HIST} at {NOW}")
    for g in ["totals", "totals_other", "aave_v3", "aave_v4", "spark", "sky_savings", "morpho_btc", "morpho_top", "venus",
              "kamino", "fluid", "jupiter", "binance"]:
        print(f"  {g}: {sum(1 for r in rows if r['group'] == g)}")
    if failed:
        print("FAILED (kept old rows): " + "; ".join(failed))
    if skipped:
        print("SKIPPED optional source (old rows kept if any): " + "; ".join(skipped))


if __name__ == "__main__":
    main()
