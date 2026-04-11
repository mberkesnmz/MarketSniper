import aiohttp
import asyncio
import urllib.parse
from datetime import datetime


API_KEY = "CS FLOAT API KEY HERE"
WEBHOOK = "DISCORD WEBHOOK HERE"
BUFF_COOKIE = "BUFF COOKIE HERE"

CHECK_DELAY = 10
MAX_BACKOFF = 60
MIN_DISCOUNT = 0.05
MIN_PROFIT_USD = 2.0
MIN_WEEKLY_SALES = 10
BUFF_CNY_TO_USD = 0.14

seen_items = set()

BLACKLIST = [
    "Sticker",
    "Graffiti",
    "Patch",
    "Sealed Graffiti",
    "Charm"
]


def is_blacklisted(name):
    for b in BLACKLIST:
        if b in name:
            return True
    return False


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def now_text():
    return datetime.now().strftime("%H:%M:%S")


def get_buff_headers():
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://buff.163.com/market/csgo",
        "X-Requested-With": "XMLHttpRequest"
    }
    if BUFF_COOKIE:
        headers["Cookie"] = BUFF_COOKIE
    return headers


async def buff_json_get(session, url):
    async with session.get(url, headers=get_buff_headers()) as r:
        if r.status != 200:
            return None
        try:
            return await r.json(content_type=None)
        except Exception:
            return None


def pick_buff_item(items, name):
    target = name.strip().lower()

    for candidate in items:
        if str(candidate.get("name", "")).strip().lower() == target:
            return candidate

    for candidate in items:
        candidate_name = str(candidate.get("name", "")).strip().lower()
        if target in candidate_name or candidate_name in target:
            return candidate

    return items[0] if items else None


def extract_price_from_order(order):
    if not isinstance(order, dict):
        return None
    return safe_float(
        order.get("price")
        or order.get("sell_min_price")
        or order.get("buy_max_price")
        or order.get("unit_price")
    )


async def get_csfloat_items(session):

    url = "https://csfloat.com/api/v1/listings"

    headers = {
        "Authorization": API_KEY
    }
    params = {
        "limit": 5,
        "sort_by": "most_recent",
        "category": "1",
        "type": "buy_now"
    }

    async with session.get(url, headers=headers, params=params) as r:
        if r.status == 429:
            retry_after = safe_float(r.headers.get("Retry-After"))
            raise RuntimeError(f"CSFLOAT_429:{retry_after if retry_after else ''}")

        if r.status != 200:
            body = (await r.text())[:300]
            raise RuntimeError(f"CSFloat HTTP {r.status}: {body}")

        data = await r.json(content_type=None)

        # Docs currently show this endpoint returning a JSON array.
        if isinstance(data, list):
            return data

        # Backward-compatible fallback if API wraps listings.
        if isinstance(data, dict):
            if isinstance(data.get("data"), list):
                return data["data"]
            if isinstance(data.get("listings"), list):
                return data["listings"]

        return []


async def get_buff_price(session, name):

    search = urllib.parse.quote(name)
    search_url = f"https://buff.163.com/api/market/goods?game=csgo&page_num=1&search={search}"

    data = await buff_json_get(session, search_url)
    if not isinstance(data, dict):
        return None, None, 0, None

    items = data.get("data", {}).get("items", [])
    if not items:
        return None, None, 0, None

    item = pick_buff_item(items, name)
    if not item:
        return None, None, 0, None

    goods_id = item.get("id") or item.get("goods_id")
    buff_name = item.get("name")

    weekly_sales = safe_int(
        item.get("transacted_num")
        or item.get("sell_num")
        or item.get("goods_info", {}).get("transacted_num")
        or item.get("goods_info", {}).get("sell_num")
    )

    if not goods_id:
        lowest_sell_cny = safe_float(item.get("sell_min_price"))
        buy_order_cny = safe_float(item.get("buy_max_price"))
        if lowest_sell_cny is None:
            return None, None, weekly_sales, buff_name

        lowest_sell_usd = lowest_sell_cny * BUFF_CNY_TO_USD
        buy_order_usd = buy_order_cny * BUFF_CNY_TO_USD if buy_order_cny is not None else None
        return lowest_sell_usd, buy_order_usd, weekly_sales, buff_name

    sell_url = (
        "https://buff.163.com/api/market/goods/sell_order"
        f"?game=csgo&goods_id={goods_id}&page_num=1&sort_by=default&allow_tradable_cooldown=1"
    )
    sell_data = await buff_json_get(session, sell_url)
    sell_items = sell_data.get("data", {}).get("items", []) if isinstance(sell_data, dict) else []
    best_sell_cny = extract_price_from_order(sell_items[0]) if sell_items else None

    buy_url = (
        "https://buff.163.com/api/market/goods/buy_order"
        f"?game=csgo&goods_id={goods_id}&page_num=1"
    )
    buy_data = await buff_json_get(session, buy_url)
    buy_items = buy_data.get("data", {}).get("items", []) if isinstance(buy_data, dict) else []
    best_buy_cny = extract_price_from_order(buy_items[0]) if buy_items else None

    
    if best_sell_cny is None:
        best_sell_cny = safe_float(item.get("sell_min_price"))
    if best_buy_cny is None:
        best_buy_cny = safe_float(item.get("buy_max_price"))

    if best_sell_cny is None:
        return None, None, weekly_sales, buff_name

    best_sell_usd = best_sell_cny * BUFF_CNY_TO_USD
    best_buy_usd = best_buy_cny * BUFF_CNY_TO_USD if best_buy_cny is not None else None

    return best_sell_usd, best_buy_usd, weekly_sales, buff_name


async def send_discord(session, name, csfloat_price, buff_price, buy_order, float_value, item_id, weekly_sales):

    profit_usd = (buff_price - csfloat_price) * 0.98
    profit_percent = (profit_usd / buff_price) * 100

    embed = {
        "title": "Undervalued item:",
        "description": name,
        "color": 65280,
        "fields": [
            {"name": "CSFLOAT Price", "value": f"${csfloat_price:.2f}", "inline": True},
            {"name": "BUFF Lowest Sell (USD)", "value": f"${buff_price:.2f}", "inline": True},
            {"name": "BUFF Buy Order (USD)", "value": f"${buy_order:.2f}" if buy_order is not None else "-", "inline": True},
            {"name": "Weekly Sales", "value": str(weekly_sales), "inline": True},
            {"name": "Float", "value": f"{float_value}", "inline": True},
            {"name": "Net Profit (2% Fee)", "value": f"${profit_usd:.2f}", "inline": True},
            {"name": "Profit", "value": f"%{profit_percent:.2f}", "inline": True},
            {"name": "Link", "value": f"https://csfloat.com/item/{item_id}", "inline": False}
        ]
    }

    await session.post(WEBHOOK, json={"embeds": [embed]})


async def sniper():

    async with aiohttp.ClientSession() as session:
        backoff_delay = CHECK_DELAY

        while True:

            checked = 0
            skipped_liquidity = 0
            matches = 0

            try:

                items = await get_csfloat_items(session)

                for item in items:

                    item_id = item["id"]

                    if item_id in seen_items:
                        continue

                    seen_items.add(item_id)
                    checked += 1

                    name = item["item"]["market_hash_name"]

                    if is_blacklisted(name):
                        continue

                    csfloat_price = item["price"] / 100
                    float_value = item.get("item", {}).get("float_value", "N/A")

                    buff_price, buy_order, weekly_sales, matched_buff_name = await get_buff_price(session, name)

                    if not buff_price:
                        continue

                    if weekly_sales < MIN_WEEKLY_SALES:
                        skipped_liquidity += 1
                        continue

                    profit_usd = buff_price - csfloat_price
                    if profit_usd < MIN_PROFIT_USD:
                        continue

                    if csfloat_price > buff_price * (1 - MIN_DISCOUNT):
                        continue

                    await send_discord(
                        session,
                        name,
                        csfloat_price,
                        buff_price,
                        buy_order,
                        float_value,
                        item_id,
                        weekly_sales
                    )

                    matches += 1
                    print(
                        f"[{now_text()}] PROFIT FOUND: {name} | BUFF: {matched_buff_name} "
                        f"| Weekly: {weekly_sales} | Profit: ${profit_usd:.2f}"
                    )

                print(
                    f"[{now_text()}] Alive | fetched={len(items)} checked={checked} "
                    f"liquidity_skips={skipped_liquidity} matches={matches}"
                )
                backoff_delay = CHECK_DELAY

            except Exception as e:
                msg = str(e)
                if msg.startswith("CSFLOAT_429:"):
                    retry_value = msg.split(":", 1)[1].strip()
                    retry_after = safe_float(retry_value) if retry_value else None

                    if retry_after and retry_after > 0:
                        backoff_delay = max(CHECK_DELAY, min(retry_after, MAX_BACKOFF))
                    else:
                        backoff_delay = min(max(CHECK_DELAY, backoff_delay * 2), MAX_BACKOFF)

                    print(
                        f"[{now_text()}] CSFloat 429 rate limit. "
                        f"Cooling down for {backoff_delay:.1f}s"
                    )
                else:
                    print(f"[{now_text()}] error: {repr(e)}")
                    backoff_delay = min(max(CHECK_DELAY, backoff_delay * 1.5), MAX_BACKOFF)

            await asyncio.sleep(backoff_delay)


asyncio.run(sniper())
