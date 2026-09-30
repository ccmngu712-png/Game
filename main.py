import os
import json
import asyncio
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

import discord
from discord.ext import commands, tasks


# =========================================================
# CONFIG
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Bot tự tìm channel theo tên này.
# Có thể đặt:
# 🎁・thong-bao-game
# 🎮・tim-game
#
# Hoặc:
# thong-bao-game
# tim-game

NOTIFICATION_CHANNEL_NAME = "thong-bao-game"
SEARCH_CHANNEL_NAME = "tim-game"

DATA_FILE = "bot_data.json"

# Kiểm tra game miễn phí mỗi 15 phút
CHECK_INTERVAL_MINUTES = 15

# False = lần đầu bot chạy sẽ không spam những game
# vốn đã miễn phí từ trước.
ANNOUNCE_EXISTING_ON_FIRST_RUN = False

# Tỷ giá tham khảo cho Epic
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

def default_data():
    return {
        "epic_sent": [],
        "steam_sent": [],
        "first_run_done": False
    }


def load_data():
    if not os.path.exists(DATA_FILE):
        return default_data()

    try:
        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            saved = json.load(f)

        data = default_data()

        if isinstance(saved, dict):
            data.update(saved)

        if not isinstance(data.get("epic_sent"), list):
            data["epic_sent"] = []

        if not isinstance(data.get("steam_sent"), list):
            data["steam_sent"] = []

        if not isinstance(
            data.get("first_run_done"),
            bool
        ):
            data["first_run_done"] = False

        return data

    except Exception as e:
        print(
            "⚠️ Không đọc được bot_data.json:",
            e
        )

        return default_data()


def save_data():
    try:
        with open(
            DATA_FILE,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )
    except Exception as e:
        print(
            "❌ Không lưu được data:",
            e
        )


data = load_data()


# =========================================================
# CHANNEL
# =========================================================

def normalize_channel_name(name: str):
    """
    Cho phép:

    thong-bao-game
    🎁・thong-bao-game
    🎁・Thong-Bao-Game

    tim-game
    🎮・tim-game
    """

    name = name.strip().lower()

    # Bỏ phần emoji trước dấu ・
    if "・" in name:
        name = name.split(
            "・",
            1
        )[1]

    name = name.replace(
        "_",
        "-"
    )

    name = name.replace(
        " ",
        "-"
    )

    return name


def find_channel(
    guild: discord.Guild,
    wanted_name: str
):
    wanted = normalize_channel_name(
        wanted_name
    )

    for channel in guild.text_channels:

        current = normalize_channel_name(
            channel.name
        )

        if current == wanted:
            return channel

    return None


def get_notification_channel():
    """
    Tự động tìm:
    🎁・thong-bao-game
    """

    for guild in bot.guilds:

        channel = find_channel(
            guild,
            NOTIFICATION_CHANNEL_NAME
        )

        if channel:
            return channel

    return None


def get_search_channel():
    """
    Tự động tìm:
    🎮・tim-game
    """

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

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 "
        "Version/18.0 Mobile/15E148 Safari/604.1"
    )
}


async def get_json(
    session,
    url,
    **kwargs
):
    try:

        headers = kwargs.pop(
            "headers",
            None
        )

        if headers is None:
            headers = DEFAULT_HEADERS

        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=30
            ),
            **kwargs
        ) as response:

            if response.status != 200:
                print(
                    f"⚠️ GET {response.status}: {url}"
                )
                return None

            return await response.json(
                content_type=None
            )

    except Exception as e:

        print(
            "⚠️ GET JSON error:",
            e
        )

        return None


async def get_text(
    session,
    url,
    **kwargs
):
    try:

        headers = kwargs.pop(
            "headers",
            None
        )

        if headers is None:
            headers = DEFAULT_HEADERS

        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=30
            ),
            **kwargs
        ) as response:

            if response.status != 200:
                print(
                    f"⚠️ GET {response.status}: {url}"
                )
                return None

            return await response.text()

    except Exception as e:

        print(
            "⚠️ GET text error:",
            e
        )

        return None


# =========================================================
# EPIC CONFIG
# =========================================================

EPIC_FREE_URL = (
    "https://store-site-backend-static.ak.epicgames.com/"
    "freeGamesPromotions"
)

EPIC_GRAPHQL_URL = (
    "https://store.epicgames.com/graphql"
)


# =========================================================
# EPIC PRICE
# =========================================================

def epic_usd_to_prices(
    usd
):
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

    except Exception:

        print(
            "⚠️ Epic response không đúng format."
        )

        return []

    games = []

    for game in elements:

        try:

            title = game.get(
                "title"
            )

            if not title:
                continue

            promotions = game.get(
                "promotions"
            )

            if not promotions:
                continue

            promotional_offers = (
                promotions.get(
                    "promotionalOffers"
                )
                or []
            )

            if not promotional_offers:
                continue

            offers = promotional_offers[0].get(
                "promotionalOffers"
            ) or []

            if not offers:
                continue

            offer = offers[0]

            discount = offer.get(
                "discountSetting",
                {}
            )

            discount_percentage = (
                discount.get(
                    "discountPercentage"
                )
            )

            # 0% discount = đang FREE
            if discount_percentage != 0:
                continue

            start_date = offer.get(
                "startDate"
            )

            end_date = offer.get(
                "endDate"
            )

            slug = (
                game.get("productSlug")
                or game.get("urlSlug")
            )

            if not slug:
                continue

            slug = slug.split(
                "/"
            )[0]

            url = (
                "https://store.epicgames.com/p/"
                + slug
            )

            game_id = (
                game.get("id")
                or slug
            )

            # ---------------------------------------------
            # IMAGE
            # ---------------------------------------------

            image = None

            for img in game.get(
                "keyImages",
                []
            ):

                if img.get("type") in (
                    "OfferImageWide",
                    "DieselStoreFrontWide"
                ):

                    image = img.get(
                        "url"
                    )

                    if image:
                        break

            if not image:

                for img in game.get(
                    "keyImages",
                    []
                ):

                    image = img.get(
                        "url"
                    )

                    if image:
                        break

            # ---------------------------------------------
            # ORIGINAL PRICE
            # ---------------------------------------------

            original_krw = 0
            original_vnd = 0

            price = game.get(
                "price",
                {}
            )

            total_price = price.get(
                "totalPrice",
                {}
            )

            original_price = (
                total_price.get(
                    "originalPrice"
                )
                or total_price.get(
                    "discountedPrice"
                )
            )

            if original_price:

                try:

                    usd = (
                        float(original_price)
                        / 100000
                    )

                    (
                        original_krw,
                        original_vnd
                    ) = epic_usd_to_prices(
                        usd
                    )

                except Exception:
                    pass

            games.append({
                "id": str(game_id),
                "title": title,
                "url": url,
                "image": image,
                "start_date": start_date,
                "end_date": end_date,
                "original_krw": original_krw,
                "original_vnd": original_vnd
            })

        except Exception as e:

            print(
                "⚠️ Epic game parse error:",
                e
            )

            continue

    return games


# =========================================================
# EPIC SEARCH
# =========================================================

async def search_epic(
    session,
    query
):

    graphql = """
    query searchStoreQuery(
        $keyword: String!
        $locale: String
    ) {
        Catalog {
            searchStore(
                keywords: $keyword
                locale: $locale
                count: 30
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
                        }
                    }
                }
            }
        }
    }
    """

    payload = {
        "query": graphql,
        "variables": {
            "keyword": query,
            "locale": "en-US"
        }
    }

    try:

        async with session.post(
            EPIC_GRAPHQL_URL,
            json=payload,
            headers=DEFAULT_HEADERS,
            timeout=aiohttp.ClientTimeout(
                total=30
            )
        ) as response:

            if response.status != 200:

                print(
                    "⚠️ Epic search status:",
                    response.status
                )

                return []

            result = await response.json(
                content_type=None
            )

    except Exception as e:

        print(
            "❌ Epic search error:",
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

    results = []

    query_lower = query.lower().strip()

    for game in elements:

        title = game.get(
            "title"
        )

        if not title:
            continue

        if (
            query_lower
            not in title.lower()
        ):
            continue

        slug = (
            game.get("productSlug")
            or game.get("urlSlug")
        )

        if not slug:
            continue

        slug = slug.split(
            "/"
        )[0]

        url = (
            "https://store.epicgames.com/p/"
            + slug
        )

        # ---------------------------------------------
        # IMAGE
        # ---------------------------------------------

        image = None

        for img in game.get(
            "keyImages",
            []
        ):

            if img.get("type") in (
                "OfferImageWide",
                "DieselStoreFrontWide"
            ):

                image = img.get(
                    "url"
                )

                if image:
                    break

        if not image:

            for img in game.get(
                "keyImages",
                []
            ):

                image = img.get(
                    "url"
                )

                if image:
                    break

        # ---------------------------------------------
        # PRICE
        # ---------------------------------------------

        original_krw = 0
        original_vnd = 0

        try:

            original_price = (
                game
                .get("price", {})
                .get("totalPrice", {})
                .get("originalPrice")
            )

            if original_price:

                usd = (
                    float(original_price)
                    / 100000
                )

                (
                    original_krw,
                    original_vnd
                ) = epic_usd_to_prices(
                    usd
                )

        except Exception:
            pass

        results.append({
            "id": str(
                game.get("id")
                or slug
            ),
            "title": title,
            "url": url,
            "image": image,
            "original_krw": original_krw,
            "original_vnd": original_vnd
        })

    # Loại trùng
    unique = []
    seen = set()

    for item in results:

        key = item["id"]

        if key in seen:
            continue

        seen.add(key)

        unique.append(item)

    return unique[:10]


# =========================================================
# STEAM CONFIG
# =========================================================

STEAM_SEARCH_URL = (
    "https://store.steampowered.com/search/results/"
)

STEAM_APPDETAILS_URL = (
    "https://store.steampowered.com/api/appdetails"
)


# =========================================================
# STEAM PRICE
# =========================================================

def parse_steam_price(
    text
):
    if not text:
        return None

    text = text.strip()

    digits = ""

    for char in text:

        if char.isdigit():
            digits += char

    if not digits:
        return None

    try:
        return int(digits)

    except Exception:
        return None


def steam_minor_to_krw(
    value
):
    if value is None:
        return None

    try:

        value = int(value)

        # Steam API thường trả minor unit
        return round(
            value / 100
        )

    except Exception:
        return None


def steam_minor_to_vnd(
    value
):
    if value is None:
        return None

    try:

        value = int(value)

        return round(
            value / 100
        )

    except Exception:
        return None


# =========================================================
# STEAM APP DETAILS
# =========================================================

async def get_steam_app_details(
    session,
    appid,
    country="KR"
):

    url = (
        STEAM_APPDETAILS_URL
        + f"?appids={appid}"
        + f"&cc={country}"
        + "&l=english"
    )

    result = await get_json(
        session,
        url
    )

    if not result:
        return None

    try:

        item = result.get(
            str(appid)
        )

        if not item:
            return None

        if not item.get(
            "success"
        ):
            return None

        return item.get(
            "data"
        )

    except Exception:

        return None


# =========================================================
# STEAM SEARCH
# =========================================================

async def search_steam(
    session,
    query
):

    # QUAN TRỌNG:
    # Có dấu + giữa STEAM_SEARCH_URL
    # và f-string.
    url = (
        STEAM_SEARCH_URL
        + f"?term={quote_plus(query)}"
        + "&category1=998"
        + "&infinite=1"
        + "&count=30"
        + "&cc=KR"
        + "&l=english"
    )

    html = await get_text(
        session,
        url
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

    query_lower = (
        query
        .lower()
        .strip()
    )

    for row in rows:

        try:

            title_element = row.select_one(
                ".title"
            )

            if not title_element:
                continue

            title = title_element.get_text(
                strip=True
            )

            if not title:
                continue

            # Ví dụ:
            # Hitman
            # HITMAN 2
            # HITMAN 3
            #
            # sẽ đều được lấy.
            if (
                query_lower
                not in title.lower()
            ):
                continue

            href = row.get(
                "href"
            )

            if not href:
                continue

            appid = row.get(
                "data-ds-appid"
            )

            if not appid:

                import re

                match = re.search(
                    r"/app/(\d+)",
                    href
                )

                if match:
                    appid = match.group(
                        1
                    )

            if not appid:
                continue

            # Một số kết quả Steam có:
            # 123,456
            #
            # Lấy app đầu tiên.
            appid = str(
                appid
            ).split(
                ","
            )[0].strip()

            # ---------------------------------------------
            # IMAGE
            # ---------------------------------------------

            image = None

            img = row.select_one(
                "img"
            )

            if img:

                image = (
                    img.get("src")
                    or img.get("data-src")
                )

            results.append({
                "appid": appid,
                "title": title,
                "url": (
                    "https://store.steampowered.com/"
                    f"app/{appid}/"
                ),
                "image": image,
                "krw": None,
                "vnd": None
            })

        except Exception as e:

            print(
                "⚠️ Steam search row error:",
                e
            )

            continue

    # ---------------------------------------------
    # LOẠI TRÙNG
    # ---------------------------------------------

    unique = []
    seen = set()

    for item in results:

        if item["appid"] in seen:
            continue

        seen.add(
            item["appid"]
        )

        unique.append(item)

    unique = unique[:10]

    # ---------------------------------------------
    # LẤY CHI TIẾT
    # ---------------------------------------------

    final_results = []

    for item in unique:

        try:

            details_kr = (
                await get_steam_app_details(
                    session,
                    item["appid"],
                    "KR"
                )
            )

            if details_kr:

                item["image"] = (
                    details_kr.get(
                        "header_image"
                    )
                    or item["image"]
                )

                price_kr = (
                    details_kr.get(
                        "price_overview"
                    )
                )

                if price_kr:

                    item["krw"] = (
                        price_kr.get(
                            "final"
                        )
                    )

            details_vn = (
                await get_steam_app_details(
                    session,
                    item["appid"],
                    "VN"
                )
            )

            if details_vn:

                price_vn = (
                    details_vn.get(
                        "price_overview"
                    )
                )

                if price_vn:

                    item["vnd"] = (
                        price_vn.get(
                            "final"
                        )
                    )

            final_results.append(
                item
            )

        except Exception as e:

            print(
                "⚠️ Steam details error:",
                e
            )

            final_results.append(
                item
            )

    return final_results[:10]


# =========================================================
# STEAM FREE GAMES
# =========================================================

async def get_steam_free_games(
    session
):

    # ĐÃ SỬA LỖI SYNTAX:
    # STEAM_SEARCH_URL + "?specials=1"
    url = (
        STEAM_SEARCH_URL
        + "?specials=1"
        + "&maxprice=free"
        + "&hidef2p=1"
        + "&category1=998"
        + "&count=50"
        + "&cc=KR"
        + "&l=english"
    )

    html = await get_text(
        session,
        url
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

    games = []

    for row in rows:

        try:

            title_element = row.select_one(
                ".title"
            )

            if not title_element:
                continue

            title = title_element.get_text(
                strip=True
            )

            if not title:
                continue

            href = row.get(
                "href"
            )

            if not href:
                continue

            appid = row.get(
                "data-ds-appid"
            )

            if not appid:

                import re

                match = re.search(
                    r"/app/(\d+)",
                    href
                )

                if match:
                    appid = match.group(
                        1
                    )

            if not appid:
                continue

            appid = str(
                appid
            ).split(
                ","
            )[0].strip()

            details = (
                await get_steam_app_details(
                    session,
                    appid,
                    "KR"
                )
            )

            if not details:
                continue

            # Chỉ lấy game
            if details.get(
                "type"
            ) != "game":
                continue

            # Game F2P vốn đã free
            # không phải Free-to-Keep.
            if details.get(
                "is_free"
            ):
                continue

            price = details.get(
                "price_overview"
            )

            if not price:
                continue

            initial = price.get(
                "initial"
            )

            final = price.get(
                "final"
            )

            discount_percent = price.get(
                "discount_percent"
            )

            if initial is None:
                continue

            if final is None:
                continue

            # Phải giảm 100%
            if discount_percent != 100:
                continue

            if final != 0:
                continue

            # ---------------------------------------------
            # Loại Free Weekend / Trial
            # ---------------------------------------------

            combined = (
                title
                + " "
                + str(
                    details.get(
                        "short_description",
                        ""
                    )
                )
            ).lower()

            blocked_words = (
                "free weekend",
                "free trial",
                "weekend trial",
                "limited trial"
            )

            is_trial = any(
                word in combined
                for word in blocked_words
            )

            if is_trial:
                continue

            image = (
                details.get(
                    "header_image"
                )
            )

            url_game = (
                "https://store.steampowered.com/"
                f"app/{appid}/"
            )

            # ---------------------------------------------
            # VNĐ
            # ---------------------------------------------

            details_vn = (
                await get_steam_app_details(
                    session,
                    appid,
                    "VN"
                )
            )

            original_vnd = None

            if details_vn:

                price_vn = (
                    details_vn.get(
                        "price_overview"
                    )
                )

                if price_vn:

                    original_vnd = (
                        price_vn.get(
                            "initial"
                        )
                    )

            games.append({
                "id": appid,
                "title": title,
                "url": url_game,
                "image": image,
                "original_krw": initial,
                "original_vnd": original_vnd,
                "end_date": None
            })

        except Exception as e:

            print(
                "⚠️ Steam free-game error:",
                e
            )

            continue

    return games


# =========================================================
# FORMAT PRICE
# =========================================================

def format_krw(
    value
):

    if value is None:
        return None

    try:

        value = int(value)

        # Steam API dùng minor unit
        # Ví dụ 149900 -> ₩1,499
        if value >= 100:

            value = round(
                value / 100
            )

        return (
            f"₩{value:,}"
        )

    except Exception:
        return None


def format_vnd(
    value
):

    if value is None:
        return None

    try:

        value = int(value)

        if value >= 100:

            value = round(
                value / 100
            )

        return (
            f"{value:,}₫"
        )

    except Exception:
        return None


# =========================================================
# STORE INFO
# =========================================================

def get_store_info(
    store
):

    if store == "epic":

        return {
            "name": "EPIC GAMES STORE",
            "icon": "🟣",
            "color": 0x6F42C1
        }

    return {
        "name": "STEAM",
        "icon": "🔵",
        "color": 0x1B9FFF
    }


# =========================================================
# SEARCH EMBED
# =========================================================

def make_game_search_embed(
    store,
    query,
    results
):

    info = get_store_info(
        store
    )

    embed = discord.Embed(
        title=(
            f"🎮・KẾT QUẢ TÌM GAME"
        ),
        description=(
            f"{info['icon']} **{info['name']}**\n"
            f"🔎 Từ khóa: **{query}**\n\n"
            "━━━━━━━━━━━━━━━━━━"
        ),
        color=info["color"]
    )

    for index, game in enumerate(
        results,
        start=1
    ):

        title = game.get(
            "title",
            "Không có tên"
        )

        # ---------------------------------------------
        # EPIC
        # ---------------------------------------------

        if store == "epic":

            krw = game.get(
                "original_krw"
            )

            vnd = game.get(
                "original_vnd"
            )

            prices = []

            if krw:

                prices.append(
                    f"🇰🇷 ₩{krw:,}"
                )

            if vnd:

                prices.append(
                    f"🇻🇳 {vnd:,.0f}₫"
                )

            if prices:

                price_text = (
                    " / ".join(
                        prices
                    )
                )

            else:

                price_text = (
                    "💰 Không lấy được giá"
                )

        # ---------------------------------------------
        # STEAM
        # ---------------------------------------------

        else:

            krw = game.get(
                "krw"
            )

            vnd = game.get(
                "vnd"
            )

            prices = []

            if krw is not None:

                prices.append(
                    format_krw(
                        krw
                    )
                )

            if vnd is not None:

                prices.append(
                    format_vnd(
                        vnd
                    )
                )

            prices = [
                p for p in prices
                if p
            ]

            if prices:

                price_text = (
                    " / ".join(
                        prices
                    )
                )

            else:

                price_text = (
                    "💰 Không lấy được giá"
                )

        # ---------------------------------------------
        # FIELD
        # ---------------------------------------------

        embed.add_field(
            name=(
                f"#{index}・{title}"
            ),
            value=(
                f"💰 **Giá:** {price_text}\n"
                f"🔗 [Mở game trên {info['name']}]"
                f"({game['url']})"
            ),
            inline=False
        )

    # ---------------------------------------------
    # ONE LARGE IMAGE
    # ---------------------------------------------

    first_image = None

    for game in results:

        if game.get("image"):

            first_image = game.get(
                "image"
            )

            break

    if first_image:

        embed.set_image(
            url=first_image
        )

    embed.set_footer(
        text=(
            "🎮 Free Game Bot • "
            "Kết quả tìm kiếm"
        )
    )

    return embed


# =========================================================
# NO RESULT EMBED
# =========================================================

def make_no_result_embed(
    store,
    query
):

    info = get_store_info(
        store
    )

    embed = discord.Embed(
        title="🔎・KHÔNG TÌM THẤY",
        description=(
            f"{info['icon']} **{info['name']}**\n\n"
            f"Không tìm thấy game phù hợp với:\n"
            f"**{query}**\n\n"
            "💡 Thử nhập tên ngắn hơn."
        ),
        color=0xED4245
    )

    embed.set_footer(
        text="🎮 Free Game Bot"
    )

    return embed


# =========================================================
# FREE GAME NOTIFICATION EMBED
# =========================================================

def make_free_notification_embed(
    store,
    game
):

    info = get_store_info(
        store
    )

    embed = discord.Embed(
        title="🎁・GAME ĐANG MIỄN PHÍ",
        description=(
            f"{info['icon']} **{info['name']}**\n\n"
            f"🎮 **{game['title']}**\n\n"
            "🟢 **MIỄN PHÍ 100%**"
        ),
        color=info["color"],
        url=game["url"]
    )

    # ---------------------------------------------
    # GIÁ GỐC
    # ---------------------------------------------

    original_krw = game.get(
        "original_krw"
    )

    original_vnd = game.get(
        "original_vnd"
    )

    prices = []

    if original_krw:

        if store == "steam":

            krw_text = format_krw(
                original_krw
            )

        else:

            krw_text = (
                f"₩{original_krw:,}"
            )

        if krw_text:

            prices.append(
                f"🇰🇷 {krw_text}"
            )

    if original_vnd:

        if store == "steam":

            vnd_text = format_vnd(
                original_vnd
            )

        else:

            vnd_text = (
                f"{original_vnd:,.0f}₫"
            )

        if vnd_text:

            prices.append(
                f"🇻🇳 {vnd_text}"
            )

    if prices:

        embed.add_field(
            name="💰 Giá gốc",
            value=(
                " / ".join(
                    prices
                )
            ),
            inline=True
        )

    embed.add_field(
        name="🎁 Giá hiện tại",
        value="**FREE**",
        inline=True
    )

    # ---------------------------------------------
    # EPIC END DATE
    # ---------------------------------------------

    if game.get(
        "end_date"
    ):

        end_date = str(
            game["end_date"]
        )

        embed.add_field(
            name="⏰ Hạn",
            value=end_date[:19],
            inline=False
        )

    # ---------------------------------------------
    # IMAGE
    # ---------------------------------------------

    if game.get(
        "image"
    ):

        embed.set_image(
            url=game["image"]
        )

    embed.set_footer(
        text=(
            "🎮 Free Game Bot • "
            "Tự động cập nhật"
        )
    )

    return embed


# =========================================================
# STORE LINK BUTTON
# =========================================================

class StoreLinkButton(
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
                emoji="🛒"
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

            title = (
                "🟣 Tìm game trên Epic"
            )

        else:

            title = (
                "🔵 Tìm game trên Steam"
            )

        super().__init__(
            title=title
        )

        self.game_name = (
            discord.ui.TextInput(
                label="Tên game",
                placeholder=(
                    "Ví dụ: Hitman, GTA, Minecraft..."
                ),
                required=True,
                min_length=1,
                max_length=100
            )
        )

        self.add_item(
            self.game_name
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        # ---------------------------------------------
        # CHECK CHANNEL
        # ---------------------------------------------

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
                        "🎮 Hãy dùng `/game` "
                        "trong kênh "
                        f"{search_channel.mention}."
                    ),
                    ephemeral=True
                )

                return

        # ---------------------------------------------
        # LOADING
        # ---------------------------------------------

        await interaction.response.defer()

        query = (
            self.game_name.value
            .strip()
        )

        if not query:

            await interaction.followup.send(
                "❌ Hãy nhập tên game."
            )

            return

        # ---------------------------------------------
        # SEARCH
        # ---------------------------------------------

        async with aiohttp.ClientSession() as session:

            if self.store == "epic":

                results = (
                    await search_epic(
                        session,
                        query
                    )
                )

            else:

                results = (
                    await search_steam(
                        session,
                        query
                    )
                )

        # ---------------------------------------------
        # NO RESULT
        # ---------------------------------------------

        if not results:

            embed = (
                make_no_result_embed(
                    self.store,
                    query
                )
            )

            await interaction.followup.send(
                embed=embed
            )

            return

        # ---------------------------------------------
        # RESULT
        # ---------------------------------------------

        embed = (
            make_game_search_embed(
                self.store,
                query,
                results
            )
        )

        await interaction.followup.send(
            embed=embed
        )


# =========================================================
# STORE SEARCH VIEW
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
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.send_modal(
            GameSearchModal(
                "epic"
            )
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
            GameSearchModal(
                "steam"
            )
        )


# =========================================================
# /GAME
# =========================================================

@bot.tree.command(
    name="game",
    description=(
        "🔎 Tìm game trên Epic Games hoặc Steam"
    )
)
async def game_command(
    interaction: discord.Interaction
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
                    "❌ Lệnh này chỉ dùng trong "
                    f"{search_channel.mention}"
                ),
                ephemeral=True
            )

            return

    embed = discord.Embed(
        title="🎮・TÌM GAME",
        description=(
            "🔎 **Tìm game trên Epic Games hoặc Steam**\n\n"
            "Chọn cửa hàng bên dưới để bắt đầu.\n\n"
            "🟣 **Epic Games Store**\n"
            "🔵 **Steam**\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 Ví dụ: nhập `Hitman` để xem "
            "các phiên bản Hitman tìm được."
        ),
        color=0x5865F2
    )

    embed.add_field(
        name="🟣 Epic",
        value=(
            "Tìm game trên Epic Games Store"
        ),
        inline=True
    )

    embed.add_field(
        name="🔵 Steam",
        value=(
            "Tìm game trên Steam"
        ),
        inline=True
    )

    embed.set_footer(
        text=(
            "🎮 Free Game Bot • "
            "Epic + Steam"
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
        "📡 Kiểm tra các kênh bot đang sử dụng"
    )
)
async def channels_command(
    interaction: discord.Interaction
):

    if not interaction.guild:

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
        title="📡・BOT CHANNELS",
        color=0x5865F2
    )

    if notification:

        notification_text = (
            f"✅ {notification.mention}"
        )

    else:

        notification_text = (
            "❌ Không tìm thấy "
            "`🎁・thong-bao-game`"
        )

    if search:

        search_text = (
            f"✅ {search.mention}"
        )

    else:

        search_text = (
            "❌ Không tìm thấy "
            "`🎮・tim-game`"
        )

    embed.add_field(
        name="🎁 Thông báo game",
        value=notification_text,
        inline=False
    )

    embed.add_field(
        name="🎮 Tìm game",
        value=search_text,
        inline=False
    )

    embed.add_field(
        name="⚙️ Cách nhận diện",
        value=(
            "Bot tự tìm channel theo tên.\n"
            "Không cần `/setchannel`."
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# SEND EPIC NOTIFICATIONS
# =========================================================

async def send_epic_notifications(
    session,
    channel
):

    games = await get_epic_free_games(
        session
    )

    if not games:

        print(
            "ℹ️ Epic: không lấy được game free."
        )

        return

    first_run = not data.get(
        "first_run_done",
        False
    )

    for game in games:

        game_id = str(
            game["id"]
        )

        # Đã gửi rồi
        if game_id in data["epic_sent"]:
            continue

        # Lần đầu không spam
        if (
            first_run
            and not ANNOUNCE_EXISTING_ON_FIRST_RUN
        ):

            data["epic_sent"].append(
                game_id
            )

            continue

        embed = (
            make_free_notification_embed(
                "epic",
                game
            )
        )

        view = StoreLinkButton(
            game["url"],
            "MỞ EPIC"
        )

        try:

            await channel.send(
                embed=embed,
                view=view
            )

            data["epic_sent"].append(
                game_id
            )

            save_data()

            print(
                "🎁 Epic free:",
                game["title"]
            )

            await asyncio.sleep(
                2
            )

        except Exception as e:

            print(
                "❌ Epic send error:",
                e
            )

    data["epic_sent"] = (
        data["epic_sent"][-2000:]
    )

    save_data()


# =========================================================
# SEND STEAM NOTIFICATIONS
# =========================================================

async def send_steam_notifications(
    session,
    channel
):

    games = await get_steam_free_games(
        session
    )

    if not games:

        print(
            "ℹ️ Steam: không lấy được game free."
        )

        return

    first_run = not data.get(
        "first_run_done",
        False
    )

    for game in games:

        game_id = str(
            game["id"]
        )

        # Đã gửi rồi
        if game_id in data["steam_sent"]:
            continue

        # Lần đầu không spam
        if (
            first_run
            and not ANNOUNCE_EXISTING_ON_FIRST_RUN
        ):

            data["steam_sent"].append(
                game_id
            )

            continue

        embed = (
            make_free_notification_embed(
                "steam",
                game
            )
        )

        view = StoreLinkButton(
            game["url"],
            "MỞ STEAM"
        )

        try:

            await channel.send(
                embed=embed,
                view=view
            )

            data["steam_sent"].append(
                game_id
            )

            save_data()

            print(
                "🎁 Steam free:",
                game["title"]
            )

            await asyncio.sleep(
                2
            )

        except Exception as e:

            print(
                "❌ Steam send error:",
                e
            )

    data["steam_sent"] = (
        data["steam_sent"][-2000:]
    )

    save_data()


# =========================================================
# FREE GAME CHECKER
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
            "⚠️ Không tìm thấy channel "
            f"#{NOTIFICATION_CHANNEL_NAME}"
        )

        return

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    print(
        "🔎 Đang kiểm tra game miễn phí..."
    )

    async with aiohttp.ClientSession() as session:

        # Epic
        try:

            await send_epic_notifications(
                session,
                channel
            )

        except Exception as e:

            print(
                "❌ Epic checker error:",
                e
            )

        # Steam
        try:

            await send_steam_notifications(
                session,
                channel
            )

        except Exception as e:

            print(
                "❌ Steam checker error:",
                e
            )

    data["first_run_done"] = True

    save_data()

    print(
        "✅ Kiểm tra game hoàn tất."
    )

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )


@free_game_checker.before_loop
async def before_free_game_checker():

    await bot.wait_until_ready()


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    print(
        f"🤖 Bot: {bot.user}"
    )

    print(
        f"🆔 Bot ID: {bot.user.id}"
    )

    print(
        f"📡 Servers: {len(bot.guilds)}"
    )

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    for guild in bot.guilds:

        print(
            f"🏠 Server: {guild.name}"
        )

        notification = find_channel(
            guild,
            NOTIFICATION_CHANNEL_NAME
        )

        search = find_channel(
            guild,
            SEARCH_CHANNEL_NAME
        )

        # ---------------------------------------------
        # NOTIFICATION CHANNEL
        # ---------------------------------------------

        if notification:

            print(
                "🎁 Notification channel:",
                f"#{notification.name}"
            )

        else:

            print(
                "❌ Không thấy:",
                f"#{NOTIFICATION_CHANNEL_NAME}"
            )

        # ---------------------------------------------
        # SEARCH CHANNEL
        # ---------------------------------------------

        if search:

            print(
                "🎮 Search channel:",
                f"#{search.name}"
            )

        else:

            print(
                "❌ Không thấy:",
                f"#{SEARCH_CHANNEL_NAME}"
            )

        # ---------------------------------------------
        # SYNC COMMAND
        # ---------------------------------------------

        try:

            bot.tree.copy_global_to(
                guild=guild
            )

            synced = await bot.tree.sync(
                guild=guild
            )

            print(
                f"⚡ Synced {len(synced)} commands "
                f"→ {guild.name}"
            )

        except Exception as e:

            print(
                "❌ Sync error:",
                e
            )

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    print(
        "📋 Commands:"
    )

    for command in bot.tree.get_commands():

        print(
            f"   /{command.name}"
        )

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    print(
        "✅ Bot is ready!"
    )

    # ---------------------------------------------
    # START FREE GAME CHECKER
    # ---------------------------------------------

    if not free_game_checker.is_running():

        free_game_checker.start()

        print(
            "▶️ Free Game Checker started."
        )

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )


# =========================================================
# ERROR
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
# GLOBAL ERROR
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
                (
                    "❌ Có lỗi xảy ra khi chạy lệnh."
                ),
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                (
                    "❌ Có lỗi xảy ra khi chạy lệnh."
                ),
                ephemeral=True
            )

    except Exception as e:

        print(
            "❌ Không thể gửi error message:",
            e
        )


# =========================================================
# TOKEN CHECK
# =========================================================

if not DISCORD_TOKEN:

    raise RuntimeError(
        "❌ Chưa có DISCORD_TOKEN "
        "trong Railway Variables."
    )


# =========================================================
# START BOT
# =========================================================

print(
    "🚀 Starting Free Game Bot..."
)

bot.run(
    DISCORD_TOKEN
)
