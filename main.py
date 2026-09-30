import os
import json
import asyncio
import unicodedata
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

# Không spam những game đã free từ trước khi bot chạy lần đầu
ANNOUNCE_EXISTING_ON_FIRST_RUN = False

# Tỷ giá tham khảo cho Epic
USD_TO_KRW = 1400
USD_TO_VND = 25000


# =========================================================
# DISCORD INTENTS
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
    if not os.path.exists(DATA_FILE):
        return {
            "epic_sent": [],
            "steam_sent": [],
            "first_run_done": False
        }

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        data.setdefault("epic_sent", [])
        data.setdefault("steam_sent", [])
        data.setdefault("first_run_done", False)

        return data

    except Exception:
        return {
            "epic_sent": [],
            "steam_sent": [],
            "first_run_done": False
        }


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


data = load_data()


# =========================================================
# CHANNEL NAME
# =========================================================

def normalize_channel_name(name: str):
    """
    Cho phép:
    thong-bao-game
    🎁・thong-bao-game
    🎁・Thong-Bao-Game
    🎮・tim-game
    """

    name = name.strip().lower()

    # Nếu có dấu ・ thì lấy phần phía sau
    if "・" in name:
        name = name.split("・", 1)[1]

    name = name.replace("_", "-")
    name = name.replace(" ", "-")

    return name


def find_channel(guild: discord.Guild, wanted_name: str):
    wanted = normalize_channel_name(wanted_name)

    for channel in guild.text_channels:
        current = normalize_channel_name(channel.name)

        if current == wanted:
            return channel

    return None


def get_notification_channel():
    for guild in bot.guilds:
        channel = find_channel(guild, NOTIFICATION_CHANNEL_NAME)

        if channel:
            return channel

    return None


def get_search_channel():
    for guild in bot.guilds:
        channel = find_channel(guild, SEARCH_CHANNEL_NAME)

        if channel:
            return channel

    return None


# =========================================================
# HTTP
# =========================================================

async def get_json(session, url, **kwargs):
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=25),
            **kwargs
        ) as response:

            if response.status != 200:
                return None

            return await response.json(content_type=None)

    except Exception:
        return None


async def get_text(session, url, **kwargs):
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=25),
            **kwargs
        ) as response:

            if response.status != 200:
                return None

            return await response.text()

    except Exception:
        return None


# =========================================================
# EPIC
# =========================================================

EPIC_FREE_URL = (
    "https://store-site-backend-static.ak.epicgames.com/"
    "freeGamesPromotions"
)

EPIC_GRAPHQL_URL = "https://store.epicgames.com/graphql"


def epic_usd_to_prices(usd):
    try:
        usd = float(usd)

        krw = round(usd * USD_TO_KRW)
        vnd = round(usd * USD_TO_VND)

        return krw, vnd

    except Exception:
        return 0, 0


async def get_epic_free_games(session):
    data_json = await get_json(session, EPIC_FREE_URL)

    if not data_json:
        return []

    try:
        elements = (
            data_json
            ["data"]
            ["Catalog"]
            ["searchStore"]
            ["elements"]
        )
    except Exception:
        return []

    results = []

    for game in elements:
        try:
            title = game.get("title")

            if not title:
                continue

            promotions = game.get("promotions")

            if not promotions:
                continue

            offers = promotions.get("promotionalOffers") or []

            if not offers:
                continue

            current_offer = offers[0]["promotionalOffers"][0]

            discount = current_offer.get("discountSetting", {})

            if discount.get("discountPercentage") != 0:
                continue

            start_date = current_offer.get("startDate")
            end_date = current_offer.get("endDate")

            slug = game.get("productSlug") or game.get("urlSlug")

            if not slug:
                continue

            slug = slug.split("/")[0]

            url = f"https://store.epicgames.com/p/{slug}"

            key = game.get("id") or slug

            # Ảnh
            image = None

            for img in game.get("keyImages", []):
                if img.get("type") in (
                    "OfferImageWide",
                    "DieselStoreFrontWide"
                ):
                    image = img.get("url")
                    break

            if not image:
                for img in game.get("keyImages", []):
                    image = img.get("url")

                    if image:
                        break

            # Giá gốc
            original_krw = 0
            original_vnd = 0

            price_info = game.get("price", {})

            total_price = price_info.get("totalPrice", {})

            original_price = (
                total_price.get("originalPrice")
                or total_price.get("discountedPrice")
            )

            if original_price:
                usd = original_price / 100000
                original_krw, original_vnd = epic_usd_to_prices(usd)

            results.append({
                "id": key,
                "title": title,
                "url": url,
                "image": image,
                "start_date": start_date,
                "end_date": end_date,
                "original_krw": original_krw,
                "original_vnd": original_vnd
            })

        except Exception:
            continue

    return results


# =========================================================
# EPIC SEARCH
# =========================================================

async def search_epic(session, query):
    gql = """
    query searchStoreQuery(
        $keyword: String!
        $locale: String
    ) {
        Catalog {
            searchStore(
                keywords: $keyword
                locale: $locale
                count: 20
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
            "keyword": query,
            "locale": "en-US"
        }
    }

    try:
        async with session.post(
            EPIC_GRAPHQL_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=25)
        ) as response:

            if response.status != 200:
                return []

            result = await response.json()

    except Exception:
        return []

    try:
        elements = result["data"]["Catalog"]["searchStore"]["elements"]
    except Exception:
        return []

    results = []

    query_lower = query.lower().strip()

    for game in elements:
        title = game.get("title")

        if not title:
            continue

        # Bỏ những kết quả quá lệch từ khóa
        if query_lower not in title.lower():
            continue

        slug = game.get("productSlug") or game.get("urlSlug")

        if not slug:
            continue

        slug = slug.split("/")[0]

        url = f"https://store.epicgames.com/p/{slug}"

        image = None

        for img in game.get("keyImages", []):
            if img.get("type") in (
                "OfferImageWide",
                "DieselStoreFrontWide"
            ):
                image = img.get("url")
                break

        if not image:
            for img in game.get("keyImages", []):
                image = img.get("url")

                if image:
                    break

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
                usd = original_price / 100000
                original_krw, original_vnd = epic_usd_to_prices(usd)

        except Exception:
            pass

        results.append({
            "id": game.get("id") or slug,
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
# STEAM SEARCH
# =========================================================

STEAM_SEARCH_URL = "https://store.steampowered.com/search/results/"


async def search_steam(session, query):
    url = (
        STEAM_SEARCH_URL
        f"?term={quote_plus(query)}"
        "&category1=998"
        "&infinite=1"
        "&count=30"
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

    try:
        soup = BeautifulSoup(html, "html.parser")

        rows = soup.select("a.search_result_row")

        results = []

        query_lower = query.lower().strip()

        for row in rows:

            title_el = row.select_one(".title")

            if not title_el:
                continue

            title = title_el.get_text(strip=True)

            if query_lower not in title.lower():
                continue

            href = row.get("href")

            if not href:
                continue

            appid = row.get("data-ds-appid")

            if not appid:
                # thử lấy từ link
                import re

                match = re.search(
                    r"/app/(\d+)",
                    href
                )

                if match:
                    appid = match.group(1)

            if not appid:
                continue

            # Lấy ảnh
            image = None

            img = row.select_one("img")

            if img:
                image = (
                    img.get("src")
                    or img.get("data-src")
                )

            # Giá KRW
            price_el = row.select_one(".discount_final_price")

            krw = None

            if price_el:
                text_price = price_el.get_text(
                    " ",
                    strip=True
                )

                krw = parse_steam_price(
                    text_price
                )

            results.append({
                "appid": str(appid),
                "title": title,
                "url": f"https://store.steampowered.com/app/{appid}/",
                "image": image,
                "krw": krw,
                "vnd": None
            })

        # lấy thông tin chi tiết
        final_results = []

        for item in results[:15]:

            details = await get_steam_app_details(
                session,
                item["appid"],
                "KR"
            )

            if not details:
                continue

            item["image"] = (
                details.get("header_image")
                or item["image"]
            )

            price = details.get("price_overview")

            if price:
                item["krw"] = price.get("final")

            details_vnd = await get_steam_app_details(
                session,
                item["appid"],
                "VN"
            )

            if details_vnd:
                price_vnd = details_vnd.get(
                    "price_overview"
                )

                if price_vnd:
                    item["vnd"] = price_vnd.get("final")

            final_results.append(item)

        # Loại trùng
        unique = []
        seen = set()

        for item in final_results:

            if item["appid"] in seen:
                continue

            seen.add(item["appid"])
            unique.append(item)

        return unique[:10]

    except Exception:
        return []


def parse_steam_price(text):
    if not text:
        return None

    import re

    numbers = re.findall(
        r"[\d,.]+",
        text.replace(",", "")
    )

    if not numbers:
        return None

    try:
        value = float(numbers[-1])

        return int(value)

    except Exception:
        return None


async def get_steam_app_details(
    session,
    appid,
    country="KR"
):
    url = (
        "https://store.steampowered.com/api/appdetails"
        f"?appids={appid}&cc={country}&l=english"
    )

    data_json = await get_json(session, url)

    if not data_json:
        return None

    try:
        item = data_json[str(appid)]

        if not item.get("success"):
            return None

        return item.get("data")

    except Exception:
        return None


# =========================================================
# FORMAT PRICE
# =========================================================

def format_krw(value):
    if value is None:
        return "Không có giá"

    try:
        # Steam trả won theo đơn vị nhỏ
        if value > 100000:
            value = value // 100

        return f"₩{value:,}"

    except Exception:
        return "Không có giá"


def format_vnd(value):
    if value is None:
        return "Không có giá"

    try:
        # Steam VND thường trả đơn vị VND trực tiếp
        if value > 1000000:
            value = value // 100

        return f"{value:,.0f}₫"

    except Exception:
        return "Không có giá"


# =========================================================
# STEAM FREE GAME
# =========================================================

async def get_steam_free_games(session):
    url = (
        STEAM_SEARCH_URL
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

    soup = BeautifulSoup(html, "html.parser")

    rows = soup.select("a.search_result_row")

    results = []

    for row in rows:

        title_el = row.select_one(".title")

        if not title_el:
            continue

        title = title_el.get_text(strip=True)

        href = row.get("href")

        if not href:
            continue

        import re

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

        # Phải là game
        if details.get("type") != "game":
            continue

        # Bỏ game vốn free
        if details.get("is_free"):
            continue

        price = details.get("price_overview")

        if not price:
            continue

        initial = price.get("initial", 0)
        final = price.get("final", 0)
        discount = price.get("discount_percent", 0)

        # Free thật do giảm 100%
        if initial <= 0:
            continue

        if final != 0:
            continue

        if discount != 100:
            continue

        # Loại Free Weekend / Trial
        combined = (
            title + " " +
            str(details.get("short_description", ""))
        ).lower()

        blocked_words = [
            "free weekend",
            "free trial",
            "weekend trial",
            "limited trial"
        ]

        if any(word in combined for word in blocked_words):
            continue

        results.append({
            "id": appid,
            "title": title,
            "url": f"https://store.steampowered.com/app/{appid}/",
            "image": details.get("header_image"),
            "original_krw": initial,
            "original_vnd": None
        })

    return results


# =========================================================
# DISCORD EMBEDS
# =========================================================

def make_game_search_embed(
    store,
    query,
    results
):
    if store == "epic":
        color = 0x7C3AED
        store_name = "EPIC GAMES STORE"
        store_icon = "🟣"

    else:
        color = 0x1B9FFF
        store_name = "STEAM"
        store_icon = "🔵"

    embed = discord.Embed(
        title=f"🎮・{query.upper()}",
        description=(
            f"{store_icon} **{store_name}**\n"
            f"🔎 Tìm thấy **{len(results)}** kết quả\n"
            f"━━━━━━━━━━━━━━━━━━"
        ),
        color=color
    )

    for index, game in enumerate(results, start=1):

        if store == "epic":
            krw = format_krw(
                game.get("original_krw")
            )

            vnd = format_vnd(
                game.get("original_vnd")
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
            f"🔗 [Mở game]({game['url']})"
        )

        embed.add_field(
            name=f"#{index}・{game['title']}",
            value=value,
            inline=False
        )

    # =====================================================
    # 1 ẢNH DUY NHẤT Ở DƯỚI CÙNG
    # =====================================================

    image = None

    # Ưu tiên ảnh của kết quả đầu tiên
    if results:
        image = results[0].get("image")

    if image:
        embed.set_image(url=image)

    embed.set_footer(
        text=(
            f"🎮 Free Game Bot • "
            f"{store_name} • {len(results)} kết quả"
        )
    )

    return embed


def make_no_result_embed(
    store,
    query
):
    if store == "epic":
        color = 0x7C3AED
        store_name = "Epic Games Store"
    else:
        color = 0x1B9FFF
        store_name = "Steam"

    embed = discord.Embed(
        title="🔎 Không tìm thấy game",
        description=(
            f"Không tìm thấy **{query}** trên "
            f"**{store_name}**.\n\n"
            "💡 Thử nhập tên ngắn hơn, ví dụ:\n"
            "`Hitman`\n"
            "`GTA`\n"
            "`FIFA`\n"
            "`Minecraft`"
        ),
        color=color
    )

    return embed


def make_free_notification_embed(
    store,
    game
):
    if store == "epic":
        color = 0x7C3AED
        store_name = "EPIC GAMES STORE"
        icon = "🟣"
        button_text = "🛒 MỞ EPIC"

    else:
        color = 0x1B9FFF
        store_name = "STEAM"
        icon = "🔵"
        button_text = "🛒 MỞ STEAM"

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

    original_krw = game.get("original_krw")
    original_vnd = game.get("original_vnd")

    if original_krw or original_vnd:

        prices = []

        if original_krw:
            prices.append(
                f"🇰🇷 ₩{original_krw:,}"
            )

        if original_vnd:
            prices.append(
                f"🇻🇳 {original_vnd:,.0f}₫"
            )

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

    if game.get("end_date"):
        embed.add_field(
            name="⏰ Hạn",
            value=str(game["end_date"])[:19],
            inline=False
        )

    if game.get("image"):
        embed.set_image(
            url=game["image"]
        )

    embed.set_footer(
        text="🎮 Free Game Bot • Tự động cập nhật"
    )

    return embed


# =========================================================
# STORE BUTTON
# =========================================================

class StoreLinkButton(discord.ui.View):

    def __init__(self, url, label):
        super().__init__(timeout=None)

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

class GameSearchModal(discord.ui.Modal):

    def __init__(self, store):
        self.store = store

        if store == "epic":
            title = "🟣 Tìm game trên Epic"
        else:
            title = "🔵 Tìm game trên Steam"

        super().__init__(title=title)

        self.game_name = discord.ui.TextInput(
            label="Tên game",
            placeholder="Ví dụ: Hitman, GTA, Minecraft...",
            required=True,
            min_length=1,
            max_length=100
        )

        self.add_item(self.game_name)

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):
        search_channel = get_search_channel()

        if search_channel:
            if interaction.channel_id != search_channel.id:
                await interaction.response.send_message(
                    "🎮 Hãy dùng `/game` trong kênh `🎮・tim-game`.",
                    ephemeral=True
                )
                return

        await interaction.response.defer()

        query = self.game_name.value.strip()

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

            embed = make_no_result_embed(
                self.store,
                query
            )

            await interaction.followup.send(
                embed=embed
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
# STORE SELECT BUTTONS
# =========================================================

class StoreSearchView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=180)

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
    description="🔎 Tìm game trên Epic Games hoặc Steam"
)
async def game_command(
    interaction: discord.Interaction
):

    search_channel = get_search_channel()

    if search_channel:
        if interaction.channel_id != search_channel.id:

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

    embed.set_footer(
        text="🎮 Free Game Bot • Epic + Steam"
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
    description="📡 Kiểm tra các kênh bot đang sử dụng"
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

    embed = discord.Embed(
        title="📡・BOT CHANNELS",
        color=0x5865F2
    )

    if notification:
        notification_text = notification.mention
    else:
        notification_text = "❌ Không tìm thấy"

    if search:
        search_text = search.mention
    else:
        search_text = "❌ Không tìm thấy"

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

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# SEND FREE GAME
# =========================================================

async def send_epic_notifications(
    session,
    channel
):
    games = await get_epic_free_games(session)

    if not games:
        return

    first_run = not data.get("first_run_done", False)

    for game in games:

        game_id = str(game["id"])

        if game_id in data["epic_sent"]:
            continue

        # Lần đầu không spam các game đang free sẵn
        if first_run and not ANNOUNCE_EXISTING_ON_FIRST_RUN:

            data["epic_sent"].append(game_id)

            continue

        embed = make_free_notification_embed(
            "epic",
            game
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

            data["epic_sent"].append(game_id)

            save_data()

            await asyncio.sleep(2)

        except Exception as e:
            print(
                "❌ Epic send error:",
                e
            )

    # Giới hạn database
    data["epic_sent"] = data["epic_sent"][-2000:]


async def send_steam_notifications(
    session,
    channel
):
    games = await get_steam_free_games(session)

    if not games:
        return

    first_run = not data.get("first_run_done", False)

    for game in games:

        game_id = str(game["id"])

        if game_id in data["steam_sent"]:
            continue

        if first_run and not ANNOUNCE_EXISTING_ON_FIRST_RUN:

            data["steam_sent"].append(game_id)

            continue

        embed = make_free_notification_embed(
            "steam",
            game
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

            data["steam_sent"].append(game_id)

            save_data()

            await asyncio.sleep(2)

        except Exception as e:
            print(
                "❌ Steam send error:",
                e
            )

    data["steam_sent"] = data["steam_sent"][-2000:]


# =========================================================
# FREE GAME CHECKER
# =========================================================

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def free_game_checker():

    channel = get_notification_channel()

    if not channel:
        print(
            "⚠️ Không tìm thấy "
            f"#{NOTIFICATION_CHANNEL_NAME}"
        )
        return

    print(
        "🔎 Đang kiểm tra game miễn phí..."
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
                f"🎁 Notification channel: "
                f"#{notification.name}"
            )
        else:
            print(
                f"❌ Không thấy "
                f"#{NOTIFICATION_CHANNEL_NAME}"
            )

        if search:
            print(
                f"🎮 Search channel: "
                f"#{search.name}"
            )
        else:
            print(
                f"❌ Không thấy "
                f"#{SEARCH_CHANNEL_NAME}"
            )

        # Sync slash commands riêng từng server
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
        "⚡ Tổng số command đã sync:",
        len(bot.tree.get_commands())
    )

    print(
        "✅ Bot is ready."
    )

    if not free_game_checker.is_running():
        free_game_checker.start()


# =========================================================
# ERROR
# =========================================================

@bot.event
async def on_command_error(
    ctx,
    error
):
    print(
        "Command error:",
        error
    )


# =========================================================
# START
# =========================================================

if not DISCORD_TOKEN:
    raise RuntimeError(
        "❌ Chưa có DISCORD_TOKEN trong Environment Variables."
    )


bot.run(DISCORD_TOKEN)
