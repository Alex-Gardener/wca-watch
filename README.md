# WCA 新比赛邮件通知

自动监控 WCA（世界魔方协会）新公告的比赛，通过邮件及时通知。支持云端 GitHub Actions 运行，Mac 休眠时也能持续监控。

## ✨ 功能特点

- 🔔 **自动监控**：每 30 分钟检查 WCA 官方 API
- ☁️ **云端运行**：GitHub Actions 部署，无需本地常驻
- 📧 **精美邮件**：现代化卡片设计，包含详细比赛信息
- 🌏 **地区筛选**：支持按国家/地区过滤（如只看中国比赛）
- 🎯 **智能推送**：首次运行只建立基线，之后仅推送新比赛
- 🔄 **自动重试**：发送失败时不推进游标，下次自动重试

## 📧 邮件内容

- 📍 比赛地点（城市、场馆、详细地址）
- 🗓️ 比赛日期
- 📅 报名时间（开始/截止）
- 🌐 明确的报名时区（默认转换为北京时间）
- 👥 参赛人数限制
- 🎲 比赛项目（中文名称：三阶、二阶、单手等）
- 🎯 主办方信息
- ✅ WCA 代表信息

## 🚀 快速开始

### 1. Fork 本仓库

点击右上角 Fork 按钮，将项目复制到你的账号下。

### 2. 配置 GitHub Secrets

在你的仓库中设置以下 Secrets（Settings → Secrets and variables → Actions → New repository secret）：

| Secret 名称 | 说明 | 示例 |
|------------|------|------|
| `SMTP_USER` | SMTP 发件邮箱 | `your-email@icloud.com` |
| `SMTP_PASSWORD` | SMTP 密码或应用专用密码 | `abcd-efgh-ijkl-mnop` |
| `MAIL_TO` | 收件邮箱（多个用逗号分隔） | `recipient@example.com` |

**获取 iCloud 应用专用密码**：
1. 访问 [appleid.apple.com](https://appleid.apple.com/)
2. 登录后进入”登录与安全性”
3. 点击”App 专用密码”生成

### 3. 可选配置

编辑 `.github/workflows/wca-watch.yml`，修改环境变量：

```yaml
env:
  WCA_COUNTRY_CODES: CN,HK,MO  # 仅推送中国大陆、香港和澳门
  SMTP_HOST: smtp.mail.me.com  # iCloud SMTP
  SMTP_PORT: “587”
  SMTP_USE_SSL: “false”
```

**国家代码参考**（ISO 3166-1）：
- `CN` - 中国大陆
- `HK` - 中国香港
- `TW` - 中国台湾
- `MO` - 中国澳门
- `US` - 美国
- `JP` - 日本
- 留空 - 全球所有比赛

### 4. 启用 Actions

1. 进入仓库的 Actions 标签
2. 点击 “I understand my workflows, go ahead and enable them”
3. Workflow 将自动每 30 分钟运行一次

### 5. 手动测试

```bash
# 方法 1：在 GitHub 网页端
# Actions → WCA 新比赛邮件通知 → Run workflow → 勾选 test_email → Run workflow

# 方法 2：使用 gh CLI
gh workflow run wca-watch.yml -f test_email=true
```

## 📖 使用说明

```bash
./.venv/bin/python wca.py --test-email
./.venv/bin/python wca.py --dry-run
```

脚本只使用 Python 标准库；若 `.venv` 不存在，也可以使用 `python3`。

## 📖 使用说明

### 云端运行（推荐）

GitHub Actions 会自动每 30 分钟检查一次新比赛，无需本地运行。

**手动触发检查**：
```bash
# 发送测试邮件
gh workflow run wca-watch.yml -f test_email=true

# 执行增量检查
gh workflow run wca-watch.yml
```

**查看运行日志**：
```bash
gh run list --workflow=wca-watch.yml
gh run view <run-id> --log
```

### 本地运行（可选）

如果需要在本地 Mac 运行：

**本地测试**：
```bash
git clone https://github.com/your-username/wca-watch.git
cd wca-watch
cp .env.example .env
# 编辑 .env 填写邮件配置
chmod 600 .env

python3 wca.py --test-email  # 测试邮件
python3 wca.py --dry-run     # 模拟检查
python3 wca.py               # 正常检查
```

**安装 launchd 定时任务**（仅 macOS）：
```bash
python3 install_launchd.py       # 安装
python3 install_launchd.py --uninstall  # 卸载
```

安装后任务每 30 分钟运行一次，运行副本位于 `~/Library/Application Support/WCA Watch`。

## 🔧 高级配置

### 使用其他 SMTP 服务

编辑 workflow 中的环境变量或本地 `.env`：

**Gmail**：
```yaml
SMTP_HOST: smtp.gmail.com
SMTP_PORT: "587"
SMTP_USE_SSL: "false"
```

**QQ 邮箱**：
```yaml
SMTP_HOST: smtp.qq.com
SMTP_PORT: "587"
SMTP_USE_SSL: "false"
```

**163 邮箱**：
```yaml
SMTP_HOST: smtp.163.com
SMTP_PORT: "465"
SMTP_USE_SSL: "true"
```

### 调整邮件时间显示

报名开放和截止时间默认转换为北京时间，并在邮件中明确标注。可通过环境变量修改：

```yaml
MAIL_TIMEZONE: Asia/Shanghai
MAIL_TIMEZONE_LABEL: 北京时间
```

### 调整检查频率

编辑 `.github/workflows/wca-watch.yml`：

```yaml
on:
  schedule:
    - cron: "*/30 * * * *"  # 每 30 分钟
    # - cron: "0 * * * *"   # 每小时
    # - cron: "0 */6 * * *" # 每 6 小时
```

## 🛠️ 开发

```bash
# 运行测试
python3 -m unittest test_wca -v

# 语法检查
python3 -m py_compile wca.py
```

## 📝 工作原理

1. **增量检查**：使用 WCA API 的 `sort=-announced_at` 参数按公告时间倒序获取比赛
2. **游标机制**：记录最新公告时间戳和 ID，只推送该时间点之后的新比赛
3. **状态持久化**：GitHub Actions 将游标保存在 `.github/wca_state.json`，每次有新比赛时自动提交
4. **首次运行**：只建立基线，不发送历史比赛
5. **失败重试**：发送失败时不推进游标，下次检查时自动重试

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

[MIT License](LICENSE)

## 🔗 相关链接

- [WCA 官网](https://www.worldcubeassociation.org)
- [WCA API 文档](https://github.com/thewca/worldcubeassociation.org/wiki/API-documentation)

---

**注意**：本项目非 WCA 官方项目，数据来源于 WCA 公开 API。
