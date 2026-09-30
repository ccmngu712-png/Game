import os
import json
import asyncio
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup

import discord
from discord.ext import commands, tasks
from discord import app_commands


# =========================================================
# CONFIG
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# TÊN 2 KÊNH DISCORD
NOTIFICATION_CHANNEL_NAME = "thong-bao-game"
SEARCH_CHANNEL_NAME = "tim-game"

# Cứ bao nhiêu phút kiểm tra game free
CHECK_INTERVAL_MINUTES = 15

# File lưu game đã gửi
DATA_FILE = "bot_data.json"

# False:
# Lần đầu bot chạy sẽ ghi nhớ các game đang free nhưng
# không gửi hàng loạt game cũ.
#
# True:
# Lần đầu chạy sẽ gửi tất cả game đang free tìm được.
ANNOUNCE_EXISTING_ON_FIRST_RUN = False


# =========================================================
# INTENTS
# =========================================================

intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# HTTP
# =========================================================

http_session = None


async def get_session():
    global http_session

    if http_session is None or http_session.closed:
        timeout = aiohttp.ClientTimeout(total=30)

        http_session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(iPhone; CPU iPhone OS 18_7 like Mac OS X) "
                    "AppleWebKit/605.1.15 "
                    "Version/18.7 Mobile/15E148 "
                    "Safari/604.1"
                )
            }
        )

    return http_session


# =========================================================
# DATA
# =========================================================

DEFAULT_DATA = {
    "sent_games": [],
    "initialized": False
}


def load_data():
    if not os.path.exists(DATA_FILE):
        return DEFAULT_DATA.copy()

    try:
        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        for key, value in DEFAULT_DATA.items():
            if key not in data:
                data[key] = value

        return data

    except Exception as e:
        print(f"[DATA] Cannot read data file: {e}")
        return DEFAULT_DATA.copy()


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
        print(f"[DATA] Cannot save: {e}")


data = load_data()


# =========================================================
# GENERAL HELPERS
# =========================================================

def normalize_channel_name(name):
    """
    Cho phép:
    thong-bao-game
    Thong-Bao-Game
    thong bao game
    thong_bao_game
    """
    return (
        name
        .lower()
        .replace(" ", "-")
        .replace("_", "-")
    )


def format_vnd(value):
    try:
        value = float(value)
        return f"{round(value):,}₫".replace(",", ".")
    except Exception:
        return "N/A"


def format_krw(value):
    try:
        value = float(value)
        return f"₩{round(value):,}"
    except Exception:
        return "N/A"


def format_usd(value):
    try:
        value = float(value)
        return f"${value:,.2f}"
    except Exception:
        return "N/A"


def usd_to_krw(value):
    return float(value) * 1400


def usd_to_vnd(value):
    return float(value) * 25000


def first_image(images):
    if not images:
        return None

    preferred = [
        "DieselGameBoxArt",
        "OfferImageWide",
        "Thumbnail",
        "DieselGameBox",
        "ProductImage",
        "featured"
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


def game_key(store, game_id):
    return f"{store.lower()}:{game_id}"


# =========================================================
# FIND CHANNELS AUTOMATICALLY
# =========================================================

def find_channel(guild, wanted_name):
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


def find_notification_channel():
    """
    Tìm #thong-bao-game trong tất cả server
    mà bot đang tham gia.
    """

    for guild in bot.guilds:
        channel = find_channel(
            guild,
            NOTIFICATION_CHANNEL_NAME
        )

        if channel:
            return channel

    return None


def find_search_channel(guild):
    return find_channel(
        guild,
        SEARCH_CHANNEL_NAME
    )


# =========================================================
# EPIC FREE GAMES
# =========================================================

EPIC_FREE_URL = (
    "https://store-site-backend-static.ak.epicgames.com/"
    "freeGamesPromotions"
)


async def get_epic_free_games():
    session = await get_session()

    params = {
        "locale": "en-US",
        "country": "US",
        "allowCountries": "US"
    }

    try:
        async with session.get(
            EPIC_FREE_URL,
            params=params
        ) as response:

            if response.status != 200:
                print(
                    f"[EPIC] HTTP {response.status}"
                )
                return []

            payload = await response.json()

    except Exception as e:
        print(f"[EPIC] Error: {e}")
        return []

    elements = (
        payload
        .get("data", {})
        .get("Catalog", {})
        .get("searchStore", {})
        .get("elements", [])
    )

    results = []

    for game in elements:
        try:
            title = game.get("title")

            if not title:
                continue

            promotions = game.get(
                "promotions"
            ) or {}

            # Nếu Epic không có promotion data,
            # bỏ qua để tránh nhận game bình thường.
            offers = (
                promotions.get("promotionalOffers")
                or []
            )

            upcoming = (
                promotions.get("upcomingPromotionalOffers")
                or []
            )

            current_offer = None

            for group in offers:
                for offer in group.get(
                    "promotionalOffers",
                    []
                ):
                    current_offer = offer
                    break

                if current_offer:
                    break

            # Một số response không cần check offer,
            # nhưng nếu có dữ liệu offer thì phải chắc chắn
            # đang trong thời gian promotion.
            if offers and not current_offer:
                continue

            # Kiểm tra giá.
            price = game.get("price") or {}

            total_price = (
                price.get("totalPrice")
                or {}
            )

            original_price = (
                total_price.get("originalPrice")
            )

            discount_price = (
                total_price.get("discountPrice")
            )

            # Epic có thể trả giá dưới dạng số cent.
            try:
                original_price = float(
                    original_price or 0
                )

                discount_price = float(
                    discount_price or 0
                )

            except Exception:
                continue

            # Nếu discount price chưa bằng 0
            # thì không coi là free.
            if discount_price != 0:
                continue

            # Phải có giá gốc > 0 để tránh F2P.
            if original_price <= 0:
                continue

            namespace = game.get(
                "namespace"
            )

            product_id = game.get(
                "id"
            )

            slug = (
                game.get("productSlug")
                or game.get("urlSlug")
                or ""
            )

            url = game.get("url")

            if not url and slug:
                url = (
                    "https://store.epicgames.com/"
                    "en-US/p/"
                    + slug
                )

            image = first_image(
                game.get("keyImages", [])
            )

            end_date = None

            if current_offer:
                end_date = current_offer.get(
                    "endDate"
                )

            results.append({
                "store": "Epic",
                "id": (
                    namespace
                    or product_id
                    or slug
                    or title
                ),
                "title": title,
                "image": image,
                "url": url,
                "original_price": original_price,
                "discount_price": discount_price,
                "currency": total_price.get(
                    "currencyCode",
                    "USD"
                ),
                "end_date": end_date
            })

        except Exception as e:
            print(
                f"[EPIC] Game parse error: {e}"
            )

    return results


# =========================================================
# EPIC SEARCH
# =========================================================

EPIC_GRAPHQL_URL = (
    "https://store.epicgames.com/graphql"
)


EPIC_SEARCH_QUERY = """
query searchStoreQuery(
    $category: String,
    $count: Int,
    $country: String!,
    $keywords: String,
    $locale: String,
    $start: Int
) {
    Catalog {
        searchStore(
            category: $category,
            count: $count,
            country: $country,
            keywords: $keywords,
            locale: $locale,
            start: $start
        ) {
            elements {
                title
                id
                namespace
                keyImages {
                    type
                    url
                }
                productSlug
                urlSlug
                url
                price(country: $country) {
                    totalPrice {
                        discountPrice
                        originalPrice
                        currencyCode
                    }
                }
            }
        }
    }
}
"""


async def epic_search(keyword):
    session = await get_session()

    payload = {
        "query": EPIC_SEARCH_QUERY,
        "variables": {
            "category": "games/edition/base",
            "count": 10,
            "country": "KR",
            "keywords": keyword,
            "locale": "ko-KR",
            "start": 0
        }
    }

    try:
        async with session.post(
            EPIC_GRAPHQL_URL,
            json=payload,
            headers={
                "Content-Type": "application/json"
            }
        ) as response:

            if response.status != 200:
                print(
                    f"[EPIC SEARCH] "
                    f"HTTP {response.status}"
                )
                return []

            result = await response.json()

    except Exception as e:
        print(
            f"[EPIC SEARCH] Error: {e}"
        )
        return []

    elements = (
        result
        .get("data", {})
        .get("Catalog", {})
        .get("searchStore", {})
        .get("elements", [])
    )

    games = []

    for game in elements:
        try:
            title = game.get("title")

            if not title:
                continue

            slug = (
                game.get("productSlug")
                or game.get("urlSlug")
                or ""
            )

            url = game.get("url")

            if not url and slug:
                url = (
                    "https://store.epicgames.com/"
                    "en-US/p/"
                    + slug
                )

            image = first_image(
                game.get("keyImages", [])
            )

            price = (
                game.get("price")
                or {}
            )

            total = (
                price.get("totalPrice")
                or {}
            )

            original_price = float(
                total.get(
                    "originalPrice",
                    0
                ) or 0
            )

            discount_price = float(
                total.get(
                    "discountPrice",
                    0
                ) or 0
            )

            games.append({
                "store": "Epic",
                "id": (
                    game.get("namespace")
                    or game.get("id")
                    or slug
                ),
                "title": title,
                "image": image,
                "url": url,
                "original_price": original_price,
                "discount_price": discount_price,
                "currency": total.get(
                    "currencyCode",
                    "KRW"
                )
            })

        except Exception:
            continue

    return games


# =========================================================
# STEAM
# =========================================================

STEAM_SEARCH_URL = (
    "https://store.steampowered.com/search/results/"
)

STEAM_APPDETAILS_URL = (
    "https://store.steampowered.com/api/appdetails"
)

STEAM_STORE_URL = (
    "https://store.steampowered.com/app/"
)


async def steam_search_page(
    keyword="",
    free_only=False,
    count=50
):
    session = await get_session()

    params = {
        "query": keyword,
        "start": 0,
        "count": count,
        "dynamic_data": "",
        "force_infinite": 1,
        "ndl": 1,
        "cc": "US",
        "l": "english"
    }

    if free_only:
        params.update({
            "specials": 1,
            "maxprice": "free",
            "hidef2p": 1
        })

    try:
        async with session.get(
            STEAM_SEARCH_URL,
            params=params
        ) as response:

            if response.status != 200:
                print(
                    f"[STEAM SEARCH] "
                    f"HTTP {response.status}"
                )
                return []

            payload = await response.json()

    except Exception as e:
        print(
            f"[STEAM SEARCH] Error: {e}"
        )
        return []

    html = payload.get(
        "results_html",
        ""
    )

    if not html:
        return []

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    results = []

    for row in soup.select(
        "a.search_result_row"
    ):
        try:
            appid = row.get(
                "data-ds-appid"
            )

            if not appid:
                continue

            # Có trường hợp Steam trả:
            # 12345,67890
            appid = str(
                appid
            ).split(",")[0].strip()

            title_el = row.select_one(
                ".title"
            )

            title = (
                title_el.get_text(
                    " ",
                    strip=True
                )
                if title_el
                else ""
            )

            if not title:
                continue

            image = None

            img = row.select_one(
                ".search_capsule img"
            )

            if img:
                image = (
                    img.get("src")
                    or img.get("data-src")
                )

            discount_el = row.select_one(
                ".search_discount"
            )

            discount_text = (
                discount_el.get_text(
                    " ",
                    strip=True
                )
                if discount_el
                else ""
            )

            price_el = row.select_one(
                ".search_price"
            )

            price_text = (
                price_el.get_text(
                    " ",
                    strip=True
                )
                if price_el
                else ""
            )

            results.append({
                "appid": appid,
                "title": title,
                "image": image,
                "discount_text": discount_text,
                "price_text": price_text
            })

        except Exception:
            continue

    return results


async def steam_app_details(
    appid,
    country="KR",
    language="english"
):
    session = await get_session()

    params = {
        "appids": appid,
        "cc": country,
        "l": language
    }

    try:
        async with session.get(
            STEAM_APPDETAILS_URL,
            params=params
        ) as response:

            if response.status != 200:
                return None

            payload = await response.json()

    except Exception as e:
        print(
            f"[STEAM DETAILS] "
            f"{appid}: {e}"
        )
        return None

    item = payload.get(
        str(appid)
    )

    if not item:
        return None

    if not item.get("success"):
        return None

    return item.get("data")


# =========================================================
# STEAM FREE-TO-KEEP
# =========================================================

async def get_steam_free_to_keep():
    """
    Steam Store search:
      specials=1
      maxprice=free
      hidef2p=1

    Sau đó kiểm tra appdetails để chắc chắn:
      - type = game
      - không phải permanent F2P
      - có price_overview
      - discount = 100%
      - giá hiện tại = 0
      - giá gốc > 0
    """

    candidates = await steam_search_page(
        keyword="",
        free_only=True,
        count=50
    )

    results = []

    for candidate in candidates:
        appid = candidate["appid"]

        details = await steam_app_details(
            appid,
            country="US",
            language="english"
        )

        if not details:
            continue

        if details.get("type") != "game":
            continue

        # Game vốn Free-to-Play -> bỏ
        if details.get("is_free") is True:
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
            -1
        )

        discount = price.get(
            "discount_percent",
            0
        )

        # Phải là game trả phí đang giảm 100%
        if initial <= 0:
            continue

        if final != 0:
            continue

        if discount != 100:
            continue

        # Steam Free Weekend / Trial
        # thường không có cấu trúc giá 100% off
        # như Free-to-Keep.
        # Thêm kiểm tra chữ để tránh false positive.
        text = (
            str(details.get("name", ""))
            + " "
            + str(details.get(
                "short_description",
                ""
            ))
        ).lower()

        bad_phrases = [
            "free weekend",
            "free trial",
            "weekend trial",
            "limited trial"
        ]

        if any(
            phrase in text
            for phrase in bad_phrases
        ):
            continue

        results.append({
            "store": "Steam",
            "id": str(appid),
            "appid": str(appid),
            "title": details.get(
                "name",
                candidate.get(
                    "title",
                    "Unknown"
                )
            ),
            "image": (
                details.get("header_image")
                or candidate.get("image")
            ),
            "url": (
                STEAM_STORE_URL
                + str(appid)
            ),
            "initial_cents": initial,
            "final_cents": final,
            "discount_percent": discount
        })

        await asyncio.sleep(0.15)

    return results


# =========================================================
# STEAM GAME SEARCH
# =========================================================

async def steam_search_game(keyword):
    candidates = await steam_search_page(
        keyword=keyword,
        free_only=False,
        count=20
    )

    results = []

    for candidate in candidates[:8]:
        appid = candidate["appid"]

        details_kr = await steam_app_details(
            appid,
            country="KR",
            language="koreana"
        )

        details_vn = await steam_app_details(
            appid,
            country="VN",
            language="english"
        )

        if not details_kr:
            continue

        title = (
            details_kr.get("name")
            or candidate.get("title")
        )

        image = (
            details_kr.get("header_image")
            or candidate.get("image")
        )

        price_kr = details_kr.get(
            "price_overview"
        )

        price_vn = (
            details_vn.get(
                "price_overview"
            )
            if details_vn
            else None
        )

        krw = None
        vnd = None
        discount = 0

        if price_kr:
            krw = (
                price_kr.get(
                    "final",
                    0
                ) / 100
            )

            discount = price_kr.get(
                "discount_percent",
                0
            )

        if price_vn:
            vnd = (
                price_vn.get(
                    "final",
                    0
                ) / 100
            )

        results.append({
            "store": "Steam",
            "id": str(appid),
            "title": title,
            "image": image,
            "url": (
                STEAM_STORE_URL
                + str(appid)
            ),
            "krw": krw,
            "vnd": vnd,
            "discount": discount
        })

        await asyncio.sleep(0.15)

    return results


# =========================================================
# EMBEDS
# =========================================================

def build_free_embed(game):
    store = game["store"]

    embed = discord.Embed(
        title=f"🎁 {game['title']}",
        url=game["url"],
        description=(
            f"🔥 **FREE TO KEEP**\n\n"
            f"🏪 Store: **{store}**"
        )
    )

    if store == "Steam":
        original_usd = (
            game.get(
                "initial_cents",
                0
            ) / 100
        )

        embed.add_field(
            name="💰 Giá gốc",
            value=(
                f"{format_usd(original_usd)}\n"
                f"🇰🇷 ≈ "
                f"{format_krw(usd_to_krw(original_usd))}\n"
                f"🇻🇳 ≈ "
                f"{format_vnd(usd_to_vnd(original_usd))}"
            ),
            inline=True
        )

    else:
        original = float(
            game.get(
                "original_price",
                0
            ) or 0
        )

        currency = game.get(
            "currency",
            "USD"
        )

        embed.add_field(
            name="💰 Giá gốc",
            value=(
                f"{currency} {original:,.2f}\n"
                f"🇰🇷 ≈ "
                f"{format_krw(usd_to_krw(original))}\n"
                f"🇻🇳 ≈ "
                f"{format_vnd(usd_to_vnd(original))}"
            ),
            inline=True
        )

    embed.add_field(
        name="🎉 Giá hiện tại",
        value=(
            "**FREE**\n"
            "₩0 / 0₫"
        ),
        inline=True
    )

    end_date = game.get(
        "end_date"
    )

    if end_date:
        embed.add_field(
            name="⏰ Hạn",
            value=end_date.replace(
                "T",
                " "
            ),
            inline=False
        )

    if game.get("image"):
        embed.set_image(
            url=game["image"]
        )

    embed.set_footer(
        text="Free Game Bot • Epic + Steam"
    )

    return embed


def build_search_embed(game):
    embed = discord.Embed(
        title=f"🎮 {game['title']}",
        url=game["url"],
        description=(
            f"🏪 **{game['store']}**"
        )
    )

    if game["store"] == "Steam":

        krw = game.get("krw")
        vnd = game.get("vnd")

        krw_text = (
            format_krw(krw)
            if krw is not None
            else "N/A"
        )

        vnd_text = (
            format_vnd(vnd)
            if vnd is not None
            else "N/A"
        )

        embed.add_field(
            name="💰 Giá",
            value=(
                f"🇰🇷 **{krw_text}**\n"
                f"🇻🇳 **{vnd_text}**"
            ),
            inline=False
        )

        if game.get("discount"):
            embed.add_field(
                name="🏷️ Giảm giá",
                value=(
                    f"-{game['discount']}%"
                ),
                inline=True
            )

    else:
        original = float(
            game.get(
                "original_price",
                0
            ) or 0
        )

        current = float(
            game.get(
                "discount_price",
                0
            ) or 0
        )

        currency = game.get(
            "currency",
            "KRW"
        )

        embed.add_field(
            name="💰 Giá hiện tại",
            value=(
                f"{currency} "
                f"{current:,.2f}\n"
                f"🇰🇷 ≈ "
                f"{format_krw(usd_to_krw(current))}\n"
                f"🇻🇳 ≈ "
                f"{format_vnd(usd_to_vnd(current))}"
            ),
            inline=False
        )

        if original > 0:
            embed.add_field(
                name="💵 Giá gốc",
                value=(
                    f"{currency} "
                    f"{original:,.2f}"
                ),
                inline=True
            )

    if game.get("image"):
        embed.set_thumbnail(
            url=game["image"]
        )

    embed.set_footer(
        text="Game Search"
    )

    return embed


# =========================================================
# GAME SEARCH MODAL
# =========================================================

class GameSearchModal(discord.ui.Modal):

    def __init__(self, store):
        self.store = store

        super().__init__(
            title=f"Tìm game {store}"
        )

        self.game_name = discord.ui.TextInput(
            label="Tên game",
            placeholder=(
                "Ví dụ: GTA V, Minecraft..."
            ),
            required=True,
            max_length=100
        )

        self.add_item(
            self.game_name
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):
        keyword = str(
            self.game_name.value
        ).strip()

        await interaction.response.defer(
            ephemeral=True
        )

        if self.store == "Steam":
            results = await steam_search_game(
                keyword
            )
        else:
            results = await epic_search(
                keyword
            )

        if not results:
            await interaction.followup.send(
                f"❌ Không tìm thấy "
                f"**{keyword}** trên "
                f"{self.store}.",
                ephemeral=True
            )
            return

        # Hiện kết quả đầu tiên.
        game = results[0]

        embed = build_search_embed(
            game
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True
        )


# =========================================================
# STORE BUTTONS
# =========================================================

class GameStoreView(discord.ui.View):

    def __init__(self):
        super().__init__(
            timeout=180
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
            GameSearchModal("Epic")
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
            GameSearchModal("Steam")
        )


# =========================================================
# /GAME
# =========================================================

@bot.tree.command(
    name="game",
    description="Tìm game trên Epic hoặc Steam"
)
async def game(
    interaction: discord.Interaction
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Lệnh này chỉ dùng trong server.",
            ephemeral=True
        )
        return

    search_channel = find_search_channel(
        interaction.guild
    )

    if search_channel is None:
        await interaction.response.send_message(
            "❌ Không tìm thấy kênh "
            f"`{SEARCH_CHANNEL_NAME}`.",
            ephemeral=True
        )
        return

    if interaction.channel_id != search_channel.id:
        await interaction.response.send_message(
            "❌ Hãy dùng `/game` tại "
            f"{search_channel.mention}.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🎮 GAME SEARCH",
        description=(
            "Chọn cửa hàng muốn tìm game:\n\n"
            "🟣 **Epic Games Store**\n"
            "🔵 **Steam**"
        )
    )

    await interaction.response.send_message(
        embed=embed,
        view=GameStoreView()
    )


# =========================================================
# /CHANNELS
# =========================================================

@bot.tree.command(
    name="channels",
    description="Kiểm tra bot đang nhận kênh nào"
)
async def channels(
    interaction: discord.Interaction
):
    notification = find_notification_channel()

    search = None

    if interaction.guild:
        search = find_search_channel(
            interaction.guild
        )

    embed = discord.Embed(
        title="📋 Kênh của Free Game Bot"
    )

    embed.add_field(
        name="📢 Thông báo",
        value=(
            notification.mention
            if notification
            else f"❌ Không tìm thấy "
                 f"`{NOTIFICATION_CHANNEL_NAME}`"
        ),
        inline=False
    )

    embed.add_field(
        name="🎮 Tìm game",
        value=(
            search.mention
            if search
            else f"❌ Không tìm thấy "
                 f"`{SEARCH_CHANNEL_NAME}`"
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# SEND FREE GAME
# =========================================================

async def send_free_game(game):
    channel = find_notification_channel()

    if channel is None:
        print(
            "[CHANNEL] Không tìm thấy "
            f"#{NOTIFICATION_CHANNEL_NAME}"
        )
        return False

    try:
        await channel.send(
            embed=build_free_embed(
                game
            )
        )

        print(
            "[SEND] "
            f"{game['store']} - "
            f"{game['title']}"
        )

        return True

    except discord.Forbidden:
        print(
            "[CHANNEL] Bot không có quyền "
            "Send Messages/Embed Links."
        )

    except Exception as e:
        print(
            f"[SEND ERROR] {e}"
        )

    return False


# =========================================================
# CHECK EPIC
# =========================================================

async def check_epic():
    games = await get_epic_free_games()

    print(
        f"[EPIC] Found {len(games)} free games."
    )

    for game in games:

        key = game_key(
            "epic",
            game["id"]
        )

        if key in data["sent_games"]:
            continue

        # Không spam toàn bộ game cũ lần đầu.
        if (
            not data["initialized"]
            and not ANNOUNCE_EXISTING_ON_FIRST_RUN
        ):
            data["sent_games"].append(
                key
            )
            continue

        sent = await send_free_game(
            game
        )

        if sent:
            data["sent_games"].append(
                key
            )

        await asyncio.sleep(1)

    save_data()


# =========================================================
# CHECK STEAM
# =========================================================

async def check_steam():
    games = await get_steam_free_to_keep()

    print(
        "[STEAM] Found "
        f"{len(games)} Free-to-Keep games."
    )

    for game in games:

        key = game_key(
            "steam",
            game["id"]
        )

        if key in data["sent_games"]:
            continue

        if (
            not data["initialized"]
            and not ANNOUNCE_EXISTING_ON_FIRST_RUN
        ):
            data["sent_games"].append(
                key
            )
            continue

        sent = await send_free_game(
            game
        )

        if sent:
            data["sent_games"].append(
                key
            )

        await asyncio.sleep(1)

    save_data()


# =========================================================
# CLEAN OLD SENT IDS
# =========================================================

def clean_sent_games():
    """
    Chỉ giữ 2000 ID gần nhất để bot không
    làm file bot_data.json quá lớn.
    """

    if len(data["sent_games"]) > 2000:
        data["sent_games"] = (
            data["sent_games"][-2000:]
        )

        save_data()


# =========================================================
# AUTO CHECKER
# =========================================================

@tasks.loop(
    minutes=CHECK_INTERVAL_MINUTES
)
async def free_game_checker():

    print(
        "\n=============================="
    )

    print(
        "[CHECK] Bắt đầu kiểm tra game..."
    )

    notification = find_notification_channel()

    if notification:
        print(
            "[CHANNEL] Thông báo: "
            f"#{notification.name}"
        )
    else:
        print(
            "[CHANNEL] ❌ Không tìm thấy "
            f"#{NOTIFICATION_CHANNEL_NAME}"
        )

    try:
        await check_epic()
    except Exception as e:
        print(
            f"[EPIC ERROR] {e}"
        )

    try:
        await check_steam()
    except Exception as e:
        print(
            f"[STEAM ERROR] {e}"
        )

    if not data["initialized"]:
        data["initialized"] = True
        save_data()

    clean_sent_games()

    print(
        "[CHECK] Hoàn thành."
    )

    print(
        "==============================\n"
    )


@free_game_checker.before_loop
async def before_checker():
    await bot.wait_until_ready()


# =========================================================
# BOT READY
# =========================================================

@bot.event
async def on_ready():

    print(
        "\n================================"
    )

    print(
        f"🤖 Bot: {bot.user}"
    )

    print(
        f"📡 Servers: {len(bot.guilds)}"
    )

    notification = find_notification_channel()

    if notification:
        print(
            "📢 Notification channel: "
            f"#{notification.name}"
        )
    else:
        print(
            "❌ Notification channel NOT FOUND: "
            f"#{NOTIFICATION_CHANNEL_NAME}"
        )

    for guild in bot.guilds:
        search = find_search_channel(
            guild
        )

        if search:
            print(
                f"🎮 Search channel "
                f"({guild.name}): "
                f"#{search.name}"
            )

    try:
        synced = await bot.tree.sync()

        print(
            f"⚡ Slash commands synced: "
            f"{len(synced)}"
        )

    except Exception as e:
        print(
            f"❌ Slash sync error: {e}"
        )

    if not free_game_checker.is_running():
        free_game_checker.start()

    print(
        "✅ Bot is ready."
    )

    print(
        "================================\n"
    )


# =========================================================
# COMMAND ERROR
# =========================================================

@bot.tree.error
async def on_app_command_error(
    interaction,
    error
):

    print(
        f"[COMMAND ERROR] {error}"
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
# CLOSE HTTP SESSION
# =========================================================

async def close_http_session():
    global http_session

    if (
        http_session
        and not http_session.closed
    ):
        await http_session.close()


# =========================================================
# MAIN
# =========================================================

async def main():

    if not DISCORD_TOKEN:
        raise RuntimeError(
            "❌ Chưa có DISCORD_TOKEN "
            "trong Railway Variables."
        )

    try:
        await bot.start(
            DISCORD_TOKEN
        )

    finally:
        await close_http_session()


if __name__ == "__main__":
    try:
        asyncio.run(
            main()
        )
    except KeyboardInterrupt:
        pass
