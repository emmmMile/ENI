# X Monitor to Telegram

免费 GitHub Actions 版 X 推文监控，发现新推文后自动发送到 Telegram 频道。

## 功能
- 监控多个 X 账号
- 检测新推文并推送到 Telegram
- 首次运行只记录，不刷屏
- 多镜像源回退
- 自动保存上次已读推文 ID
- GitHub Actions 云端定时运行

## 目录
- `monitor_x.py`: 主脚本
- `state/last_seen.json`: 已读状态
- `.github/workflows/monitor-x.yml`: 定时任务
- `requirements.txt`: 依赖

## GitHub Secrets
在仓库 `Settings -> Secrets and variables -> Actions` 添加：

### `X_ACCOUNTS`
例如：
`ENI__Official,DAOaaS_Official,ELEVATE_ENI`

### `TELEGRAM_BOT_TOKEN`
你的 Telegram bot token

### `TELEGRAM_CHAT_ID`
你的 Telegram 频道 chat id，例如：
`-1001234567890`

## 部署步骤
1. 新建 GitHub 仓库
2. 上传本项目文件
3. 配置 Secrets
4. 打开 `Actions`
5. 手动运行一次 `Monitor X Accounts`

## 说明
- 第一次运行不会推送消息，只会记录当前最新推文
- 第二次起如果有新推文才会推送
- 默认每 10 分钟检查一次
- 如需改频率，修改 `.github/workflows/monitor-x.yml` 的 cron

## 常见问题

### 1. 为什么没有消息？
- 第一次运行只初始化
- 频道 chat id 错误
- bot 没有频道管理员权限
- 镜像源暂时不可用

### 2. 如何测试 Telegram 是否正常？
浏览器访问：

`https://api.telegram.org/bot<你的BOT_TOKEN>/sendMessage?chat_id=<你的CHAT_ID>&text=test`

### 3. 如何测试脚本抓取是否正常？
看 `Actions` 日志，是否出现：
- `[INIT]`
- `[OK]`
- `[SEND]`

## 提醒
X 页面和镜像站可能变化，若后续某个镜像失效，可替换 `monitor_x.py` 里的 `MIRRORS`
