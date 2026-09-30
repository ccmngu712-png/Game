import os
import json
import asyncio
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

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

# False = bot không spam những game đã free từ trước khi bot chạy
ANNOUNCE_EXISTING_ON_FIRST_RUN = False

# Tỷ giá tham khảo cho Epic nếu API không trả trực tiếp
USD_TO_KRW = 1400
USD_TO_VND = 25000


# =========================================================
# DISCORD
# =========================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# DATA
# =========================================================

def load_data():
    default = {
        "epic_sent": [],
        "steam_sent": [],
        "epic_schedule_sent": [],
        "first_run_done": False
    }

    if not os.path.exists(DATA_FILE):
        return default

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)

        for key, value in default.items():
            saved.setdefault(key, value)

        return saved

    except Exception:
        return default


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
# CHANNEL
# =========================================================

def normalize_channel_name(name: str):
    name = name.strip().lower()

    if "・" in name:
        name = name.split("・", 1)[1]

    name = name.replace("_", "-")
    name = name.replace(" ", "-")

    return name


def find_channel(guild: discord.Guild, wanted_name: str):
    wanted = normalize_channel_name(wanted_name)

    for channel in guild.text_channels:
        if normalize_channel_name(channel.name) == wanted:
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

async def get_json(session, url, **kwargs):
    try:
        timeout = aiohttp.ClientTimeout(total=30)

        async with session.get(
            url,
            timeout=timeout,
            **kwargs
        ) as response:

            if response.status != 200:
                print(
                    f"⚠️ HTTP {response.status}: {url}"
                )
                return None

            return await response.json(
                content_type=None
            )

    except Exception as e:
        print("HTTP JSON error:", e)
        return None


async def get_text(session, url, **kwargs):
    try:
        timeout = aiohttp.ClientTimeout(total=30)

        async with session.get(
            url,
            timeout=timeout,
            **kwargs
        ) as response:

            if response.status != 200:
                return None

            return await response.text()

    except Exception as e:
        print("HTTP text error:", e)
        return None


async def post_json(
    session,
    url,
    payload,
    **kwargs
):
    try:
        timeout = aiohttp.ClientTimeout(total=30)

        async with session.post(
            url,
            json=payload,
            timeout=timeout,
            **kwargs
        ) as response:

            if response.status != 200:
                print(
                    f"⚠️ POST HTTP {response.status}: {url}"
                )
                return None

            return await response.json(
                content_type=None
            )

    except Exception as e:
        print("POST JSON error:", e)
        return None


# =========================================================
# GENERAL HELPERS
# =========================================================

def clean_text(text):
    if not text:
        return ""

    return " ".join(
        str(text).strip().split()
    )


def normalize_text(text):
    text = clean_text(text)

    text = unicodedata.normalize(
        "NFKD",
        text
    )

    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    return text.lower()


def tokenize(text):
    text = normalize_text(text)

    return [
        x
        for x in re.split(
            r"[^a-z0-9]+",
            text
        )
        if x
    ]


def unique_by_key(items, key_func):
    output = []
    seen = set()

    for item in items:
        try:
            key = key_func(item)
        except Exception:
            continue

        if not key:
            continue

        if key in seen:
            continue

        seen.add(key)
        output.append(item)

    return output


def now_utc():
    return datetime.now(timezone.utc)


def parse_datetime(value):
    if not value:
        return None

    try:
        value = value.replace(
            "Z",
            "+00:00"
        )

        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt

    except Exception:
        return None


def format_datetime(value):
    dt = parse_datetime(value)

    if not dt:
        return "Không rõ"

    return dt.astimezone().strftime(
        "%d/%m/%Y %H:%M"
    )


def countdown_text(value):
    dt = parse_datetime(value)

    if not dt:
        return "Không rõ"

    seconds = int(
        (dt - now_utc()).total_seconds()
    )

    if seconds <= 0:
        return "Đã bắt đầu"

    days = seconds // 86400
    seconds %= 86400

    hours = seconds // 3600
    seconds %= 3600

    minutes = seconds // 60

    parts = []

    if days:
        parts.append(f"{days} ngày")

    if hours:
        parts.append(f"{hours} giờ")

    if minutes:
        parts.append(f"{minutes} phút")

    if not parts:
        return "Sắp bắt đầu"

    return " ".join(parts[:3])


def usd_to_prices(usd):
    try:
        usd = float(usd)

        return (
            round(usd * USD_TO_KRW),
            round(usd * USD_TO_VND)
        )

    except Exception:
        return 0, 0


def format_krw(value):
    if value is None:
        return "Không có giá"

    try:
        value = float(value)

        # Steam price_overview.final thường là đơn vị nhỏ
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

        # Không chia nếu API đã trả VNĐ trực tiếp
        if value > 10000000:
            value = value / 100

        return f"{int(value):,}₫"

    except Exception:
        return "Không có giá"


# =========================================================
# SEARCH ALIASES
# =========================================================

def get_search_queries(query):
    """
    Một số game có tên mà người dùng thường gõ khác
    tên chính thức của store.

    Ví dụ:
    gta 5 -> gta 5 / gta v / grand theft auto v
    fifa  -> fifa / ea sports fc
    """
    original = clean_text(query)

    normalized = normalize_text(original)

    queries = [original]

    if normalized in (
        "gta",
        "gta 5",
        "gta5"
    ):
        queries.extend([
            "GTA",
            "GTA V",
            "Grand Theft Auto",
            "Grand Theft Auto V"
        ])

    elif normalized in (
        "fifa",
        "ea fc",
        "fc"
    ):
        queries.extend([
            "FIFA",
            "EA SPORTS FC",
            "EA FC"
        ])

    elif normalized in (
        "hitman",
        "hit man"
    ):
        queries.extend([
            "HITMAN",
            "HITMAN 2",
            "HITMAN 3",
            "Hitman World of Assassination"
        ])

    elif normalized in (
        "cod",
        "call of duty"
    ):
        queries.extend([
            "Call of Duty"
        ])

    elif normalized in (
        "spiderman",
        "spider man"
    ):
        queries.extend([
            "Marvel's Spider-Man",
            "Spider-Man"
        ])

    # Loại trùng
    output = []
    seen = set()

    for item in queries:
        key = normalize_text(item)

        if key in seen:
            continue

        seen.add(key)
        output.append(item)

    return output


# =========================================================
# SEARCH SCORE
# =========================================================

BAD_TITLE_WORDS = [
    "soundtrack",
    "ost",
    "wallpaper",
    "avatar",
    "artbook",
    "guide",
    "tutorial",
    "demo",
    "playtest"
]


def score_game_title(
    title,
    query
):
    title_n = normalize_text(title)
    query_n = normalize_text(query)

    if not title_n:
        return -999999

    if not query_n:
        return 0

    score = 0

    title_tokens = tokenize(title_n)
    query_tokens = tokenize(query_n)

    # Tên chính xác
    if title_n == query_n:
        score += 10000

    # Bắt đầu bằng query
    if title_n.startswith(query_n):
        score += 5000

    # Chứa nguyên query
    if query_n in title_n:
        score += 3500

    # Từng từ của query
    for token in query_tokens:

        if token in title_tokens:
            score += 1200

        elif token in title_n:
            score += 500

        else:
            score -= 300

    # Game title ngắn thường gần kết quả hơn
    if len(title_tokens) <= len(query_tokens) + 3:
        score += 300

    # Bản game / edition / bundle vẫn giữ,
    # nhưng DLC linh tinh bị giảm điểm
    for word in BAD_TITLE_WORDS:
        if word in title_n:
            score -= 5000

    if "dlc" in title_n:
        score -= 1800

    if "season pass" in title_n:
        score -= 1800

    # Edition / Bundle vẫn là kết quả hợp lệ
    if "edition" in title_n:
        score += 150

    if "bundle" in title_n:
        score += 100

    if "complete" in title_n:
        score += 100

    if "deluxe" in title_n:
        score += 100

    if "gold" in title_n:
        score += 100

    if "ultimate" in title_n:
        score += 100

    return score


# =========================================================
# EPIC
# =========================================================

EPIC_GRAPHQL_URL = (
    "https://store.epicgames.com/graphql"
)

EPIC_FREE_URL = (
    "https://store-site-backend-static.ak.epicgames.com/"
    "freeGamesPromotions"
)


async def search_epic_once(
    session,
    query
):
    """
    Epic searchStore.

    Lấy nhiều kết quả thay vì chỉ lấy 1.
    """

    graphql = """
    query searchStoreQuery(
        $keywords: String!
        $country: String!
        $locale: String
        $start: Int
        $count: Int
        $category: String
    ) {
        Catalog {
            searchStore(
                keywords: $keywords
                country: $country
                locale: $locale
                start: $start
                count: $count
                category: $category
            ) {
                paging {
                    total
                    count
                }
                elements {
                    id
                    namespace
                    title
                    productSlug
                    urlSlug
                    keyImages {
                        type
                        url
                    }
                    price(country: $country) {
                        totalPrice {
                            originalPrice
                            discountPrice
                            fmtPrice(locale: $locale) {
                                originalPrice
                                discountPrice
                            }
                        }
                    }
                }
            }
        }
    }
    """

    variables = {
        "keywords": query,
        "country": "KR",
        "locale": "en-US",
        "start": 0,
        "count": 40,
        "category": (
            "games/edition/base|"
            "games/edition|"
            "bundles/games|"
            "games/experience|"
            "games/demo"
        )
    }

    payload = {
        "query": graphql,
        "variables": variables
    }

    result = await post_json(
        session,
        EPIC_GRAPHQL_URL,
        payload
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

    except Exception as e:
        print(
            "❌ Epic search parse error:",
            e
        )
        return []

    results = []

    for game in elements:

        title = clean_text(
            game.get("title")
        )

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

        for image_data in (
            game.get("keyImages") or []
        ):
            image_type = image_data.get("type")

            if image_type in (
                "OfferImageWide",
                "DieselStoreFrontWide",
                "Thumbnail"
            ):
                image = image_data.get("url")
                break

        if not image:
            for image_data in (
                game.get("keyImages") or []
            ):
                image = image_data.get("url")

                if image:
                    break

        original_krw = None
        original_vnd = None

        try:
            total_price = (
                game
                .get("price", {})
                .get("totalPrice", {})
            )

            original_price = (
                total_price.get(
                    "originalPrice"
                )
            )

            if original_price is not None:

                # Epic thường trả giá theo đơn vị nhỏ
                if original_price > 100000:
                    original_krw = (
                        original_price / 100
                    )
                else:
                    original_krw = (
                        original_price
                    )

        except Exception:
            pass

        score = score_game_title(
            title,
            query
        )

        results.append({
            "id": (
                game.get("id")
                or game.get("namespace")
                or slug
            ),
            "title": title,
            "url": url,
            "image": image,
            "original_krw": original_krw,
            "original_vnd": original_vnd,
            "score": score
        })

    return results


async def search_epic(
    session,
    query
):
    """
    Tìm nhiều biến thể từ khóa rồi gom lại.
    """

    queries = get_search_queries(query)

    tasks_list = [
        search_epic_once(
            session,
            item
        )
        for item in queries
    ]

    responses = await asyncio.gather(
        *tasks_list,
        return_exceptions=True
    )

    combined = []

    for response in responses:
        if isinstance(
            response,
            list
        ):
            combined.extend(response)

    combined = unique_by_key(
        combined,
        lambda x: x.get("id")
    )

    # Chấm điểm lại theo từ khóa gốc
    for item in combined:
        item["score"] = score_game_title(
            item.get("title", ""),
            query
        )

    # Không vứt edition/bundle.
    # Chỉ đẩy DLC/demo linh tinh xuống dưới.
    combined.sort(
        key=lambda x: (
            x.get("score", -999999),
            normalize_text(
                x.get("title", "")
            )
        ),
        reverse=True
    )

    return combined[:25]


# =========================================================
# EPIC FREE GAMES
# =========================================================

async def get_epic_free_games(
    session
):
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

    except Exception as e:
        print(
            "❌ Epic free parse error:",
            e
        )
        return []

    games = []

    for game in elements:

        try:
            title = clean_text(
                game.get("title")
            )

            if not title:
                continue

            promotions = (
                game.get("promotions")
                or {}
            )

            promotional_offers = (
                promotions
                .get("promotionalOffers")
                or []
            )

            upcoming_offers = (
                promotions
                .get("upcomingPromotionalOffers")
                or []
            )

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

            for img in (
                game.get("keyImages") or []
            ):
                if img.get("type") in (
                    "OfferImageWide",
                    "DieselStoreFrontWide"
                ):
                    image = img.get("url")
                    break

            if not image:
                for img in (
                    game.get("keyImages") or []
                ):
                    image = img.get("url")

                    if image:
                        break

            # ---------------------------------------------
            # ĐANG FREE
            # ---------------------------------------------

            for offer_group in promotional_offers:

                for offer in (
                    offer_group.get(
                        "promotionalOffers"
                    )
                    or []
                ):

                    discount_setting = (
                        offer.get(
                            "discountSetting"
                        )
                        or {}
                    )

                    if (
                        discount_setting
                        .get("discountPercentage")
                        != 0
                    ):
                        continue

                    games.append({
                        "id": (
                            game.get("id")
                            or slug
                        ),
                        "title": title,
                        "url": url,
                        "image": image,
                        "start_date": (
                            offer.get(
                                "startDate"
                            )
                        ),
                        "end_date": (
                            offer.get(
                                "endDate"
                            )
                        ),
                        "original_krw": 0,
                        "original_vnd": 0,
                        "active": True
                    })

            # ---------------------------------------------
            # SẮP FREE
            # ---------------------------------------------

            for offer_group in upcoming_offers:

                for offer in (
                    offer_group.get(
                        "promotionalOffers"
                    )
                    or []
                ):

                    discount_setting = (
                        offer.get(
                            "discountSetting"
                        )
                        or {}
                    )

                    if (
                        discount_setting
                        .get("discountPercentage")
                        != 0
                    ):
                        continue

                    games.append({
                        "id": (
                            game.get("id")
                            or slug
                        ),
                        "title": title,
                        "url": url,
                        "image": image,
                        "start_date": (
                            offer.get(
                                "startDate"
                            )
                        ),
                        "end_date": (
                            offer.get(
                                "endDate"
                            )
                        ),
                        "original_krw": 0,
                        "original_vnd": 0,
                        "active": False
                    })

        except Exception:
            continue

    # Loại trùng
    games = unique_by_key(
        games,
        lambda x: (
            str(x.get("id"))
            + "|"
            + str(x.get("start_date"))
            + "|"
            + str(x.get("end_date"))
        )
    )

    return games


# =========================================================
# STEAM SEARCH API
# =========================================================

STEAM_SEARCH_API = (
    "https://store.steampowered.com/api/storesearch"
)

STEAM_APPDETAILS_API = (
    "https://store.steampowered.com/api/appdetails"
)


async def steam_search_once(
    session,
    query
):
    params = {
        "term": query,
        "l": "english",
        "cc": "KR",
        "category1": "998"
    }

    data_json = await get_json(
        session,
        STEAM_SEARCH_API,
        params=params,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(iPhone; CPU iPhone OS 18_0 like Mac OS X) "
                "AppleWebKit/605.1.15 "
                "Version/18.0 Mobile/15E148 Safari/604.1"
            )
        }
    )

    if not data_json:
        return []

    items = (
        data_json.get("items")
        or []
    )

    results = []

    for item in items:

        # Chỉ lấy app/game
        item_type = (
            item.get("type")
            or "app"
        )

        if item_type != "app":
            continue

        appid = (
            item.get("id")
            or item.get("appid")
        )

        title = clean_text(
            item.get("name")
        )

        if not appid or not title:
            continue

        url = (
            f"https://store.steampowered.com/app/"
            f"{appid}/"
        )

        image = (
            item.get("tiny_image")
            or item.get("header_image")
        )

        score = score_game_title(
            title,
            query
        )

        # Giá Steam search
        price_data = (
            item.get("price")
            or {}
        )

        results.append({
            "appid": str(appid),
            "title": title,
            "url": url,
            "image": image,
            "score": score,
            "krw": (
                price_data.get("final")
                if isinstance(
                    price_data,
                    dict
                )
                else None
            ),
            "vnd": None
        })

    return results


async def get_steam_app_details(
    session,
    appid,
    country
):
    params = {
        "appids": str(appid),
        "cc": country,
        "l": "english"
    }

    result = await get_json(
        session,
        STEAM_APPDETAILS_API,
        params=params
    )

    if not result:
        return None

    try:
        item = result.get(
            str(appid)
        )

        if not item:
            return None

        if not item.get("success"):
            return None

        return item.get("data")

    except Exception:
        return None


async def get_steam_prices(
    session,
    appid
):
    """
    Lấy KRW + VNĐ song song.
    """

    kr_task = get_steam_app_details(
        session,
        appid,
        "KR"
    )

    vn_task = get_steam_app_details(
        session,
        appid,
        "VN"
    )

    kr_data, vn_data = await asyncio.gather(
        kr_task,
        vn_task,
        return_exceptions=True
    )

    if isinstance(
        kr_data,
        Exception
    ):
        kr_data = None

    if isinstance(
        vn_data,
        Exception
    ):
        vn_data = None

    return kr_data, vn_data


async def search_steam(
    session,
    query
):
    """
    Tìm nhiều từ khóa rồi gom tất cả kết quả.
    """

    queries = get_search_queries(query)

    tasks_list = [
        steam_search_once(
            session,
            item
        )
        for item in queries
    ]

    responses = await asyncio.gather(
        *tasks_list,
        return_exceptions=True
    )

    combined = []

    for response in responses:
        if isinstance(
            response,
            list
        ):
            combined.extend(response)

    # Loại app trùng
    combined = unique_by_key(
        combined,
        lambda x: x.get("appid")
    )

    # Chấm lại theo query gốc
    for item in combined:
        item["score"] = score_game_title(
            item.get("title", ""),
            query
        )

    # Sắp xếp theo độ khớp
    combined.sort(
        key=lambda x: (
            x.get("score", -999999),
            normalize_text(
                x.get("title", "")
            )
        ),
        reverse=True
    )

    # Lấy nhiều kết quả.
    # Không chỉ 5 game.
    candidates = combined[:30]

    # Lấy giá song song
    price_tasks = [
        get_steam_prices(
            session,
            item["appid"]
        )
        for item in candidates
    ]

    price_results = await asyncio.gather(
        *price_tasks,
        return_exceptions=True
    )

    final = []

    for item, prices in zip(
        candidates,
        price_results
    ):

        if isinstance(
            prices,
            Exception
        ):
            prices = (
                None,
                None
            )

        kr_data, vn_data = prices

        # ---------------------------------------------
        # KR
        # ---------------------------------------------

        if kr_data:
            item["image"] = (
                kr_data.get(
                    "header_image"
                )
                or item.get("image")
            )

            price = kr_data.get(
                "price_overview"
            )

            if price:
                item["krw"] = price.get(
                    "final"
                )

        # ---------------------------------------------
        # VN
        # ---------------------------------------------

        if vn_data:
            price = vn_data.get(
                "price_overview"
            )

            if price:
                item["vnd"] = price.get(
                    "final"
                )

        # Nếu API trả game nhưng không có giá
        # vẫn giữ kết quả.
        final.append(item)

    return final


# =========================================================
# STEAM FREE GAMES
# =========================================================

async def get_steam_free_games(
    session
):
    """
    Tìm các game đang giảm 100%.

    Không tự đoán lịch tương lai của Steam.
    """

    url = (
        "https://store.steampowered.com/search/results/"
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
        url,
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )

    if not html:
        return []

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    rows = soup.select(
        "a.search_result_row"
    )

    results = []

    for row in rows:

        title_el = row.select_one(
            ".title"
        )

        if not title_el:
            continue

        title = clean_text(
            title_el.get_text()
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

        # Game vốn free từ đầu
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

        final_price = price.get(
            "final",
            0
        )

        discount = price.get(
            "discount_percent",
            0
        )

        if initial <= 0:
            continue

        if final_price != 0:
            continue

        if discount != 100:
            continue

        description = clean_text(
            details.get(
                "short_description",
                ""
            )
        )

        combined = (
            title
            + " "
            + description
        ).lower()

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
                "https://store.steampowered.com/app/"
                f"{appid}/"
            ),
            "image": details.get(
                "header_image"
            ),
            "original_krw": initial,
            "original_vnd": None
        })

    return unique_by_key(
        results,
        lambda x: x.get("id")
    )


# =========================================================
# SEARCH EMBED
# =========================================================

def make_search_embed(
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
            f"🎮・KẾT QUẢ: "
            f"{query.upper()}"
        ),
        description=(
            f"{icon} **{store_name}**\n"
            f"🔎 Tìm thấy **{len(results)}** "
            "kết quả liên quan\n"
            "⚡ Các phiên bản / Edition / Bundle "
            "liên quan đều được liệt kê."
        ),
        color=color
    )

    for index, game in enumerate(
        results,
        start=1
    ):
        title = game.get(
            "title",
            "Không tên"
        )

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

        url = game.get(
            "url",
            "#"
        )

        value = (
            f"💰 **KRW:** {krw}\n"
            f"💵 **VNĐ:** {vnd}\n"
            f"🔗 [Mở trang game]({url})"
        )

        embed.add_field(
            name=f"#{index}・{title}",
            value=value,
            inline=False
        )

    # Discord giới hạn embed.
    # Nếu quá dài, chỉ lấy các kết quả
    # quan trọng nhất nhưng vẫn cố giữ nhiều edition.
    if len(embed.fields) > 20:
        # Discord cho phép tối đa 25 field.
        pass

    # Một ảnh lớn ở dưới
    image = None

    for game in results:
        if game.get("image"):
            image = game.get("image")
            break

    if image:
        embed.set_image(
            url=image
        )

    embed.set_footer(
        text=(
            "🎮 Free Game Bot • "
            f"{store_name} • "
            f"{len(results)} kết quả"
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

    return discord.Embed(
        title="🔎・KHÔNG TÌM THẤY",
        description=(
            f"{icon} Không tìm thấy kết quả phù hợp "
            f"với **{query}** trên **{name}**.\n\n"
            "💡 Thử tên ngắn hơn:\n"
            "`gta`\n"
            "`hitman`\n"
            "`fifa`\n"
            "`minecraft`"
        ),
        color=color
    )


# =========================================================
# FREE GAME EMBED
# =========================================================

def make_free_embed(
    store,
    game
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
            "⚡✨🎁・GAME FREE "
            "CHỚP CHỚP・🎁✨⚡"
        ),
        description=(
            f"{icon} **{store_name}**\n\n"
            f"🎮 **{game['title']}**\n\n"
            "🟢 **MIỄN PHÍ 100%**\n"
            "🎁 Nhận game ngay trước khi hết hạn!"
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
            name="💰 Giá gốc KRW",
            value=(
                f"~~₩{int(original_krw):,}~~"
            ),
            inline=True
        )

    if original_vnd:
        embed.add_field(
            name="💵 Giá gốc VNĐ",
            value=(
                f"~~{int(original_vnd):,}₫~~"
            ),
            inline=True
        )

    embed.add_field(
        name="🎁 Giá hiện tại",
        value="**FREE**",
        inline=True
    )

    if game.get("start_date"):
        embed.add_field(
            name="📅 Bắt đầu",
            value=format_datetime(
                game.get(
                    "start_date"
                )
            ),
            inline=True
        )

    if game.get("end_date"):
        embed.add_field(
            name="⏳ Hết hạn",
            value=(
                f"{format_datetime(game.get('end_date'))}\n"
                f"⚡ Còn: "
                f"**{countdown_text(game.get('end_date'))}**"
            ),
            inline=True
        )

    if game.get("image"):
        embed.set_image(
            url=game["image"]
        )

    embed.set_footer(
        text=(
            "⚡🎁 Free Game Bot • "
            "Đừng quên claim game!"
        )
    )

    return embed


# =========================================================
# EPIC SCHEDULE EMBED
# =========================================================

def make_schedule_embed(
    game
):
    embed = discord.Embed(
        title=(
            "📅⚡・SẮP CÓ GAME FREE・⚡📅"
        ),
        description=(
            "✨ Canh lịch để nhận game miễn phí!\n\n"
            f"🎮 **{game['title']}**\n"
            "🟣 **Epic Games Store**"
        ),
        color=0x7C3AED,
        url=game["url"]
    )

    if game.get("start_date"):
        embed.add_field(
            name="📅 Ngày bắt đầu",
            value=(
                f"**{format_datetime(game['start_date'])}**\n"
                f"⚡ Còn khoảng: "
                f"**{countdown_text(game['start_date'])}**"
            ),
            inline=False
        )

    if game.get("end_date"):
        embed.add_field(
            name="⏳ Ngày kết thúc",
            value=format_datetime(
                game["end_date"]
            ),
            inline=True
        )

    embed.add_field(
        name="🗓 Hôm nay",
        value=now_utc().astimezone().strftime(
            "%d/%m/%Y"
        ),
        inline=True
    )

    if game.get("image"):
        embed.set_image(
            url=game["image"]
        )

    embed.set_footer(
        text=(
            "📅⚡ Free Game Bot • "
            "Canh giờ nhận game"
        )
    )

    return embed


# =========================================================
# STORE BUTTON
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
                url=url,
                style=discord.ButtonStyle.link
            )
        )


# =========================================================
# SEARCH MODAL
# =========================================================

class GameSearchModal(
    discord.ui.Modal
):
    def __init__(self, store):
        super().__init__(
            title=(
                "🔎 Tìm game - "
                + (
                    "Epic"
                    if store == "epic"
                    else "Steam"
                )
            )
        )

        self.store = store

        self.game_name = discord.ui.TextInput(
            label="Tên game",
            placeholder=(
                "Ví dụ: gta / hitman / fifa / minecraft"
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
        interaction: discord.Interaction
    ):
        query = clean_text(
            self.game_name.value
        )

        await interaction.response.defer(
            ephemeral=True
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
                ),
                ephemeral=True
            )
            return

        embed = make_search_embed(
            self.store,
            query,
            results
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True
        )


# =========================================================
# STORE SEARCH BUTTONS
# =========================================================

class StoreSearchView(
    discord.ui.View
):
    def __init__(self):
        super().__init__(
            timeout=300
        )

    @discord.ui.button(
        label="Epic",
        emoji="🟣",
        style=discord.ButtonStyle.primary
    )
    async def epic_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
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
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.send_modal(
            GameSearchModal("steam")
        )


# =========================================================
# /GAME
# =========================================================

@bot.tree.command(
    name="game",
    description="Tìm game trên Epic hoặc Steam"
)
async def game_command(
    interaction: discord.Interaction
):
    embed = discord.Embed(
        title=(
            "🎮⚡・TÌM GAME"
        ),
        description=(
            "Chọn cửa hàng muốn tìm:\n\n"
            "🟣 **Epic**\n"
            "🔵 **Steam**\n\n"
            "💡 Chỉ cần nhập một phần tên.\n"
            "Ví dụ `hitman` sẽ liệt kê "
            "các phiên bản Hitman mà store tìm thấy."
        ),
        color=0x5865F2
    )

    embed.set_footer(
        text=(
            "🎁 Free Game Bot • "
            "Search nhiều phiên bản"
        )
    )

    await interaction.response.send_message(
        embed=embed,
        view=StoreSearchView(),
        ephemeral=True
    )


# =========================================================
# /CHANNELS
# =========================================================

@bot.tree.command(
    name="channels",
    description="Kiểm tra bot đang nhận diện channel nào"
)
async def channels_command(
    interaction: discord.Interaction
):
    notification = find_channel(
        interaction.guild,
        NOTIFICATION_CHANNEL_NAME
    )

    search = find_channel(
        interaction.guild,
        SEARCH_CHANNEL_NAME
    )

    notification_text = (
        notification.mention
        if notification
        else "❌ Không tìm thấy"
    )

    search_text = (
        search.mention
        if search
        else "❌ Không tìm thấy"
    )

    embed = discord.Embed(
        title="📡・CHANNEL BOT",
        description=(
            f"🎁 Thông báo game: "
            f"{notification_text}\n"
            f"🎮 Tìm game: "
            f"{search_text}"
        ),
        color=0x5865F2
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# SEND EPIC SCHEDULE
# =========================================================

async def send_epic_schedule(
    channel,
    games
):
    changed = False

    current = now_utc()

    for game in games:

        if game.get("active"):
            continue

        start = parse_datetime(
            game.get("start_date")
        )

        if not start:
            continue

        if start <= current:
            continue

        unique_id = (
            f"{game.get('id')}:"
            f"{game.get('start_date')}:"
            f"{game.get('end_date')}"
        )

        if unique_id in data[
            "epic_schedule_sent"
        ]:
            continue

        embed = make_schedule_embed(
            game
        )

        try:
            await channel.send(
                embed=embed,
                view=StoreLinkView(
                    game["url"],
                    "🎁 MỞ EPIC"
                )
            )

            data[
                "epic_schedule_sent"
            ].append(
                unique_id
            )

            changed = True

        except Exception as e:
            print(
                "❌ Lỗi gửi Epic schedule:",
                e
            )

    if changed:
        save_data()


# =========================================================
# SEND EPIC FREE
# =========================================================

async def send_epic_notifications(
    channel,
    games
):
    changed = False

    for game in games:

        if not game.get("active"):
            continue

        game_id = str(
            game.get("id")
        )

        if game_id in data[
            "epic_sent"
        ]:
            continue

        # Lần chạy đầu không spam game cũ
        if not data[
            "first_run_done"
        ] and not ANNOUNCE_EXISTING_ON_FIRST_RUN:

            data[
                "epic_sent"
            ].append(game_id)

            changed = True
            continue

        embed = make_free_embed(
            "epic",
            game
        )

        try:
            await channel.send(
                content=(
                    "⚡✨🎁 "
                    "@everyone "
                    "GAME FREE MỚI "
                    "🎁✨⚡"
                ),
                embed=embed,
                view=StoreLinkView(
                    game["url"],
                    "🟣 MỞ EPIC"
                ),
                allowed_mentions=discord.AllowedMentions(
                    everyone=True
                )
            )

            data[
                "epic_sent"
            ].append(game_id)

            changed = True

        except Exception as e:
            print(
                "❌ Lỗi gửi Epic:",
                e
            )

    if changed:
        save_data()


# =========================================================
# SEND STEAM FREE
# =========================================================

async def send_steam_notifications(
    channel,
    games
):
    changed = False

    for game in games:

        game_id = str(
            game.get("id")
        )

        if game_id in data[
            "steam_sent"
        ]:
            continue

        # Lần chạy đầu
        if not data[
            "first_run_done"
        ] and not ANNOUNCE_EXISTING_ON_FIRST_RUN:

            data[
                "steam_sent"
            ].append(game_id)

            changed = True
            continue

        embed = make_free_embed(
            "steam",
            game
        )

        try:
            await channel.send(
                content=(
                    "⚡✨🎁 "
                    "@everyone "
                    "STEAM ĐANG FREE "
                    "🎁✨⚡"
                ),
                embed=embed,
                view=StoreLinkView(
                    game["url"],
                    "🔵 MỞ STEAM"
                ),
                allowed_mentions=discord.AllowedMentions(
                    everyone=True
                )
            )

            data[
                "steam_sent"
            ].append(game_id)

            changed = True

        except Exception as e:
            print(
                "❌ Lỗi gửi Steam:",
                e
            )

    if changed:
        save_data()


# =========================================================
# FREE GAME CHECKER
# =========================================================

@tasks.loop(
    minutes=CHECK_INTERVAL_MINUTES
)
async def free_game_checker():

    channel = get_notification_channel()

    if not channel:
        print(
            "⚠️ Không tìm thấy channel:",
            NOTIFICATION_CHANNEL_NAME
        )
        return

    print(
        "🔎 Đang kiểm tra game miễn phí..."
    )

    async with aiohttp.ClientSession() as session:

        epic_task = get_epic_free_games(
            session
        )

        steam_task = get_steam_free_games(
            session
        )

        epic_games, steam_games = await asyncio.gather(
            epic_task,
            steam_task,
            return_exceptions=True
        )

    if isinstance(
        epic_games,
        Exception
    ):
        print(
            "❌ Epic checker:",
            epic_games
        )
        epic_games = []

    if isinstance(
        steam_games,
        Exception
    ):
        print(
            "❌ Steam checker:",
            steam_games
        )
        steam_games = []

    # Epic lịch
    await send_epic_schedule(
        channel,
        epic_games
    )

    # Epic đang free
    await send_epic_notifications(
        channel,
        epic_games
    )

    # Steam đang free
    await send_steam_notifications(
        channel,
        steam_games
    )

    data[
        "first_run_done"
    ] = True

    save_data()

    print(
        "✅ Kiểm tra game hoàn tất."
    )


# =========================================================
# ERROR HANDLER
# =========================================================

@bot.tree.error
async def on_app_command_error(
    interaction,
    error
):
    print(
        "❌ Slash command error:",
        error
    )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "❌ Có lỗi xảy ra.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ Có lỗi xảy ra.",
                ephemeral=True
            )

    except Exception:
        pass


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    print(
        f"✅ Bot online: {bot.user}"
    )

    print(
        f"🏠 Servers: {len(bot.guilds)}"
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

        print(
            f"📡 {guild.name}"
        )

        print(
            "   🎁 thông báo:",
            notification.name
            if notification
            else "KHÔNG TÌM THẤY"
        )

        print(
            "   🎮 tìm game:",
            search.name
            if search
            else "KHÔNG TÌM THẤY"
        )

    try:
        synced = await bot.tree.sync()

        print(
            f"✅ Sync {len(synced)} slash command"
        )

    except Exception as e:
        print(
            "❌ Sync command lỗi:",
            e
        )

    if not free_game_checker.is_running():
        free_game_checker.start()


# =========================================================
# START
# =========================================================

if not DISCORD_TOKEN:
    raise RuntimeError(
        "❌ Thiếu DISCORD_TOKEN trong Railway Variables."
    )

bot.run(DISCORD_TOKEN)
