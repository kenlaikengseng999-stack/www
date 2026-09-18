import os
import re
import asyncio
from datetime import datetime
from urllib.parse import urlparse, urlunparse
import aiohttp
from bs4 import BeautifulSoup
import discord
from discord.ext import tasks, commands
from pymongo import MongoClient
from aiohttp import web

# ==========================================
# 1. 讀取環境變數 (Environment Variables)
# ==========================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))
MONGO_URI = os.getenv("MONGO_URI")

# 網站與爬蟲設定
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
BASE_URL = "https://9y.bfage.com/news/detail/"
DEFAULT_START_ID = 3810
CHECK_INTERVAL_MINUTES = 1

# MongoDB 設定
DB_NAME = "news_bot"
COLLECTION_NAME = "settings"

# ==========================================
# 2. 初始化 MongoDB & 連線 Ping 測試
# ==========================================
if not MONGO_URI:
    print("⚠️ 警告: 未設定 MONGO_URI 環境變數！")

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]
collection = db[COLLECTION_NAME]

try:
    mongo_client.admin.command('ping')
    print("✅ MongoDB 連線成功！")
except Exception as e:
    print(f"❌ MongoDB 連線失敗，請檢查 MONGO_URI: {e}")

# ==========================================
# 3. 資料庫讀寫函式 (Database Helpers)
# ==========================================
def load_last_id() -> int:
    """從 MongoDB 讀取上次檢查的最後新聞 ID"""
    try:
        doc = collection.find_one({"_id": "last_checked_id"})
        if doc and "val" in doc:
            print(f"📥 從 MongoDB 讀取進度 ID: {doc['val']}")
            return int(doc["val"])
    except Exception as e:
        print(f"⚠️ 讀取 MongoDB 發生異常: {e}")
    print(f"ℹ️ 使用預設起始 ID: {DEFAULT_START_ID}")
    return DEFAULT_START_ID

def save_last_id(last_id: int):
    """寫入最新的新聞 ID 到 MongoDB"""
    try:
        collection.update_one(
            {"_id": "last_checked_id"},
            {"$set": {"val": last_id, "updated_at": datetime.utcnow()}},
            upsert=True
        )
        print(f"💾 進度 ID {last_id} 已儲存至 MongoDB")
    except Exception as e:
        print(f"❌ 寫入 MongoDB 失敗: {e}")

# 全域進度變數
last_checked_id = load_last_id()

# ==========================================
# 4. 網址正規化與內頁解析邏輯 (精準對接 9y.bfage)
# ==========================================
def fix_and_normalize_url(url):
    """補全協定標頭並正規化網址"""
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
    """【NEWS 內頁專用】依據 DOM 結構精準定位標題與日期 (YYYY-MM-DD)"""
    title = "無標題頁面"
    pub_date = "日期未標明"

    # 1. 抓取新聞精準日期
    date_match = re.search(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})', text)
    if date_match:
        d_str = date_match.group(1).replace('/', '-')
        parts = d_str.split('-')
        pub_date = f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"

    # 2. 定位 DOM 結構中的標題
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

async def fetch_and_parse_news(session: aiohttp.ClientSession, news_id: int):
    """發送 HTTP 請求解析文章，若頁面不存在或無標題則回傳 None"""
    target_url = f"{BASE_URL}{news_id}/"
    try:
        async with session.get(target_url, headers=HEADERS, timeout=5, allow_redirects=False) as res:
            if res.status == 200:
                html = await res.text(encoding="utf-8")
                soup = BeautifulSoup(html, "html.parser")
                valid_url = fix_and_normalize_url(str(res.url))
                title, pub_date = extract_news_detail_info(soup, html)
                
                # 🛡️ 嚴格過濾：若依然找不到有效標題，判定為不存在的文章
                if title in ["無標題頁面", "官方頁面", ""]:
                    return None
                    
                return valid_url, title, pub_date
    except Exception:
        pass
    return None

# ==========================================
# 5. 初始化 Discord Bot & aiohttp Web Server
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def handle_health_check(request):
    return web.Response(text="Bot is alive!", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)
    
    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 保活 Web Server 已啟動，通訊埠: {port}")

# ==========================================
# 6. 定時輪詢任務 (Background Loop Task)
# ==========================================
@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def auto_check_news():
    global last_checked_id
    
    try:
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            print(f"❌ 找不到目標頻道 ID: {TARGET_CHANNEL_ID}，請檢查環境變數 TARGET_CHANNEL_ID")
            return

        max_failures = 5
        consecutive_failures = 0
        curr_id = last_checked_id
        found_new_article = False

        async with aiohttp.ClientSession() as session:
            while consecutive_failures < max_failures:
                result = await fetch_and_parse_news(session, curr_id)

                if result:
                    valid_url, title, pub_date = result
                    consecutive_failures = 0
                    found_new_article = True
                    
                    embed = discord.Embed(
                        title=f"📰 {title}",
                        url=valid_url,
                        color=discord.Color.blue()
                    )
                    embed.add_field(name="發布日期", value=pub_date, inline=True)
                    embed.add_field(name="新聞 ID", value=str(curr_id), inline=True)

                    await channel.send(embed=embed)
                    print(f"🎉 成功發送新聞推播！[ID: {curr_id}] [日期: {pub_date}] 標題: {title}")

                    last_checked_id = curr_id + 1
                    save_last_id(last_checked_id)
                else:
                    consecutive_failures += 1

                curr_id += 1

        if not found_new_article:
            print(f"🔍 檢查完成（目前最新探測進度 ID: {last_checked_id}），暫無新文章")

    except Exception as general_err:
        print(f"💥 auto_check_news 發生非預期例外（已保護攔截）: {general_err}")

# ==========================================
# 7. Bot 事件處理與啟動 (Bot Lifecycle)
# ==========================================
@bot.event
async def on_ready():
    print(f"🤖 機器人已成功登入: {bot.user} (ID: {bot.user.id})")
    
    bot.loop.create_task(start_web_server())

    if not auto_check_news.is_running():
        auto_check_news.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ 錯誤：未設定 DISCORD_TOKEN 環境變數！")
    else:
        bot.run(DISCORD_TOKEN)
