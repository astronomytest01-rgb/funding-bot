import time
import requests

from config import GEMINI_API_KEY

GEMINI_BULK_PROMPT = """Действуй как риск-менеджер криптофонда. Ниже список монет, которые прошли фильтр фандинга за {days} дней.
Твоя задача — быстро отсеять опасные активы: мемкоины, сверхволатильные щиткоины, монеты без понятной инфраструктуры и активы с высоким риском скама.
Оставь только монеты, которые выглядят достаточно надёжными для дальнейшей ручной проверки в стратегии сбора фандинга.

Список монет:
{coins_list}

Выведи ТОЛЬКО список монет, которые ты рекомендуешь оставить.
Формат каждой строки: [ЭМОДЖИ] [МОНЕТА] — [причина 3-6 слов].
Строго ставь 🟢 только для ЛОНГ и 🔴 только для ШОРТ по направлению из списка. Если монета не подходит — пропусти её.
Если ни одна не подходит — напиши: Подходящих фундаментальных монет нет."""

GEMINI_SINGLE_PROMPT = """Действуй как риск-менеджер криптофонда. Проанализируй {coin} как актив для стратегии заработка на ставках финансирования.
Не анализируй сами ставки фандинга: бот считает их отдельно.
Оцени, что за монета, кто за ней стоит, инфраструктуру, ликвидность, волатильность, риск пампов/дампов на 30%+ за сутки и риск скама.

Напиши без Markdown-разметки. Структура:
✅ Вердикт: 1-4 слова.
📊 Волатильность и риски: коротко.
💰 Фундаментал: что за проект.
⚖️ Риск-менеджмент: плечо, размер позиции и стопы."""


def gemini_generate(prompt, temperature=0.3, timeout=30):
    """Возвращает текст Gemini или None. Не влияет на основную логику бота."""
    if not GEMINI_API_KEY:
        return None
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception:
            time.sleep(5)
    return None


def gemini_analyze_bulk(coins_list_text, days):
    return gemini_generate(
        GEMINI_BULK_PROMPT.format(coins_list=coins_list_text, days=days),
        temperature=0.2,
        timeout=30,
    )


def gemini_analyze_single(coin):
    return gemini_generate(
        GEMINI_SINGLE_PROMPT.format(coin=coin.upper()),
        temperature=0.4,
        timeout=30,
    )


def extract_gemini_approved_coins(ai_text, candidates):
    """Простая привязка ответа Gemini к исходным тикерам."""
    if not ai_text:
        return []
    upper = ai_text.upper()
    return [coin for coin in candidates if coin.upper() in upper]


def enforce_direction_emojis(ai_text, directions_by_coin):
    """Fix Gemini output emoji so LONG is green and SHORT is red."""
    if not ai_text:
        return ai_text
    fixed_lines = []
    for line in ai_text.splitlines():
        fixed = line
        upper = line.upper()
        for coin, direction in directions_by_coin.items():
            if coin.upper() not in upper:
                continue
            expected = "🟢" if direction == "LONG" else "🔴"
            stripped = fixed.lstrip()
            if stripped.startswith(("🟢", "🔴")):
                fixed = fixed[:len(fixed) - len(stripped)] + expected + stripped[1:]
            else:
                fixed = f"{expected} {stripped}"
            break
        fixed_lines.append(fixed)
    return "\n".join(fixed_lines)


async def send_gemini_scan_review(msg, passed, days):
    """AI-фильтр в конце /analyze. Показывает только монеты, которые Gemini оставил."""
    if not GEMINI_API_KEY or not passed:
        return
    targets = []
    seen = set()
    for coin, avg, _outlier, direction, category, income in passed:
        if coin in seen:
            continue
        seen.add(coin)
        direction_label = "ЛОНГ" if direction == "LONG" else "ШОРТ"
        income_part = f", ~${income:.1f}/день" if income else ""
        targets.append((coin, direction, f"- {coin} ({direction_label}, {category}, avg {avg:+.4f}%{income_part})"))

    await msg.reply_text("🤖 Gemini быстро фильтрует найденные монеты по фундаменталу...")
    directions_by_coin = {coin: direction for coin, direction, _row in targets}
    bulk_results = []
    for i in range(0, len(targets), 15):
        chunk = targets[i:i+15]
        answer = gemini_analyze_bulk("\n".join(row for _, _, row in chunk), days)
        if answer and "Подходящих фундаментальных монет нет" not in answer:
            bulk_results.append(enforce_direction_emojis(answer, directions_by_coin))
        time.sleep(3)

    if bulk_results:
        text = "🤖 *Gemini AI оставил для ручной проверки:*\n\n" + "\n".join(bulk_results)
    else:
        text = "🤖 Gemini не оставил монет после фундаментального фильтра."
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        await msg.reply_text(chunk)


