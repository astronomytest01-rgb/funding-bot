from __future__ import annotations

import asyncio
import os
import secrets
import time
from dataclasses import dataclass

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ai import gemini_generate, get_last_gemini_error
from analysis import calc_std
from config import EXCHANGES_ENABLED, GEMINI_API_KEY, REPORT_CHAT_ID, TEMPORARILY_DISABLED_EXCHANGES
from exchanges import EXCHANGE_FETCHERS, EXCHANGE_LABELS, EXCHANGE_SYMBOL_FETCHERS, phemex_get_all_symbols
from oi import COINGECKO_DERIVATIVE_IDS


LONGTERM_DAYS = int(os.getenv("LONGTERM_DAYS", "7"))
LONGTERM_AMOUNT_PER_LEG = float(os.getenv("LONGTERM_AMOUNT_PER_LEG", "20000"))
LONGTERM_MIN_MONTHLY_USD = float(os.getenv("LONGTERM_MIN_MONTHLY_USD", "300"))
LONGTERM_SYMBOL_LIMIT = int(os.getenv("LONGTERM_SYMBOL_LIMIT", "180"))
LONGTERM_TOP_COINS = int(os.getenv("LONGTERM_TOP_COINS", "60"))
LONGTERM_PAGE_SIZE = int(os.getenv("LONGTERM_PAGE_SIZE", "5"))
LONGTERM_VARIANTS_PER_COIN = int(os.getenv("LONGTERM_VARIANTS_PER_COIN", "4"))
LONGTERM_GEMINI_FILTER = os.getenv("LONGTERM_GEMINI_FILTER", "1").lower() not in ("0", "false", "off", "no")
LONGTERM_GEMINI_CHUNK_SIZE = int(os.getenv("LONGTERM_GEMINI_CHUNK_SIZE", "15"))
LONGTERM_MIN_OI_WARN_USD = float(os.getenv("LONGTERM_MIN_OI_WARN_USD", "1000000"))
LONGTERM_MIN_VOL_WARN_USD = float(os.getenv("LONGTERM_MIN_VOL_WARN_USD", "400000"))
LONGTERM_EXAMPLE_SYMBOLS = ("BCH", "XMR", "AMZNX", "ATH")

_cg_market_cache = {}
_cg_market_cache_ts = {}
_CG_CACHE_TTL = 15 * 60
_longterm_sessions = {}
_SESSION_TTL = 30 * 60

LONGTERM_GEMINI_PROMPT = """Действуй как риск-менеджер криптофонда для долгосрочной delta-neutral funding стратегии.
Бот уже посчитал funding. Твоя задача НЕ пересчитывать funding, а отфильтровать только качество актива.

Нужно оставить только монеты, в которые теоретически можно заходить на долгосрок с плечом x3-x4 и не бояться типичного пампа/дампа 30-50% за день.
Исключай мемкоины, микрокапы, хайповые щиткоины, активы без устойчивой капитализации/инфраструктуры, токены с высоким риском скама, манипуляций или резких свечей.
Приоритет: крупные/понятные активы, устойчивая ликвидность, серьёзная инфраструктура, меньше риск внезапной новости/делистинга/манипуляции.

Список кандидатов после funding-математики:
{coins_list}

Верни ТОЛЬКО монеты, которые стоит оставить для ручной проверки.
Формат каждой строки строго:
KEEP TICKER — причина 3-8 слов

Если ничего не подходит, напиши:
NO_KEEPERS"""


@dataclass(frozen=True)
class LongTermLeg:
    exchange: str
    rates: tuple[float, ...]
    avg: float
    std: float
    latest: float
    payments_per_day: float
    oi_usd: float | None
    volume_24h_usd: float | None


@dataclass(frozen=True)
class LongTermPair:
    symbol: str
    long: LongTermLeg
    short: LongTermLeg
    net_per_payment_pct: float
    daily_usd: float
    monthly_usd: float
    score: float


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _normalize_coin(symbol):
    raw = (symbol or "").upper().replace("-", "").replace("_", "").replace("/", "")
    raw = raw.replace("PERP", "")
    for suffix in ("USDTM", "USDT", "USD"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    return "BTC" if raw == "XBT" else raw


def _usd_value(value):
    if isinstance(value, dict):
        value = value.get("usd")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ticker_usd(ticker, keys):
    for key in keys:
        value = _usd_value(ticker.get(key))
        if value is not None:
            return value
    return None


def _fetch_cg_market(exchange):
    exchange = exchange.lower()
    now = time.time()
    if exchange in _cg_market_cache and now - _cg_market_cache_ts.get(exchange, 0) < _CG_CACHE_TTL:
        return _cg_market_cache[exchange]
    cg_id = COINGECKO_DERIVATIVE_IDS.get(exchange)
    if not cg_id:
        return _cg_market_cache.get(exchange, {})
    try:
        url = f"https://api.coingecko.com/api/v3/derivatives/exchanges/{cg_id}"
        r = requests.get(url, params={"include_tickers": "all"}, timeout=10)
        if r.status_code == 429:
            return _cg_market_cache.get(exchange, {})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return _cg_market_cache.get(exchange, {})

    result = {}
    for ticker in data.get("tickers") or []:
        target = str(ticker.get("target") or ticker.get("quote") or "").upper()
        if target not in ("USDT", "USD", ""):
            continue
        oi = _ticker_usd(ticker, ("open_interest_usd", "open_interest", "converted_open_interest_usd"))
        volume = _ticker_usd(ticker, ("converted_volume", "h24_volume"))
        coins = {
            _normalize_coin(str(ticker.get("base") or "")),
            _normalize_coin(str(ticker.get("symbol") or "")),
        }
        for coin in coins:
            if not coin:
                continue
            old_oi, old_volume = result.get(coin, (None, None))
            result[coin] = (
                max(x for x in (old_oi, oi) if x is not None) if old_oi is not None or oi is not None else None,
                max(x for x in (old_volume, volume) if x is not None) if old_volume is not None or volume is not None else None,
            )
    _cg_market_cache[exchange] = result
    _cg_market_cache_ts[exchange] = now
    return result


def _market_meta(exchange, coin):
    return _fetch_cg_market(exchange).get(_normalize_coin(coin), (None, None))


def _fmt_usd(value):
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    return f"${value / 1_000:.0f}K"


def _liquidity_warnings(pair):
    warnings = []
    for leg in (pair.long, pair.short):
        label = EXCHANGE_LABELS.get(leg.exchange, leg.exchange.upper())
        if leg.oi_usd is None:
            warnings.append(f"{label} OI n/a")
        elif leg.oi_usd < LONGTERM_MIN_OI_WARN_USD:
            warnings.append(f"{label} OI {_fmt_usd(leg.oi_usd)}")
        if leg.volume_24h_usd is None:
            warnings.append(f"{label} vol n/a")
        elif leg.volume_24h_usd < LONGTERM_MIN_VOL_WARN_USD:
            warnings.append(f"{label} vol {_fmt_usd(leg.volume_24h_usd)}")
    return warnings[:4]


def _direction_quality(rates, side):
    income_rates = tuple((-r if side == "LONG" else r) for r in rates)
    positive_ratio = sum(1 for r in income_rates if r >= 0) / len(income_rates)
    recent = income_rates[-min(6, len(income_rates)) :]
    return positive_ratio, _mean(recent)


def _clean_rates(rows):
    rates = []
    for _ts, rate in rows:
        try:
            value = float(rate)
        except (TypeError, ValueError):
            continue
        if abs(value) <= 1.0:
            rates.append(value)
    return tuple(rates)


def _leg_from_rows(exchange, symbol, rows, days):
    rates = _clean_rates(rows)
    if len(rates) < 6:
        return None
    oi_usd, volume_24h_usd = _market_meta(exchange, symbol)
    return LongTermLeg(
        exchange=exchange,
        rates=rates,
        avg=_mean(rates),
        std=calc_std(rates),
        latest=rates[-1],
        payments_per_day=len(rates) / days,
        oi_usd=oi_usd,
        volume_24h_usd=volume_24h_usd,
    )


def _pair_candidate(symbol, long_leg, short_leg):
    long_positive_ratio, long_recent = _direction_quality(long_leg.rates, "LONG")
    short_positive_ratio, short_recent = _direction_quality(short_leg.rates, "SHORT")
    net_per_payment_pct = -long_leg.avg + short_leg.avg
    recent_net_pct = long_recent + short_recent
    daily_usd = (
        LONGTERM_AMOUNT_PER_LEG * (-long_leg.avg) / 100 * long_leg.payments_per_day
        + LONGTERM_AMOUNT_PER_LEG * short_leg.avg / 100 * short_leg.payments_per_day
    )
    monthly_usd = daily_usd * 30
    if monthly_usd < LONGTERM_MIN_MONTHLY_USD:
        return None
    if net_per_payment_pct <= 0 or recent_net_pct <= 0:
        return None
    if long_positive_ratio < 0.55 and short_positive_ratio < 0.55:
        return None
    stability_penalty = long_leg.std + short_leg.std
    direction_bonus = long_positive_ratio + short_positive_ratio
    score = monthly_usd + direction_bonus * 100 - stability_penalty * 800
    return LongTermPair(symbol, long_leg, short_leg, net_per_payment_pct, daily_usd, monthly_usd, score)


def _symbol_universe(active_exchanges):
    counts = {}
    for exchange in active_exchanges:
        fetcher = EXCHANGE_SYMBOL_FETCHERS.get(exchange)
        try:
            symbols = fetcher() if fetcher else phemex_get_all_symbols()
        except Exception:
            continue
        for symbol in symbols:
            coin = _normalize_coin(symbol)
            if coin:
                counts[coin] = counts.get(coin, 0) + 1
    for symbol in LONGTERM_EXAMPLE_SYMBOLS:
        counts[symbol] = max(counts.get(symbol, 0), 99)
    ordered = sorted(counts, key=lambda coin: (-counts[coin], coin))
    if LONGTERM_SYMBOL_LIMIT > 0:
        return ordered[:LONGTERM_SYMBOL_LIMIT]
    return ordered


def scan_longterm_funding(days=LONGTERM_DAYS, active_exchanges=None):
    active = active_exchanges or [
        exchange for exchange, fetcher in EXCHANGE_FETCHERS.items()
        if fetcher and EXCHANGES_ENABLED.get(exchange, False) and exchange not in TEMPORARILY_DISABLED_EXCHANGES
    ]
    active = [exchange for exchange in active if exchange in EXCHANGE_FETCHERS]
    for exchange in active:
        _fetch_cg_market(exchange)

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * 60 * 1000
    symbols = _symbol_universe(active)
    pairs = []
    errors = {}

    for symbol in symbols:
        legs = []
        for exchange in active:
            fetcher = EXCHANGE_FETCHERS.get(exchange)
            if not fetcher:
                continue
            try:
                rows, _sym = fetcher(symbol, start_ms, now_ms)
            except Exception as exc:
                errors[f"{exchange}:{symbol}"] = str(exc)
                rows = []
            leg = _leg_from_rows(exchange, symbol, rows, days) if rows else None
            if leg:
                legs.append(leg)
            time.sleep(0.04)
        for long_leg in legs:
            for short_leg in legs:
                if long_leg.exchange == short_leg.exchange:
                    continue
                pair = _pair_candidate(symbol, long_leg, short_leg)
                if pair:
                    pairs.append(pair)

    best_by_route = {}
    for pair in pairs:
        key = (pair.symbol, pair.long.exchange, pair.short.exchange)
        old = best_by_route.get(key)
        if old is None or pair.score > old.score:
            best_by_route[key] = pair
    ranked = sorted(best_by_route.values(), key=lambda item: -item.score)
    grouped = {}
    for pair in ranked:
        grouped.setdefault(pair.symbol, []).append(pair)
    top_symbols = sorted(grouped, key=lambda symbol: -grouped[symbol][0].score)
    return {
        "days": days,
        "symbols_scanned": len(symbols),
        "pairs_found": len(ranked),
        "groups": [(symbol, grouped[symbol]) for symbol in top_symbols[:LONGTERM_TOP_COINS]],
        "groups_total": len(grouped),
        "errors": errors,
        "active_exchanges": active,
    }


def format_longterm_summary(result):
    labels = ", ".join(EXCHANGE_LABELS.get(exchange, exchange.upper()) for exchange in result["active_exchanges"])
    before_gemini = result.get("groups_before_gemini")
    gemini_line = (
        f"После Gemini longterm-фильтра: `{len(result['groups'])}` из `{before_gemini}` монет\n"
        if before_gemini is not None
        else ""
    )
    return (
        "🧲 *Долгосрочный funding scan*\n\n"
        f"Биржи: `{labels}`\n"
        f"Период: `{result['days']}` дней\n"
        f"Размер: `${LONGTERM_AMOUNT_PER_LEG:,.0f}` на ногу\n"
        f"Монет проверено: `{result['symbols_scanned']}`\n"
        f"Связок найдено: `{result['pairs_found']}`\n\n"
        f"Монет с вариантами: `{result.get('groups_total', len(result['groups']))}`\n"
        f"{gemini_line}"
        f"Показываю порциями по `{LONGTERM_PAGE_SIZE}` монет\n\n"
        "OI/24h volume не фильтруют результат, только дают warning."
    )


def format_longterm_coin(symbol, pairs):
    best = pairs[0]
    lines = [
        f"🧲 *{symbol}*",
        f"Best: `~${best.monthly_usd:,.0f}/мес`, вариантов: `{len(pairs)}`",
        "",
    ]
    for idx, pair in enumerate(pairs[:LONGTERM_VARIANTS_PER_COIN], 1):
        long_label = EXCHANGE_LABELS.get(pair.long.exchange, pair.long.exchange.upper())
        short_label = EXCHANGE_LABELS.get(pair.short.exchange, pair.short.exchange.upper())
        lines.append(
            f"*#{idx}* 🟢 `{long_label}` avg `{pair.long.avg:+.4f}%` std `{pair.long.std:.4f}`\n"
            f"    🔴 `{short_label}` avg `{pair.short.avg:+.4f}%` std `{pair.short.std:.4f}`\n"
            f"    💰 `~${pair.monthly_usd:,.0f}/мес` | `${pair.daily_usd:,.1f}/день` | net `{pair.net_per_payment_pct:+.4f}%`"
        )
        warnings = _liquidity_warnings(pair)
        if warnings:
            lines.append("    ⚠️ " + " | ".join(warnings))
    return "\n".join(lines)


def _gemini_candidate_row(symbol, pairs):
    best = pairs[0]
    routes = []
    for pair in pairs[:2]:
        long_label = EXCHANGE_LABELS.get(pair.long.exchange, pair.long.exchange.upper())
        short_label = EXCHANGE_LABELS.get(pair.short.exchange, pair.short.exchange.upper())
        routes.append(
            f"{long_label} long avg {pair.long.avg:+.4f}% / "
            f"{short_label} short avg {pair.short.avg:+.4f}%"
        )
    warnings = _liquidity_warnings(best)
    warning_text = "; warnings: " + " | ".join(warnings) if warnings else ""
    return f"- {symbol}: best ~${best.monthly_usd:,.0f}/month; routes: {'; '.join(routes)}{warning_text}"


def _extract_gemini_keep_symbols(answer, candidates):
    if not answer or "NO_KEEPERS" in answer.upper():
        return set()
    allowed = {symbol.upper(): symbol for symbol in candidates}
    keep = set()
    for raw_line in answer.splitlines():
        line = raw_line.strip()
        if not line.upper().startswith("KEEP "):
            continue
        token = line.split(" ", 1)[1].split("—", 1)[0].split("-", 1)[0].strip().upper()
        token = token.replace("$", "").replace("`", "").replace("*", "")
        if token in allowed:
            keep.add(allowed[token])
    return keep


def apply_longterm_gemini_filter(result):
    """Filter longterm groups by asset quality. If Gemini fails, return result unchanged."""
    if not GEMINI_API_KEY or not LONGTERM_GEMINI_FILTER or not result.get("groups"):
        return result, None

    groups = result["groups"]
    kept_symbols = set()
    ai_notes = []
    errors = []
    for i in range(0, len(groups), LONGTERM_GEMINI_CHUNK_SIZE):
        chunk = groups[i:i + LONGTERM_GEMINI_CHUNK_SIZE]
        symbols = [symbol for symbol, _pairs in chunk]
        prompt = LONGTERM_GEMINI_PROMPT.format(
            coins_list="\n".join(_gemini_candidate_row(symbol, pairs) for symbol, pairs in chunk)
        )
        answer = gemini_generate(prompt, temperature=0.1, timeout=20)
        if not answer:
            errors.append(get_last_gemini_error() or "no response")
            continue
        kept_symbols.update(_extract_gemini_keep_symbols(answer, symbols))
        ai_notes.append(answer)
        time.sleep(1)

    if errors and not ai_notes:
        return result, "⚠️ Gemini-фильтр долгосрока не ответил, показываю математический результат без AI-фильтра: " + errors[0]

    filtered_groups = [(symbol, pairs) for symbol, pairs in groups if symbol in kept_symbols]
    filtered = dict(result)
    filtered["groups_before_gemini"] = len(groups)
    filtered["groups"] = filtered_groups
    filtered["gemini_notes"] = ai_notes
    filtered["gemini_errors"] = errors
    note = (
        f"🤖 Gemini longterm-фильтр: оставил {len(filtered_groups)} из {len(groups)} монет "
        "по критерию качества актива для долгосрока."
    )
    if errors:
        note += f"\n⚠️ Часть Gemini-порций не ответила: {errors[0]}"
    return filtered, note


def _cleanup_sessions():
    now = time.time()
    expired = [
        token for token, session in _longterm_sessions.items()
        if now - session.get("created_at", 0) > _SESSION_TTL
    ]
    for token in expired:
        _longterm_sessions.pop(token, None)


def _save_session(result):
    _cleanup_sessions()
    token = secrets.token_urlsafe(6)
    _longterm_sessions[token] = {
        "created_at": time.time(),
        "groups": result["groups"],
    }
    return token


def _more_markup(token, next_offset, total):
    if next_offset >= total:
        return None
    count = min(LONGTERM_PAGE_SIZE, total - next_offset)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Показать ещё {count}", callback_data=f"lt_more:{token}:{next_offset}")]
    ])


async def send_longterm_page(bot, chat_id, token, offset):
    session = _longterm_sessions.get(token)
    if not session:
        await bot.send_message(chat_id, "Сессия longfunding устарела. Запусти /longfunding ещё раз.")
        return

    groups = session["groups"]
    total = len(groups)
    end = min(offset + LONGTERM_PAGE_SIZE, total)
    for symbol, pairs in groups[offset:end]:
        await bot.send_message(chat_id, format_longterm_coin(symbol, pairs), parse_mode="Markdown")

    markup = _more_markup(token, end, total)
    if markup:
        await bot.send_message(
            chat_id,
            f"Показано `{end}` из `{total}` монет.",
            parse_mode="Markdown",
            reply_markup=markup,
        )
    else:
        await bot.send_message(chat_id, f"Готово. Показано {total} монет.")


async def longterm_more_callback(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _prefix, token, offset_raw = query.data.split(":", 2)
        offset = int(offset_raw)
    except Exception:
        await query.message.reply_text("Не смог открыть следующую порцию. Запусти /longfunding ещё раз.")
        return
    await send_longterm_page(context.bot, query.message.chat_id, token, offset)


async def send_longterm_report(bot, chat_id, manual=False):
    prefix = "🔎 Запускаю долгосрочный funding scan..." if manual else "🕘 Авто-скан долгосрочного funding запущен..."
    await bot.send_message(chat_id, prefix)
    result = await asyncio.to_thread(scan_longterm_funding)
    if GEMINI_API_KEY and LONGTERM_GEMINI_FILTER and result.get("groups"):
        await bot.send_message(
            chat_id,
            "🤖 Gemini проверяет монеты на долгосрок: капитализация, серьёзность, риск 30-50% свечи, мем/скам риск...",
        )
        result, gemini_note = await asyncio.to_thread(apply_longterm_gemini_filter, result)
        if gemini_note:
            await bot.send_message(chat_id, gemini_note)
    await bot.send_message(chat_id, format_longterm_summary(result), parse_mode="Markdown")
    if not result["groups"]:
        await bot.send_message(chat_id, "✅ Подходящих долгосрочных связок сейчас нет.")
        return
    token = _save_session(result)
    await send_longterm_page(bot, chat_id, token, 0)


async def auto_longterm_job(context: ContextTypes.DEFAULT_TYPE):
    if not REPORT_CHAT_ID:
        return
    await send_longterm_report(context.bot, int(REPORT_CHAT_ID), manual=False)
