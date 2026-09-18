import os
import asyncio
from datetime import datetime
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

# 網站設定
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

# 強制驗證 MongoDB 連線狀態
try:
    mongo_client.admin.command('ping')
    print("✅ MongoDB 連線成功！")
except Exception as e:
    print(f"❌ MongoDB 連線失敗，請檢查 MONGO_URI 或 IP 白名單: {e}")

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

# 全域控制變數
last_checked_id = load_last_id()

# ==========================================
# 4. 初始化 Discord Bot & aiohttp Web Server
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 簡易 Health Check 網頁，用於 Render / Keep-Alive 保活
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
# 5. 網頁爬蟲函式 (Web Scraper)
# ==========================================
async def fetch_and_parse_news(session: aiohttp.ClientSession, news_id: int):
    """發送 HTTP 請求解析文章（含嚴格防空頁機制）"""
    target_url = f"{BASE_URL}{news_id}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        async with session.get(target_url, headers=headers, timeout=10) as response:
            # 1. 狀態碼非 200 直接過濾
            if response.status != 200:
                return None
            
            html = await response.text()
            soup = BeautifulSoup(html, "html.parser")

            # 2. 抓取標題
            title_tag = soup.select_one("h1, .news-title, .title")
            title = title_tag.get_text(strip=True) if title_tag else ""

            # 🛡️ 關鍵防護：過濾無效標題/空頁面
            invalid_keywords = ["未命名文章", "404", "不存在", "找不到", "Error", "頁面未找到"]
            if not title or any(keyword in title for keyword in invalid_keywords):
                return None

            # 3. 抓取發布日期
            date_tag = soup.select_one(".date, .time, .news-date")
            pub_date = date_tag.get_text(strip=True) if date_tag else datetime.now().strftime("%Y-%m-%d")

            return target_url, title, pub_date
    except Exception:
        return None

# ==========================================
# 6. 定時輪詢任務 (Background Loop Task)
# ==========================================
@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def auto_check_news():
    global last_checked_id
    
    try:
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            print(f"❌ 找不到目標頻道 ID: {TARGET_CHANNEL_ID}")
            return

        max_failures = 5
        consecutive_failures = 0
        curr_id = last_checked_id
        found_new_article = False

        async with aiohttp.ClientSession() as session:
            while consecutive_failures < max_failures:
                try:
                    result = await fetch_and_parse_news(session, curr_id)
                except Exception as req_err:
                    print(f"⚠️ 探測 ID {curr_id} 發生網路錯誤: {req_err}")
                    result = None

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
                    print(f"🎉 成功發送新聞推播！ID: {curr_id} - {title}")

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
    
    # 啟動 Web Server 保活服務
    bot.loop.create_task(start_web_server())

    # 啟動背景檢查任務
    if not auto_check_news.is_running():
        auto_check_news.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ 錯誤：未設定 DISCORD_TOKEN 環境變數！")
    else:
        bot.run(DISCORD_TOKEN)
