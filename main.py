import os
import json
import asyncio
import re
import html
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

import discord
from discord import app_commands
from discord.ext import commands, tasks


# =========================================================
# CONFIG
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

NOTIFICATION_CHANNEL_NAME = "thong-bao-game"
SEARCH_CHANNEL_NAME = "tim-game"

DATA_FILE = "bot_data.json"

CHECK_INTERVAL_MINUTES = 15

# False = lần đầu bot chạy sẽ không spam các game FREE hiện tại
ANNOUNCE_EXISTING_ON_FIRST_RUN = False

EPIC_GRAPHQL_URLS = [
    "https://graphql.epicgames.com/graphql",
    "https://store.epicgames.com/graphql",
]

EPIC_FREE_URL = (
    "https://store-site-backend-static.ak.epicgames.com/"
    "freeGamesPromotions"
)

STEAM_SEARCH_URL = "https://store.steampowered.com/search/results/"
STEAM_STORESEARCH_URL = "https://store.steampowered.com/api/storesearch/"
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails/"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)


# =========================================================
# DISCORD
# =========================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
)


# =========================================================
# GLOBAL CACHE
# =========================================================

http_session = None

steam_detail_cache = {}
epic_search_cache = {}
steam_search_cache = {}

CACHE_SECONDS = 600

steam_detail_semaphore = asyncio.Semaphore(5)


# =========================================================
# DATABASE
# =========================================================

def load_data():
    if not os.path.exists(DATA_FILE):
        return {
            "sent_free_games": [],
            "first_run_done": False,
        }

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("Invalid database")

        data.setdefault("sent_free_games", [])
        data.setdefault("first_run_done", False)

        return data

    except Exception:
        return {
            "sent_free_games": [],
            "first_run_done": False,
        }


DATA = load_data()


def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                DATA,
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        print("DATABASE SAVE ERROR:", e)


# =========================================================
# HELPERS
# =========================================================

def clean_text(value):
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_text(value):
    value = clean_text(value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )

    return value.lower().strip()


def truncate(text, length):
    text = clean_text(text)

    if len(text) <= length:
        return text

    return text[: length - 3] + "..."


def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(value):
    if not value:
        return None

    try:
        value = str(value)

        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    except Exception:
        return None


def discord_timestamp(dt):
    if not dt:
        return "Không rõ"

    return f"<t:{int(dt.timestamp())}:F>"


def countdown(dt):
    if not dt:
        return "Không rõ"

    seconds = int((dt - now_utc()).total_seconds())

    if seconds <= 0:
        return "Đã bắt đầu"

    days = seconds // 86400
    seconds %= 86400

    hours = seconds // 3600
    seconds %= 3600

    minutes = seconds // 60

    result = []

    if days:
        result.append(f"{days} ngày")

    if hours:
        result.append(f"{hours} giờ")

    if minutes:
        result.append(f"{minutes} phút")

    return " ".join(result) or "dưới 1 phút"


def normalize_channel_name(name):
    """
    Ví dụ:

    🎁・thong-bao-game
    thong-bao-game
    🎮・tim-game

    đều được nhận diện.
    """

    name = name.strip()

    if "・" in name:
        name = name.split("・", 1)[1]

    name = name.lower()
    name = name.replace("_", "-")
    name = re.sub(r"\s+", "-", name)

    return name.strip("-")


def find_notification_channel(guild):
    wanted = normalize_channel_name(NOTIFICATION_CHANNEL_NAME)

    for channel in guild.text_channels:
        if normalize_channel_name(channel.name) == wanted:
            return channel

    return None


def find_search_channel(guild):
    wanted = normalize_channel_name(SEARCH_CHANNEL_NAME)

    for channel in guild.text_channels:
        if normalize_channel_name(channel.name) == wanted:
            return channel

    return None


def get_all_guilds_channels():
    notification_channels = []
    search_channels = []

    for guild in bot.guilds:
        notification = find_notification_channel(guild)
        search = find_search_channel(guild)

        if notification:
            notification_channels.append(notification)

        if search:
            search_channels.append(search)

    return notification_channels, search_channels


def safe_url(url):
    if not url:
        return None

    if not url.startswith(("http://", "https://")):
        return None

    return url


# =========================================================
# HTTP
# =========================================================

async def get_session():
    global http_session

    if http_session is None or http_session.closed:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        }

        http_session = aiohttp.ClientSession(
            timeout=REQUEST_TIMEOUT,
            headers=headers,
        )

    return http_session


async def http_json(
    url,
    method="GET",
    params=None,
    json_data=None,
    headers=None,
):
    session = await get_session()

    try:
        request_headers = {}

        if headers:
            request_headers.update(headers)

        async with session.request(
            method,
            url,
            params=params,
            json=json_data,
            headers=request_headers,
        ) as response:

            if response.status != 200:
                print(
                    "HTTP ERROR:",
                    response.status,
                    url,
                )
                return None

            return await response.json(
                content_type=None
            )

    except Exception as e:
        print("HTTP JSON ERROR:", url, e)
        return None


async def http_text(
    url,
    method="GET",
    params=None,
):
    session = await get_session()

    try:
        async with session.request(
            method,
            url,
            params=params,
        ) as response:

            if response.status != 200:
                print(
                    "HTTP TEXT ERROR:",
                    response.status,
                    url,
                )
                return None

            return await response.text()

    except Exception as e:
        print("HTTP TEXT ERROR:", url, e)
        return None


# =========================================================
# EPIC GRAPHQL
# =========================================================

EPIC_SEARCH_QUERY = r"""
query searchStoreQuery(
    $allowCountries: String
    $category: String
    $comingSoon: Boolean
    $count: Int
    $country: String!
    $keywords: String
    $locale: String
    $namespace: String
    $itemNs: String
    $sortBy: String
    $sortDir: String
    $start: Int
    $tag: String
    $releaseDate: String
    $withPrice: Boolean
    $withPromotions: Boolean
    $priceRange: String
    $freeGame: Boolean
    $onSale: Boolean
    $effectiveDate: String
) {
    Catalog {
        searchStore(
            allowCountries: $allowCountries
            category: $category
            comingSoon: $comingSoon
            count: $count
            country: $country
            keywords: $keywords
            locale: $locale
            namespace: $namespace
            itemNs: $itemNs
            sortBy: $sortBy
            sortDir: $sortDir
            releaseDate: $releaseDate
            start: $start
            tag: $tag
            priceRange: $priceRange
            freeGame: $freeGame
            onSale: $onSale
            effectiveDate: $effectiveDate
        ) {
            elements {
                id
                namespace
                title
                description
                productSlug
                urlSlug

                keyImages {
                    type
                    url
                }

                categories {
                    path
                }

                seller {
                    name
                }

                developerDisplayName
                publisherDisplayName

                price(country: "KR") {
                    totalPrice {
                        originalPrice
                        discountPrice
                        currencyCode
                    }
                }

                price(country: "VN") {
                    totalPrice {
                        originalPrice
                        discountPrice
                        currencyCode
                    }
                }

                promotions {
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


EPIC_CATEGORIES = (
    "games/edition/base"
    "|bundles/games"
    "|games/edition"
    "|games/experience"
    "|games/demo"
)


def epic_aliases(query):
    q = normalize_text(query)

    aliases = [query]

    if q in ("gta", "grand theft auto"):
        aliases += [
            "GTA",
            "Grand Theft Auto",
            "Grand Theft Auto V",
            "GTA V",
            "Grand Theft Auto Online",
        ]

    elif q in ("hitman", "hitman 2", "hitman 3"):
        aliases += [
            "HITMAN",
            "HITMAN 2",
            "HITMAN 3",
            "Hitman World of Assassination",
            "World of Assassination",
        ]

    elif q in (
        "fifa",
        "ea fc",
        "ea sports fc",
        "fc",
    ):
        aliases += [
            "FIFA",
            "EA SPORTS FC",
            "EA FC",
        ]

    elif q in ("cod", "call of duty"):
        aliases += [
            "Call of Duty",
            "Modern Warfare",
            "Black Ops",
            "Warzone",
        ]

    elif q in ("minecraft",):
        aliases += [
            "Minecraft",
            "Minecraft Dungeons",
            "Minecraft Legends",
        ]

    elif q in ("red dead", "rdr"):
        aliases += [
            "Red Dead Redemption",
            "Red Dead Redemption 2",
        ]

    # bỏ duplicate
    result = []

    for item in aliases:
        if item and normalize_text(item) not in [
            normalize_text(x) for x in result
        ]:
            result.append(item)

    return result


async def epic_graphql_search(keyword):
    variables = {
        "allowCountries": "KR",
        "category": EPIC_CATEGORIES,
        "comingSoon": False,
        "count": 40,
        "country": "KR",
        "keywords": keyword,
        "locale": "en-US",
        "start": 0,
        "sortBy": "releaseDate",
        "sortDir": "DESC",
        "withPrice": True,
        "withPromotions": True,
    }

    payload = {
        "operationName": "searchStoreQuery",
        "variables": variables,
        "query": EPIC_SEARCH_QUERY,
    }

    for endpoint in EPIC_GRAPHQL_URLS:

        data = await http_json(
            endpoint,
            method="POST",
            json_data=payload,
            headers={
                "Content-Type": "application/json",
                "Origin": "https://store.epicgames.com",
                "Referer": "https://store.epicgames.com/",
            },
        )

        if not data:
            continue

        if "errors" in data:
            print(
                "EPIC GRAPHQL ERROR:",
                data["errors"],
            )
            continue

        try:
            elements = (
                data
                .get("data", {})
                .get("Catalog", {})
                .get("searchStore", {})
                .get("elements", [])
            )

            if elements:
                return elements

        except Exception as e:
            print("EPIC PARSE ERROR:", e)

    return []


def epic_image(element):
    images = element.get("keyImages") or []

    preferred = [
        "DieselStoreFrontWide",
        "OfferImageWide",
        "ProductBanner",
        "DieselStoreFrontTall",
        "Thumbnail",
    ]

    for image_type in preferred:
        for image in images:
            if image.get("type") == image_type:
                if image.get("url"):
                    return image["url"]

    for image in images:
        if image.get("url"):
            return image["url"]

    return None


def epic_slug(element):
    slug = (
        element.get("productSlug")
        or element.get("urlSlug")
        or ""
    )

    slug = slug.strip("/")

    if slug.endswith("/home"):
        slug = slug[:-5]

    if slug.endswith("-home"):
        slug = slug[:-5]

    return slug


def epic_link(element):
    slug = epic_slug(element)

    if not slug:
        return "https://store.epicgames.com/"

    return (
        "https://store.epicgames.com/"
        f"en-US/p/{slug}"
    )


def epic_price(element, country):
    prices = element.get("price")

    # GraphQL có thể trả object hoặc list
    if isinstance(prices, list):
        price_data = None

        for item in prices:
            if item:
                price_data = item
                break
    else:
        price_data = prices

    if not price_data:
        return None

    total = price_data.get("totalPrice")

    if not total:
        return None

    original = total.get("originalPrice")
    discount = total.get("discountPrice")
    currency = total.get("currencyCode")

    if discount is None:
        discount = original

    if original is None and discount is None:
        return None

    if country == "KR":
        currency = currency or "KRW"
    else:
        currency = currency or "VND"

    return {
        "original": original,
        "discount": discount,
        "currency": currency,
    }


def parse_epic_element(element):
    title = clean_text(
        element.get("title")
        or ""
    )

    if not title:
        return None

    categories = []

    for category in (
        element.get("categories") or []
    ):
        path = category.get("path")

        if path:
            categories.append(path)

    return {
        "store": "Epic",
        "id": str(element.get("id") or ""),
        "namespace": str(
            element.get("namespace") or ""
        ),
        "title": title,
        "description": clean_text(
            element.get("description")
            or ""
        ),
        "image": epic_image(element),
        "url": epic_link(element),
        "price_kr": epic_price(
            element,
            "KR",
        ),
        "price_vn": epic_price(
            element,
            "VN",
        ),
        "categories": categories,
        "developer": clean_text(
            element.get(
                "developerDisplayName"
            )
            or ""
        ),
        "publisher": clean_text(
            element.get(
                "publisherDisplayName"
            )
            or ""
        ),
    }


def epic_match_score(game, query):
    q = normalize_text(query)
    title = normalize_text(
        game.get("title", "")
    )

    if not title:
        return 0

    score = 0

    if title == q:
        score += 1000

    if q in title:
        score += 500

    words = q.split()

    for word in words:
        if word and word in title:
            score += 100

    # Tăng điểm cho các series nổi tiếng
    if q == "gta" and (
        "grand theft auto" in title
        or title.startswith("gta")
    ):
        score += 500

    if q == "hitman" and (
        "hitman" in title
        or "world of assassination" in title
    ):
        score += 500

    if q == "fifa" and (
        "fifa" in title
        or "ea sports fc" in title
        or "ea fc" in title
    ):
        score += 500

    return score


async def epic_search(query):
    cache_key = normalize_text(query)

    cached = epic_search_cache.get(
        cache_key
    )

    if cached:
        saved_time, saved_data = cached

        if (
            (now_utc().timestamp() - saved_time)
            < CACHE_SECONDS
        ):
            return saved_data

    all_results = {}

    for keyword in epic_aliases(query):

        elements = await epic_graphql_search(
            keyword
        )

        for element in elements:

            game = parse_epic_element(
                element
            )

            if not game:
                continue

            game_id = (
                game["id"]
                or game["namespace"]
                or game["title"]
            )

            if game_id not in all_results:
                all_results[game_id] = game

    results = list(all_results.values())

    for game in results:
        game["_score"] = epic_match_score(
            game,
            query,
        )

    results.sort(
        key=lambda x: (
            x.get("_score", 0),
            normalize_text(
                x.get("title", "")
            ),
        ),
        reverse=True,
    )

    # chỉ lấy những kết quả thực sự liên quan
    filtered = [
        game
        for game in results
        if game.get("_score", 0) > 0
    ]

    epic_search_cache[cache_key] = (
        now_utc().timestamp(),
        filtered,
    )

    return filtered


# =========================================================
# EPIC FREE GAMES
# =========================================================

def extract_epic_promotions(data):
    results = []

    try:
        elements = (
            data
            .get("data", {})
            .get("Catalog", {})
            .get("searchStore", {})
            .get("elements", [])
        )

        if elements:
            for element in elements:
                game = parse_epic_element(
                    element
                )

                if game:
                    results.append(game)

            return results

    except Exception:
        pass

    return results


async def get_epic_free_games():
    data = await http_json(
        EPIC_FREE_URL
    )

    if not data:
        return []

    results = []

    try:
        elements = (
            data
            .get("data", {})
            .get("Catalog", {})
            .get("searchStore", {})
            .get("elements", [])
        )

        for element in elements:

            title = clean_text(
                element.get("title")
                or element.get("name")
                or ""
            )

            if not title:
                continue

            promotions = (
                element.get(
                    "promotions"
                )
                or {}
            )

            current_promotions = (
                promotions.get(
                    "promotionalOffers"
                )
                or []
            )

            upcoming_promotions = (
                promotions.get(
                    "upcomingPromotionalOffers"
                )
                or []
            )

            active = None
            upcoming = None

            for block in current_promotions:
                for offer in (
                    block.get(
                        "promotionalOffers"
                    )
                    or []
                ):
                    discount = (
                        offer
                        .get("discountSetting")
                        or {}
                    )

                    percentage = (
                        discount
                        .get(
                            "discountPercentage"
                        )
                    )

                    if percentage == 0:
                        active = offer

            if not active:
                for block in upcoming_promotions:
                    for offer in (
                        block.get(
                            "promotionalOffers"
                        )
                        or []
                    ):
                        discount = (
                            offer
                            .get(
                                "discountSetting"
                            )
                            or {}
                        )

                        percentage = (
                            discount
                            .get(
                                "discountPercentage"
                            )
                        )

                        if percentage == 0:
                            upcoming = offer

            if not active and not upcoming:
                continue

            images = (
                element.get(
                    "keyImages"
                )
                or []
            )

            image = None

            for preferred in [
                "DieselStoreFrontWide",
                "OfferImageWide",
                "ProductBanner",
                "Thumbnail",
            ]:
                for item in images:
                    if (
                        item.get("type")
                        == preferred
                    ):
                        image = item.get(
                            "url"
                        )
                        break

                if image:
                    break

            if not image:
                for item in images:
                    if item.get("url"):
                        image = item.get(
                            "url"
                        )
                        break

            slug = (
                element.get(
                    "productSlug"
                )
                or element.get(
                    "urlSlug"
                )
                or ""
            )

            slug = str(slug).strip("/")

            if slug.endswith("/home"):
                slug = slug[:-5]

            url = (
                "https://store.epicgames.com/"
                f"en-US/p/{slug}"
            )

            start_date = None
            end_date = None

            selected = active or upcoming

            if selected:
                start_date = parse_iso(
                    selected.get(
                        "startDate"
                    )
                )

                end_date = parse_iso(
                    selected.get(
                        "endDate"
                    )
                )

            results.append(
                {
                    "store": "Epic",
                    "id": str(
                        element.get(
                            "id"
                        )
                        or element.get(
                            "namespace"
                        )
                        or title
                    ),
                    "title": title,
                    "description": clean_text(
                        element.get(
                            "description"
                        )
                        or ""
                    ),
                    "image": image,
                    "url": url,
                    "start_date": start_date,
                    "end_date": end_date,
                    "is_active": bool(active),
                    "is_upcoming": bool(
                        upcoming
                    ),
                    "price_kr": None,
                    "price_vn": None,
                }
            )

    except Exception as e:
        print(
            "EPIC FREE PARSE ERROR:",
            e,
        )

    return results


# =========================================================
# STEAM SEARCH
# =========================================================

async def steam_store_search(
    query,
    cc="kr",
    limit=50,
):
    cache_key = (
        f"{normalize_text(query)}:"
        f"{cc}:{limit}"
    )

    cached = steam_search_cache.get(
        cache_key
    )

    if cached:
        saved_time, saved_data = cached

        if (
            now_utc().timestamp()
            - saved_time
            < CACHE_SECONDS
        ):
            return saved_data

    params = {
        "term": query,
        "l": "english",
        "cc": cc.upper(),
        "start": 0,
        "count": limit,
        "infinite": 1,
    }

    data = await http_json(
        STEAM_STORESEARCH_URL,
        params=params,
    )

    results = []

    if data:
        items = data.get(
            "items",
            []
        )

        for item in items:

            appid = item.get("id")

            if not appid:
                continue

            results.append(
                {
                    "appid": int(appid),
                    "name": clean_text(
                        item.get(
                            "name"
                        )
                        or ""
                    ),
                    "price": item.get(
                        "price"
                    ),
                    "tiny_image": item.get(
                        "tiny_image"
                    ),
                }
            )

    steam_search_cache[cache_key] = (
        now_utc().timestamp(),
        results,
    )

    return results


async def steam_html_search(
    query,
    cc="kr",
):
    """
    Fallback cho trường hợp storesearch
    trả quá ít kết quả.
    """

    params = {
        "term": query,
        "cc": cc.upper(),
        "l": "english",
        "start": 0,
        "count": 50,
        "json": 1,
    }

    data = await http_json(
        STEAM_SEARCH_URL,
        params=params,
    )

    if isinstance(data, dict):
        items = (
            data.get("items")
            or data.get("results")
            or []
        )

        return items

    return []


# =========================================================
# STEAM DETAILS
# =========================================================

async def steam_app_details(
    appid,
    cc="KR",
):
    key = f"{appid}:{cc.upper()}"

    cached = steam_detail_cache.get(
        key
    )

    if cached:
        saved_time, saved_data = cached

        if (
            now_utc().timestamp()
            - saved_time
            < CACHE_SECONDS
        ):
            return saved_data

    async with steam_detail_semaphore:

        params = {
            "appids": str(appid),
            "cc": cc.upper(),
            "l": "english",
            "filters": (
                "basic,price_overview,"
                "genres,categories,"
                "screenshots,release_date"
            ),
        }

        data = await http_json(
            STEAM_APPDETAILS_URL,
            params=params,
        )

    if not data:
        return None

    raw = data.get(
        str(appid)
    )

    if not raw:
        return None

    if not raw.get("success"):
        return None

    result = raw.get("data")

    if not result:
        return None

    steam_detail_cache[key] = (
        now_utc().timestamp(),
        result,
    )

    return result


def steam_price_string(
    price,
    country,
):
    if not price:
        return "FREE"

    currency = (
        price.get("currency")
        or ""
    )

    final_formatted = (
        price.get(
            "final_formatted"
        )
        or ""
    )

    if final_formatted:
        return final_formatted

    final = price.get("final")

    if final is None:
        return "Không rõ"

    # Steam trả KRW/VND không có phần thập phân
    if currency in (
        "KRW",
        "VND",
    ):
        return (
            f"{int(final):,} "
            f"{currency}"
        )

    try:
        return (
            f"{float(final) / 100:.2f} "
            f"{currency}"
        )
    except Exception:
        return str(final)


def steam_image(data):
    if not data:
        return None

    return (
        data.get("header_image")
        or data.get("capsule_image")
    )


def steam_genres(data):
    result = []

    for genre in (
        data.get("genres")
        or []
    ):
        name = genre.get(
            "description"
        )

        if name:
            result.append(
                clean_text(name)
            )

    return result


def steam_categories(data):
    result = []

    for category in (
        data.get("categories")
        or []
    ):
        name = category.get(
            "description"
        )

        if name:
            result.append(
                clean_text(name)
            )

    return result


async def build_steam_game(
    appid,
):
    kr = await steam_app_details(
        appid,
        "KR",
    )

    if not kr:
        return None

    # VN request riêng để lấy giá Việt Nam
    vn = await steam_app_details(
        appid,
        "VN",
    )

    title = clean_text(
        kr.get("name")
        or ""
    )

    if not title:
        return None

    genres = steam_genres(
        kr
    )

    categories = steam_categories(
        kr
    )

    price_kr = kr.get(
        "price_overview"
    )

    price_vn = (
        vn.get(
            "price_overview"
        )
        if vn
        else None
    )

    is_free = bool(
        kr.get("is_free")
    )

    if not is_free:
        if not price_kr:
            is_free = True

    return {
        "store": "Steam",
        "id": str(appid),
        "appid": int(appid),
        "title": title,
        "description": clean_text(
            kr.get(
                "short_description"
            )
            or kr.get(
                "detailed_description"
            )
            or ""
        ),
        "image": steam_image(kr),
        "url": (
            "https://store.steampowered.com/"
            f"app/{appid}/"
        ),
        "price_kr": price_kr,
        "price_vn": price_vn,
        "price_kr_text": steam_price_string(
            price_kr,
            "KR",
        ),
        "price_vn_text": steam_price_string(
            price_vn,
            "VN",
        ),
        "genres": genres,
        "categories": categories,
        "release_date": clean_text(
            (
                kr.get(
                    "release_date"
                )
                or {}
            ).get(
                "date"
            )
            or ""
        ),
        "developer": ", ".join(
            kr.get(
                "developers"
            )
            or []
        ),
        "publisher": ", ".join(
            kr.get(
                "publishers"
            )
            or []
        ),
        "is_free": is_free,
    }


def steam_aliases(query):
    q = normalize_text(query)

    aliases = [query]

    if q == "gta":
        aliases += [
            "Grand Theft Auto",
            "GTA V",
            "Grand Theft Auto V",
            "Grand Theft Auto IV",
        ]

    elif q == "hitman":
        aliases += [
            "HITMAN",
            "HITMAN 2",
            "HITMAN 3",
            "Hitman World of Assassination",
        ]

    elif q == "fifa":
        aliases += [
            "FIFA",
            "EA SPORTS FC",
            "EA FC",
        ]

    elif q == "cod":
        aliases += [
            "Call of Duty",
            "Modern Warfare",
            "Black Ops",
        ]

    elif q == "minecraft":
        aliases += [
            "Minecraft",
            "Minecraft Dungeons",
            "Minecraft Legends",
        ]

    return list(
        dict.fromkeys(
            aliases
        )
    )


def steam_match_score(
    game,
    query,
):
    q = normalize_text(query)
    title = normalize_text(
        game.get("title")
        or ""
    )

    score = 0

    if title == q:
        score += 1000

    if q in title:
        score += 500

    for word in q.split():
        if word in title:
            score += 100

    if q == "gta" and (
        "grand theft auto" in title
        or title.startswith("gta")
    ):
        score += 500

    if q == "hitman" and (
        "hitman" in title
        or "world of assassination" in title
    ):
        score += 500

    if q == "fifa" and (
        "fifa" in title
        or "ea sports fc" in title
        or "ea fc" in title
    ):
        score += 500

    return score


async def steam_search(query):
    cache_key = normalize_text(query)

    results_by_id = {}

    for keyword in steam_aliases(query):

        search_results = await steam_store_search(
            keyword,
            "kr",
            50,
        )

        for item in search_results:

            appid = item.get("appid")

            if not appid:
                continue

            if appid not in results_by_id:

                # lấy chi tiết
                game = await build_steam_game(
                    appid
                )

                if game:
                    results_by_id[
                        appid
                    ] = game

    results = list(
        results_by_id.values()
    )

    for game in results:
        game["_score"] = steam_match_score(
            game,
            query,
        )

    results = [
        x for x in results
        if x.get("_score", 0) > 0
    ]

    results.sort(
        key=lambda x: (
            x.get("_score", 0),
            normalize_text(
                x.get("title", "")
            ),
        ),
        reverse=True,
    )

    steam_search_cache[
        cache_key
    ] = (
        now_utc().timestamp(),
        results,
    )

    return results


# =========================================================
# GENRE SEARCH
# =========================================================

GENRES = {
    "Kinh dị": [
        "horror",
        "survival horror",
        "psychological horror",
    ],
    "Hành động": [
        "action",
    ],
    "Phiêu lưu": [
        "adventure",
    ],
    "RPG": [
        "rpg",
        "role-playing",
    ],
    "Thể thao": [
        "sports",
    ],
    "Đua xe": [
        "racing",
    ],
    "Chiến thuật": [
        "strategy",
    ],
    "Indie": [
        "indie",
    ],
    "Mô phỏng": [
        "simulation",
    ],
    "Casual": [
        "casual",
    ],
}


def genre_matches(
    game,
    genre_name,
):
    targets = [
        normalize_text(x)
        for x in GENRES.get(
            genre_name,
            [],
        )
    ]

    game_genres = [
        normalize_text(x)
        for x in (
            game.get(
                "genres"
            )
            or []
        )
    ]

    game_categories = [
        normalize_text(x)
        for x in (
            game.get(
                "categories"
            )
            or []
        )
    ]

    haystack = (
        game_genres
        + game_categories
    )

    for target in targets:
        for value in haystack:

            if (
                target in value
                or value in target
            ):
                return True

    return False


async def search_steam_genre(
    genre_name,
):
    targets = GENRES.get(
        genre_name,
        [],
    )

    all_games = {}

    for target in targets:

        search_results = await steam_store_search(
            target,
            "kr",
            50,
        )

        for item in search_results:

            appid = item.get("appid")

            if not appid:
                continue

            if appid in all_games:
                continue

            game = await build_steam_game(
                appid
            )

            if not game:
                continue

            if genre_matches(
                game,
                genre_name,
            ):
                all_games[
                    appid
                ] = game

    results = list(
        all_games.values()
    )

    results.sort(
        key=lambda x:
        normalize_text(
            x.get("title", "")
        )
    )

    return results


async def search_epic_genre(
    genre_name,
):
    results_by_id = {}

    keywords = GENRES.get(
        genre_name,
        [],
    )

    for keyword in keywords:

        elements = await epic_graphql_search(
            keyword
        )

        for element in elements:

            game = parse_epic_element(
                element
            )

            if not game:
                continue

            title = normalize_text(
                game.get(
                    "title",
                    "",
                )
            )

            description = normalize_text(
                game.get(
                    "description",
                    "",
                )
            )

            categories = [
                normalize_text(x)
                for x in (
                    game.get(
                        "categories"
                    )
                    or []
                )
            ]

            targets = [
                normalize_text(x)
                for x in keywords
            ]

            haystack = (
                [title, description]
                + categories
            )

            matched = False

            for target in targets:
                for value in haystack:

                    if (
                        target in value
                    ):
                        matched = True
                        break

                if matched:
                    break

            if matched:
                gid = (
                    game.get("id")
                    or game.get(
                        "namespace"
                    )
                    or title
                )

                results_by_id[
                    gid
                ] = game

    results = list(
        results_by_id.values()
    )

    results.sort(
        key=lambda x:
        normalize_text(
            x.get("title", "")
        )
    )

    return results


async def search_genre(
    genre_name,
):
    steam_results, epic_results = (
        await asyncio.gather(
            search_steam_genre(
                genre_name
            ),
            search_epic_genre(
                genre_name
            ),
        )
    )

    combined = []

    for game in steam_results:
        combined.append(game)

    for game in epic_results:
        combined.append(game)

    return combined


# =========================================================
# CURRENT FREE STEAM
# =========================================================

async def get_steam_free_games():
    """
    Steam không có endpoint public chính thức
    cho "toàn bộ game free-to-keep đang active".

    Dùng trang Specials của Steam làm nguồn
    tìm các deal giá 0, sau đó kiểm tra AppDetails.
    """

    params = {
        "specials": 1,
        "maxprice": 0,
        "cc": "KR",
        "l": "english",
        "start": 0,
        "count": 50,
        "json": 1,
    }

    data = await http_json(
        STEAM_SEARCH_URL,
        params=params,
    )

    results = []

    if data:

        items = (
            data.get("items")
            or []
        )

        for item in items:

            appid = item.get("id")

            if not appid:
                continue

            game = await build_steam_game(
                appid
            )

            if not game:
                continue

            if game.get(
                "is_free"
            ):
                results.append(
                    game
                )

    return results


# =========================================================
# FREE EMBEDS
# =========================================================

def free_game_key(game):
    return (
        f"{game.get('store')}::"
        f"{game.get('id')}::"
        f"{normalize_text(game.get('title'))}"
    )


def make_free_embed(
    game,
):
    store = game.get(
        "store",
        "Game",
    )

    title = game.get(
        "title",
        "Game",
    )

    if store == "Epic":

        embed = discord.Embed(
            title=f"🎁 {title}",
            description=(
                "⚡ **GAME ĐANG FREE** ⚡\n\n"
                f"🏪 Store: **Epic Games Store**\n"
                "💰 Giá hiện tại: **FREE**"
            ),
            color=0x7C3AED,
            url=game.get("url"),
        )

        if game.get("start_date"):
            embed.add_field(
                name="📅 Bắt đầu",
                value=discord_timestamp(
                    game["start_date"]
                ),
                inline=True,
            )

        if game.get("end_date"):
            embed.add_field(
                name="⏳ Hết FREE",
                value=discord_timestamp(
                    game["end_date"]
                ),
                inline=True,
            )

            embed.add_field(
                name="🔥 Còn lại",
                value=countdown(
                    game["end_date"]
                ),
                inline=True,
            )

        if game.get("image"):
            embed.set_image(
                url=game["image"]
            )

        embed.set_footer(
            text="🎁 Free Game • Epic Games Store"
        )

        return embed

    embed = discord.Embed(
        title=f"🎁 {title}",
        description=(
            "⚡ **GAME ĐANG FREE** ⚡\n\n"
            f"🏪 Store: **Steam**\n"
            "💰 Giá hiện tại: **FREE**"
        ),
        color=0x1B9AAA,
        url=game.get("url"),
    )

    if game.get("image"):
        embed.set_image(
            url=game["image"]
        )

    embed.set_footer(
        text="🎁 Free Game • Steam"
    )

    return embed


# =========================================================
# SEARCH DETAIL EMBEDS
# =========================================================

def make_game_detail_embed(
    game,
):
    store = game.get(
        "store"
    )

    title = game.get(
        "title",
        "Game",
    )

    if store == "Epic":

        price_kr = game.get(
            "price_kr"
        )

        price_vn = game.get(
            "price_vn"
        )

        if price_kr:
            kr_original = price_kr.get(
                "original"
            )

            kr_discount = price_kr.get(
                "discount"
            )

            if (
                kr_discount is not None
                and kr_discount == 0
            ):
                kr_text = "FREE"
            elif (
                kr_original is not None
                and kr_discount is not None
                and kr_discount
                != kr_original
            ):
                kr_text = (
                    f"₩{kr_discount:,}"
                    f" ~~₩{kr_original:,}~~"
                )
            elif kr_original is not None:
                kr_text = (
                    f"₩{kr_original:,}"
                )
            else:
                kr_text = "Không rõ"
        else:
            kr_text = "Không rõ"

        if price_vn:
            vn_original = price_vn.get(
                "original"
            )

            vn_discount = price_vn.get(
                "discount"
            )

            if (
                vn_discount is not None
                and vn_discount == 0
            ):
                vn_text = "FREE"
            elif (
                vn_original is not None
                and vn_discount is not None
                and vn_discount
                != vn_original
            ):
                vn_text = (
                    f"{vn_discount:,}₫"
                    f" ~~{vn_original:,}₫~~"
                )
            elif vn_original is not None:
                vn_text = (
                    f"{vn_original:,}₫"
                )
            else:
                vn_text = "Không rõ"
        else:
            vn_text = "Không rõ"

        embed = discord.Embed(
            title=f"🎮 {title}",
            description=truncate(
                game.get(
                    "description",
                    "",
                ),
                1000,
            ),
            color=0x7C3AED,
            url=game.get("url"),
        )

        embed.add_field(
            name="🟣 Epic Games Store",
            value="",
            inline=False,
        )

        embed.add_field(
            name="🇰🇷 Giá KRW",
            value=kr_text,
            inline=True,
        )

        embed.add_field(
            name="🇻🇳 Giá VNĐ",
            value=vn_text,
            inline=True,
        )

        if game.get("developer"):
            embed.add_field(
                name="👨‍💻 Developer",
                value=truncate(
                    game["developer"],
                    100,
                ),
                inline=True,
            )

        if game.get("publisher"):
            embed.add_field(
                name="🏢 Publisher",
                value=truncate(
                    game["publisher"],
                    100,
                ),
                inline=True,
            )

        embed.add_field(
            name="🔗 Link",
            value=(
                f"[Mở Epic Games Store]"
                f"({game.get('url')})"
            ),
            inline=False,
        )

        if game.get("image"):
            embed.set_image(
                url=game["image"]
            )

        embed.set_footer(
            text="Epic Games Store"
        )

        return embed

    # -------------------------
    # STEAM
    # -------------------------

    embed = discord.Embed(
        title=f"🎮 {title}",
        description=truncate(
            game.get(
                "description",
                "",
            ),
            1000,
        ),
        color=0x1B9AAA,
        url=game.get("url"),
    )

    embed.add_field(
        name="🔵 Steam",
        value="",
        inline=False,
    )

    embed.add_field(
        name="🇰🇷 Giá KRW",
        value=game.get(
            "price_kr_text",
            "Không rõ",
        ),
        inline=True,
    )

    embed.add_field(
        name="🇻🇳 Giá VNĐ",
        value=game.get(
            "price_vn_text",
            "Không rõ",
        ),
        inline=True,
    )

    genres = game.get(
        "genres"
    ) or []

    if genres:
        embed.add_field(
            name="🎭 Thể loại",
            value=truncate(
                ", ".join(genres),
                300,
            ),
            inline=False,
        )

    if game.get(
        "release_date"
    ):
        embed.add_field(
            name="📅 Phát hành",
            value=game[
                "release_date"
            ],
            inline=True,
        )

    if game.get(
        "developer"
    ):
        embed.add_field(
            name="👨‍💻 Developer",
            value=truncate(
                game["developer"],
                100,
            ),
            inline=True,
        )

    if game.get(
        "publisher"
    ):
        embed.add_field(
            name="🏢 Publisher",
            value=truncate(
                game["publisher"],
                100,
            ),
            inline=True,
        )

    embed.add_field(
        name="🔗 Link",
        value=(
            f"[Mở Steam]"
            f"({game.get('url')})"
        ),
        inline=False,
    )

    if game.get("image"):
        embed.set_image(
            url=game["image"]
        )

    embed.set_footer(
        text="Steam Store"
    )

    return embed


# =========================================================
# PAGINATION VIEW
# =========================================================

RESULTS_PER_PAGE = 20


class ResultButton(
    discord.ui.Button
):
    def __init__(
        self,
        game,
        index,
    ):
        self.game = game

        label = truncate(
            game.get(
                "title",
                "Game",
            ),
            70,
        )

        if game.get(
            "store"
        ) == "Epic":
            label = f"🟣 {label}"
            style = discord.ButtonStyle.primary
        else:
            label = f"🔵 {label}"
            style = discord.ButtonStyle.secondary

        super().__init__(
            label=label,
            style=style,
            row=index // 4,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        embed = make_game_detail_embed(
            self.game
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )


class PaginationView(
    discord.ui.View
):
    def __init__(
        self,
        results,
        title,
        color=0x5865F2,
    ):
        super().__init__(
            timeout=300
        )

        self.results = results
        self.title = title
        self.color = color

        self.page = 0

        self.total_pages = max(
            1,
            (
                len(results)
                + RESULTS_PER_PAGE
                - 1
            )
            // RESULTS_PER_PAGE,
        )

        self.refresh()

    def current_items(self):
        start = (
            self.page
            * RESULTS_PER_PAGE
        )

        end = (
            start
            + RESULTS_PER_PAGE
        )

        return self.results[
            start:end
        ]

    def build_embed(self):
        items = self.current_items()

        embed = discord.Embed(
            title=self.title,
            color=self.color,
        )

        if not items:
            embed.description = (
                "❌ Không tìm thấy game."
            )

            return embed

        lines = []

        start_index = (
            self.page
            * RESULTS_PER_PAGE
        )

        for i, game in enumerate(
            items,
            start=start_index + 1,
        ):
            store = game.get(
                "store"
            )

            icon = (
                "🟣"
                if store == "Epic"
                else "🔵"
            )

            title = truncate(
                game.get(
                    "title",
                    "Game",
                ),
                70,
            )

            lines.append(
                f"**{i}.** {icon} {title}"
            )

        embed.description = (
            "\n".join(lines)
            + "\n\n"
            "👇 Bấm vào tên game để xem "
            "giá + ảnh + link."
        )

        embed.set_footer(
            text=(
                f"Trang {self.page + 1}"
                f"/{self.total_pages}"
                f" • {len(self.results)} game"
            )
        )

        return embed

    def refresh(self):
        self.clear_items()

        items = self.current_items()

        for index, game in enumerate(
            items
        ):
            self.add_item(
                ResultButton(
                    game,
                    index,
                )
            )

        # navigation
        nav_row = 4

        previous = discord.ui.Button(
            label="⬅️ Trang trước",
            style=discord.ButtonStyle.secondary,
            disabled=(
                self.page <= 0
            ),
            row=nav_row,
        )

        next_button = discord.ui.Button(
            label="Trang sau ➡️",
            style=discord.ButtonStyle.secondary,
            disabled=(
                self.page
                >= self.total_pages - 1
            ),
            row=nav_row,
        )

        async def previous_callback(
            interaction
        ):
            if self.page > 0:
                self.page -= 1
                self.refresh()

            await interaction.response.edit_message(
                embed=self.build_embed(),
                view=self,
            )

        async def next_callback(
            interaction
        ):
            if self.page < (
                self.total_pages - 1
            ):
                self.page += 1
                self.refresh()

            await interaction.response.edit_message(
                embed=self.build_embed(),
                view=self,
            )

        previous.callback = (
            previous_callback
        )

        next_button.callback = (
            next_callback
        )

        self.add_item(previous)
        self.add_item(next_button)


# =========================================================
# SEARCH MODAL
# =========================================================

class GameSearchModal(
    discord.ui.Modal,
    title="🔎 Tìm game",
):
    query = discord.ui.TextInput(
        label="Tên game",
        placeholder=(
            "Ví dụ: gta / hitman / fifa / minecraft"
        ),
        required=True,
        max_length=100,
    )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):
        await interaction.response.defer()

        query = str(
            self.query
        ).strip()

        if not query:
            await interaction.followup.send(
                "❌ Nhập tên game.",
                ephemeral=True,
            )
            return

        # mặc định tìm cả 2 store
        epic_task = epic_search(
            query
        )

        steam_task = steam_search(
            query
        )

        epic_results, steam_results = (
            await asyncio.gather(
                epic_task,
                steam_task,
                return_exceptions=True,
            )
        )

        if isinstance(
            epic_results,
            Exception,
        ):
            print(
                "EPIC SEARCH ERROR:",
                epic_results,
            )
            epic_results = []

        if isinstance(
            steam_results,
            Exception,
        ):
            print(
                "STEAM SEARCH ERROR:",
                steam_results,
            )
            steam_results = []

        combined = []

        combined.extend(
            epic_results
        )

        combined.extend(
            steam_results
        )

        if not combined:
            await interaction.followup.send(
                (
                    f"❌ Không tìm thấy **{query}**.\n\n"
                    "Thử tên ngắn hơn, ví dụ:\n"
                    "• `gta`\n"
                    "• `hitman`\n"
                    "• `fifa`\n"
                    "• `minecraft`"
                ),
                ephemeral=True,
            )
            return

        # score chung
        combined.sort(
            key=lambda x: (
                x.get("_score", 0),
                normalize_text(
                    x.get(
                        "title",
                        "",
                    )
                ),
            ),
            reverse=True,
        )

        view = PaginationView(
            combined,
            f"🔎 KẾT QUẢ: {query}",
        )

        await interaction.followup.send(
            embed=view.build_embed(),
            view=view,
            ephemeral=True,
        )


# =========================================================
# GENRE SELECT
# =========================================================

class GenreSelect(
    discord.ui.Select
):
    def __init__(self):
        options = []

        emoji_map = {
            "Kinh dị": "👻",
            "Hành động": "⚔️",
            "Phiêu lưu": "🗺️",
            "RPG": "🧙",
            "Thể thao": "⚽",
            "Đua xe": "🏎️",
            "Chiến thuật": "♟️",
            "Indie": "🎨",
            "Mô phỏng": "🏗️",
            "Casual": "🎮",
        }

        for name in GENRES:
            options.append(
                discord.SelectOption(
                    label=name,
                    emoji=emoji_map.get(
                        name,
                        "🎮",
                    ),
                    value=name,
                )
            )

        super().__init__(
            placeholder=(
                "🎭 Chọn thể loại game..."
            ),
            options=options,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        genre = self.values[0]

        await interaction.response.defer(
            ephemeral=True
        )

        try:
            results = await search_genre(
                genre
            )

        except Exception as e:
            print(
                "GENRE ERROR:",
                e,
            )

            await interaction.followup.send(
                "❌ Lỗi khi tìm thể loại.",
                ephemeral=True,
            )

            return

        if not results:
            await interaction.followup.send(
                (
                    f"❌ Không tìm thấy game "
                    f"thể loại **{genre}**."
                ),
                ephemeral=True,
            )
            return

        view = PaginationView(
            results,
            f"🎭 THỂ LOẠI: {genre}",
        )

        await interaction.followup.send(
            embed=view.build_embed(),
            view=view,
            ephemeral=True,
        )


class GenreView(
    discord.ui.View
):
    def __init__(self):
        super().__init__(
            timeout=180
        )

        self.add_item(
            GenreSelect()
        )


# =========================================================
# FREE GAME VIEW
# =========================================================

async def get_all_current_free():
    epic_task = get_epic_free_games()
    steam_task = get_steam_free_games()

    epic, steam = await asyncio.gather(
        epic_task,
        steam_task,
        return_exceptions=True,
    )

    if isinstance(
        epic,
        Exception,
    ):
        print(
            "EPIC FREE ERROR:",
            epic,
        )
        epic = []

    if isinstance(
        steam,
        Exception,
    ):
        print(
            "STEAM FREE ERROR:",
            steam,
        )
        steam = []

    current = []

    for game in epic:
        if game.get(
            "is_active"
        ):
            current.append(game)

    for game in steam:
        current.append(game)

    # chống duplicate
    unique = {}

    for game in current:
        key = free_game_key(
            game
        )

        unique[key] = game

    return list(
        unique.values()
    )


# =========================================================
# MAIN /GAME PANEL
# =========================================================

class GamePanelView(
    discord.ui.View
):
    def __init__(self):
        super().__init__(
            timeout=None
        )

    @discord.ui.button(
        label="TÌM GAME",
        emoji="🔎",
        style=discord.ButtonStyle.primary,
        custom_id="game_panel_search",
        row=0,
    )
    async def search_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_modal(
            GameSearchModal()
        )

    @discord.ui.button(
        label="FREE HIỆN TẠI",
        emoji="🎁",
        style=discord.ButtonStyle.success,
        custom_id="game_panel_free",
        row=0,
    )
    async def free_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer(
            ephemeral=True
        )

        try:
            results = (
                await get_all_current_free()
            )

        except Exception as e:
            print(
                "FREE BUTTON ERROR:",
                e,
            )

            await interaction.followup.send(
                "❌ Không thể lấy danh sách FREE.",
                ephemeral=True,
            )
            return

        if not results:
            await interaction.followup.send(
                (
                    "😢 Hiện tại chưa lấy được "
                    "game FREE nào."
                ),
                ephemeral=True,
            )
            return

        view = PaginationView(
            results,
            "🎁 GAME FREE HIỆN TẠI",
            color=0x2ECC71,
        )

        await interaction.followup.send(
            embed=view.build_embed(),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="THỂ LOẠI",
        emoji="🎭",
        style=discord.ButtonStyle.secondary,
        custom_id="game_panel_genre",
        row=0,
    )
    async def genre_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        embed = discord.Embed(
            title="🎭 CHỌN THỂ LOẠI",
            description=(
                "Chọn thể loại bên dưới.\n\n"
                "👻 Kinh dị\n"
                "⚔️ Hành động\n"
                "🗺️ Phiêu lưu\n"
                "🧙 RPG\n"
                "⚽ Thể thao\n"
                "🏎️ Đua xe\n"
                "♟️ Chiến thuật\n"
                "🎨 Indie\n"
                "🏗️ Mô phỏng\n"
                "🎮 Casual"
            ),
            color=0x9B59B6,
        )

        await interaction.response.send_message(
            embed=embed,
            view=GenreView(),
            ephemeral=True,
        )


# =========================================================
# SEND /GAME PANEL
# =========================================================

async def send_game_panel(channel):
    embed = discord.Embed(
        title="🎮 GAME CENTER",
        description=(
            "✨ **TÌM GAME • FREE GAME • THỂ LOẠI** ✨\n\n"
            "🔎 **TÌM GAME**\n"
            "Tìm game trên Epic + Steam.\n\n"
            "🎁 **FREE HIỆN TẠI**\n"
            "Xem game đang FREE ngay lúc này.\n\n"
            "🎭 **THỂ LOẠI**\n"
            "Chọn thể loại rồi xem danh sách game.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💰 Có giá **KRW + VNĐ**\n"
            "🖼️ Có ảnh game\n"
            "🔗 Có link store\n"
            "🎮 Bấm tên game để xem chi tiết."
        ),
        color=0x5865F2,
    )

    embed.set_footer(
        text="Epic Games Store • Steam"
    )

    await channel.send(
        embed=embed,
        view=GamePanelView(),
    )


# =========================================================
# /GAME COMMAND
# =========================================================

@bot.tree.command(
    name="game",
    description="Mở Game Center",
)
async def game_command(
    interaction: discord.Interaction,
):
    search_channel = find_search_channel(
        interaction.guild
    ) if interaction.guild else None

    if (
        search_channel
        and interaction.channel_id
        != search_channel.id
    ):
        await interaction.response.send_message(
            (
                f"❌ Lệnh này chỉ dùng trong "
                f"{search_channel.mention}"
            ),
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="🎮 GAME CENTER",
        description=(
            "⚡ Chọn chức năng bên dưới ⚡\n\n"
            "🔎 **Tìm Game**\n"
            "Tìm GTA, HITMAN, FIFA, Minecraft...\n\n"
            "🎁 **Free hiện tại**\n"
            "Xem game đang FREE.\n\n"
            "🎭 **Thể loại**\n"
            "Lọc game theo thể loại."
        ),
        color=0x5865F2,
    )

    await interaction.response.send_message(
        embed=embed,
        view=GamePanelView(),
    )


# =========================================================
# /CHANNELS
# =========================================================

@bot.tree.command(
    name="channels",
    description="Xem bot đang nhận diện channel nào",
)
async def channels_command(
    interaction: discord.Interaction,
):
    if not interaction.guild:
        await interaction.response.send_message(
            "❌ Chỉ dùng trong server.",
            ephemeral=True,
        )
        return

    notification = find_notification_channel(
        interaction.guild
    )

    search = find_search_channel(
        interaction.guild
    )

    embed = discord.Embed(
        title="📡 CHANNEL AUTO-DETECT",
        color=0x5865F2,
    )

    embed.add_field(
        name="🎁 Thông báo FREE",
        value=(
            notification.mention
            if notification
            else "❌ Không tìm thấy"
        ),
        inline=False,
    )

    embed.add_field(
        name="🎮 Tìm game",
        value=(
            search.mention
            if search
            else "❌ Không tìm thấy"
        ),
        inline=False,
    )

    embed.set_footer(
        text=(
            "Bot tự nhận diện tên channel "
            "không cần /setchannel"
        )
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


# =========================================================
# NOTIFICATION
# =========================================================

async def notify_free_game(
    channel,
    game,
):
    key = free_game_key(
        game
    )

    embed = make_free_embed(
        game
    )

    await channel.send(
        content=(
            "🚨✨ **FREE GAME MỚI!** ✨🚨"
        ),
        embed=embed,
    )

    if key not in DATA[
        "sent_free_games"
    ]:
        DATA[
            "sent_free_games"
        ].append(key)

    # giữ database gọn
    DATA[
        "sent_free_games"
    ] = DATA[
        "sent_free_games"
    ][-500:]

    save_data()


async def check_free_games():
    channels, _ = (
        get_all_guilds_channels()
    )

    if not channels:
        print(
            "⚠️ Không tìm thấy channel "
            "thong-bao-game"
        )

        return

    try:
        games = (
            await get_all_current_free()
        )

    except Exception as e:
        print(
            "FREE CHECK ERROR:",
            e,
        )
        return

    if not games:
        print(
            "ℹ️ Không lấy được game FREE."
        )
        return

    first_run = not DATA.get(
        "first_run_done",
        False,
    )

    for game in games:

        key = free_game_key(
            game
        )

        if key in DATA[
            "sent_free_games"
        ]:
            continue

        if first_run and not ANNOUNCE_EXISTING_ON_FIRST_RUN:

            DATA[
                "sent_free_games"
            ].append(key)

            continue

        for channel in channels:

            try:
                await notify_free_game(
                    channel,
                    game,
                )

                await asyncio.sleep(
                    1
                )

            except Exception as e:
                print(
                    "NOTIFY ERROR:",
                    e,
                )

    DATA[
        "first_run_done"
    ] = True

    save_data()


@tasks.loop(
    minutes=CHECK_INTERVAL_MINUTES
)
async def free_game_checker():
    print(
        "🔎 Checking free games..."
    )

    await check_free_games()


@free_game_checker.before_loop
async def before_checker():
    await bot.wait_until_ready()


# =========================================================
# AUTO SEND GAME PANEL
# =========================================================

async def ensure_game_panel():
    """
    Nếu tim-game tồn tại nhưng chưa có panel
    thì bot gửi panel.

    Không tự spam mỗi lần restart.
    """

    for guild in bot.guilds:

        channel = find_search_channel(
            guild
        )

        if not channel:
            continue

        found_recent = False

        try:
            async for message in channel.history(
                limit=50
            ):
                if (
                    message.author.id
                    == bot.user.id
                ):
                    if (
                        message.embeds
                        and message.components
                    ):
                        found_recent = True
                        break

        except Exception as e:
            print(
                "PANEL HISTORY ERROR:",
                e,
            )

        if not found_recent:

            try:
                await send_game_panel(
                    channel
                )

            except Exception as e:
                print(
                    "PANEL SEND ERROR:",
                    e,
                )


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    print(
        f"✅ BOT ONLINE: {bot.user}"
    )

    print(
        f"🆔 ID: {bot.user.id}"
    )

    print(
        f"🌐 Servers: {len(bot.guilds)}"
    )

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    try:
        synced = await bot.tree.sync()

        print(
            f"✅ Synced {len(synced)} slash commands"
        )

    except Exception as e:
        print(
            "❌ COMMAND SYNC ERROR:",
            e,
        )

    # persistent main view
    try:
        bot.add_view(
            GamePanelView()
        )
    except Exception:
        pass

    if not free_game_checker.is_running():
        free_game_checker.start()

    # cho Discord có thời gian hoàn tất ready
    await asyncio.sleep(3)

    await ensure_game_panel()


# =========================================================
# GUILD JOIN
# =========================================================

@bot.event
async def on_guild_join(
    guild
):
    print(
        f"➕ Joined guild: {guild.name}"
    )

    await asyncio.sleep(3)

    channel = find_search_channel(
        guild
    )

    if channel:
        try:
            await send_game_panel(
                channel
            )
        except Exception as e:
            print(
                "GUILD PANEL ERROR:",
                e,
            )


# =========================================================
# ERROR HANDLER
# =========================================================

@bot.tree.error
async def on_app_command_error(
    interaction,
    error,
):
    print(
        "SLASH COMMAND ERROR:",
        error,
    )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "❌ Có lỗi xảy ra.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Có lỗi xảy ra.",
                ephemeral=True,
            )

    except Exception:
        pass


# =========================================================
# SHUTDOWN
# =========================================================

async def close_session():
    global http_session

    if (
        http_session
        and not http_session.closed
    ):
        await http_session.close()


# =========================================================
# START BOT
# =========================================================

if not DISCORD_TOKEN:
    raise RuntimeError(
        "❌ Chưa có biến môi trường DISCORD_TOKEN"
    )


try:
    bot.run(
        DISCORD_TOKEN
    )

finally:
    try:
        loop = asyncio.get_event_loop()

        if not loop.is_closed():
            loop.run_until_complete(
                close_session()
            )

    except Exception:
        pass
