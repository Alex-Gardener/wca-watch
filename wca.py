#!/usr/bin/env python3
"""WCA 新比赛邮件通知器。

首次运行只记录当前最新公告作为基线；后续运行仅通知新公告的比赛。
所有私密配置均从脚本同目录的 .env 或系统环境变量读取。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import smtplib
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formataddr
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
STATE_FILE = Path(os.getenv("WCA_STATE_FILE", str(APP_DIR / ".wca_seen.json")))
LOCK_FILE = APP_DIR / ".wca_watch.lock"
WCA_API = "https://www.worldcubeassociation.org/api/v0/competitions"
WCA_BASE = "https://www.worldcubeassociation.org"
USER_AGENT = "WCA-New-Competition-Notifier/2.0"
PAGE_SIZE = 5
MAX_PAGES = 400
STATE_VERSION = 2


def load_dotenv(path: Path = ENV_FILE) -> None:
    """加载简单的 KEY=VALUE 配置，已有系统环境变量优先。"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] in {'"', "'"} and value[-1:] == value[0]:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str) -> list[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] 状态文件损坏，将重新建立基线：{exc}", file=sys.stderr)
        return {}


def save_state(state: dict[str, Any]) -> None:
    """同目录原子写入，避免进程中断造成半截 JSON。"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".wca_seen.", dir=STATE_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, STATE_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def request_json(params: dict[str, str | int]) -> list[dict[str, Any]]:
    url = f"{WCA_API}?{urlencode(params)}"
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.load(response)
            if not isinstance(payload, list):
                raise RuntimeError("WCA API 返回了意外的数据格式")
            return payload
        except (HTTPError, URLError, TimeoutError, HTTPException, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError(f"访问 WCA API 失败：{last_error}") from last_error


def fetch_page(page: int) -> list[dict[str, Any]]:
    # WCA 默认按比赛日期排序；必须显式按公告时间倒序，增量游标才可靠。
    return request_json(
        {"announced": "true", "sort": "-announced_at", "per_page": PAGE_SIZE, "page": page}
    )


def fetch_since(cursor_text: str, seen_ids: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """取回游标之后的所有公告；第二项包含为更新状态而读到的项目。"""
    cursor = parse_time(cursor_text)
    new_items: list[dict[str, Any]] = []
    fetched: list[dict[str, Any]] = []

    for page_number in range(1, MAX_PAGES + 1):
        page = fetch_page(page_number)
        fetched.extend(page)
        reached_older_item = False

        for item in page:
            announced_at = parse_time(item.get("announced_at"))
            competition_id = str(item.get("id") or "")
            if announced_at > cursor or (announced_at == cursor and competition_id not in seen_ids):
                if competition_id:
                    new_items.append(item)
            elif announced_at < cursor:
                reached_older_item = True

        if len(page) < PAGE_SIZE or reached_older_item:
            return new_items, fetched

    raise RuntimeError(f"新公告超过 {PAGE_SIZE * MAX_PAGES} 条，未安全推进游标，请再次运行")


def newest_cursor(items: list[dict[str, Any]], fallback: str = "") -> str:
    timestamps = [item.get("announced_at") for item in items if item.get("announced_at")]
    return max(timestamps, key=parse_time) if timestamps else fallback


def normalized_item(item: dict[str, Any]) -> dict[str, Any]:
    competition_id = str(item.get("id") or "")

    # 格式化项目列表，使用更友好的中文名称
    event_map = {
        "333": "三阶", "222": "二阶", "444": "四阶", "555": "五阶",
        "666": "六阶", "777": "七阶", "333bf": "三盲", "333fm": "最少步",
        "333oh": "单手", "clock": "魔表", "minx": "五魔", "pyram": "金字塔",
        "skewb": "斜转", "sq1": "SQ1", "444bf": "四盲", "555bf": "五盲",
        "333mbf": "多盲"
    }
    event_ids = item.get("event_ids") or []
    events = ", ".join(event_map.get(str(e), str(e)) for e in event_ids) or "待公布"

    # 处理报名信息
    reg_open = item.get("registration_open") or ""
    reg_close = item.get("registration_close") or ""
    competitor_limit = item.get("competitor_limit")

    # 处理主办方和代表
    organizers = item.get("organizers") or []
    delegates = item.get("delegates") or []
    organizer_names = ", ".join(o.get("name", "") for o in organizers[:3] if o.get("name"))
    delegate_names = ", ".join(d.get("name", "") for d in delegates[:2] if d.get("name"))

    # 场馆详细信息
    venue_address = str(item.get("venue_address") or "")
    venue_details = str(item.get("venue_details") or "")

    return {
        "id": competition_id,
        "name": str(item.get("name") or competition_id),
        "country_code": str(item.get("country_iso2") or ""),
        "city": str(item.get("city") or ""),
        "venue": str(item.get("venue") or ""),
        "venue_address": venue_address,
        "venue_details": venue_details,
        "start_date": str(item.get("start_date") or ""),
        "end_date": str(item.get("end_date") or item.get("start_date") or ""),
        "announced_at": str(item.get("announced_at") or ""),
        "events": events,
        "registration_open": reg_open,
        "registration_close": reg_close,
        "competitor_limit": competitor_limit,
        "organizers": organizer_names,
        "delegates": delegate_names,
        "url": str(item.get("url") or f"{WCA_BASE}/competitions/{competition_id}"),
        "website": str(item.get("website") or ""),
    }


def filter_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    country_codes = {code.upper() for code in env_list("WCA_COUNTRY_CODES")}
    if not country_codes:
        return items
    return [item for item in items if str(item.get("country_iso2") or "").upper() in country_codes]


def build_email(items: list[dict[str, Any]]) -> tuple[str, str]:
    cards: list[str] = []
    for raw_item in sorted(items, key=lambda value: (value.get("start_date") or "", value.get("name") or "")):
        normalized = normalized_item(raw_item)

        # HTML 转义字符串字段
        item = {}
        for key, value in normalized.items():
            if isinstance(value, str):
                item[key] = html.escape(value)
            else:
                item[key] = value

        # 格式化比赛日期
        date_text = item["start_date"] if item["start_date"] == item["end_date"] else f'{item["start_date"]} 至 {item["end_date"]}'

        # 格式化报名时间
        reg_info = ""
        if item.get("registration_open") and item.get("registration_close"):
            try:
                reg_open_dt = parse_time(item["registration_open"])
                reg_close_dt = parse_time(item["registration_close"])
                reg_open_str = reg_open_dt.strftime("%Y-%m-%d %H:%M")
                reg_close_str = reg_close_dt.strftime("%Y-%m-%d %H:%M")
                reg_info = f'<div style="margin-bottom:8px"><b>📅 报名时间：</b>{reg_open_str} 至 {reg_close_str}</div>'
            except:
                pass

        # 参赛人数限制
        limit_info = ""
        if item.get("competitor_limit"):
            limit_info = f'<div style="margin-bottom:8px"><b>👥 参赛人数：</b>限 {item["competitor_limit"]} 人</div>'

        # 场馆详细信息
        venue_info = item.get('venue') or '待公布'
        if item.get('venue_address'):
            venue_info += f'<div style="margin-top:4px;font-size:13.5px;color:#486581">{item["venue_address"]}</div>'
        if item.get('venue_details'):
            venue_info += f'<div style="margin-top:4px;font-size:13.5px;color:#486581">{item["venue_details"]}</div>'

        # 主办方和代表信息
        organizer_info = ""
        if item.get("organizers"):
            organizer_info = f'<div style="margin-bottom:8px"><b>🎯 主办方：</b>{item["organizers"]}</div>'

        delegate_info = ""
        if item.get("delegates"):
            delegate_info = f'<div style="margin-bottom:8px"><b>✅ WCA 代表：</b>{item["delegates"]}</div>'

        # 比赛项目标签
        event_tags = item.get('events', '').split(', ') if isinstance(item.get('events'), str) else []
        event_badges = ''.join(f'<span style="background:#e8f4fc;color:#1976d2;padding:2px 8px;border-radius:9999px;font-size:12px;margin-right:6px">{tag}</span>' for tag in event_tags[:6])

        cards.append(
            f"""
            <div style="background:white;border:1px solid #e5e7eb;border-radius:16px;margin:20px 0;overflow:hidden;box-shadow:0 8px 25px -5px rgba(25, 118, 210, 0.1), 0 4px 12px -2px rgba(0, 0, 0, 0.07);transition:transform 0.2s">
              <div style="background:linear-gradient(135deg, #1976d2, #1565c0);color:#ffffff;padding:16px 20px;display:flex;align-items:center;gap:12px">
                <div style="background:rgba(255,255,255,0.25);width:42px;height:42px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:22px">🧊</div>
                <div style="flex:1">
                  <div style="font-size:15px;opacity:0.9">{item.get('country_code') or 'WCA'}</div>
                  <div style="font-size:17px;font-weight:600;line-height:1.2">{item.get('name')}</div>
                </div>
              </div>
              <div style="padding:20px;background:#fafbfc">
                <div style="display:flex;gap:8px;margin-bottom:12px">
                  {event_badges}
                </div>
                <div style="margin-bottom:10px"><b>📍 地点：</b>{item.get('city') or '待公布'}</div>
                <div style="margin-bottom:10px"><b>🏢 场馆：</b>{venue_info}</div>
                <div style="margin-bottom:10px"><b>🗓️ 比赛日期：</b>{date_text}</div>
                {reg_info}
                {limit_info}
                {organizer_info}
                {delegate_info}
                <div style="margin-top:14px">
                  <a href="{item.get('url')}" target="_blank" style="display:inline-block;background:linear-gradient(90deg, #1976d2, #1565c0);color:#fff;padding:11px 24px;border-radius:9999px;text-decoration:none;font-weight:600;font-size:15px;box-shadow:0 4px 15px rgba(25, 118, 210, 0.3);transition:transform 0.2s">📋 立即查看详情</a>
                </div>
              </div>
            </div>
            """
        )

    subject = f"🎉 【WCA Watch】发现 {len(items)} 场新比赛"
    body = f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Microsoft YaHei',sans-serif;color:#243b53;background:#f5f7fa;padding:20px">
      <div style="max-width:700px;margin:0 auto;background:#fff;border-radius:16px;box-shadow:0 10px 40px -10px rgba(0,0,0,0.15);overflow:hidden">
        <div style="background:linear-gradient(90deg, #1976d2, #1565c0);color:#ffffff;padding:28px 32px;text-align:center">
          <div style="font-size:28px;margin-bottom:8px">🧊 WCA 官方公告</div>
          <div style="font-size:17px;opacity:0.95">最新比赛通知</div>
        </div>
        <div style="padding:32px 28px">
          <p style="font-size:15.5px;color:#486581;margin-bottom:24px">WCA 刚刚公布了 <b style="color:#1976d2">{len(items)}</b> 场新的比赛，<b>快来报名吧！</b></p>
          {''.join(cards)}
          <div style="margin-top:32px;padding-top:24px;border-top:1px solid #e5e7eb;display:flex;justify-content:space-between;align-items:center;font-size:12px;color:#829ab1">
            <div>此邮件由 WCA Watch 自动生成</div>
            <a href="https://www.worldcubeassociation.org" target="_blank" style="color:#1976d2;text-decoration:none">WCA 官网 →</a>
          </div>
        </div>
      </div>
    </body></html>
    """
    return subject, body


def keychain_password(user: str) -> str:
    """从 macOS 登录钥匙串读取 SMTP 密码，避免明文配置。"""
    if sys.platform != "darwin" or not user:
        return ""
    service = os.getenv("SMTP_KEYCHAIN_SERVICE", "WCA Watch SMTP")
    result = subprocess.run(
        ["security", "find-generic-password", "-a", user, "-s", service, "-w"],
        text=True,
        capture_output=True,
    )
    return result.stdout.rstrip("\n") if result.returncode == 0 else ""


def smtp_config() -> dict[str, Any]:
    use_ssl = env_bool("SMTP_USE_SSL", False)
    user = os.getenv("SMTP_USER", "").strip()
    return {
        "host": os.getenv("SMTP_HOST", "smtp.mail.me.com"),
        "port": int(os.getenv("SMTP_PORT", "465" if use_ssl else "587")),
        "user": user,
        "password": os.getenv("SMTP_PASSWORD", "").strip() or keychain_password(user),
        "recipients": env_list("MAIL_TO"),
        "from_name": os.getenv("MAIL_FROM_NAME", "WCA Watch").strip() or "WCA Watch",
        "use_ssl": use_ssl,
    }


def validate_smtp(config: dict[str, Any]) -> None:
    missing = [name for name in ("host", "user", "password", "recipients") if not config.get(name)]
    if missing:
        raise RuntimeError("邮件配置不完整，请填写 .env（缺少：" + ", ".join(missing) + "）")


def send_email(subject: str, body: str) -> None:
    config = smtp_config()
    validate_smtp(config)
    message = MIMEText(body, "html", "utf-8")
    message["Subject"] = subject
    message["From"] = formataddr((config["from_name"], config["user"]))
    message["To"] = ", ".join(config["recipients"])
    context = ssl.create_default_context()

    if config["use_ssl"]:
        with smtplib.SMTP_SSL(config["host"], config["port"], timeout=30, context=context) as server:
            server.login(config["user"], config["password"])
            server.sendmail(config["user"], config["recipients"], message.as_string())
    else:
        with smtplib.SMTP(config["host"], config["port"], timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(config["user"], config["password"])
            server.sendmail(config["user"], config["recipients"], message.as_string())


def make_state(cursor: str, fetched: list[dict[str, Any]], initialized_at: str | None = None) -> dict[str, Any]:
    ids = [str(item.get("id")) for item in fetched if item.get("id")]
    return {
        "version": STATE_VERSION,
        "initialized_at": initialized_at or now_iso(),
        "last_checked_at": now_iso(),
        "last_announced_at": cursor,
        "recent_ids": list(dict.fromkeys(ids))[:500],
    }


def initialize() -> None:
    page = fetch_page(1)
    if not page:
        raise RuntimeError("WCA API 没有返回比赛，拒绝建立空基线")
    state = make_state(newest_cursor(page), page)
    save_state(state)
    print(f"[OK] 已建立基线（{state['last_announced_at']}），不会发送历史比赛。")


def check(dry_run: bool = False) -> None:
    state = load_state()
    if state.get("version") != STATE_VERSION or not state.get("last_announced_at"):
        if dry_run:
            page = fetch_page(1)
            print(f"[DRY-RUN] 尚未建立 v{STATE_VERSION} 基线；当前最新比赛：{page[0].get('id') if page else '无'}")
            return
        initialize()
        return

    seen_ids = {str(value) for value in state.get("recent_ids", [])}
    new_items, fetched = fetch_since(str(state["last_announced_at"]), seen_ids)
    matching_items = filter_items(new_items)

    if dry_run:
        print(json.dumps([normalized_item(item) for item in matching_items], ensure_ascii=False, indent=2))
        print(f"[DRY-RUN] 共发现 {len(new_items)} 场新公告，其中 {len(matching_items)} 场符合筛选；未发信、未改状态。")
        return

    if matching_items:
        subject, body = build_email(matching_items)
        send_email(subject, body)
        print(f"[OK] 已发送 {len(matching_items)} 场新比赛通知。")
    else:
        print("[OK] 没有符合条件的新比赛。")

    combined = fetched + [{"id": value} for value in state.get("recent_ids", [])]
    next_cursor = newest_cursor(fetched, str(state["last_announced_at"]))
    # 没有新公告时无需写入 last_checked_at，避免云端工作流每 30 分钟产生一次提交。
    if new_items or next_cursor != str(state["last_announced_at"]):
        save_state(make_state(next_cursor, combined, state.get("initialized_at")))


def send_test_email() -> None:
    subject = "【WCA Watch】邮件配置测试成功"
    body = "<p>如果你看到了这封邮件，说明 WCA 新比赛通知器的邮件配置正常。</p>"
    send_email(subject, body)
    print("[OK] 测试邮件已发送。")


def acquire_lock():
    import fcntl

    stream = LOCK_FILE.open("w", encoding="utf-8")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise RuntimeError("已有一个检查任务正在运行")
    return stream


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WCA 新比赛邮件通知器")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--init", action="store_true", help="以当前最新公告重新建立基线")
    action.add_argument("--dry-run", action="store_true", help="检查但不发邮件、不更新状态")
    action.add_argument("--test-email", action="store_true", help="发送一封测试邮件")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    lock = None
    try:
        lock = acquire_lock()
        if args.init:
            initialize()
        elif args.test_email:
            send_test_email()
        else:
            check(dry_run=args.dry_run)
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
