import os
import json
import asyncio
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

import aiohttp
import discord
from discord.ext import commands, tasks


# =========================================================
# CONFIG
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

NOTIFICATION_CHANNEL_NAME = "thong-bao-game"
SEARCH_CHANNEL_NAME = "tim-game"

DATA_FILE = "bot_data.json"

CHECK_INTERVAL_MINUTES = 15

# False = lần đầu chạy không spam game đang FREE sẵn
# True  = lần đầu chạy cũng gửi game đang FREE
ANNOUNCE_EXISTING_ON_FIRST_RUN = False

USD_TO_KRW = 1400
USD_TO_VND = 25000

KST = timezone(timedelta(hours=9))

MAX_HISTORY = 3000


# =========================================================
# DISCORD
# =========================================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# DATA
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
        print("DATA ERROR:", e)
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
        print("SAVE ERROR:", e)


data = load_data()


# =========================================================
# CHANNEL
# =========================================================

def normalize_channel_name(name):
    name = name.strip().lower()

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
# HTTP
# =========================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/140 Safari/537.36"
    ),
    "Accept": "*/*"
}


async def get_json(session, url, **kwargs):
    try:
        headers = kwargs.pop("headers", {})

        final_headers = HEADERS.copy()
        final_headers.update(headers)

        async with session.get(
            url,
            headers=final_headers,
            timeout=aiohttp.ClientTimeout(total=30),
            **kwargs
        ) as response:

            if response.status != 200:
                print(
                    "HTTP ERROR:",
                    response.status,
                    url
                )
                return None

            text = await response.text()

            try:
                return json.loads(text)

            except Exception:
                return None

    except Exception as e:
        print("REQUEST ERROR:", e)
        return None


# =========================================================
# DATE
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

    return dt.astimezone(KST).strftime(
        "%d/%m/%Y • %H:%M"
    )


def countdown(value):
    dt = parse_datetime(value)

    if not dt:
        return "⏳ Không rõ"

    now = datetime.now(timezone.utc)

    seconds = int(
        (dt - now).total_seconds()
    )

    if seconds <= 0:
        return "⚡ Đã đến thời gian"

    days = seconds // 86400
    seconds %= 86400

    hours = seconds // 3600
    seconds %= 3600

    minutes = seconds // 60

    if days:
        return (
            f"⏳ **{days} ngày "
            f"{hours} giờ "
            f"{minutes} phút**"
        )

    if hours:
        return (
            f"⏳ **{hours} giờ "
            f"{minutes} phút**"
        )

    return f"⏳ **{minutes} phút**"


def today():
    return datetime.now(KST).strftime(
        "%d/%m/%Y"
    )


# =========================================================
# PRICE
# =========================================================

def format_krw(value):
    if value is None:
        return "Không có giá"

    try:
        value = float(value)

        if value > 100000:
            value /= 100

        return f"₩{int(value):,}"

    except Exception:
        return "Không có giá"


def format_vnd(value):
    if value is None:
        return "Không có giá"

    try:
        value = float(value)

        if value > 1000000:
            value /= 100

        return f"{int(value):,}₫"

    except Exception:
        return "Không có giá"


def usd_to_prices(value):
    try:
        usd = float(value)

        return (
            round(usd * USD_TO_KRW),
            round(usd * USD_TO_VND)
        )

    except Exception:
        return 0, 0


# =========================================================
# EPIC
# =========================================================

EPIC_FREE_URL = (
    "https://store-site-backend-static.ak.epicgames.com/"
    "freeGamesPromotions"
)

EPIC_GRAPHQL_URL = (
    "https://store.epicgames.com/graphql"
)


async def get_epic_free_games(session):
    result = await get_json(
        session,
        EPIC_FREE_URL
    )

    if not result:
        return []

    try:
        elements = (
            result
            ["data"]
            ["Catalog"]
            ["searchStore"]
            ["elements"]
        )
    except Exception:
        return []

    games = []

    now = datetime.now(timezone.utc)

    for game in elements:

        title = game.get("title")

        if not title:
            continue

        slug = (
            game.get("productSlug")
            or game.get("urlSlug")
        )

        if not slug:
            continue

        slug = slug.split("/")[0]

        url = (
            f"https://store.epicgames.com/p/{slug}"
        )

        image = None

        for item in game.get(
            "keyImages",
            []
        ):
            if item.get("type") in (
                "OfferImageWide",
                "DieselStoreFrontWide",
                "Thumbnail"
            ):
                image = item.get("url")

                if image:
                    break

        if not image:
            for item in game.get(
                "keyImages",
                []
            ):
                if item.get("url"):
                    image = item["url"]
                    break

        original_krw = 0
        original_vnd = 0

        try:
            price = game.get(
                "price",
                {}
            )

            total = price.get(
                "totalPrice",
                {}
            )

            original = total.get(
                "originalPrice"
            )

            if original:
                usd = float(original) / 100000

                original_krw, original_vnd = (
                    usd_to_prices(usd)
                )

        except Exception:
            pass

        promotions = game.get(
            "promotions"
        )

        if not promotions:
            continue

        # -------------------------------------------------
        # ĐANG FREE
        # -------------------------------------------------

        current_groups = promotions.get(
            "promotionalOffers"
        ) or []

        for group in current_groups:

            offers = group.get(
                "promotionalOffers"
            ) or []

            for offer in offers:

                discount = offer.get(
                    "discountSetting",
                    {}
                )

                percentage = discount.get(
                    "discountPercentage"
                )

                if percentage != 0:
                    continue

                start_date = offer.get(
                    "startDate"
                )

                end_date = offer.get(
                    "endDate"
                )

                end_dt = parse_datetime(
                    end_date
                )

                if end_dt and end_dt <= now:
                    continue

                games.append({
                    "id": (
                        f"{game.get('id')}"
                        f"-current"
                    ),
                    "base_id": game.get("id"),
                    "title": title,
                    "url": url,
                    "image": image,
                    "start_date": start_date,
                    "end_date": end_date,
                    "original_krw": original_krw,
                    "original_vnd": original_vnd,
                    "status": "current"
                })

        # -------------------------------------------------
        # SẮP FREE
        # -------------------------------------------------

        upcoming_groups = promotions.get(
            "upcomingPromotionalOffers"
        ) or []

        for group in upcoming_groups:

            offers = group.get(
                "promotionalOffers"
            ) or []

            for offer in offers:

                discount = offer.get(
                    "discountSetting",
                    {}
                )

                percentage = discount.get(
                    "discountPercentage"
                )

                if percentage != 0:
                    continue

                start_date = offer.get(
                    "startDate"
                )

                end_date = offer.get(
                    "endDate"
                )

                start_dt = parse_datetime(
                    start_date
                )

                if not start_dt:
                    continue

                if start_dt <= now:
                    continue

                games.append({
                    "id": (
                        f"{game.get('id')}"
                        f"-upcoming-"
                        f"{start_date}"
                    ),
                    "base_id": game.get("id"),
                    "title": title,
                    "url": url,
                    "image": image,
                    "start_date": start_date,
                    "end_date": end_date,
                    "original_krw": original_krw,
                    "original_vnd": original_vnd,
                    "status": "upcoming"
                })

    # Xóa trùng
    unique = []
    seen = set()

    for game in games:

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
# EPIC SEARCH
# =========================================================

EPIC_SEARCH_QUERY = """
query SearchStore(
    $keywords: String!
    $country: String!
    $locale: String!
    $start: Int
    $count: Int
) {
    Catalog {
        searchStore(
            keywords: $keywords
            country: $country
            locale: $locale
            start: $start
            count: $count
        ) {
            elements {
                id
                title
                productSlug
                urlSlug

                keyImages {
                    type
                    url
                }

                price {
                    totalPrice {
                        originalPrice
                        discountPrice
                        currencyCode
                    }
                }
            }
        }
    }
}
"""


async def search_epic(
    session,
    query
):
    payload = {
        "query": EPIC_SEARCH_QUERY,
        "variables": {
            "keywords": query,
            "country": "KR",
            "locale": "en-US",
            "start": 0,
            "count": 40
        }
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

            if response.status != 200:
                print(
                    "EPIC SEARCH HTTP:",
                    response.status
                )
                return []

            result = await response.json()

    except Exception as e:
        print(
            "EPIC SEARCH ERROR:",
            e
        )
        return []

    try:
        elements = (
            result
            ["data"]
            ["Catalog"]
            ["searchStore"]
            ["elements"]
        )
    except Exception:
        return []

    query_lower = query.lower().strip()

    results = []

    for game in elements:

        title = game.get("title")

        if not title:
            continue

        title_lower = title.lower()

        score = 0

        if title_lower == query_lower:
            score += 1000

        if query_lower in title_lower:
            score += 500

        words = re.findall(
            r"[a-zA-Z0-9]+",
            query_lower
        )

        for word in words:
            if len(word) >= 2:
                if word in title_lower:
                    score += 100

        if score <= 0:
            continue

        slug = (
            game.get("productSlug")
            or game.get("urlSlug")
        )

        if not slug:
            continue

        slug = slug.split("/")[0]

        url = (
            f"https://store.epicgames.com/p/{slug}"
        )

        image = None

        for item in game.get(
            "keyImages",
            []
        ):
            if item.get("type") in (
                "OfferImageWide",
                "DieselStoreFrontWide",
                "Thumbnail"
            ):
                image = item.get("url")

                if image:
                    break

        if not image:
            for item in game.get(
                "keyImages",
                []
            ):
                if item.get("url"):
                    image = item["url"]
                    break

        original_krw = 0
        original_vnd = 0

        try:
            total = (
                game
                .get("price", {})
                .get("totalPrice", {})
            )

            original = total.get(
                "originalPrice"
            )

            if original:
                usd = float(original) / 100000

                original_krw, original_vnd = (
                    usd_to_prices(usd)
                )

        except Exception:
            pass

        results.append({
            "id": game.get("id"),
            "title": title,
            "url": url,
            "image": image,
            "original_krw": original_krw,
            "original_vnd": original_vnd,
            "_score": score
        })

    results.sort(
        key=lambda x: x["_score"],
        reverse=True
    )

    final = []
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

        final.append(item)

    print(
        f"EPIC SEARCH: {query} "
        f"-> {len(final)}"
    )

    return final[:10]


# =========================================================
# STEAM
# =========================================================

STEAM_SEARCH_API = (
    "https://store.steampowered.com/api/storesearch"
)

STEAM_DETAILS_API = (
    "https://store.steampowered.com/api/appdetails"
)


async def get_steam_details(
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


async def search_steam(
    session,
    query
):
    url = (
        f"{STEAM_SEARCH_API}"
        f"?term={quote_plus(query)}"
        "&l=english"
        "&cc=KR"
        "&category1=998"
    )

    result = await get_json(
        session,
        url
    )

    if not result:
        print(
            f"STEAM SEARCH: {query} -> 0"
        )
        return []

    items = result.get(
        "items",
        []
    )

    if not items:
        return []

    ranked = []

    query_lower = query.lower().strip()

    for item in items:

        appid = item.get("id")
        title = item.get("name")

        if not appid or not title:
            continue

        title_lower = title.lower()

        score = 0

        if title_lower == query_lower:
            score += 1000

        if query_lower in title_lower:
            score += 500

        words = re.findall(
            r"[a-zA-Z0-9]+",
            query_lower
        )

        for word in words:
            if len(word) >= 2:
                if word in title_lower:
                    score += 100

        if score <= 0:
            continue

        ranked.append({
            "appid": str(appid),
            "title": title,
            "_score": score
        })

    ranked.sort(
        key=lambda x: x["_score"],
        reverse=True
    )

    ranked = ranked[:10]

    async def process(item):

        appid = item["appid"]

        details_kr = (
            await get_steam_details(
                session,
                appid,
                "KR"
            )
        )

        if not details_kr:
            return None

        if details_kr.get("type") != "game":
            return None

        krw = None

        price_kr = details_kr.get(
            "price_overview"
        )

        if price_kr:
            krw = price_kr.get(
                "final"
            )

        details_vn = (
            await get_steam_details(
                session,
                appid,
                "VN"
            )
        )

        vnd = None

        if details_vn:

            price_vn = details_vn.get(
                "price_overview"
            )

            if price_vn:
                vnd = price_vn.get(
                    "final"
                )

        return {
            "appid": appid,
            "title": item["title"],
            "url": (
                "https://store.steampowered.com/"
                f"app/{appid}/"
            ),
            "image": (
                details_kr.get(
                    "header_image"
                )
            ),
            "krw": krw,
            "vnd": vnd
        }

    processed = await asyncio.gather(
        *[
            process(item)
            for item in ranked
        ],
        return_exceptions=True
    )

    final = []

    for item in processed:

        if isinstance(
            item,
            Exception
        ):
            continue

        if item:
            final.append(item)

    print(
        f"STEAM SEARCH: {query} "
        f"-> {len(final)}"
    )

    return final


# =========================================================
# STEAM FREE
# =========================================================

async def get_steam_free_games(
    session
):
    url = (
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

    try:
        async with session.get(
            url,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(
                total=30
            )
        ) as response:

            if response.status != 200:
                return []

            html = await response.text()

    except Exception as e:
        print(
            "STEAM FREE ERROR:",
            e
        )
        return []

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        rows = soup.select(
            "a.search_result_row"
        )

    except Exception:
        return []

    games = []

    # Không gọi appdetails từng cái tuần tự
    # để tránh bot quá chậm.

    appids = []

    for row in rows:

        href = row.get("href")

        if not href:
            continue

        match = re.search(
            r"/app/(\d+)",
            href
        )

        if not match:
            continue

        appids.append(
            match.group(1)
        )

    appids = list(
        dict.fromkeys(appids)
    )[:25]

    async def check(appid):

        details = await get_steam_details(
            session,
            appid,
            "KR"
        )

        if not details:
            return None

        if details.get("type") != "game":
            return None

        # Game vốn miễn phí
        if details.get("is_free"):
            return None

        price = details.get(
            "price_overview"
        )

        if not price:
            return None

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

        if initial <= 0:
            return None

        if final != 0:
            return None

        if discount != 100:
            return None

        title = details.get(
            "name"
        )

        description = str(
            details.get(
                "short_description",
                ""
            )
        ).lower()

        combined = (
            str(title).lower()
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
            return None

        return {
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
            "status": "current"
        }

    checked = await asyncio.gather(
        *[
            check(appid)
            for appid in appids
        ],
        return_exceptions=True
    )

    for game in checked:

        if isinstance(
            game,
            Exception
        ):
            continue

        if game:
            games.append(game)

    return games


# =========================================================
# SEARCH EMBED
# =========================================================

def search_embed(
    store,
    query,
    results
):
    if store == "epic":
        color = 0x7C3AED
        store_name = "EPIC GAMES STORE"
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
            f"☑️ Tìm thấy "
            f"**{len(results)}** game\n"
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
            f"🔗 [MỞ GAME]({game['url']})"
        )

        embed.add_field(
            name=(
                f"⚡ #{index}・"
                f"{game['title']}"
            ),
            value=value,
            inline=False
        )

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
            "⚡✨ Free Game Bot "
            "• Epic + Steam"
        )
    )

    return embed


def no_result_embed(
    store,
    query
):
    if store == "epic":
        color = 0x7C3AED
        store_name = "Epic Games Store"
        icon = "🟣"

    else:
        color = 0x1B9FFF
        store_name = "Steam"
        icon = "🔵"

    return discord.Embed(
        title=(
            "⚡🔎 Không tìm thấy game"
        ),
        description=(
            f"{icon} **{store_name}**\n\n"
            f"Không tìm thấy **{query}**.\n\n"
            "💡 Thử:\n"
            "☑️ `GTA`\n"
            "☑️ `GTA 5`\n"
            "☑️ `Hitman`\n"
            "☑️ `FIFA`\n"
            "☑️ `Minecraft`"
        ),
        color=color
    )


# =========================================================
# FREE EMBED
# =========================================================

def free_embed(
    store,
    game
):
    if store == "epic":
        color = 0x7C3AED
        store_name = "EPIC GAMES STORE"
        icon = "🟣"
        button = "MỞ EPIC"

    else:
        color = 0x1B9FFF
        store_name = "STEAM"
        icon = "🔵"
        button = "MỞ STEAM"

    embed = discord.Embed(
        title=(
            "⚡🎁・GAME ĐANG MIỄN PHÍ・🎁⚡"
        ),
        description=(
            f"{icon} **{store_name}**\n\n"
            f"🎮 **{game['title']}**\n\n"
            "✨🟢 **FREE 100%** 🟢✨\n\n"
            f"📅 Hôm nay: **{today()}**"
        ),
        color=color,
        url=game["url"]
    )

    krw = game.get(
        "original_krw"
    )

    vnd = game.get(
        "original_vnd"
    )

    if krw:
        embed.add_field(
            name="🇰🇷 Giá gốc",
            value=f"**₩{int(krw):,}**",
            inline=True
        )

    if vnd:
        embed.add_field(
            name="🇻🇳 Giá gốc",
            value=f"**{int(vnd):,}₫**",
            inline=True
        )

    embed.add_field(
        name="🎁 Hiện tại",
        value="**FREE**",
        inline=True
    )

    if game.get("end_date"):

        embed.add_field(
            name="⏰ Hết FREE",
            value=(
                f"**{format_date(game['end_date'])} KST**\n"
                f"{countdown(game['end_date'])}"
            ),
            inline=False
        )

    if game.get("image"):
        embed.set_image(
            url=game["image"]
        )

    embed.set_footer(
        text=(
            f"⚡✨ {button} "
            "• Free Game Bot"
        )
    )

    return embed


# =========================================================
# BUTTON
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
                emoji="⚡",
                style=discord.ButtonStyle.link,
                url=url
            )
        )


# =========================================================
# SEARCH MODAL
# =========================================================

class GameSearchModal(
    discord.ui.Modal
):
    def __init__(
        self,
        store
    ):
        self.store = store

        if store == "epic":
            title = "🟣 Tìm game Epic"
        else:
            title = "🔵 Tìm game Steam"

        super().__init__(
            title=title
        )

        self.name_input = discord.ui.TextInput(
            label="Tên game",
            placeholder=(
                "GTA 5 / GTA / Hitman / FIFA..."
            ),
            required=True,
            min_length=1,
            max_length=100
        )

        self.add_item(
            self.name_input
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
                        f"❌ Hãy dùng lệnh "
                        f"trong {search_channel.mention}"
                    ),
                    ephemeral=True
                )

                return

        await interaction.response.defer()

        query = (
            self.name_input.value
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
                embed=no_result_embed(
                    self.store,
                    query
                )
            )

            return

        await interaction.followup.send(
            embed=search_embed(
                self.store,
                query,
                results
            )
        )


# =========================================================
# SEARCH VIEW
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
    async def epic(
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
    async def steam(
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
    description="⚡ Tìm game Epic hoặc Steam"
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
                    f"❌ Dùng `/game` "
                    f"trong {search_channel.mention}"
                ),
                ephemeral=True
            )

            return

    embed = discord.Embed(
        title="⚡🎮・TÌM GAME・🎮⚡",
        description=(
            "✨ Chọn cửa hàng:\n\n"
            "🟣 **EPIC GAMES STORE**\n"
            "☑️ Tìm game + giá + ảnh + link\n\n"
            "🔵 **STEAM**\n"
            "☑️ Tìm game + giá + ảnh + link\n\n"
            "⚡━━━━━━━━━━━━━━━━━━⚡\n"
            "💡 Ví dụ: `GTA 5`, `Hitman`, "
            "`FIFA`, `Minecraft`"
        ),
        color=0x5865F2
    )

    embed.set_footer(
        text=(
            "⚡✨ Free Game Bot"
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
    description="📡 Kiểm tra channel của bot"
)
async def channels_command(
    interaction
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Chỉ dùng trong server.",
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
        title="⚡📡・BOT CHANNELS",
        color=0x5865F2
    )

    embed.add_field(
        name="🎁 Thông báo",
        value=(
            notification.mention
            if notification
            else "❌ Không tìm thấy"
        ),
        inline=False
    )

    embed.add_field(
        name="🎮 Tìm game",
        value=(
            search.mention
            if search
            else "❌ Không tìm thấy"
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# EPIC SCHEDULE
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

    for game in upcoming[:10]:

        key = (
            f"{game['title']}"
            f"|{game.get('start_date')}"
            f"|{game.get('end_date')}"
        )

        if key in data[
            "epic_schedule_sent"
        ]:
            continue

        embed = discord.Embed(
            title=(
                "⚡📅・GAME SẮP FREE・📅⚡"
            ),
            description=(
                "🟣 **EPIC GAMES STORE**\n\n"
                f"🎮 **{game['title']}**\n\n"
                f"📅 Bắt đầu:\n"
                f"**{format_date(game['start_date'])} KST**\n"
                f"{countdown(game['start_date'])}\n\n"
                "✨☑️ **Canh giờ nhận game!**"
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
                "⚡✨ Free Game Bot "
                "• Lịch tự động"
            )
        )

        try:
            await channel.send(
                embed=embed,
                view=StoreLinkView(
                    game["url"],
                    "MỞ EPIC"
                )
            )

            data[
                "epic_schedule_sent"
            ].append(key)

            await asyncio.sleep(1)

        except Exception as e:
            print(
                "EPIC SCHEDULE SEND ERROR:",
                e
            )

    data[
        "epic_schedule_sent"
    ] = data[
        "epic_schedule_sent"
    ][-MAX_HISTORY:]

    save_data()


# =========================================================
# EPIC NOTIFICATION
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

    first_run = not data.get(
        "first_run_done",
        False
    )

    for game in current:

        key = str(
            game["id"]
        )

        if key in data[
            "epic_sent"
        ]:
            continue

        if (
            first_run
            and not ANNOUNCE_EXISTING_ON_FIRST_RUN
        ):
            data[
                "epic_sent"
            ].append(key)

            continue

        try:
            await channel.send(
                embed=free_embed(
                    "epic",
                    game
                ),
                view=StoreLinkView(
                    game["url"],
                    "MỞ EPIC"
                )
            )

            data[
                "epic_sent"
            ].append(key)

            await asyncio.sleep(1)

        except Exception as e:
            print(
                "EPIC SEND ERROR:",
                e
            )

    data[
        "epic_sent"
    ] = data[
        "epic_sent"
    ][-MAX_HISTORY:]

    save_data()


# =========================================================
# STEAM NOTIFICATION
# =========================================================

async def send_steam_notifications(
    session,
    channel
):
    games = await get_steam_free_games(
        session
    )

    first_run = not data.get(
        "first_run_done",
        False
    )

    for game in games:

        key = str(
            game["id"]
        )

        if key in data[
            "steam_sent"
        ]:
            continue

        if (
            first_run
            and not ANNOUNCE_EXISTING_ON_FIRST_RUN
        ):
            data[
                "steam_sent"
            ].append(key)

            continue

        try:
            await channel.send(
                embed=free_embed(
                    "steam",
                    game
                ),
                view=StoreLinkView(
                    game["url"],
                    "MỞ STEAM"
                )
            )

            data[
                "steam_sent"
            ].append(key)

            await asyncio.sleep(1)

        except Exception as e:
            print(
                "STEAM SEND ERROR:",
                e
            )

    data[
        "steam_sent"
    ] = data[
        "steam_sent"
    ][-MAX_HISTORY:]

    save_data()


# =========================================================
# CHECKER
# =========================================================

@tasks.loop(
    minutes=CHECK_INTERVAL_MINUTES
)
async def free_game_checker():

    channel = get_notification_channel()

    if not channel:
        print(
            f"❌ Không tìm thấy "
            f"{NOTIFICATION_CHANNEL_NAME}"
        )
        return

    print(
        "⚡ Đang kiểm tra game miễn phí..."
    )

    async with aiohttp.ClientSession() as session:

        await send_epic_schedule(
            session,
            channel
        )

        await send_epic_notifications(
            session,
            channel
        )

        await send_steam_notifications(
            session,
            channel
        )

    data[
        "first_run_done"
    ] = True

    save_data()

    print(
        "✅ Kiểm tra game hoàn tất."
    )


@free_game_checker.before_loop
async def before_checker():
    await bot.wait_until_ready()


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    print()
    print(
        "========================================"
    )
    print(
        f"BOT ONLINE: {bot.user}"
    )
    print(
        f"SERVER: {len(bot.guilds)}"
    )
    print(
        "========================================"
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
                "NOTIFICATION:",
                notification.name
            )
        else:
            print(
                "NOTIFICATION: NOT FOUND"
            )

        if search:
            print(
                "SEARCH:",
                search.name
            )
        else:
            print(
                "SEARCH: NOT FOUND"
            )

        try:
            bot.tree.copy_global_to(
                guild=guild
            )

            synced = await bot.tree.sync(
                guild=guild
            )

            print(
                f"SYNCED COMMANDS: "
                f"{len(synced)}"
            )

        except Exception as e:
            print(
                "SYNC ERROR:",
                e
            )

    try:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Game(
                name=(
                    "⚡ Free Games • "
                    "Epic + Steam"
                )
            )
        )
    except Exception:
        pass

    if not free_game_checker.is_running():
        free_game_checker.start()


# =========================================================
# ERROR
# =========================================================

@bot.event
async def on_error(
    event,
    *args,
    **kwargs
):
    print(
        f"BOT ERROR: {event}"
    )


# =========================================================
# START
# =========================================================

if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN chưa được đặt "
        "trong Railway Variables."
    )


bot.run(DISCORD_TOKEN)
