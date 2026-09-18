import discord
from discord.ext import commands, tasks
from aiohttp import web
import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse
import re
import os
from pymongo import MongoClient

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ⚙️ 設定區
TARGET_CHANNEL_ID = 123456789012345678  # ⚠️ 請替換為接收新聞的 Discord 頻道 ID
DEFAULT_START_ID = 3810                # 若資料庫完全無紀錄時的預設起始 ID
CHECK_INTERVAL_MINUTES = 5             # 自動探測間隔（分鐘）

# --- MongoDB 雲端資料庫邏輯 ---

MONGO_URI = os.environ.get("MONGO_URI")

def get_db_collection():
    if MONGO_URI:
        try:
            client = MongoClient(MONGO_URI)
            db = client["news_bot_db"]
            return db["bot_state"]
        except Exception as e:
            print(f"⚠️ MongoDB 連線失敗: {e}")
    return None

def load_last_id():
    """從雲端資料庫讀取最新新聞 ID"""
    collection = get_db_collection()
    if collection is not None:
        try:
            doc = collection.find_one({"_id": "last_news_id"})
            if doc and "val" in doc:
                print(f"☁️ 成功從雲端資料庫讀取上次紀錄 ID：{doc['val']}")
                return doc["val"]
        except Exception as e:
            print(f"⚠️ 讀取雲端 ID 失敗: {e}")
    print(f"📌 使用預設起始 ID：{DEFAULT_START_ID}")
    return DEFAULT_START_ID

def save_last_id(news_id):
    """將最新 ID 自動同步寫入雲端資料庫"""
    collection = get_db_collection()
    if collection is not None:
        try:
            collection.update_one(
                {"_id": "last_news_id"},
                {"$set": {"val": news_id}},
                upsert=True
            )
            print(f"☁️ 已將最新 ID ({news_id}) 自動同步儲存至雲端資料庫！")
        except Exception as e:
            print(f"⚠️ 儲存至雲端失敗: {e}")

last_checked_id = load_last_id()

# --- Web Server（給 Render 保活用） ---

async def handle_ping(request):
    return web.Response(text="Bot is alive!", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/health', handle_ping)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Web Server 已在通訊埠 {port} 啟動")

# --- 網址處理與解析邏輯 ---

def fix_and_normalize_url(url):
    if url.startswith("//"):
        url = "http:" + url
    elif not url.startswith("http"):
        url = "http://" + url

    parsed = urlparse(url)
    clean_path = re.sub(r'/index\.(html?|php)$', '/', parsed.path, flags=re.IGNORECASE)
    
    if not clean_path.endswith('/') and not re.search(r'\.[a-zA-Z0-9]+$', clean_path):
        clean_path += '/'

    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        clean_path,
        parsed.params,
        parsed.query,
        parsed.fragment
    ))

def extract_news_detail_info(soup, text):
    title = "無標題頁面"
    pub_date = "日期未標明"

    date_match = re.search(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})', text)
    if date_match:
        d_str = date_match.group(1).replace('/', '-')
        parts = d_str.split('-')
        pub_date = f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"

    share_div = soup.find("div", class_="newsContShare")
    if share_div:
        prev_a = share_div.find_previous("a")
        if prev_a and prev_a.get_text(strip=True):
            title = prev_a.get_text(strip=True)

    if title == "無標題頁面":
        tit_block = soup.find(class_=re.compile(r'modTit|newsContModTit'))
        if tit_block:
            a_tag = tit_block.find("a")
            if a_tag and a_tag.get_text(strip=True):
                title = a_tag.get_text(strip=True)
            elif tit_block.get_text(strip=True):
                title = tit_block.get_text(strip=True)

    if title == "無標題頁面":
        if soup.title and soup.title.string:
            title = re.sub(r'[-_\|].*$', '', soup.title.string).strip() or "官方頁面"

    return title, pub_date

async def fetch_and_parse_news(session, news_id):
    target_url = f"https://9y.bfage.com/news/detail/{news_id}/"
    try:
        async with session.get(target_url, headers=HEADERS, timeout=4, allow_redirects=False) as res:
            if res.status == 200:
                html_text = await res.text(encoding="utf-8")
                soup = BeautifulSoup(html_text, "html.parser")
                valid_url = fix_and_normalize_url(str(res.url))
                title, pub_date = extract_news_detail_info(soup, html_text)
                return valid_url, title, pub_date
    except Exception:
        pass
    return None

# --- Discord Bot 設定 ---

intents = discord.Intents.default()
intents.message_content = True

class NewsBot(commands.Bot):
    async def setup_hook(self):
        await start_web_server()
        if not auto_check_news.is_running():
            auto_check_news.start()
            print(f"⏰ 自動探測已啟動（每 {CHECK_INTERVAL_MINUTES} 分鐘檢查一次）")

bot = NewsBot(command_prefix="!", intents=intents)

# --- 每 5 分鐘自動執行的 Task ---

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def auto_check_news():
    global last_checked_id
    
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if not channel:
        print(f"❌ 找不到頻道 ID: {TARGET_CHANNEL_ID}")
        return

    max_failures = 5
    consecutive_failures = 0
    curr_id = last_checked_id

    async with aiohttp.ClientSession() as session:
        while consecutive_failures < max_failures:
            result = await fetch_and_parse_news(session, curr_id)

            if result:
                valid_url, title, pub_date = result
                consecutive_failures = 0
                
                embed = discord.Embed(
                    title=f"📰 {title}",
                    url=valid_url,
                    color=discord.Color.green()
                )
                embed.add_field(name="發布日期", value=pub_date, inline=True)
                embed.add_field(name="新聞 ID", value=str(curr_id), inline=True)

                await channel.send(embed=embed)

                last_checked_id = curr_id + 1
                save_last_id(last_checked_id)
            else:
                consecutive_failures += 1

            curr_id += 1

@auto_check_news.before_loop
async def before_auto_check():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f"🤖 Bot 已成功上線！登入身分：{bot.user.name}")
    print(f"📌 目前起始探測 ID 為：{last_checked_id}")

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("❌ 未找到 DISCORD_TOKEN 環境變數，請在 Render 設定！")

bot.run(TOKEN)
