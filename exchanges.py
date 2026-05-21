import time
import requests
from datetime import datetime, timezone
from config import SUPABASE_URL, SUPABASE_KEY

def phemex_fetch(coin, start_ms, end_ms):
    candidates = [f".{coin}FR8H"] if coin.endswith("USDT") or coin.endswith("USD") else [f".{coin}USDTFR8H", f".{coin}USDFR8H"]
    last_err = None
    for sym in candidates:
        try:
            url = "https://api.phemex.com/api-data/public/data/funding-rate-history"
            params = {"symbol": sym, "start": start_ms, "end": end_ms, "limit": 1000}
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0: raise ValueError(data.get("msg"))
            rows = [x for x in data.get("data", {}).get("rows", []) if x["fundingTime"] >= start_ms and abs(float(x["fundingRate"])) < 0.01]
            if rows: return [(x["fundingTime"], float(x["fundingRate"]) * 100) for x in rows], sym
        except Exception as e:
            last_err = str(e)
        time.sleep(0.15)
    return [], last_err

def xt_fetch(coin, start_ms, end_ms):
    sym = coin.lower() if coin.endswith("USDT") else (coin.lower() + "t" if coin.endswith("USD") else f"{coin.lower()}_usdt")
    try:
        url = "https://fapi.xt.com/future/market/v1/public/q/funding-rate-record"
        r = requests.get(url, params={"symbol": sym, "limit": 500, "direction": "NEXT"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("returnCode") != 0: raise ValueError("API error")
        items = data.get("result", [])
        if isinstance(items, dict): items = items.get("items", [])
        filtered = [(x.get("createdTime") or x.get("settleTime") or x.get("fundingTime") or 0, float(x.get("fundingRate", 0)) * 100) for x in items]
        filtered = [x for x in filtered if x[0] >= start_ms]
        if filtered: return filtered, sym
    except Exception as e: return [], str(e)
    return [], "Нет данных"

def toobit_fetch(coin, start_ms, end_ms):
    sym = f"{coin[:-4]}-SWAP-USDT" if coin.endswith("USDT") else (f"{coin[:-3]}-SWAP-USDT" if coin.endswith("USD") else f"{coin}-SWAP-USDT")
    try:
        url = "https://api.toobit.com/api/v1/futures/historyFundingRate"
        r = requests.get(url, params={"symbol": sym, "limit": 1000}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list): raise ValueError("Format error")
        filtered = [(int(x.get("settleTime", 0)), float(x.get("settleRate", 0)) * 100) for x in data]
        filtered = [x for x in filtered if x[0] >= start_ms]
        if filtered: return filtered, sym
    except Exception as e: return [], str(e)
    return [], "Нет данных"

def okx_fetch(coin, start_ms, end_ms):
    sym = f"{coin[:-4]}-USDT-SWAP" if coin.endswith("USDT") else (f"{coin[:-3]}-USDT-SWAP" if coin.endswith("USD") else f"{coin}-USDT-SWAP")
    try:
        url = "https://www.okx.com/api/v5/public/funding-rate-history"
        r = requests.get(url, params={"instId": sym, "limit": 100}, timeout=10)
        if r.status_code in (451, 403): return [], "OKX заблокирован"
        r.raise_for_status()
        items = r.json().get("data", [])
        filtered = [(int(x.get("fundingTime", 0)), float(x.get("fundingRate", 0)) * 100) for x in items]
        filtered = [x for x in filtered if x[0] >= start_ms]
        if filtered: return filtered, sym
    except Exception as e: return [], str(e)
    return [], "Нет данных"

def bingx_fetch(coin, start_ms, end_ms):
    sym = coin[:-4] + "-USDT" if coin.endswith("USDT") else (coin[:-3] + "-USDT" if coin.endswith("USD") else f"{coin}-USDT")
    try:
        url = "https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate"
        r = requests.get(url, params={"symbol": sym, "limit": 1000}, timeout=10)
        if r.status_code in (451, 403): return [], "BingX заблокирован"
        r.raise_for_status()
        items = r.json().get("data", [])
        filtered = [(int(x.get("fundingTime", 0)), float(x.get("fundingRate", 0)) * 100) for x in items]
        filtered = [x for x in filtered if x[0] >= start_ms]
        if filtered: return filtered, sym
    except Exception as e: return [], str(e)
    return [], "Нет данных"

def kucoin_fetch(coin, start_ms, end_ms):
    sym = coin + "M" if coin.endswith("USDT") else (coin + "TM" if coin.endswith("USD") else f"{coin}USDTM")
    try:
        url = "https://api-futures.kucoin.com/api/v1/contract/funding-rates"
        r = requests.get(url, params={"symbol": sym, "from": start_ms, "to": end_ms}, timeout=10)
        if r.status_code in (451, 403): return [], "KuCoin заблокирован"
        r.raise_for_status()
        items = r.json().get("data", [])
        filtered = [(int(x.get("timepoint", 0)), float(x.get("fundingRate", 0)) * 100) for x in items]
        if filtered: return filtered, sym
    except Exception as e: return [], str(e)
    return [], "Нет данных"

def gate_fetch(coin, start_ms, end_ms):
    sym = coin if coin.endswith("USDT") else (coin + "T" if coin.endswith("USD") else f"{coin}_USDT")
    try:
        url = "https://api.gateio.ws/api/v4/futures/usdt/funding_rate"
        r = requests.get(url, params={"contract": sym, "from": start_ms // 1000, "to": end_ms // 1000, "limit": 1000}, timeout=10)
        r.raise_for_status()
        data = r.json()
        filtered = [(int(x.get("t", 0)) * 1000, float(x.get("r", 0)) * 100) for x in data]
        filtered = [x for x in filtered if x[0] >= start_ms]
        if filtered: return filtered, sym
    except Exception as e: return [], str(e)
    return [], "Нет данных"

def blofin_fetch(coin, start_ms, end_ms):
    sym = coin[:-4] + "-USDT" if coin.endswith("USDT") else (coin[:-3] + "-USDT" if coin.endswith("USD") else f"{coin}-USDT")
    try:
        url = "https://openapi.blofin.com/api/v1/market/funding-rate-history"
        r = requests.get(url, params={"instId": sym, "limit": 100}, timeout=10)
        r.raise_for_status()
        items = r.json().get("data", [])
        filtered = [(int(x["fundingTime"]), float(x["fundingRate"]) * 100) for x in items if int(x["fundingTime"]) >= start_ms]
        if filtered: return filtered, sym
    except Exception as e: return [], str(e)
    return [], "Нет данных"

def weex_fetch(coin, start_ms, end_ms):
    sym = coin if coin.endswith("USDT") else (coin + "T" if coin.endswith("USD") else f"{coin}USDT")
    SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
    all_rows, chunk_start = [], start_ms
    try:
        while chunk_start < end_ms:
            chunk_end = min(chunk_start + SEVEN_DAYS_MS, end_ms)
            r = requests.get("https://api-contract.weex.com/capi/v3/market/fundingRate", params={"symbol": sym, "startTime": chunk_start, "endTime": chunk_end, "limit": 1000}, timeout=10)
            r.raise_for_status()
            data = r.json()
            items = data if isinstance(data, list) else data.get("data", [])
            all_rows.extend(items)
            chunk_start = chunk_end + 1
            time.sleep(0.1)
        filtered = [(int(x.get("fundingTime", 0)), float(x.get("fundingRate", 0)) * 100) for x in all_rows]
        filtered = sorted(list(set([x for x in filtered if start_ms <= x[0] <= end_ms])), key=lambda k: k[0])
        if filtered: return filtered, sym
    except Exception as e: return [], str(e)
    return [], "Нет данных"

def zoomex_fetch(coin, start_ms, end_ms):
    sym = f"{coin.upper()}USDT"
    url, all_rows, cursor = "https://openapi.zoomex.com/cloud/trade/v3/market/funding/history", [], None
    try:
        while True:
            params = {"category": "linear", "symbol": sym, "limit": 200}
            if cursor: params["cursor"] = cursor
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            rows = data.get("result", {}).get("list", [])
            if not rows: break
            for row in rows:
                ts = int(row["fundingRateTimestamp"])
                if ts < start_ms: return sorted(all_rows, key=lambda x: x[0]), sym
                if ts <= end_ms: all_rows.append((ts, float(row["fundingRate"]) * 100))
            cursor = data.get("result", {}).get("nextPageCursor")
            if not cursor: break
            time.sleep(0.1)
        return sorted(all_rows, key=lambda x: x[0]), sym
    except Exception as e: return [], str(e)

def coinw_fetch(coin, start_ms, end_ms):
    if not SUPABASE_URL or not SUPABASE_KEY: return [], "Supabase не задан"
    symbol = coin.upper()
    start_iso = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat()
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/funding_rates", headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, params={"symbol": f"eq.{symbol}", "collected_at": f"gte.{start_iso}", "order": "funding_time.asc", "limit": "1000", "select": "rate_pct,collected_at,funding_time"}, timeout=10)
        r.raise_for_status()
        rows, seen, result = r.json(), set(), []
        for row in rows:
            ft = row.get("funding_time")
            if ft:
                if ft in seen: continue
                seen.add(ft)
                dt = datetime.fromisoformat(ft.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(row["collected_at"].replace("Z", "+00:00"))
            result.append((int(dt.timestamp() * 1000), float(row["rate_pct"])))
        return result, f"coinw_{symbol}"
    except Exception as e: return [], str(e)

def phemex_get_all_symbols():
    r = requests.get("https://api.phemex.com/exchange/public/cfg/v2/products", timeout=15)
    r.raise_for_status()
    coins, seen = [], set()
    for p in r.json().get("data", {}).get("products", []):
        if p.get("type") == "PerpetualV2" and p.get("quoteCurrency") == "USDT" and p.get("status") == "Listed":
            sym = p.get("symbol", "")
            if sym.endswith("USDT") and sym[:-4] not in seen:
                coins.append(sym[:-4])
                seen.add(sym[:-4])
    return coins

EXCHANGE_FETCHERS = {
    "phemex": phemex_fetch, "xt": xt_fetch, "toobit": toobit_fetch, "okx": okx_fetch,
    "bingx": bingx_fetch, "kucoin": kucoin_fetch, "gate": gate_fetch, "blofin": blofin_fetch,
    "weex": weex_fetch, "coinw": coinw_fetch, "zoomex": zoomex_fetch,
}