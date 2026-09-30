import os
import json
import asyncio
import re
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

# False = lần đầu bot chạy sẽ không spam các game đang free sẵn
ANNOUNCE_EXISTING_ON_FIRST_RUN = False

USD_TO_KRW = 1400
USD_TO_VND = 25000


# =========================================================
# INTENTS
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

def load_data():
    default = {
        "epic_sent": [],
        "steam_sent": [],
        "first_run_done": False
    }

    if not os.path.exists(DATA_FILE):
        return default

    try:
        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            saved = json.load(f)

        for key in default:
            saved.setdefault(key, default[key])

        return saved

    except Exception as e:
        print("⚠️ Load data error:", e)
        return default


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
        print("❌ Save data error:", e)


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

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9"
}


async def get_json(
    session,
    url,
    **kwargs
):
    try:
        headers = DEFAULT_HEADERS.copy()

        extra_headers = kwargs.pop(
            "headers",
            None
        )

        if extra_headers:
            headers.update(extra_headers)

        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(
                total=30
            ),
            headers=headers,
            **kwargs
        ) as response:

            text = await response.text()

            if response.status != 200:
                print(
                    f"❌ HTTP {response.status}: {url}"
                )
                return None

            try:
                return json.loads(text)
            except Exception:
                return None

    except Exception as e:
        print(
            "❌ GET JSON error:",
            repr(e)
        )
        return None


async def get_text(
    session,
    url,
    **kwargs
):
    try:
        headers = DEFAULT_HEADERS.copy()

        extra_headers = kwargs.pop(
            "headers",
            None
        )

        if extra_headers:
            headers.update(extra_headers)

        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(
                total=30
            ),
            headers=headers,
            **kwargs
        ) as response:

            if response.status != 200:
                print(
                    f"❌ HTTP {response.status}: {url}"
                )
                return None

            return await response.text()

    except Exception as e:
        print(
            "❌ GET text error:",
            repr(e)
        )
        return None


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

    for image_type in preferred:
        for image in images:
            if image.get("type") == image_type:
                if image.get("url"):
                    return image["url"]

    for image in images:
        if image.get("url"):
            return image["url"]

    return None


def epic_slug(game):
    slug = (
        game.get("productSlug")
        or game.get("urlSlug")
    )

    if not slug:
        return None

    slug = slug.split("/")[0]

    return slug


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
        print(
            "❌ Epic free API format changed"
        )
        return []

    games = []

    for game in elements:

        try:
            title = game.get("title")

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

            offers = (
                promotional_offers[0]
                .get(
                    "promotionalOffers",
                    []
                )
            )

            if not offers:
                continue

            offer = offers[0]

            discount_setting = (
                offer.get(
                    "discountSetting",
                    {}
                )
            )

            discount_percentage = (
                discount_setting.get(
                    "discountPercentage"
                )
            )

            # 0% discount = free
            if discount_percentage != 0:
                continue

            slug = epic_slug(game)

            if not slug:
                continue

            url = (
                "https://store.epicgames.com/p/"
                + slug
            )

            image = get_epic_image(
                game
            )

            original_krw = 0
            original_vnd = 0

            price = game.get(
                "price",
                {}
            )

            total = price.get(
                "totalPrice",
                {}
            )

            original_price = (
                total.get("originalPrice")
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

            games.append({
                "id": str(
                    game.get("id")
                    or slug
                ),
                "title": title,
                "url": url,
                "image": image,
                "start_date": (
                    offer.get("startDate")
                ),
                "end_date": (
                    offer.get("endDate")
                ),
                "original_krw": original_krw,
                "original_vnd": original_vnd
            })

        except Exception as e:
            print(
                "⚠️ Epic game parse error:",
                repr(e)
            )

    return games


# =========================================================
# EPIC SEARCH
# =========================================================

async def search_epic(
    session,
    query
):

    query = query.strip()

    if not query:
        return []

    gql = """
    query searchStoreQuery(
        $keywords: String
        $locale: String
        $country: String
        $start: Int
        $count: Int
    ) {
        Catalog {
            searchStore(
                keywords: $keywords
                locale: $locale
                country: $country
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
                        }
                    }
                }
            }
        }
    }
    """

    payload = {
        "query": gql,
        "variables": {
            "keywords": query,
            "locale": "en-US",
            "country": "KR",
            "start": 0,
            "count": 40
        }
    }

    try:
        async with session.post(
            EPIC_GRAPHQL_URL,
            json=payload,
            headers={
                **DEFAULT_HEADERS,
                "Content-Type": "application/json",
                "Origin": "https://store.epicgames.com",
                "Referer": "https://store.epicgames.com/"
            },
            timeout=aiohttp.ClientTimeout(
                total=30
            )
        ) as response:

            text = await response.text()

            if response.status != 200:
                print(
                    "❌ Epic search HTTP:",
                    response.status,
                    text[:500]
                )
                return []

            try:
                result = json.loads(text)
            except Exception:
                print(
                    "❌ Epic search trả về không phải JSON"
                )
                return []

    except Exception as e:
        print(
            "❌ Epic search error:",
            repr(e)
        )
        return []

    if result.get("errors"):
        print(
            "❌ Epic GraphQL errors:",
            result["errors"]
        )

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

    query_lower = query.lower()

    for game in elements:

        title = game.get("title")

        if not title:
            continue

        # Không bắt buộc query phải nằm nguyên
        # trong title để tránh mất kết quả.
        title_lower = title.lower()

        if (
            query_lower not in title_lower
            and not any(
                part in title_lower
                for part in query_lower.split()
                if len(part) >= 3
            )
        ):
            continue

        slug = epic_slug(game)

        if not slug:
            continue

        image = get_epic_image(
            game
        )

        original_krw = 0
        original_vnd = 0

        try:
            total = (
                game
                .get("price", {})
                .get("totalPrice", {})
            )

            original_price = (
                total.get(
                    "originalPrice"
                )
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
            "url": (
                "https://store.epicgames.com/p/"
                + slug
            ),
            "image": image,
            "original_krw": original_krw,
            "original_vnd": original_vnd
        })

    # Remove duplicates
    unique = []
    seen = set()

    for item in results:

        if item["id"] in seen:
            continue

        seen.add(
            item["id"]
        )

        unique.append(item)

    print(
        f"🔎 Epic '{query}' → "
        f"{len(unique)} results"
    )

    return unique[:10]


# =========================================================
# STEAM
# =========================================================

STEAM_SEARCH_API = (
    "https://store.steampowered.com/"
    "api/storesearch/"
)

STEAM_APPDETAILS_API = (
    "https://store.steampowered.com/"
    "api/appdetails"
)


def steam_price_to_display(
    value,
    currency
):
    if value is None:
        return None

    try:
        value = float(value)

        # Steam API thường trả minor units.
        if currency == "KRW":
            return round(
                value / 100
            )

        if currency == "VND":
            return round(
                value / 100
            )

        return round(
            value / 100,
            2
        )

    except Exception:
        return None


async def get_steam_app_details(
    session,
    appid,
    country="KR"
):

    url = (
        STEAM_APPDETAILS_API
        + f"?appids={appid}"
        + f"&cc={country}"
        + "&l=english"
    )

    data = await get_json(
        session,
        url
    )

    if not data:
        return None

    try:
        item = data.get(
            str(appid)
        )

        if not item:
            return None

        if not item.get(
            "success"
        ):
            return None

        return item.get("data")

    except Exception:
        return None


def get_steam_price(
    details,
    currency
):
    if not details:
        return None

    price = details.get(
        "price_overview"
    )

    if not price:
        return None

    final = price.get(
        "final"
    )

    if final is None:
        return None

    return steam_price_to_display(
        final,
        currency
    )


async def search_steam(
    session,
    query
):

    query = query.strip()

    if not query:
        return []

    url = (
        STEAM_SEARCH_API
        + f"?term={quote_plus(query)}"
        + "&cc=KR"
        + "&l=english"
        + "&category1=998"
        + "&infinite=1"
    )

    result = await get_json(
        session,
        url
    )

    if not result:
        print(
            "❌ Steam search API không trả data"
        )
        return []

    items = result.get(
        "items",
        []
    )

    if not items:
        print(
            f"🔎 Steam '{query}' → 0 results"
        )
        return []

    results = []
    seen = set()

    query_lower = query.lower()

    for item in items:

        try:
            appid = item.get(
                "id"
            )

            title = item.get(
                "name"
            )

            if not appid or not title:
                continue

            appid = str(appid)

            if appid in seen:
                continue

            title_lower = title.lower()

            # Giữ kết quả liên quan
            words = [
                word
                for word in query_lower.split()
                if len(word) >= 2
            ]

            relevant = (
                query_lower in title_lower
                or all(
                    word in title_lower
                    for word in words
                )
            )

            if not relevant:
                continue

            seen.add(appid)

            details_kr = (
                await get_steam_app_details(
                    session,
                    appid,
                    "KR"
                )
            )

            if not details_kr:
                continue

            if details_kr.get(
                "type"
            ) != "game":
                continue

            details_vn = (
                await get_steam_app_details(
                    session,
                    appid,
                    "VN"
                )
            )

            krw = get_steam_price(
                details_kr,
                "KRW"
            )

            vnd = get_steam_price(
                details_vn,
                "VND"
            )

            image = (
                details_kr.get(
                    "header_image"
                )
                or item.get(
                    "tiny_image"
                )
            )

            results.append({
                "appid": appid,
                "title": title,
                "url": (
                    "https://store.steampowered.com/"
                    f"app/{appid}/"
                ),
                "image": image,
                "krw": krw,
                "vnd": vnd
            })

            if len(results) >= 10:
                break

        except Exception as e:
            print(
                "⚠️ Steam result error:",
                repr(e)
            )

    print(
        f"🔎 Steam '{query}' → "
        f"{len(results)} results"
    )

    return results


# =========================================================
# STEAM FREE GAMES
# =========================================================

async def get_steam_free_games(
    session
):

    url = (
        STEAM_SEARCH_API
        + "?term="
        + "&cc=KR"
        + "&l=english"
        + "&category1=998"
        + "&specials=1"
        + "&maxprice=free"
    )

    result = await get_json(
        session,
        url
    )

    if not result:
        return []

    items = result.get(
        "items",
        []
    )

    results = []

    for item in items:

        try:
            appid = item.get(
                "id"
            )

            title = item.get(
                "name"
            )

            if not appid or not title:
                continue

            appid = str(appid)

            details = (
                await get_steam_app_details(
                    session,
                    appid,
                    "KR"
                )
            )

            if not details:
                continue

            if details.get(
                "type"
            ) != "game":
                continue

            # Game vốn free -> bỏ
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
                continue

            if final != 0:
                continue

            if discount != 100:
                continue

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
                "original_krw": (
                    steam_price_to_display(
                        initial,
                        "KRW"
                    )
                ),
                "original_vnd": None
            })

        except Exception as e:
            print(
                "⚠️ Steam free parse error:",
                repr(e)
            )

    return results


# =========================================================
# PRICE FORMAT
# =========================================================

def format_krw(value):

    if value is None:
        return "Không có giá"

    try:
        return f"₩{int(value):,}"

    except Exception:
        return "Không có giá"


def format_vnd(value):

    if value is None:
        return "Không có giá"

    try:
        return f"{int(value):,}₫"

    except Exception:
        return "Không có giá"


# =========================================================
# SEARCH EMBED
# =========================================================

def make_game_search_embed(
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
        title=f"🎮・{query.upper()}",
        description=(
            f"{icon} **{store_name}**\n"
            f"🔎 Tìm thấy **{len(results)}** kết quả\n"
            "━━━━━━━━━━━━━━━━━━━━"
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
                game.get(
                    "krw"
                )
            )

            vnd = format_vnd(
                game.get(
                    "vnd"
                )
            )

        value = (
            f"💰 **KRW:** {krw}\n"
            f"💵 **VNĐ:** {vnd}\n"
            f"🔗 [Mở game]({game['url']})"
        )

        embed.add_field(
            name=(
                f"#{index}・"
                f"{game['title']}"
            ),
            value=value,
            inline=False
        )

    # Discord giới hạn embed field.
    # Nếu có quá nhiều kết quả vẫn giữ 1 embed.
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
            "🎮 Free Game Bot • "
            f"{store_name} • "
            f"{len(results)} kết quả"
        )
    )

    return embed


# =========================================================
# NO RESULT
# =========================================================

def make_no_result_embed(
    store,
    query
):

    if store == "epic":

        color = 0x7C3AED
        store_name = (
            "Epic Games Store"
        )

    else:

        color = 0x1B9FFF
        store_name = "Steam"

    embed = discord.Embed(
        title="🔎・KHÔNG TÌM THẤY GAME",
        description=(
            f"Không tìm thấy **{query}** "
            f"trên **{store_name}**.\n\n"
            "💡 Thử các từ khóa ngắn hơn:\n"
            "`Hitman`\n"
            "`GTA`\n"
            "`Minecraft`\n"
            "`FIFA`"
        ),
        color=color
    )

    embed.set_footer(
        text="🎮 Free Game Bot"
    )

    return embed


# =========================================================
# FREE NOTIFICATION EMBED
# =========================================================

def make_free_notification_embed(
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
        title="🎁・GAME ĐANG MIỄN PHÍ",
        description=(
            f"{icon} **{store_name}**\n\n"
            f"🎮 **{game['title']}**\n\n"
            "🟢 **MIỄN PHÍ 100%**"
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

    prices = []

    if original_krw:
        prices.append(
            f"🇰🇷 ₩{int(original_krw):,}"
        )

    if original_vnd:
        prices.append(
            f"🇻🇳 {int(original_vnd):,}₫"
        )

    if prices:

        embed.add_field(
            name="💰 Giá gốc",
            value=" / ".join(prices),
            inline=True
        )

    embed.add_field(
        name="🎁 Giá hiện tại",
        value="**FREE**",
        inline=True
    )

    if game.get(
        "end_date"
    ):

        embed.add_field(
            name="⏰ Hạn",
            value=str(
                game["end_date"]
            )[:19],
            inline=False
        )

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
                    "Ví dụ: Hitman, "
                    "GTA, Minecraft..."
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
                        "🎮 Hãy dùng `/game` "
                        "trong kênh "
                        "`🎮・tim-game`."
                    ),
                    ephemeral=True
                )

                return

        await interaction.response.defer()

        query = (
            self.game_name.value
            .strip()
        )

        print(
            f"🔎 Search request: "
            f"{self.store} / {query}"
        )

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
# STORE SEARCH BUTTONS
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
        interaction,
        button
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
        "🔎 Tìm game trên "
        "Epic Games hoặc Steam"
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
                    "❌ Lệnh này chỉ dùng "
                    f"trong {search_channel.mention}"
                ),
                ephemeral=True
            )

            return

    embed = discord.Embed(
        title="🎮・TÌM GAME",
        description=(
            "🔎 **Tìm game trên Epic Games "
            "hoặc Steam**\n\n"
            "Chọn cửa hàng bên dưới:\n\n"
            "🟣 **Epic Games Store**\n"
            "🔵 **Steam**\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💡 Ví dụ: nhập `Hitman` "
            "để xem nhiều phiên bản."
        ),
        color=0x5865F2
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
        "📡 Kiểm tra kênh bot"
    )
)
async def channels_command(
    interaction
):

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

    embed.add_field(
        name="🎁 Thông báo game",
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
# EPIC NOTIFICATIONS
# =========================================================

async def send_epic_notifications(
    session,
    channel
):

    games = await get_epic_free_games(
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

        if (
            game_id
            in data["epic_sent"]
        ):
            continue

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

            await asyncio.sleep(2)

        except Exception as e:

            print(
                "❌ Epic send error:",
                repr(e)
            )

    data["epic_sent"] = (
        data["epic_sent"][-2000:]
    )


# =========================================================
# STEAM NOTIFICATIONS
# =========================================================

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

        if (
            game_id
            in data["steam_sent"]
        ):
            continue

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

            await asyncio.sleep(2)

        except Exception as e:

            print(
                "❌ Steam send error:",
                repr(e)
            )

    data["steam_sent"] = (
        data["steam_sent"][-2000:]
    )


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
            "⚠️ Không tìm thấy "
            f"#{NOTIFICATION_CHANNEL_NAME}"
        )

        return

    print(
        "🔎 Đang kiểm tra "
        "game miễn phí..."
    )

    async with aiohttp.ClientSession() as session:

        await send_epic_notifications(
            session,
            channel
        )

        await send_steam_notifications(
            session,
            channel
        )

    data["first_run_done"] = True

    save_data()

    print(
        "✅ Kiểm tra game hoàn tất."
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
        f"🤖 Bot: {bot.user}"
    )

    print(
        f"📡 Servers: {len(bot.guilds)}"
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
                "🎁 Notification channel: "
                f"#{notification.name}"
            )

        else:

            print(
                "❌ Không thấy "
                f"#{NOTIFICATION_CHANNEL_NAME}"
            )

        if search:

            print(
                "🎮 Search channel: "
                f"#{search.name}"
            )

        else:

            print(
                "❌ Không thấy "
                f"#{SEARCH_CHANNEL_NAME}"
            )

        # Sync slash commands
        try:

            bot.tree.copy_global_to(
                guild=guild
            )

            synced = (
                await bot.tree.sync(
                    guild=guild
                )
            )

            print(
                f"⚡ Synced {len(synced)} "
                f"commands → {guild.name}"
            )

        except Exception as e:

            print(
                "❌ Sync error:",
                repr(e)
            )

    print(
        "⚡ Tổng command:",
        len(
            bot.tree.get_commands()
        )
    )

    print(
        "✅ Bot is ready."
    )

    if not free_game_checker.is_running():

        free_game_checker.start()


# =========================================================
# COMMAND ERROR
# =========================================================

@bot.event
async def on_command_error(
    ctx,
    error
):

    print(
        "❌ Command error:",
        repr(error)
    )


# =========================================================
# START
# =========================================================

if not DISCORD_TOKEN:

    raise RuntimeError(
        "❌ Chưa có DISCORD_TOKEN "
        "trong Environment Variables."
    )


bot.run(
    DISCORD_TOKEN
)
