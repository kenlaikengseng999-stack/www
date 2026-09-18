import discord
from discord.ext import commands, tasks
from aiohttp import web
import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse
import re
import os

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ⚙️ 設定區
TARGET_CHANNEL_ID = 1550398644933361685  # ⚠️ 請替換為接收新聞的 Discord 頻道 ID
START_ID = 3810                         # 初始探測的新聞 ID
CHECK_INTERVAL_MINUTES = 1             # 自動探測間隔（分鐘）
DATA_FILE = "last_id.txt"               # 紀錄最新 ID 的檔案名稱

# --- Web Server（給 Render 免費檢測用） ---

async def handle_ping(request):
    """給 Render 健康檢查用的 Ping 接口，回應 200 OK"""
    return web.Response(text="Bot is alive!", status=200)

async def start_web_server():
    """啟動非同步 Web 伺服器，自動綁定 Render 的 PORT 變數"""
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/health', handle_ping)
    
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Web Server 已在通訊埠 {port} 啟動（專供 Render 保活）")

# --- ID 讀寫紀錄機制 ---

def load_last_id():
    """讀取上一次抓取到的最新 ID"""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return int(f.read().strip())
        except Exception:
            pass
    return START_ID

def save_last_id(news_id):
    """儲存最新 ID 至檔案"""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            f.write(str(news_id))
    except Exception as e:
        print(f"紀錄 ID 失敗: {e}")

last_checked_id = load_last_id()

# --- 網址處理與解析邏輯 ---

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
    """【NEWS 內頁專用】依據 DOM 結構定位標題與日期 (YYYY-MM-DD)"""
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
    """非同步抓取特定 ID 的新聞"""
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

# --- 每 1 分鐘自動執行的 Task ---

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
    found_new_article = False  # 紀錄這次探測是否有發現新文章

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

    # 如果探測完畢且完全沒抓到新文章，印出提示
    if not found_new_article:
        print(f"🔍 檢查完成（ID {last_checked_id}），暫時沒有找到新的網站文章")

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
