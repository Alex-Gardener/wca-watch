# WCA 新比赛邮件通知

这个小程序使用 WCA 官方 API 检查最新公告，并通过邮件通知。首次运行只建立当前基线，不会把已有比赛全部发一遍；以后每次只发送新增比赛。邮件发送失败时不会推进游标，因此下次会自动重试。

**当前已部署到 GitHub Actions 云端运行**，每 30 分钟自动检查一次，Mac 休眠时也能持续监控。

## 邮件配置

当前机器使用 macOS 登录钥匙串保存 iCloud SMTP 密码，源码和 `.env` 中都没有明文密码。正常运行不需要打开“邮件”App。

如果以后更换发件账户，可复制示例配置：

```bash
cd "/Volumes/Mac 1T/代码/wca"
cp .env.example .env
chmod 600 .env
```

编辑 `.env`：

- `SMTP_USER`：你的 iCloud 邮箱。
- `SMTP_PASSWORD`：可以留空；留空时从 macOS 钥匙串的 `WCA Watch SMTP` 项读取。
- `MAIL_TO`：接收通知的邮箱；多个地址用英文逗号分隔。
- `WCA_COUNTRY_CODES`：默认留空，通知全球新比赛。若只看中国大陆，填 `CN`。

Apple 应用专用密码可在 [account.apple.com](https://account.apple.com/) 的“登录与安全性 → App 专用密码”中创建。

## 2. 测试

```bash
./.venv/bin/python wca.py --test-email
./.venv/bin/python wca.py --dry-run
```

脚本只使用 Python 标准库；若 `.venv` 不存在，也可以使用 `python3`。

## 3. 云端自动检查（GitHub Actions）

项目已部署到 GitHub Actions，每 30 分钟自动检查一次新比赛，**Mac 关机或休眠时也能持续运行**。

### 手动触发检查

```bash
# 发送测试邮件
gh workflow run wca-watch.yml -f test_email=true

# 执行增量检查
gh workflow run wca-watch.yml
```

### 查看运行日志

```bash
gh run list --workflow=wca-watch.yml
gh run view <run-id> --log
```

### 本地 macOS 安装（可选）

如果需要本地运行：

```bash
chmod +x run.sh install_launchd.py
python3 install_launchd.py
```

卸载：

```bash
python3 install_launchd.py --uninstall
```

## 手动命令

```bash
python3 wca.py             # 正常增量检查
python3 wca.py --init      # 用当前最新公告重置基线
python3 wca.py --dry-run   # 不发信、不修改状态
python3 wca.py --test-email
```
