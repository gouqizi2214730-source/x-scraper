# X 博主帖子抓取器（XScraper）

用自己的 X（Twitter）登录 cookie，按高级搜索条件把某个博主的帖子抓下来，生成一份可离线阅读的 Markdown。

- **登录**：导入浏览器导出的 cookie（Cookie-Editor 的 JSON、Netscape `cookies.txt`，或直接粘贴 `auth_token=...; ct0=...`）
- **高级搜索**：起止日期、关键词（支持 X 搜索语法）、排除回复、只要带媒体的帖子、最少点赞、最新/热门排序，按天数分段抓取
- **内容**：全文（含长推文）、原图、X 文章（Article）正文和配图、引用帖、点赞/转发/收藏/浏览数
- **视频**：不下载，给原帖链接和 mp4 直链
- **输出**：单个 Markdown（图片 base64 内嵌）或 Markdown + `images/` 文件夹，另存 `posts.json` 原始数据
- **等待时间可调**：快 / 标准 / 慢速稳妥 三档预设，或自定义滚动间隔、分段间隔、限流等待、停止条件
- **断点续抓**：同一输出目录再运行会合并已有数据；`--config` 复用上次设置

## Windows：直接用 exe

1. 从 [Releases](../../releases) 下载 `XScraper.exe`
2. 在已登录 x.com 的浏览器里用 Cookie-Editor 扩展导出 cookie（Export → JSON），存成 `cookies.json`，和 exe 放在同一个文件夹
3. 双击 `XScraper.exe`，按提示操作（回车使用默认值）

exe 会直接使用电脑上的 Microsoft Edge 或 Google Chrome；都没有时会自动下载一份 Chromium（约 150MB，只需一次）。

> Windows 可能提示“未知发布者”，点“更多信息 → 仍要运行”。

## 用 Python 运行

```bash
pip install -r requirements.txt
playwright install chromium   # 已装 Edge/Chrome 可跳过
python x_scraper.py
python x_scraper.py --config x_某账号/config.json   # 复用上次设置
python x_scraper.py --cookie cookies.json
```

## 抓取节奏

| 预设 | 滚动间隔 | 分段间隔 | 限流等待 | 连续无新帖结束 |
|---|---|---|---|---|
| 1 快 | 1.2–2.0 秒 | 2 秒 | 45 秒 | 4 次 |
| 2 标准 | 1.8–3.2 秒 | 5 秒 | 60 秒 | 5 次 |
| 3 慢速稳妥 | 3.5–6.0 秒 | 20 秒 | 120 秒 | 6 次 |
| 4 自定义 | 自填 | 自填 | 自填 | 自填 |

发帖很多的账号，把“按多少天分段”调小（例如 7 天），因为 X 单次搜索返回的数量有上限。

## 注意

- **cookie 等同于账号登录凭证**，只保存在自己电脑上，不要上传或发给别人（`.gitignore` 已排除 `cookies*.json`）。
- 抓取过快可能让账号被临时限流，大批量抓取建议用慢速档或分几次进行。
- 请遵守 X 的使用条款，仅用于个人学习与存档；帖子内容版权归原作者所有。

## 自行编译 exe

推送 `v*` 标签（如 `git tag v1.0.0 && git push --tags`），GitHub Actions 会在 Windows 上用 PyInstaller 编译并发布到 Releases。也可以在 Actions 页面手动运行 “Build Windows exe”。
