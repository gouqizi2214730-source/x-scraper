#!/usr/bin/env python3
"""
X（Twitter）博主帖子抓取器 —— 交互式

功能
  - 用浏览器导出的 cookie 登录（支持 Cookie-Editor 的 JSON、Netscape cookies.txt、或 "auth_token=...; ct0=..." 字符串）
  - 用 X 高级搜索（from: / since: / until: / 关键词 / min_faves / 排除回复 / 只要媒体）按时间分段抓取
  - 抓取全文（含长推文）、图片（原图）、X 文章（Article）正文与配图、引用帖
  - 视频不下载，给原帖链接 + mp4 直链
  - 输出：单个 Markdown（图片 base64 内嵌）或 Markdown + images 文件夹；另存 posts.json 原始数据
  - 断点续抓：同一输出目录再跑一次会合并已有数据

安装
  pip install playwright pillow
  playwright install chromium

运行
  python x_scraper.py                 # 交互式
  python x_scraper.py --config 输出目录/config.json   # 复用上次设置（cookie 仍需重新提供）

cookie 怎么拿
  在已登录 x.com 的浏览器里装 Cookie-Editor 扩展 → Export → JSON，保存成 cookies.json。
  至少要有 auth_token 和 ct0 两项。cookie 等同于账号登录凭证，只保存在自己电脑上，不要发给别人。
"""
import argparse
import asyncio
import base64
import datetime as dt
import io
import json
import random
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

GQL_RE = re.compile(
    r"/graphql/[^/]+/(SearchTimeline|UserTweets|UserTweetsAndReplies|UserOriginalsTimeline|"
    r"TweetDetail|TweetResultByRestId|UserMedia)\b"
)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ───────────────────────── 交互输入 ─────────────────────────
def ask(prompt, default=None):
    tip = f" [{default}]" if default not in (None, "") else ""
    v = input(f"{prompt}{tip}: ").strip()
    return v if v else (default if default is not None else "")


def ask_bool(prompt, default=True):
    d = "Y/n" if default else "y/N"
    v = input(f"{prompt} ({d}): ").strip().lower()
    return default if not v else v in ("y", "yes", "是", "1")


def ask_int(prompt, default):
    while True:
        v = ask(prompt, str(default))
        try:
            return int(v)
        except ValueError:
            print("  请输入整数")


def ask_float(prompt, default):
    while True:
        v = ask(prompt, str(default))
        try:
            return float(v)
        except ValueError:
            print("  请输入数字")


def ask_date(prompt, default):
    while True:
        v = ask(prompt, default)
        try:
            dt.date.fromisoformat(v)
            return v
        except ValueError:
            print("  格式应为 YYYY-MM-DD")


# ───────────────────────── Cookie ─────────────────────────
def parse_cookies(src):
    """src 可以是文件路径或 cookie 字符串，返回 Playwright cookie 列表。"""
    p = Path(src).expanduser()
    text = p.read_text(encoding="utf-8") if p.is_file() else src
    text = text.strip()
    pairs = []  # (name, value, extra)

    if text.startswith("[") or text.startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("cookies", [data])
        for c in data:
            dom = c.get("domain", ".x.com")
            if "x.com" not in dom and "twitter.com" not in dom:
                continue
            pairs.append((c["name"], c["value"], c))
    elif "\t" in text:  # Netscape cookies.txt
        for line in text.splitlines():
            if line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            parts = line.split("\t")
            if len(parts) == 7 and ("x.com" in parts[0] or "twitter.com" in parts[0]):
                pairs.append((parts[5], parts[6].strip(), {}))
    else:  # "a=b; c=d"
        for kv in text.split(";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                pairs.append((k.strip(), v.strip(), {}))

    out, seen = [], set()
    for name, value, extra in pairs:
        if not name or name in seen:
            continue
        seen.add(name)
        same = str(extra.get("sameSite", "Lax")).capitalize()
        if same not in ("Strict", "Lax", "None"):
            same = "None" if same.startswith("No") else "Lax"
        out.append({
            "name": name, "value": value, "domain": ".x.com", "path": "/",
            "secure": True, "httpOnly": bool(extra.get("httpOnly", False)), "sameSite": same,
        })
    names = {c["name"] for c in out}
    missing = {"auth_token", "ct0"} - names
    if missing:
        print(f"  ⚠ cookie 里缺少 {', '.join(sorted(missing))}，很可能无法登录")
    return out


# ───────────────────────── 数据解析 ─────────────────────────
def _get(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return d if d is not None else default


def parse_media(m):
    kind = m.get("type")
    base = m.get("media_url_https", "")
    if kind == "photo":
        return {"type": "photo", "url": base + ("&" if "?" in base else "?") + "name=orig"}
    variants = [v for v in _get(m, "video_info", "variants", default=[])
                if v.get("content_type") == "video/mp4"]
    variants.sort(key=lambda v: v.get("bitrate", 0), reverse=True)
    return {"type": kind, "poster": base, "mp4": variants[0]["url"] if variants else None}


def parse_tweet(t):
    L = t["legacy"]
    user = _get(t, "core", "user_results", "result", default={})
    sn = _get(user, "core", "screen_name") or _get(user, "legacy", "screen_name", default="")
    nt = _get(t, "note_tweet", "note_tweet_results", "result")
    text = (nt or {}).get("text") or L.get("full_text", "")
    ents = (nt or {}).get("entity_set") or L.get("entities") or {}
    for u in ents.get("urls", []):
        text = text.replace(u.get("url", "\0"), u.get("expanded_url") or u.get("url", ""))
    media = _get(L, "extended_entities", "media", default=[]) or []
    for m in media:
        if m.get("url"):
            text = text.replace(m["url"], "")
    q = _get(t, "quoted_status_result", "result") or {}
    q = q.get("tweet", q)
    art = _get(t, "article", "article_results", "result")
    rec = {
        "id": t["rest_id"],
        "screen_name": sn,
        "created_at": dt.datetime.strptime(L["created_at"], "%a %b %d %H:%M:%S %z %Y").isoformat(),
        "text": text.strip(),
        "is_long": bool(nt),
        "favorites": L.get("favorite_count", 0),
        "retweets": L.get("retweet_count", 0),
        "replies": L.get("reply_count", 0),
        "bookmarks": L.get("bookmark_count", 0),
        "views": int(_get(t, "views", "count", default=0) or 0),
        "reply_to_id": L.get("in_reply_to_status_id_str"),
        "reply_to_user": L.get("in_reply_to_screen_name"),
        "conversation_id": L.get("conversation_id_str"),
        "is_retweet": "retweeted_status_result" in L,
        "quoted_id": q.get("rest_id") or L.get("quoted_status_id_str"),
        "media": [parse_media(m) for m in media],
        "url": f"https://x.com/{sn}/status/{t['rest_id']}",
    }
    if art:
        rec["article"] = {
            "id": art.get("rest_id"),
            "title": art.get("title"),
            "preview": art.get("preview_text"),
            "cover": _get(art, "cover_media", "media_info", "original_img_url"),
            "content_state": art.get("content_state"),
            "media_entities": art.get("media_entities") or [],
        }
    return rec


class Store:
    def __init__(self, path):
        self.path = path
        self.posts = {}
        if path.exists():
            for r in json.loads(path.read_text(encoding="utf-8")):
                self.posts[r["id"]] = r
            print(f"  已载入之前抓取的 {len(self.posts)} 条，继续合并")

    def merge(self, rec):
        old = self.posts.get(rec["id"])
        if old:
            if len(old.get("text", "")) > len(rec["text"]):
                rec["text"] = old["text"]
            oa, na = old.get("article"), rec.get("article")
            if oa and (not na or (oa.get("content_state") and not na.get("content_state"))):
                rec["article"] = oa
        self.posts[rec["id"]] = rec

    def ingest(self, obj):
        stack = [obj]
        while stack:
            o = stack.pop()
            if isinstance(o, list):
                stack.extend(o)
            elif isinstance(o, dict):
                t = o.get("tweet") if o.get("__typename") == "TweetWithVisibilityResults" else o
                if isinstance(t, dict) and t.get("__typename") == "Tweet" and t.get("rest_id") and t.get("legacy"):
                    try:
                        self.merge(parse_tweet(t))
                    except Exception as e:  # 单条解析失败不影响整体
                        print(f"\n  解析 {t.get('rest_id')} 失败：{e}")
                stack.extend(o.values())

    def count(self, user):
        u = user.lower()
        return sum(1 for r in self.posts.values() if r["screen_name"].lower() == u)

    def save(self):
        self.path.write_text(json.dumps(list(self.posts.values()), ensure_ascii=False, indent=1), encoding="utf-8")


# ───────────────────────── 浏览器抓取 ─────────────────────────
def build_query(cfg, since, until):
    q = [f"from:{cfg['user']}", f"since:{since}", f"until:{until}"]
    if cfg["keywords"]:
        q.insert(0, cfg["keywords"])
    if cfg["exclude_replies"]:
        q.append("-filter:replies")
    if cfg["media_only"]:
        q.append("filter:media")
    if cfg["min_faves"] > 0:
        q.append(f"min_faves:{cfg['min_faves']}")
    return " ".join(q)


def date_slices(since, until, days):
    a, end = dt.date.fromisoformat(since), dt.date.fromisoformat(until)
    while a < end:
        b = min(a + dt.timedelta(days=days), end)
        yield a.isoformat(), b.isoformat()
        a = b


WAIT_DEFAULTS = {"scroll_min": 1.8, "scroll_max": 3.2, "slice_wait": 5, "retry_wait": 60, "max_stall": 5}
WAIT_PRESETS = {
    "1": {"scroll_min": 1.2, "scroll_max": 2.0, "slice_wait": 2, "retry_wait": 45, "max_stall": 4},
    "2": WAIT_DEFAULTS,
    "3": {"scroll_min": 3.5, "scroll_max": 6.0, "slice_wait": 20, "retry_wait": 120, "max_stall": 6},
}


def cfg_waits(cfg):
    w = dict(WAIT_DEFAULTS)
    w.update(cfg.get("waits") or {})
    if w["scroll_max"] < w["scroll_min"]:
        w["scroll_max"] = w["scroll_min"]
    return w


async def launch_browser(pw, headless):
    """优先用电脑上已装的 Edge / Chrome，都没有再用 Playwright 自带的 Chromium（必要时自动下载）。"""
    for ch in ("msedge", "chrome", None):
        try:
            b = await pw.chromium.launch(headless=headless, channel=ch) if ch else await pw.chromium.launch(headless=headless)
            print(f"  使用浏览器：{ch or 'Playwright Chromium'}")
            return b
        except Exception:
            continue
    print("  没找到 Edge/Chrome，正在下载 Chromium（约 150MB，只需一次）…")
    try:
        import subprocess
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        exe = compute_driver_executable()
        cmd = list(exe) if isinstance(exe, (tuple, list)) else [str(exe)]
        subprocess.run(cmd + ["install", "chromium"], env=get_driver_env(), check=True)
        return await pw.chromium.launch(headless=headless)
    except Exception as e:
        print(f"✗ 无法启动浏览器：{e}\n  请安装 Microsoft Edge 或 Google Chrome 后重试。")
        return None


async def handle_retry(page, wait=60):
    btn = page.locator('[role="button"]:has-text("Retry"), [role="button"]:has-text("重试"), '
                       'button:has-text("Retry"), button:has-text("重试")')
    if await btn.count():
        print(f"\n  页面加载失败（可能被限流），等待 {wait} 秒后重试…")
        await asyncio.sleep(wait)
        try:
            await btn.first.click()
        except Exception:
            pass
        await asyncio.sleep(5)
        return True
    return False


async def scroll_collect(page, store, user, label, cfg, max_scrolls=400):
    w = cfg_waits(cfg)
    max_stall = w["max_stall"]
    last, stall = store.count(user), 0
    for _ in range(max_scrolls):
        await page.evaluate("window.scrollBy(0, -700)")
        await asyncio.sleep(0.3)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(random.uniform(w["scroll_min"], w["scroll_max"]))
        n = store.count(user)
        print(f"\r  {label}：累计 {n} 条", end="", flush=True)
        if n > last:
            last, stall = n, 0
        else:
            stall += 1
            if await handle_retry(page, w["retry_wait"]):
                stall = 0
            elif stall >= max_stall or (stall >= 2 and await page.locator(
                    '[data-testid="empty_state_header_text"]').count()):
                break
    print()


async def scrape(cfg, cookies, store):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        w = cfg_waits(cfg)
        browser = await launch_browser(pw, headless=not cfg["show_browser"])
        if not browser:
            return False
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 2000},
                                        locale="en-US")
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        pending = set()

        async def handle(resp):
            try:
                store.ingest(await resp.json())
            except Exception:
                pass

        def on_response(resp):
            if resp.status == 200 and GQL_RE.search(resp.url):
                task = asyncio.ensure_future(handle(resp))
                pending.add(task)
                task.add_done_callback(pending.discard)

        page.on("response", on_response)

        print("\n验证登录…")
        try:
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_selector(
                '[data-testid="SideNav_AccountSwitcher_Button"], [data-testid="AppTabBar_Profile_Link"]',
                timeout=20000)
        except Exception as e:
            reason = "跳转到了登录页" if ("/login" in page.url or "/i/flow" in page.url) else f"{type(e).__name__}"
            print(f"✗ 登录失败（{reason}）：cookie 无效/过期，或网络打不开 x.com。")
            if cfg["show_browser"]:
                input("  浏览器窗口已打开，可在里面手动登录或处理验证，完成后按回车继续（Ctrl+C 退出）…")
            else:
                await browser.close()
                return False
        print("✓ 已登录")

        slices = list(date_slices(cfg["since"], cfg["until"], cfg["slice_days"]))
        tab = "live" if cfg["sort"] == "latest" else "top"
        for k, (a, b) in enumerate(slices, 1):
            q = build_query(cfg, a, b)
            url = f"https://x.com/search?q={quote(q)}&src=typed_query&f={tab}"
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(3, 5))
            await scroll_collect(page, store, cfg["user"], f"[{k}/{len(slices)}] {a} ~ {b}", cfg)
            if k < len(slices) and w["slice_wait"]:
                await asyncio.sleep(random.uniform(w["slice_wait"], w["slice_wait"] * 1.5))
            store.save()

        if cfg["fetch_articles"]:
            todo = [r for r in store.posts.values()
                    if r["screen_name"].lower() == cfg["user"].lower()
                    and r.get("article") and not r["article"].get("content_state")]
            if todo:
                print(f"\n抓取 {len(todo)} 篇 X 文章正文…")
            for k, r in enumerate(todo, 1):
                await page.goto(r["url"], wait_until="domcontentloaded")
                for _ in range(8):
                    await asyncio.sleep(1.5)
                    if store.posts[r["id"]].get("article", {}).get("content_state"):
                        break
                ok = "✓" if store.posts[r["id"]]["article"].get("content_state") else "✗ 未取到正文，保留摘要"
                print(f"  [{k}/{len(todo)}] {r['article'].get('title') or r['id']} {ok}")
                await asyncio.sleep(random.uniform(w["scroll_min"], w["scroll_max"]) + w["slice_wait"] / 2)
            store.save()

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        store.save()
        await browser.close()
    return True


# ───────────────────────── 图片 ─────────────────────────
def fetch(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception:
            if i == retries - 1:
                return None


def to_jpeg(data, max_w=1600, quality=85):
    try:
        from PIL import Image
    except ImportError:
        return data
    try:
        im = Image.open(io.BytesIO(data))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if im.width > max_w:
            im = im.resize((max_w, round(im.height * max_w / im.width)))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return data


def article_media_urls(art):
    urls = {}
    for m in art.get("media_entities") or []:
        u = _get(m, "media_info", "original_img_url")
        if u:
            urls[str(m.get("media_id"))] = u
    return urls


def collect_images(posts):
    """返回 [(url, 文件名)]"""
    out = []
    for r in posts:
        n = 0
        for m in r["media"]:
            if m["type"] == "photo":
                n += 1
                out.append((m["url"], f"{r['id']}_{n}.jpg"))
        art = r.get("article")
        if art:
            if art.get("cover"):
                out.append((art["cover"], f"{r['id']}_cover.jpg"))
            for j, u in enumerate(article_media_urls(art).values(), 1):
                out.append((u, f"{r['id']}_a{j}.jpg"))
    return out


def download_images(items, img_dir, embed):
    """返回 {url: markdown 中使用的图片地址}"""
    img_dir.mkdir(parents=True, exist_ok=True)
    ref = {}

    def job(item):
        url, name = item
        path = img_dir / name
        data = path.read_bytes() if path.exists() else fetch(url)
        if not data:
            return url, None
        if not path.exists():
            path.write_bytes(data)
        if embed:
            return url, "data:image/jpeg;base64," + base64.b64encode(to_jpeg(data)).decode()
        return url, f"{img_dir.name}/{name}"

    with ThreadPoolExecutor(6) as ex:
        for k, (url, val) in enumerate(ex.map(job, items), 1):
            print(f"\r  图片 {k}/{len(items)}", end="", flush=True)
            ref[url] = val
    print()
    failed = [u for u, v in ref.items() if not v]
    if failed:
        print(f"  ⚠ {len(failed)} 张图片下载失败，md 中改用在线地址")
    return {u: (v or u) for u, v in ref.items()}


# ───────────────────────── Markdown ─────────────────────────
def article_to_md(art, img):
    cs = art.get("content_state") or {}
    blocks = cs.get("blocks") or []
    if not blocks:
        return art.get("preview") or ""
    em = cs.get("entityMap") or {}
    if isinstance(em, list):
        em = {str(e.get("key")): e.get("value", {}) for e in em}
    murls = article_media_urls(art)
    prefix = {"header-one": "##### ", "header-two": "###### ", "header-three": "###### ",
              "unordered-list-item": "- ", "ordered-list-item": "1. ", "blockquote": "> "}
    lines = []
    for b in blocks:
        kind, tx = b.get("type"), b.get("text", "")
        ranges = b.get("entityRanges") or []
        if kind == "atomic":
            for r in ranges:
                ent = em.get(str(r.get("key")), {})
                et, data = ent.get("type"), ent.get("data") or {}
                if et in ("MEDIA", "IMAGE"):
                    for it in data.get("mediaItems") or []:
                        u = murls.get(str(it.get("mediaId")))
                        if u:
                            lines.append(f"![]({img(u)})")
                elif et == "DIVIDER":
                    lines.append("---")
                elif et == "TWEET":
                    lines.append(f"> 嵌入帖子：https://x.com/i/status/{data.get('tweetId')}")
            continue
        for r in sorted(ranges, key=lambda r: -r.get("offset", 0)):
            ent = em.get(str(r.get("key")), {})
            if ent.get("type") == "LINK" and (ent.get("data") or {}).get("url"):
                o, n = r.get("offset", 0), r.get("length", 0)
                tx = f"{tx[:o]}[{tx[o:o + n]}]({ent['data']['url']}){tx[o + n:]}"
        lines.append(prefix.get(kind, "") + tx)
    return "\n\n".join(lines)


def hard_breaks(text):
    return "\n".join(line + "  " if line.strip() else "" for line in text.split("\n"))


def render(cfg, posts, all_posts, img_ref):
    tz = ZoneInfo(cfg["timezone"]) if ZoneInfo else dt.timezone(dt.timedelta(hours=8))
    img = lambda u: img_ref.get(u, u)
    out = [f"# @{cfg['user']} 帖子合集", ""]
    out.append(f"> 抓取于 {dt.datetime.now(tz):%Y-%m-%d %H:%M}（{cfg['timezone']}） · 共 {len(posts)} 条  ")
    out.append(f"> 搜索条件：`{build_query(cfg, cfg['since'], cfg['until'])}`" +
               (f" · 收藏 ≥ {cfg['min_bookmarks']}" if cfg["min_bookmarks"] else ""))
    n_art = sum(1 for r in posts if r.get("article"))
    n_vid = sum(1 for r in posts for m in r["media"] if m["type"] != "photo")
    out += [f"> 其中文章 {n_art} 篇，视频/GIF {n_vid} 个（只给链接）", ""]

    if n_art:
        out += ["## 文章索引", "", "| 日期 | 标题 | 收藏 |", "|---|---|---:|"]
        for r in posts:
            if r.get("article"):
                d = dt.datetime.fromisoformat(r["created_at"]).astimezone(tz)
                out.append(f"| {d:%Y-%m-%d} | [{r['article'].get('title') or '(无标题)'}](#p{r['id']}) | {r['bookmarks']} |")
        out.append("")

    out += ["## 全部帖子", ""]
    for r in posts:
        d = dt.datetime.fromisoformat(r["created_at"]).astimezone(tz)
        tag = " · 自回复串" if r.get("reply_to_user", "") and r["reply_to_user"].lower() == cfg["user"].lower() else ""
        out.append(f'<a id="p{r["id"]}"></a>')
        out.append("")
        out.append(f"### {d:%Y-%m-%d %H:%M}{tag}")
        out.append("")
        out.append(f"❤ {r['favorites']} · 🔁 {r['retweets']} · 🔖 {r['bookmarks']} · 👁 {r['views']} · [原帖]({r['url']})")
        out.append("")
        art = r.get("article")
        if art:
            out.append(f"#### 📄 {art.get('title') or '(文章)'}")
            out.append("")
            if art.get("cover"):
                out += [f"![]({img(art['cover'])})", ""]
            out += [article_to_md(art, img), ""]
            if not art.get("content_state"):
                out += [f"*（只取到摘要，全文见[原帖]({r['url']})）*", ""]
        if r["text"]:
            out += [hard_breaks(r["text"]), ""]
        k = 0
        for m in r["media"]:
            if m["type"] == "photo":
                k += 1
                out.append(f"![图{k}]({img(m['url'])})")
            else:
                label = "GIF" if m["type"] == "animated_gif" else "视频"
                line = f"[▶ {label}（点击在 X 上观看）]({r['url']})"
                if m.get("mp4"):
                    line += f" · [mp4 直链]({m['mp4']})"
                out.append(line)
            out.append("")
        q = all_posts.get(r.get("quoted_id") or "")
        if q:
            qt = q["text"][:400] + ("…" if len(q["text"]) > 400 else "")
            out.append(f"> **引用 @{q['screen_name']}**：" + qt.replace("\n", " "))
            out.append(f"> [查看被引用的帖子]({q['url']})")
            out.append("")
        elif r.get("quoted_id"):
            out += [f"> 引用了一条帖子：https://x.com/i/status/{r['quoted_id']}", ""]
        out += ["---", ""]
    return "\n".join(out)


# ───────────────────────── 主流程 ─────────────────────────
def interactive_config(prev=None):
    p = prev or {}
    today = dt.date.today()
    print("\n=== X 博主帖子抓取器 ===\n（直接回车使用方括号里的默认值）\n")
    cfg = {}
    cfg["user"] = ask("目标账号（不带 @）", p.get("user")).lstrip("@")
    while not cfg["user"]:
        cfg["user"] = ask("目标账号不能为空").lstrip("@")
    print("\n— 高级搜索条件 —")
    cfg["since"] = ask_date("开始日期", p.get("since", (today - dt.timedelta(days=365)).isoformat()))
    cfg["until"] = ask_date("结束日期（不含当天）", p.get("until", (today + dt.timedelta(days=1)).isoformat()))
    cfg["keywords"] = ask("关键词（可留空；支持高级搜索语法，如 \"order flow\" OR CVD）", p.get("keywords", ""))
    cfg["exclude_replies"] = ask_bool("排除回复他人的帖子", p.get("exclude_replies", True))
    cfg["media_only"] = ask_bool("只要带图片/视频的帖子", p.get("media_only", False))
    cfg["min_faves"] = ask_int("最少点赞数（0=不限）", p.get("min_faves", 0))
    cfg["sort"] = "top" if ask("排序：1=最新（更全） 2=热门", "2" if p.get("sort") == "top" else "1") == "2" else "latest"
    cfg["slice_days"] = ask_int("按多少天分段搜索（发帖多就调小，一段结果太多会被截断）", p.get("slice_days", 30))
    print("\n— 抓取节奏（等待越长越不容易被限流，但更慢）—")
    pw_prev = p.get("waits")
    choice = ask("1=快  2=标准  3=慢速稳妥  4=自定义", "4" if pw_prev and pw_prev not in WAIT_PRESETS.values() else "2")
    if choice == "4":
        b = dict(WAIT_DEFAULTS, **(pw_prev or {}))
        cfg["waits"] = {
            "scroll_min": ask_float("每次滚动后最少等待（秒）", b["scroll_min"]),
            "scroll_max": ask_float("每次滚动后最多等待（秒，在最少~最多之间随机）", b["scroll_max"]),
            "slice_wait": ask_float("每个时间段之间额外等待（秒）", b["slice_wait"]),
            "retry_wait": ask_float("遇到限流/加载失败时等待（秒）", b["retry_wait"]),
            "max_stall": ask_int("连续几次滚动没有新帖就结束该时间段", b["max_stall"]),
        }
    else:
        cfg["waits"] = dict(WAIT_PRESETS.get(choice, WAIT_DEFAULTS))
    print("\n— 输出 —")
    cfg["fetch_articles"] = ask_bool("抓取 X 文章（Article）的完整正文", p.get("fetch_articles", True))
    cfg["min_bookmarks"] = ask_int("输出时只保留收藏数 ≥（0=全部；posts.json 始终保存全部）", p.get("min_bookmarks", 0))
    cfg["embed"] = ask("图片：1=内嵌进单个 md（离线可看） 2=md + images 文件夹", "2" if p.get("embed") is False else "1") != "2"
    cfg["timezone"] = ask("显示时区", p.get("timezone", "Asia/Shanghai"))
    cfg["show_browser"] = ask_bool("显示浏览器窗口（遇到验证时可手动处理）", p.get("show_browser", False))
    cfg["out_dir"] = ask("输出目录", p.get("out_dir", f"x_{cfg['user']}"))
    return cfg


def main():
    ap = argparse.ArgumentParser(description="X 博主帖子抓取器")
    ap.add_argument("--config", help="复用之前保存的 config.json")
    ap.add_argument("--cookie", help="cookie 文件路径或字符串")
    args = ap.parse_args()

    prev = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else None
    if prev and ask_bool(f"复用 {args.config} 的设置直接开始", True):
        cfg = prev
    else:
        cfg = interactive_config(prev)

    cookie_src = args.cookie or ask("\ncookie 文件路径（或直接粘贴 auth_token=...; ct0=...）", "cookies.json")
    try:
        cookies = parse_cookies(cookie_src)
    except Exception as e:
        sys.exit(f"读取 cookie 失败：{e}")
    if not cookies:
        sys.exit("没有读到任何 x.com 的 cookie")
    if not {"auth_token", "ct0"} <= {c["name"] for c in cookies} and not ask_bool("仍然继续", False):
        sys.exit("已退出")

    out = Path(cfg["out_dir"]).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    store = Store(out / "posts.json")

    try:
        ok = asyncio.run(scrape(cfg, cookies, store))
    except KeyboardInterrupt:
        print("\n已中断，已抓到的数据会保留，下次在同一输出目录运行即可继续。")
        store.save()
        ok = True
    if not ok:
        return

    u = cfg["user"].lower()
    mine = [r for r in store.posts.values()
            if r["screen_name"].lower() == u and not r["is_retweet"]
            and not (cfg["exclude_replies"] and r.get("reply_to_user") and r["reply_to_user"].lower() != u)
            and r["bookmarks"] >= cfg["min_bookmarks"]]
    mine.sort(key=lambda r: int(r["id"]))
    if not mine:
        print("\n没有抓到符合条件的帖子。可以检查账号名、日期范围，或打开浏览器窗口看看页面。")
        return

    print(f"\n符合条件的帖子 {len(mine)} 条，下载图片…")
    img_ref = download_images(collect_images(mine), out / "images", cfg["embed"])
    md = render(cfg, mine, store.posts, img_ref)
    md_path = out / f"{cfg['user']}_posts.md"
    md_path.write_text(md, encoding="utf-8")
    size = md_path.stat().st_size / 1024 / 1024
    print(f"\n✓ 完成：{md_path}（{size:.1f} MB）")
    print(f"  原始数据：{out / 'posts.json'}")
    if not cfg["embed"]:
        print(f"  图片：{out / 'images'}（md 用相对路径引用，移动时请连同文件夹一起）")
    print(f"  下次复用设置：python {Path(sys.argv[0]).name} --config {out / 'config.json'}")


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr, sys.stdin):
        try:
            if not _s.isatty():
                _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            print(e.code)
    except Exception:
        import traceback
        traceback.print_exc()
    if getattr(sys, "frozen", False):
        input("\n按回车键退出…")
