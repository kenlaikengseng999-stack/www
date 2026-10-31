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
from flask import Flask
from threading import Thread

# Flask 保活設定
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

def keep_alive():
    t = Thread(target=run)
    t.start()

# ==========================================
# 1. 讀取環境變數與基本設定
# ==========================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))
MONGO_URI = os.getenv("MONGO_URI")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
HOME_URL = "https://9y.bfage.com/home/"
BASE_URL = "https://9y.bfage.com"
CHECK_INTERVAL_MINUTES = 5

# MongoDB 設定
DB_NAME = "news_bot"
COLLECTION_NAME = "settings"

# ==========================================
# 2. 初始化 MongoDB 連線
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
# 3. 資料庫讀寫函式
# ==========================================
def load_last_seen_id() -> int:
    """從 MongoDB 讀取上次已發送的最高新聞 ID"""
    try:
        doc = collection.find_one({"_id": "latest_news_id"})
        if doc and "val" in doc:
            print(f"📥 從 MongoDB 讀取最新已發送的 ID: {doc['val']}")
            return int(doc["val"])
    except Exception as e:
        print(f"⚠️ 讀取 MongoDB 發生異常: {e}")
    return 3840

def save_last_seen_id(news_id: int):
    """寫入最新的新聞 ID 到 MongoDB"""
    try:
        collection.update_one(
            {"_id": "latest_news_id"},
            {"$set": {"val": news_id, "updated_at": datetime.utcnow()}},
            upsert=True
        )
        print(f"💾 最新新聞 ID {news_id} 已儲存至 MongoDB")
    except Exception as e:
        print(f"❌ 寫入 MongoDB 失敗: {e}")

last_seen_id = load_last_seen_id()

# ==========================================
# 4. 網址正規化與首頁解析邏輯
# ==========================================
def fix_and_normalize_url(url):
    if url.startswith("//"):
        url = "http:" + url
    elif not url.startswith("http"):
        url = "http://" + url
    parsed = urlparse(url)
    clean_path = re.sub(r'/index\.(html?|php)$', '/', parsed.path, flags=re.IGNORECASE)
    if not clean_path.endswith('/') and not re.search(r'\.[a-zA-Z0-9]+$', clean_path):
        clean_path += '/'
    return urlunparse((parsed.scheme, parsed.netloc, clean_path, parsed.params, parsed.query, parsed.fragment))

def extract_id_from_url(url):
    match = re.search(r'/news/detail/(\d+)', url)
    if match:
        return int(match.group(1))
    return 0

async def fetch_home_news(session: aiohttp.ClientSession):
    news_items = []
    try:
        async with session.get(HOME_URL, headers=HEADERS, timeout=10) as res:
            if res.status == 200:
                html = await res.text(encoding="utf-8")
                soup = BeautifulSoup(html, "html.parser")
                
                links = soup.select("a[href*='/news/detail/']")
                seen_links = set()
                for a in links:
                    href = a.get("href", "")
                    full_url = fix_and_normalize_url(BASE_URL + href if href.startswith("/") else href)
                    
                    if full_url in seen_links:
                        continue
                    seen_links.add(full_url)
                    
                    news_id = extract_id_from_url(full_url)
                    if news_id == 0:
                        continue
                        
                    title = a.get_text(strip=True)
                    fl = a.select_one(".fl")
                    fr = a.select_one(".fr")
                    if fl:
                        title = fl.get_text(strip=True)
                    date_str = fr.get_text(strip=True) if fr else "日期未標明"
                    
                    title = re.sub(r'\s+', ' ', title)
                    if not title or len(title) < 2:
                        continue

                    news_items.append({
                        "id": news_id,
                        "url": full_url,
                        "title": title,
                        "date": date_str
                    })
    except Exception as e:
        print(f"💥 爬取首頁發生錯誤: {e}")
        
    news_items.sort(key=lambda x: x["id"])
    return news_items

# ==========================================
# 5. 初始化 Discord Bot & Web Server
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
# 6. 測試指令與定時輪詢任務
# ==========================================
@bot.command(name="test_news")
async def test_news(ctx):
    """手動輸入 !test_news 可以立刻預覽最新抓到的公告格式"""
    async with aiohttp.ClientSession() as session:
        news_items = await fetch_home_news(session)
        if not news_items:
            await ctx.send("❌ 目前無法從首頁取得公告資料。")
            return
        
        # 取清單中的最後一筆（通常是最新的一筆）來做範例預覽
        latest = news_items[-1]
        
        embed = discord.Embed(
            title=f"📰 [格式預覽] {latest['title']}",
            url=latest["url"],
            color=discord.Color.blue()
        )
        embed.add_field(name="發布日期", value=latest["date"], inline=True)
        embed.add_field(name="新聞 ID", value=str(latest["id"]), inline=True)

        await ctx.send(embed=embed)
        await ctx.send(f"💡 上次記錄的已發送 ID 為: `{last_seen_id}` (若想重設進度，可以調整 MongoDB)")

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def auto_check_news():
    global last_seen_id
    
    try:
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            print(f"❌ 找不到目標頻道 ID: {TARGET_CHANNEL_ID}")
            return

        async with aiohttp.ClientSession() as session:
            news_items = await fetch_home_news(session)
            if not news_items:
                return

            new_articles_found = False
            highest_id = last_seen_id

            for item in news_items:
                n_id = item["id"]
                if n_id > last_seen_id:
                    new_articles_found = True
                    
                    embed = discord.Embed(
                        title=f"📰 {item['title']}",
                        url=item["url"],
                        color=discord.Color.blue()
                    )
                    embed.add_field(name="發布日期", value=item["date"], inline=True)
                    embed.add_field(name="新聞 ID", value=str(n_id), inline=True)

                    await channel.send(embed=embed)
                    print(f"🎉 成功發送新公告！[ID: {n_id}] 標題: {item['title']}")

                    if n_id > highest_id:
                        highest_id = n_id

            if new_articles_found:
                last_seen_id = highest_id
                save_last_seen_id(last_seen_id)

    except Exception as general_err:
        print(f"💥 auto_check_news 發生例外: {general_err}")

# ==========================================
# 7. Bot 啟動生命週期
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
