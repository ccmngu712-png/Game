```python
import os
import json
import asyncio
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

import discord
from discord.ext import commands, tasks


# =========================================================
# ⚡ CONFIG
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

NOTIFICATION_CHANNEL_NAME = "thong-bao-game"
SEARCH_CHANNEL_NAME = "tim-game"

DATA_FILE = "bot_data.json"

# Kiểm tra store mỗi 15 phút
CHECK_INTERVAL_MINUTES = 15

# Lần đầu bot chạy:
# False = không spam những game đang FREE sẵn
# True  = gửi luôn những game đang FREE
ANNOUNCE_EXISTING_ON_FIRST_RUN = False

# Tỷ giá tham khảo Epic
USD_TO_KRW = 1400
USD_TO_VND = 25000

# Múi giờ Hàn Quốc
KST = timezone(timedelta(hours=9))

# Giới hạn lưu lịch
MAX_HISTORY = 3000


# =========================================================
# ⚡ DISCORD
# =========================================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# 💾 DATA
# =========================================================

def default_data():
    return {
        "epic_sent": [],
        "steam_sent": [],
        "epic_schedule_sent": [],
        "first_run_done": False
    }


def load_data():
    if not os.path.exists(DATA_FILE):
        return default_data()

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)

        data = default_data()

        if isinstance(saved, dict):
            data.update(saved)

        return data

    except Exception as e:
        print("❌ Không đọc được bot_data.json:", e)
        return default_data()


def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )
    except Exception as e:
        print("❌ Không lưu được data:", e)


data = load_data()


# =========================================================
# 📡 CHANNEL
# =========================================================

def normalize_channel_name(name: str):
    name = name.strip().lower()

    # 🎁・thong-bao-game
    if "・" in name:
        name = name.split("・", 1)[1]

    name = name.replace("_", "-")
    name = name.replace(" ", "-")

    return name


def find_channel(guild, wanted_name):
    if guild is None:
        return None

    wanted = normalize_channel_name(wanted_name)

    for channel in guild.text_channels:
        current = normalize_channel_name(channel.name)

        if current == wanted:
            return channel

    return None


def get_notification_channel():
    for guild in bot.guilds:
        channel = find_channel(
            guild,
            NOTIFICATION_CHANNEL_NAME
        )

        if channel:
            return channel

    return None


def get_search_channel():
    for guild in bot.guilds:
        channel = find_channel(
            guild,
            SEARCH_CHANNEL_NAME
        )

        if channel:
            return channel

    return None


# =========================================================
# 🌐 HTTP
# =========================================================

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(iPhone; CPU iPhone OS 18_7 like Mac OS X) "
        "AppleWebKit/605.1.15 "
        "Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "*/*"
}


async def get_json(session, url, **kwargs):
    try:
        headers = kwargs.pop("headers", {})

        merged_headers = DEFAULT_HEADERS.copy()
        merged_headers.update(headers)

        async with session.get(
            url,
            headers=merged_headers,
            timeout=aiohttp.ClientTimeout(total=30),
            **kwargs
        ) as response:

            text = await response.text()

            if response.status != 200:
                print(
                    f"❌ HTTP {response.status}: "
                    f"{url[:120]}"
                )
                return None

            try:
                return json.loads(text)

            except Exception:
                return None

    except Exception as e:
        print("❌ GET error:", e)
        return None


async def get_text(session, url, **kwargs):
    try:
        headers = kwargs.pop("headers", {})

        merged_headers = DEFAULT_HEADERS.copy()
        merged_headers.update(headers)

        async with session.get(
            url,
            headers=merged_headers,
            timeout=aiohttp.ClientTimeout(total=30),
            **kwargs
        ) as response:

            if response.status != 200:
                return None

            return await response.text()

    except Exception as e:
        print("❌ GET text error:", e)
        return None


# =========================================================
# 🕐 DATE / COUNTDOWN
# =========================================================

def parse_datetime(value):
    if not value:
        return None

    try:
        value = str(value)

        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt

    except Exception:
        return None


def format_date(value):
    dt = parse_datetime(value)

    if not dt:
        return "Không rõ"

    local = dt.astimezone(KST)

    return local.strftime(
        "%d/%m/%Y • %H:%M"
    )


def format_countdown(value):
    dt = parse_datetime(value)

    if not dt:
        return "⏳ Không rõ thời gian"

    now = datetime.now(timezone.utc)

    remaining = dt - now

    seconds = int(
        remaining.total_seconds()
    )

    if seconds <= 0:
        return "⚡ Đã đến thời gian!"

    days = seconds // 86400
    seconds %= 86400

    hours = seconds // 3600
    seconds %= 3600

    minutes = seconds // 60

    if days > 0:
        return (
            f"⏳ **{days} ngày "
            f"{hours} giờ {minutes} phút**"
        )

    if hours > 0:
        return (
            f"⏳ **{hours} giờ "
            f"{minutes} phút**"
        )

    return (
        f"⏳ **{minutes} phút**"
    )


def today_text():
    return datetime.now(KST).strftime(
        "%d/%m/%Y"
    )


# =========================================================
# 💰 PRICE
# =========================================================

def epic_usd_to_prices(usd):
    try:
        usd = float(usd)

        krw = round(
            usd * USD_TO_KRW
        )

        vnd = round(
            usd * USD_TO_VND
        )

        return krw, vnd

    except Exception:
        return 0, 0


def format_krw(value):
    if value is None:
        return "Không có giá"

    try:
        value = float(value)

        # Steam một số response trả đơn vị nhỏ
        if value > 100000:
            value = value / 100

        return f"₩{int(value):,}"

    except Exception:
        return "Không có giá"


def format_vnd(value):
    if value is None:
        return "Không có giá"

    try:
        value = float(value)

        if value > 1000000:
            value = value / 100

        return f"{int(value):,}₫"

    except Exception:
        return "Không có giá"


# =========================================================
# 🟣 EPIC
# =========================================================

EPIC_FREE_URL = (
    "https://store-site-backend-static.ak.epicgames.com/"
    "freeGamesPromotions"
)

EPIC_GRAPHQL_URL = (
    "https://store.epicgames.com/graphql"
)


EPIC_QUERY = """
query searchStoreQuery(
    $allowCountries: String
    $category: String
    $count: Int
    $country: String!
    $keywords: String
    $locale: String
    $sortBy: String
    $sortDir: String
    $start: Int
    $withPrice: Boolean = false
    $withPromotions: Boolean = false
) {
    Catalog {
        searchStore(
            allowCountries: $allowCountries
            category: $category
            count: $count
            country: $country
            keywords: $keywords
            locale: $locale
            sortBy: $sortBy
            sortDir: $sortDir
            start: $start
        ) {
            elements {
                id
                namespace
                title
                description
                effectiveDate
                productSlug
                urlSlug

                keyImages {
                    type
                    url
                }

                price(country: $country)
                    @include(if: $withPrice) {
                    totalPrice {
                        discountPrice
                        originalPrice
                        voucherDiscount
                        discount
                        currencyCode

                        currencyInfo {
                            decimals
                        }
                    }
                }

                promotions(category: $category)
                    @include(if: $withPromotions) {

                    promotionalOffers {
                        promotionalOffers {
                            startDate
                            endDate

                            discountSetting {
                                discountType
                                discountPercentage
                            }
                        }
                    }

                    upcomingPromotionalOffers {
                        promotionalOffers {
                            startDate
                            endDate

                            discountSetting {
                                discountType
                                discountPercentage
                            }
                        }
                    }
                }
            }

            paging {
                count
                total
            }
        }
    }
}
"""


async def epic_graphql(
    session,
    keywords="",
    with_price=True,
    with_promotions=True
):
    variables = {
        "allowCountries": "KR",
        "category": "games/edition/base",
        "count": 40,
        "country": "KR",
        "keywords": keywords,
        "locale": "en-US",
        "sortBy": "relevancy",
        "sortDir": "DESC",
        "start": 0,
        "withPrice": with_price,
        "withPromotions": with_promotions
    }

    payload = {
        "query": EPIC_QUERY,
        "variables": variables
    }

    headers = {
        "Content-Type": "application/json",
        "Origin": "https://store.epicgames.com",
        "Referer": "https://store.epicgames.com/"
    }

    try:
        async with session.post(
            EPIC_GRAPHQL_URL,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as response:

            text = await response.text()

            if response.status != 200:
                print(
                    "❌ Epic GraphQL HTTP:",
                    response.status
                )
                print(text[:500])
                return None

            result = json.loads(text)

    except Exception as e:
        print("❌ Epic GraphQL error:", e)
        return None

    if result.get("errors"):
        print(
            "❌ Epic GraphQL errors:",
            result["errors"]
        )
        return None

    try:
        return (
            result["data"]
            ["Catalog"]
            ["searchStore"]
            ["elements"]
        )

    except Exception as e:
        print("❌ Epic response error:", e)
        return None


def get_epic_image(game):
    images = game.get(
        "keyImages",
        []
    )

    preferred = [
        "OfferImageWide",
        "DieselStoreFrontWide",
        "Thumbnail"
    ]

    for wanted in preferred:
        for image in images:
            if image.get("type") == wanted:
                if image.get("url"):
                    return image["url"]

    for image in images:
        if image.get("url"):
            return image["url"]

    return None


def get_epic_url(game):
    slug = (
        game.get("productSlug")
        or game.get("urlSlug")
    )

    if not slug:
        return None

    slug = slug.split("/")[0]

    return (
        f"https://store.epicgames.com/p/{slug}"
    )


def epic_price(game):
    try:
        total = (
            game
            .get("price", {})
            .get("totalPrice", {})
        )

        original = total.get(
            "originalPrice"
        )

        if not original:
            return 0, 0

        decimals = (
            total
            .get("currencyInfo", {})
            .get("decimals", 2)
        )

        usd = float(original)

        # Epic thường dùng 100000 cho USD
        if decimals == 2:
            usd = usd / 100000

        return epic_usd_to_prices(usd)

    except Exception:
        return 0, 0


def epic_relevance(
    query,
    title
):
    query = query.lower().strip()
    title = title.lower().strip()

    score = 0

    if title == query:
        score += 1000

    if query in title:
        score += 500

    words = re.findall(
        r"[a-zA-Z0-9]+",
        query
    )

    for word in words:
        if len(word) >= 2:
            if word in title:
                score += 100

    return score


async def search_epic(
    session,
    query
):
    elements = await epic_graphql(
        session,
        keywords=query,
        with_price=True,
        with_promotions=True
    )

    if not elements:
        print(
            f"🟣 Epic '{query}': 0 kết quả"
        )
        return []

    results = []

    for game in elements:

        title = game.get("title")

        if not title:
            continue

        score = epic_relevance(
            query,
            title
        )

        if score <= 0:
            continue

        url = get_epic_url(game)

        if not url:
            continue

        krw, vnd = epic_price(game)

        results.append({
            "id": game.get("id"),
            "title": title,
            "url": url,
            "image": get_epic_image(game),
            "original_krw": krw,
            "original_vnd": vnd,
            "_score": score
        })

    results.sort(
        key=lambda x: x["_score"],
        reverse=True
    )

    unique = []
    seen = set()

    for item in results:
        key = item["id"]

        if key in seen:
            continue

        seen.add(key)

        item.pop(
            "_score",
            None
        )

        unique.append(item)

    print(
        f"🟣 Epic '{query}': "
        f"{len(unique)} kết quả"
    )

    return unique[:10]


async def get_epic_free_games(
    session
):
    elements = await epic_graphql(
        session,
        keywords="",
        with_price=True,
        with_promotions=True
    )

    if not elements:
        return []

    results = []

    now = datetime.now(
        timezone.utc
    )

    for game in elements:

        title = game.get("title")

        if not title:
            continue

        promotions = (
            game.get("promotions")
            or {}
        )

        # -------------------------------------------------
        # FREE HIỆN TẠI
        # -------------------------------------------------

        current_groups = (
            promotions
            .get("promotionalOffers")
            or []
        )

        for group in current_groups:

            offers = (
                group.get(
                    "promotionalOffers"
                )
                or []
            )

            for offer in offers:

                discount = (
                    offer
                    .get("discountSetting")
                    or {}
                )

                percentage = (
                    discount
                    .get("discountPercentage")
                )

                start_date = offer.get(
                    "startDate"
                )

                end_date = offer.get(
                    "endDate"
                )

                start_dt = parse_datetime(
                    start_date
                )

                end_dt = parse_datetime(
                    end_date
                )

                if percentage != 0:
                    continue

                if end_dt and end_dt <= now:
                    continue

                url = get_epic_url(game)

                if not url:
                    continue

                krw, vnd = epic_price(game)

                results.append({
                    "id": (
                        f"{game.get('id')}"
                        f"-current"
                    ),
                    "base_id": game.get("id"),
                    "title": title,
                    "url": url,
                    "image": get_epic_image(game),
                    "start_date": start_date,
                    "end_date": end_date,
                    "original_krw": krw,
                    "original_vnd": vnd,
                    "status": "current"
                })

        # -------------------------------------------------
        # LỊCH FREE SẮP TỚI
        # -------------------------------------------------

        upcoming_groups = (
            promotions
            .get(
                "upcomingPromotionalOffers"
            )
            or []
        )

        for group in upcoming_groups:

            offers = (
                group.get(
                    "promotionalOffers"
                )
                or []
            )

            for offer in offers:

                discount = (
                    offer
                    .get("discountSetting")
                    or {}
                )

                percentage = (
                    discount
                    .get("discountPercentage")
                )

                start_date = offer.get(
                    "startDate"
                )

                end_date = offer.get(
                    "endDate"
                )

                start_dt = parse_datetime(
                    start_date
                )

                if percentage != 0:
                    continue

                if not start_dt:
                    continue

                if start_dt <= now:
                    continue

                url = get_epic_url(game)

                if not url:
                    continue

                krw, vnd = epic_price(game)

                results.append({
                    "id": (
                        f"{game.get('id')}"
                        f"-upcoming-{start_date}"
                    ),
                    "base_id": game.get("id"),
                    "title": title,
                    "url": url,
                    "image": get_epic_image(game),
                    "start_date": start_date,
                    "end_date": end_date,
                    "original_krw": krw,
                    "original_vnd": vnd,
                    "status": "upcoming"
                })

    # Xóa trùng
    unique = []
    seen = set()

    for game in results:

        key = (
            game["title"],
            game["start_date"],
            game["end_date"]
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(game)

    return unique


# =========================================================
# 🔵 STEAM
# =========================================================

STEAM_SEARCH_API = (
    "https://store.steampowered.com/api/storesearch"
)

STEAM_DETAILS_API = (
    "https://store.steampowered.com/api/appdetails"
)


async def get_steam_app_details(
    session,
    appid,
    country="KR"
):
    url = (
        f"{STEAM_DETAILS_API}"
        f"?appids={appid}"
        f"&cc={country}"
        f"&l=english"
    )

    result = await get_json(
        session,
        url
    )

    if not result:
        return None

    try:
        item = result[str(appid)]

        if not item.get("success"):
            return None

        return item.get("data")

    except Exception:
        return None


def steam_relevance(
    query,
    title
):
    query = query.lower().strip()
    title = title.lower().strip()

    score = 0

    if title == query:
        score += 1000

    if query in title:
        score += 500

    words = re.findall(
        r"[a-zA-Z0-9]+",
        query
    )

    for word in words:
        if len(word) >= 2:
            if word in title:
                score += 100

    return score


async def search_steam(
    session,
    query
):
    url = (
        f"{STEAM_SEARCH_API}"
        f"?term={quote_plus(query)}"
        "&l=english"
        "&cc=KR"
    )

    result = await get_json(
        session,
        url
    )

    if not result:
        print(
            f"🔵 Steam '{query}': API rỗng"
        )
        return []

    items = result.get(
        "items",
        []
    )

    if not items:
        print(
            f"🔵 Steam '{query}': "
            "0 kết quả"
        )
        return []

    results = []

    for item in items:

        appid = item.get("id")
        title = item.get("name")

        if not appid or not title:
            continue

        item_type = (
            str(item.get("type", ""))
            .lower()
        )

        # Steam có thể trả app/sub/bundle
        if item_type == "sub":
            continue

        score = steam_relevance(
            query,
            title
        )

        if score <= 0:
            continue

        details = await get_steam_app_details(
            session,
            str(appid),
            "KR"
        )

        if not details:
            continue

        # Chỉ game
        if details.get("type") != "game":
            continue

        krw = None
        vnd = None

        price_kr = details.get(
            "price_overview"
        )

        if price_kr:
            krw = price_kr.get(
                "final"
            )

        # VN
        details_vn = (
            await get_steam_app_details(
                session,
                str(appid),
                "VN"
            )
        )

        if details_vn:
            price_vn = (
                details_vn
                .get("price_overview")
            )

            if price_vn:
                vnd = price_vn.get(
                    "final"
                )

        results.append({
            "appid": str(appid),
            "title": title,
            "url": (
                "https://store.steampowered.com/"
                f"app/{appid}/"
            ),
            "image": (
                details.get(
                    "header_image"
                )
                or item.get(
                    "tiny_image"
                )
            ),
            "krw": krw,
            "vnd": vnd,
            "_score": score
        })

    results.sort(
        key=lambda x: x["_score"],
        reverse=True
    )

    unique = []
    seen = set()

    for item in results:

        appid = item["appid"]

        if appid in seen:
            continue

        seen.add(appid)

        item.pop(
            "_score",
            None
        )

        unique.append(item)

    print(
        f"🔵 Steam '{query}': "
        f"{len(unique)} kết quả"
    )

    return unique[:10]


# =========================================================
# 🔵 STEAM FREE GAME
# =========================================================

async def get_steam_free_games(
    session
):
    url = (
        f"{STEAM_SEARCH_API}"
        "?term="
        "&l=english"
        "&cc=KR"
    )

    # Steam search API không có lịch
    # FREE tương lai đáng tin cậy.
    #
    # Vì vậy dùng trang Specials để tìm
    # game hiện đang giảm 100%.

    specials_url = (
        "https://store.steampowered.com/"
        "search/results/"
        "?specials=1"
        "&maxprice=free"
        "&hidef2p=1"
        "&category1=998"
        "&count=50"
        "&cc=KR"
        "&l=english"
    )

    html = await get_text(
        session,
        specials_url
    )

    if not html:
        return []

    try:
        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        rows = soup.select(
            "a.search_result_row"
        )

    except Exception as e:
        print(
            "❌ Steam HTML error:",
            e
        )
        return []

    results = []

    for row in rows:

        title_el = row.select_one(
            ".title"
        )

        if not title_el:
            continue

        title = title_el.get_text(
            strip=True
        )

        href = row.get("href")

        if not href:
            continue

        match = re.search(
            r"/app/(\d+)",
            href
        )

        if not match:
            continue

        appid = match.group(1)

        details = await get_steam_app_details(
            session,
            appid,
            "KR"
        )

        if not details:
            continue

        if details.get("type") != "game":
            continue

        # Game vốn free
        if details.get("is_free"):
            continue

        price = details.get(
            "price_overview"
        )

        if not price:
            continue

        initial = price.get(
            "initial",
            0
        )

        final = price.get(
            "final",
            0
        )

        discount = price.get(
            "discount_percent",
            0
        )

        # Phải giảm từ giá > 0 xuống 0
        if initial <= 0:
            continue

        if final != 0:
            continue

        if discount != 100:
            continue

        description = str(
            details.get(
                "short_description",
                ""
            )
        ).lower()

        combined = (
            title.lower()
            + " "
            + description
        )

        blocked = [
            "free weekend",
            "free trial",
            "weekend trial",
            "limited trial"
        ]

        if any(
            word in combined
            for word in blocked
        ):
            continue

        results.append({
            "id": appid,
            "title": title,
            "url": (
                "https://store.steampowered.com/"
                f"app/{appid}/"
            ),
            "image": details.get(
                "header_image"
            ),
            "original_krw": initial,
            "original_vnd": None,
            "end_date": None,
            "status": "current"
        })

    return results


# =========================================================
# 🎮 SEARCH EMBED
# =========================================================

def make_game_search_embed(
    store,
    query,
    results
):
    if store == "epic":

        color = 0x7C3AED
        store_name = (
            "EPIC GAMES STORE"
        )
        icon = "🟣"

    else:

        color = 0x1B9FFF
        store_name = "STEAM"
        icon = "🔵"

    embed = discord.Embed(
        title=(
            f"⚡🎮・{query.upper()}"
        ),
        description=(
            f"{icon} **{store_name}**\n"
            f"☑️ Tìm thấy **{len(results)}** game\n"
            "✨━━━━━━━━━━━━━━━━━━✨"
        ),
        color=color
    )

    for index, game in enumerate(
        results,
        start=1
    ):

        if store == "epic":

            krw = format_krw(
                game.get(
                    "original_krw"
                )
            )

            vnd = format_vnd(
                game.get(
                    "original_vnd"
                )
            )

        else:

            krw = format_krw(
                game.get("krw")
            )

            vnd = format_vnd(
                game.get("vnd")
            )

        value = (
            f"💰 **KRW:** {krw}\n"
            f"💵 **VNĐ:** {vnd}\n"
            f"☑️ [MỞ GAME]({game['url']})"
        )

        embed.add_field(
            name=(
                f"⚡ #{index}・"
                f"{game['title']}"
            ),
            value=value,
            inline=False
        )

    # Chỉ 1 ảnh lớn
    image = None

    if results:
        image = results[0].get(
            "image"
        )

    if image:
        embed.set_image(
            url=image
        )

    embed.set_footer(
        text=(
            "⚡✨ FREE GAME BOT "
            "• Epic + Steam "
            "• ☑️ Search"
        )
    )

    return embed


def make_no_result_embed(
    store,
    query
):
    if store == "epic":

        color = 0x7C3AED
        name = "Epic Games Store"
        icon = "🟣"

    else:

        color = 0x1B9FFF
        name = "Steam"
        icon = "🔵"

    embed = discord.Embed(
        title=(
            "⚡🔎 Không tìm thấy game"
        ),
        description=(
            f"{icon} **{name}**\n\n"
            f"Không tìm thấy "
            f"**{query}**.\n\n"
            "✨ Thử tên ngắn hơn:\n"
            "☑️ `GTA`\n"
            "☑️ `GTA 5`\n"
            "☑️ `Hitman`\n"
            "☑️ `Minecraft`\n"
            "☑️ `FIFA`"
        ),
        color=color
    )

    embed.set_footer(
        text=(
            "⚡ Free Game Bot • "
            "Thử lại với từ khóa khác"
        )
    )

    return embed


# =========================================================
# 📅 SCHEDULE EMBED
# =========================================================

def make_schedule_embed(
    games
):
    embed = discord.Embed(
        title=(
            "⚡📅・LỊCH GAME MIỄN PHÍ"
        ),
        description=(
            "✨ **Canh lịch để vào nhận game!**\n"
            f"📅 Hôm nay: **{today_text()}**\n"
            "☑️ Bot tự động cập nhật lịch Epic.\n"
            "✨━━━━━━━━━━━━━━━━━━✨"
        ),
        color=0x8B5CF6
    )

    upcoming = [
        game
        for game in games
        if game.get("status") == "upcoming"
    ]

    current = [
        game
        for game in games
        if game.get("status") == "current"
    ]

    # -----------------------------------------------------
    # ĐANG FREE
    # -----------------------------------------------------

    if current:

        text = ""

        for game in current[:5]:

            end = game.get(
                "end_date"
            )

            text += (
                "🟢 **ĐANG FREE**\n"
                f"🎮 **{game['title']}**\n"
                f"⏳ {format_countdown(end)}\n"
                f"☑️ [Vào Epic]({game['url']})\n\n"
            )

        embed.add_field(
            name="🟢⚡ ĐANG MIỄN PHÍ",
            value=text[:1024],
            inline=False
        )

    # -----------------------------------------------------
    # SẮP FREE
    # -----------------------------------------------------

    if upcoming:

        upcoming.sort(
            key=lambda x: (
                parse_datetime(
                    x.get("start_date")
                )
                or datetime.max.replace(
                    tzinfo=timezone.utc
                )
            )
        )

        text = ""

        for game in upcoming[:8]:

            start = game.get(
                "start_date"
            )

            end = game.get(
                "end_date"
            )

            text += (
                "🟣 **EPIC GAMES**\n"
                f"🎮 **{game['title']}**\n"
                f"📆 Bắt đầu: "
                f"**{format_date(start)} KST**\n"
                f"{format_countdown(start)}\n"
            )

            if end:
                text += (
                    f"🏁 Kết thúc: "
                    f"**{format_date(end)} KST**\n"
                )

            text += (
                f"☑️ [Xem game]({game['url']})\n"
                "──────────────\n"
            )

        embed.add_field(
            name=(
                "📅⚡ GAME SẮP FREE"
            ),
            value=text[:1024],
            inline=False
        )

    if not upcoming and not current:

        embed.add_field(
            name="☑️ Lịch",
            value=(
                "Hiện chưa lấy được "
                "lịch FREE sắp tới từ Epic."
            ),
            inline=False
        )

    embed.set_footer(
        text=(
            "⚡✨ Free Game Bot "
            "• Lịch tự động "
            "• ☑️ Canh ngày nhận game"
        )
    )

    return embed


# =========================================================
# 🎁 FREE NOTIFICATION
# =========================================================

def make_free_notification_embed(
    store,
    game
):
    if store == "epic":

        color = 0x7C3AED
        store_name = (
            "EPIC GAMES STORE"
        )
        icon = "🟣"
        button_label = "MỞ EPIC"

    else:

        color = 0x1B9FFF
        store_name = "STEAM"
        icon = "🔵"
        button_label = "MỞ STEAM"

    embed = discord.Embed(
        title=(
            "⚡🎁・GAME ĐANG MIỄN PHÍ"
        ),
        description=(
            f"{icon} **{store_name}**\n\n"
            "✨☑️ **FREE 100%** ☑️✨\n\n"
            f"🎮 **{game['title']}**\n\n"
            f"📅 Hôm nay: "
            f"**{today_text()}**"
        ),
        color=color,
        url=game["url"]
    )

    original_krw = game.get(
        "original_krw"
    )

    original_vnd = game.get(
        "original_vnd"
    )

    if original_krw:

        embed.add_field(
            name="🇰🇷 Giá gốc",
            value=(
                f"**₩{int(original_krw):,}**"
            ),
            inline=True
        )

    if original_vnd:

        embed.add_field(
            name="🇻🇳 Giá gốc",
            value=(
                f"**{int(original_vnd):,}₫**"
            ),
            inline=True
        )

    embed.add_field(
        name="🎁 Giá hiện tại",
        value="**FREE** ⚡",
        inline=True
    )

    end_date = game.get(
        "end_date"
    )

    if end_date:

        embed.add_field(
            name="⏰ Hết FREE",
            value=(
                f"**{format_date(end_date)} KST**\n"
                f"{format_countdown(end_date)}"
            ),
            inline=False
        )

    if game.get("start_date"):

        embed.add_field(
            name="📆 Bắt đầu",
            value=(
                f"{format_date(game['start_date'])} KST"
            ),
            inline=False
        )

    if game.get("image"):

        embed.set_image(
            url=game["image"]
        )

    embed.set_footer(
        text=(
            f"⚡✨ Free Game Bot "
            f"• ☑️ {button_label} "
            f"• Tự động cập nhật"
        )
    )

    return embed


# =========================================================
# 🔗 BUTTON
# =========================================================

class StoreLinkView(
    discord.ui.View
):

    def __init__(
        self,
        url,
        label
    ):
        super().__init__(
            timeout=None
        )

        self.add_item(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.link,
                url=url,
                emoji="⚡"
            )
        )


# =========================================================
# 🔎 SEARCH MODAL
# =========================================================

class GameSearchModal(
    discord.ui.Modal
):

    def __init__(self, store):

        self.store = store

        if store == "epic":

            title = (
                "🟣 ⚡ Tìm game Epic"
            )

        else:

            title = (
                "🔵 ⚡ Tìm game Steam"
            )

        super().__init__(
            title=title
        )

        self.game_name = discord.ui.TextInput(
            label="☑️ Tên game",
            placeholder=(
                "GTA 5 / GTA / Hitman / FIFA..."
            ),
            required=True,
            min_length=1,
            max_length=100
        )

        self.add_item(
            self.game_name
        )

    async def on_submit(
        self,
        interaction
    ):
        search_channel = (
            get_search_channel()
        )

        if search_channel:

            if (
                interaction.channel_id
                != search_channel.id
            ):

                await interaction.response.send_message(
                    (
                        "⚡ Hãy dùng `/game` "
                        f"trong {search_channel.mention}"
                    ),
                    ephemeral=True
                )

                return

        await interaction.response.defer()

        query = (
            self.game_name.value
            .strip()
        )

        async with aiohttp.ClientSession() as session:

            if self.store == "epic":

                results = await search_epic(
                    session,
                    query
                )

            else:

                results = await search_steam(
                    session,
                    query
                )

        if not results:

            await interaction.followup.send(
                embed=make_no_result_embed(
                    self.store,
                    query
                )
            )

            return

        embed = make_game_search_embed(
            self.store,
            query,
            results
        )

        await interaction.followup.send(
            embed=embed
        )


# =========================================================
# 🎮 STORE BUTTONS
# =========================================================

class StoreSearchView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=180
        )

    @discord.ui.button(
        label="Epic Games",
        emoji="🟣",
        style=discord.ButtonStyle.primary
    )
    async def epic_button(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            GameSearchModal("epic")
        )

    @discord.ui.button(
        label="Steam",
        emoji="🔵",
        style=discord.ButtonStyle.primary
    )
    async def steam_button(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            GameSearchModal("steam")
        )


# =========================================================
# /GAME
# =========================================================

@bot.tree.command(
    name="game",
    description=(
        "⚡ Tìm game trên Epic hoặc Steam"
    )
)
async def game_command(
    interaction
):

    search_channel = (
        get_search_channel()
    )

    if search_channel:

        if (
            interaction.channel_id
            != search_channel.id
        ):

            await interaction.response.send_message(
                (
                    "❌ Chỉ dùng lệnh này trong "
                    f"{search_channel.mention}"
                ),
                ephemeral=True
            )

            return

    embed = discord.Embed(
        title=(
            "⚡🎮・TÌM GAME・🎮⚡"
        ),
        description=(
            "✨ **Chọn cửa hàng để tìm game** ✨\n\n"
            "🟣 **EPIC GAMES STORE**\n"
            "☑️ Tìm game + giá + ảnh + link\n\n"
            "🔵 **STEAM**\n"
            "☑️ Tìm game + giá + ảnh + link\n\n"
            "⚡━━━━━━━━━━━━━━━━━━⚡\n"
            "💡 Ví dụ: `GTA 5`, `Hitman`, "
            "`Minecraft`, `FIFA`"
        ),
        color=0x5865F2
    )

    embed.set_footer(
        text=(
            "⚡✨ FREE GAME BOT "
            "• ☑️ Epic + Steam"
        )
    )

    await interaction.response.send_message(
        embed=embed,
        view=StoreSearchView()
    )


# =========================================================
# /CHANNELS
# =========================================================

@bot.tree.command(
    name="channels",
    description=(
        "📡 Xem kênh bot đang sử dụng"
    )
)
async def channels_command(
    interaction
):

    if interaction.guild is None:

        await interaction.response.send_message(
            "❌ Lệnh này chỉ dùng trong server.",
            ephemeral=True
        )

        return

    notification = find_channel(
        interaction.guild,
        NOTIFICATION_CHANNEL_NAME
    )

    search = find_channel(
        interaction.guild,
        SEARCH_CHANNEL_NAME
    )

    embed = discord.Embed(
        title=(
            "⚡📡・BOT CHANNELS"
        ),
        color=0x5865F2
    )

    embed.add_field(
        name="🎁・Thông báo game",
        value=(
            notification.mention
            if notification
            else "❌ Không tìm thấy"
        ),
        inline=False
    )

    embed.add_field(
        name="🎮・Tìm game",
        value=(
            search.mention
            if search
            else "❌ Không tìm thấy"
        ),
        inline=False
    )

    embed.set_footer(
        text=(
            "⚡☑️ Bot tự nhận diện tên kênh"
        )
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# 📅 GỬI LỊCH EPIC
# =========================================================

async def send_epic_schedule(
    session,
    channel
):
    games = await get_epic_free_games(
        session
    )

    upcoming = [
        game
        for game in games
        if game.get("status")
        == "upcoming"
    ]

    if not upcoming:
        return

    # Chỉ lấy game trong khoảng gần
    upcoming.sort(
        key=lambda x: (
            parse_datetime(
                x.get("start_date")
            )
            or datetime.max.replace(
                tzinfo=timezone.utc
            )
        )
    )

    sent_count = 0

    for game in upcoming[:10]:

        schedule_id = (
            f"{game['title']}"
            f"|{game.get('start_date')}"
            f"|{game.get('end_date')}"
        )

        if schedule_id in data[
            "epic_schedule_sent"
        ]:
            continue

        embed = discord.Embed(
            title=(
                "⚡📅・GAME SẮP MIỄN PHÍ"
            ),
            description=(
                "🟣 **EPIC GAMES STORE**\n\n"
                f"🎮 **{game['title']}**\n\n"
                f"📅 Bắt đầu: "
                f"**{format_date(game['start_date'])} KST**\n"
                f"{format_countdown(game['start_date'])}\n\n"
                "☑️ **Canh giờ vào nhận game!**"
            ),
            color=0x7C3AED,
            url=game["url"]
        )

        if game.get("image"):
            embed.set_image(
                url=game["image"]
            )

        embed.set_footer(
            text=(
                "⚡✨ Lịch FREE • "
                "Bot tự động cập nhật"
            )
        )

        view = StoreLinkView(
            game["url"],
            "XEM GAME"
        )

        try:

            await channel.send(
                embed=embed,
                view=view
            )

            data[
                "epic_schedule_sent"
            ].append(schedule_id)

            sent_count += 1

            await asyncio.sleep(1)

        except Exception as e:

            print(
                "❌ Epic schedule error:",
                e
            )

    data[
        "epic_schedule_sent"
    ] = data[
        "epic_schedule_sent"
    ][-MAX_HISTORY:]

    if sent_count:
        save_data()


# =========================================================
# 🎁 GỬI GAME ĐANG FREE
# =========================================================

async def send_epic_notifications(
    session,
    channel
):
    games = await get_epic_free_games(
        session
    )

    current = [
        game
        for game in games
        if game.get("status")
        == "current"
    ]

    if not current:
        return

    first_run = not data.get(
        "first_run_done",
        False
    )

    for game in current:

        game_id = str(
            game["id"]
        )

        if game_id in data[
            "epic_sent"
        ]:
            continue

        if (
            first_run
            and not ANNOUNCE_EXISTING_ON_FIRST_RUN
        ):

            data[
                "epic_sent"
            ].append(game_id)

            continue

        embed = make_free_notification_embed(
            "epic",
            game
        )

        view = StoreLinkView(
            game["url"],
            "MỞ EPIC"
        )

        try:

            await channel.send(
                embed=embed,
                view=view
            )

            data[
                "epic_sent"
            ].append(game_id)

            save_data()

            await asyncio.sleep(2)

        except Exception as e:

            print(
                "❌ Epic notification error:",
                e
            )

    data[
        "epic_sent"
    ] = data[
        "epic_sent"
    ][-MAX_HISTORY:]


async def send_steam_notifications(
    session,
    channel
):
    games = await get_steam_free_games(
        session
    )

    if not games:
        return

    first_run = not data.get(
        "first_run_done",
        False
    )

    for game in games:

        game_id = str(
            game["id"]
        )

        if game_id in data[
            "steam_sent"
        ]:
            continue

        if (
            first_run
            and not ANNOUNCE_EXISTING_ON_FIRST_RUN
        ):

            data[
                "steam_sent"
            ].append(game_id)

            continue

        embed = make_free_notification_embed(
            "steam",
            game
        )

        view = StoreLinkView(
            game["url"],
            "MỞ STEAM"
        )

        try:

            await channel.send(
                embed=embed,
                view=view
            )

            data[
                "steam_sent"
            ].append(game_id)

            save_data()

            await asyncio.sleep(2)

        except Exception as e:

            print(
                "❌ Steam notification error:",
                e
            )

    data[
        "steam_sent"
    ] = data[
        "steam_sent"
    ][-MAX_HISTORY:]


# =========================================================
# 🔎 KIỂM TRA FREE GAME
# =========================================================

@tasks.loop(
    minutes=CHECK_INTERVAL_MINUTES
)
async def free_game_checker():

    channel = (
        get_notification_channel()
    )

    if not channel:

        print(
            "⚠️ Không tìm thấy "
            f"#{NOTIFICATION_CHANNEL_NAME}"
        )

        return

    print(
        "⚡ Đang kiểm tra "
        "Epic + Steam..."
    )

    async with aiohttp.ClientSession() as session:

        # 📅 Lịch Epic
        await send_epic_schedule(
            session,
            channel
        )

        # 🎁 Epic đang FREE
        await send_epic_notifications(
            session,
            channel
        )

        # 🎁 Steam đang FREE
        await send_steam_notifications(
            session,
            channel
        )

    data[
        "first_run_done"
    ] = True

    save_data()

    print(
        "☑️ Kiểm tra hoàn tất."
    )


@free_game_checker.before_loop
async def before_free_game_checker():

    await bot.wait_until_ready()


# =========================================================
# 🤖 BOT READY
# =========================================================

@bot.event
async def on_ready():

    print()
    print(
        "⚡━━━━━━━━━━━━━━━━━━━━━━━━━━━━⚡"
    )

    print(
        f"🤖 Bot: {bot.user}"
    )

    print(
        f"📡 Servers: {len(bot.guilds)}"
    )

    print(
        "☑️ Free Game Bot đã online!"
    )

    print(
        "⚡━━━━━━━━━━━━━━━━━━━━━━━━━━━━⚡"
    )

    for guild in bot.guilds:

        notification = find_channel(
            guild,
            NOTIFICATION_CHANNEL_NAME
        )

        search = find_channel(
            guild,
            SEARCH_CHANNEL_NAME
        )

        if notification:

            print(
                f"🎁 Notification: "
                f"#{notification.name}"
            )

        else:

            print(
                "❌ Không thấy "
                f"#{NOTIFICATION_CHANNEL_NAME}"
            )

        if search:

            print(
                f"🎮 Search: "
                f"#{search.name}"
            )

        else:

            print(
                "❌ Không thấy "
                f"#{SEARCH_CHANNEL_NAME}"
            )

        # Sync slash command
        try:

            bot.tree.copy_global_to(
                guild=guild
            )

            synced = await bot.tree.sync(
                guild=guild
            )

            print(
                f"⚡ Synced "
                f"{len(synced)} commands "
                f"→ {guild.name}"
            )

        except Exception as e:

            print(
                "❌ Sync error:",
                e
            )

    # Presence
    try:

        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Game(
                name="⚡ Free Games • ☑️ Epic + Steam"
            )
        )

    except Exception:
        pass

    if not free_game_checker.is_running():

        free_game_checker.start()


# =========================================================
# ❌ ERROR
# =========================================================

@bot.event
async def on_command_error(
    ctx,
    error
):

    print(
        "❌ Command error:",
        error
    )


# =========================================================
# 🚀 START
# =========================================================

if not DISCORD_TOKEN:

    raise RuntimeError(
        "❌ Chưa có DISCORD_TOKEN "
        "trong Railway Variables."
    )


bot.run(
    DISCORD_TOKEN
)
```
