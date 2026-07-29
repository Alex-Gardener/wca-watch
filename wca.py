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
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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


WEEKDAYS = "一二三四五六日"


def format_competition_date(start_text: str, end_text: str) -> str:
    """把 API 日期格式化成适合中文邮件快速浏览的形式。"""
    try:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text or start_text)
    except ValueError:
        return start_text if start_text == end_text else f"{start_text} 至 {end_text}"

    def full(value: date) -> str:
        return f"{value.year}年{value.month}月{value.day}日（周{WEEKDAYS[value.weekday()]}）"

    if start == end:
        return full(start)
    if start.year == end.year:
        return (
            f"{start.year}年{start.month}月{start.day}日（周{WEEKDAYS[start.weekday()]}）"
            f"— {end.month}月{end.day}日（周{WEEKDAYS[end.weekday()]}）"
        )
    return f"{full(start)}— {full(end)}"


def mail_timezone() -> tuple[timezone | ZoneInfo, str]:
    zone_name = os.getenv("MAIL_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        zone: timezone | ZoneInfo = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
        zone_name = "UTC"
    label = os.getenv("MAIL_TIMEZONE_LABEL", "").strip()
    if not label:
        label = "北京时间" if zone_name == "Asia/Shanghai" else zone_name
    return zone, label


def format_mail_time(value: str) -> str:
    if not value:
        return ""
    try:
        zone, label = mail_timezone()
        return f"{parse_time(value).astimezone(zone).strftime('%Y年%m月%d日 %H:%M')}（{label}）"
    except ValueError:
        return value


def country_flag(code: str) -> str:
    code = code.upper()
    if len(code) == 2 and code.isalpha() and code.isascii():
        return "".join(chr(127397 + ord(character)) for character in code)
    return "🌐"


def escaped_lines(value: str) -> str:
    return html.escape(value, quote=True).replace("\n", "<br>")


def detail_row(label: str, value: str, hint: str = "") -> str:
    if not value:
        return ""
    hint_html = (
        f'<div style="margin-top:3px;color:#64748b;font-size:12px;line-height:1.55">{hint}</div>'
        if hint
        else ""
    )
    return f"""
      <tr>
        <td valign="top" style="width:92px;padding:9px 0;color:#64748b;font-size:13px;line-height:1.55">{label}</td>
        <td valign="top" style="padding:9px 0;color:#1e293b;font-size:14px;font-weight:600;line-height:1.55">{value}{hint_html}</td>
      </tr>
    """


def build_plain_email(items: list[dict[str, Any]]) -> str:
    lines = [f"WCA 新比赛通知（共 {len(items)} 场）", ""]
    for index, raw_item in enumerate(
        sorted(items, key=lambda value: (value.get("start_date") or "", value.get("name") or "")), 1
    ):
        item = normalized_item(raw_item)
        lines.extend(
            [
                f"{index}. {item['name']}",
                f"比赛日期：{format_competition_date(item['start_date'], item['end_date'])}",
                f"比赛地点：{' · '.join(part for part in (item['city'], item['venue']) if part) or '待公布'}",
            ]
        )
        if item["venue_address"]:
            lines.append(f"详细地址：{item['venue_address']}")
        if item["registration_open"]:
            lines.append(f"报名开放：{format_mail_time(item['registration_open'])}")
        if item["registration_close"]:
            lines.append(f"报名截止：{format_mail_time(item['registration_close'])}")
        if item["competitor_limit"]:
            lines.append(f"参赛名额：{item['competitor_limit']} 人")
        lines.append(f"比赛项目：{item['events']}")
        if item["organizers"]:
            lines.append(f"主办方：{item['organizers']}")
        if item["delegates"]:
            lines.append(f"WCA 代表：{item['delegates']}")
        lines.extend([f"查看详情：{item['url']}", ""])
    lines.append("比赛和报名信息可能调整，请以 WCA 官方页面为准。")
    return "\n".join(lines)


def build_email(items: list[dict[str, Any]]) -> tuple[str, str]:
    cards: list[str] = []
    sorted_items = sorted(
        items, key=lambda value: (value.get("start_date") or "", value.get("name") or "")
    )
    for index, raw_item in enumerate(sorted_items, 1):
        normalized = normalized_item(raw_item)
        item = {
            key: html.escape(value, quote=True) if isinstance(value, str) else value
            for key, value in normalized.items()
        }
        date_text = html.escape(
            format_competition_date(normalized["start_date"], normalized["end_date"])
        )
        location_text = " · ".join(
            value for value in (item["city"], item["venue"]) if value
        ) or "待公布"
        venue_hint_parts = [
            escaped_lines(normalized["venue_address"]),
            escaped_lines(normalized["venue_details"]),
        ]
        venue_hint = "<br>".join(part for part in venue_hint_parts if part)
        event_badges = "".join(
            f'<span style="display:inline-block;margin:0 6px 6px 0;padding:5px 10px;background:#eff6ff;border:1px solid #dbeafe;border-radius:999px;color:#1d4ed8;font-size:12px;line-height:1">{html.escape(tag)}</span>'
            for tag in normalized["events"].split(", ")
        )
        registration_rows = "".join(
            [
                detail_row(
                    "报名开放",
                    html.escape(format_mail_time(normalized["registration_open"])),
                ),
                detail_row(
                    "报名截止",
                    html.escape(format_mail_time(normalized["registration_close"])),
                ),
                detail_row(
                    "参赛名额",
                    f"{item['competitor_limit']} 人" if item["competitor_limit"] else "",
                ),
            ]
        )
        organizer_rows = "".join(
            [
                detail_row("主办方", item["organizers"]),
                detail_row("WCA 代表", item["delegates"]),
            ]
        )
        official_site_link = ""
        if item["website"] and normalized["website"] != normalized["url"]:
            official_site_link = f'<a href="{item["website"]}" style="display:inline-block;margin-left:14px;color:#2563eb;font-size:13px;font-weight:600;text-decoration:none">比赛官网 →</a>'

        cards.append(
            f"""
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin:0 0 22px;background:#ffffff;border:1px solid #dbe3ee;border-radius:14px;border-collapse:separate;overflow:hidden">
              <tr>
                <td style="padding:20px 22px 18px;border-bottom:1px solid #e8eef5;background:#f8fbff">
                  <div style="margin-bottom:7px;color:#2563eb;font-size:12px;font-weight:700;letter-spacing:.5px">{country_flag(normalized['country_code'])} {item['country_code'] or 'WCA'} · 第 {index} 场</div>
                  <div style="color:#0f172a;font-size:20px;font-weight:750;line-height:1.35">{item['name']}</div>
                </td>
              </tr>
              <tr>
                <td style="padding:20px 22px 22px">
                  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-bottom:13px;background:#f8fafc;border-radius:10px">
                    <tr><td style="padding:13px 15px;color:#475569;font-size:12px;font-weight:700">比赛日期</td></tr>
                    <tr><td style="padding:0 15px 14px;color:#0f172a;font-size:16px;font-weight:700;line-height:1.5">{date_text}</td></tr>
                  </table>
                  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse:collapse">
                    {detail_row('地点', location_text, venue_hint)}
                    {registration_rows}
                  </table>
                  <div style="margin:15px 0 5px;color:#64748b;font-size:12px;font-weight:700">比赛项目</div>
                  <div style="line-height:2">{event_badges}</div>
                  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:7px;border-top:1px solid #eef2f7;border-collapse:collapse">
                    {organizer_rows}
                  </table>
                  <div style="margin-top:18px">
                    <a href="{item['url']}" style="display:inline-block;padding:11px 18px;background:#2563eb;border-radius:8px;color:#ffffff;font-size:14px;font-weight:700;text-decoration:none">查看 WCA 详情&nbsp; →</a>
                    {official_site_link}
                  </div>
                </td>
              </tr>
            </table>
            """
        )

    subject = f"WCA 新赛通知｜{len(items)} 场比赛已公布"
    body = f"""
    <!doctype html>
    <html lang="zh-CN">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <style>
          @media only screen and (max-width:620px) {{
            .email-shell {{ width:100% !important; border-radius:0 !important; }}
            .email-pad {{ padding-left:18px !important; padding-right:18px !important; }}
          }}
        </style>
      </head>
      <body style="margin:0;padding:0;background:#eef3f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',Arial,sans-serif">
        <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">监测到 {len(items)} 场新公布的 WCA 比赛，日期、地点和报名时间已整理好。</div>
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#eef3f8">
          <tr>
            <td align="center" style="padding:28px 12px">
              <table role="presentation" width="680" cellspacing="0" cellpadding="0" border="0" class="email-shell" style="width:680px;max-width:100%;background:#ffffff;border-radius:18px;overflow:hidden;box-shadow:0 10px 32px rgba(15,23,42,.08)">
                <tr>
                  <td class="email-pad" style="padding:34px 34px 30px;background:#0f3f8f;background-image:linear-gradient(135deg,#174ea6,#2563eb);color:#ffffff">
                    <div style="font-size:13px;font-weight:700;letter-spacing:1.2px;opacity:.82">WCA WATCH</div>
                    <div style="margin-top:12px;font-size:29px;font-weight:750;line-height:1.25">新比赛已公布</div>
                    <div style="margin-top:9px;font-size:15px;line-height:1.65;opacity:.9">本次共整理 <strong>{len(items)}</strong> 场比赛，关键时间和地点一目了然。</div>
                  </td>
                </tr>
                <tr>
                  <td class="email-pad" style="padding:28px 34px 32px">
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-bottom:24px;background:#fff7ed;border:1px solid #fed7aa;border-radius:10px">
                      <tr><td style="padding:12px 14px;color:#9a3412;font-size:13px;line-height:1.6">⏰ 请重点留意报名开放与截止时间；以下时间均已转换为{html.escape(mail_timezone()[1])}。</td></tr>
                    </table>
                    {''.join(cards)}
                    <div style="padding:16px 18px;background:#f8fafc;border-radius:10px;color:#64748b;font-size:12px;line-height:1.7">比赛安排、名额及报名时间可能调整，请以 WCA 官方页面的最新信息为准。</div>
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:25px;border-top:1px solid #e2e8f0">
                      <tr>
                        <td style="padding-top:18px;color:#94a3b8;font-size:11px;line-height:1.6">此邮件由 WCA Watch 自动整理并发送</td>
                        <td align="right" style="padding-top:18px"><a href="https://www.worldcubeassociation.org" style="color:#2563eb;font-size:12px;font-weight:600;text-decoration:none">访问 WCA 官网 →</a></td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
    </html>
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


def send_email(subject: str, body: str, plain_body: str = "") -> None:
    config = smtp_config()
    validate_smtp(config)
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = formataddr((config["from_name"], config["user"]))
    message["To"] = ", ".join(config["recipients"])
    message.attach(
        MIMEText(
            plain_body or "WCA Watch 为你整理了新的比赛信息，请使用支持 HTML 的邮件客户端查看。",
            "plain",
            "utf-8",
        )
    )
    message.attach(MIMEText(body, "html", "utf-8"))
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
        send_email(subject, body, build_plain_email(matching_items))
        print(f"[OK] 已发送 {len(matching_items)} 场新比赛通知。")
    else:
        print("[OK] 没有符合条件的新比赛。")

    combined = fetched + [{"id": value} for value in state.get("recent_ids", [])]
    next_cursor = newest_cursor(fetched, str(state["last_announced_at"]))
    # 没有新公告时无需写入 last_checked_at，避免云端工作流每 30 分钟产生一次提交。
    if new_items or next_cursor != str(state["last_announced_at"]):
        save_state(make_state(next_cursor, combined, state.get("initialized_at")))


def send_test_email() -> None:
    subject = "WCA Watch｜邮件配置测试成功"
    body = """
    <!doctype html>
    <html lang="zh-CN">
      <body style="margin:0;padding:0;background:#eef3f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',Arial,sans-serif">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#eef3f8">
          <tr><td align="center" style="padding:36px 14px">
            <table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0" style="width:600px;max-width:100%;background:#ffffff;border-radius:16px;overflow:hidden">
              <tr><td style="padding:32px;background:#0f3f8f;background-image:linear-gradient(135deg,#174ea6,#2563eb);color:#ffffff">
                <div style="font-size:13px;font-weight:700;letter-spacing:1.2px;opacity:.82">WCA WATCH</div>
                <div style="margin-top:10px;font-size:26px;font-weight:750">邮件配置成功</div>
              </td></tr>
              <tr><td style="padding:30px 32px;color:#334155;font-size:15px;line-height:1.8">
                <div style="margin-bottom:18px;padding:14px 16px;background:#ecfdf5;border:1px solid #a7f3d0;border-radius:10px;color:#047857;font-weight:700">✓ 测试邮件已正常送达</div>
                <div>SMTP 发信配置工作正常。今后监测到符合筛选条件的新比赛时，你会收到包含比赛日期、地点、报名时间和参赛项目的完整通知。</div>
                <div style="margin-top:22px;color:#94a3b8;font-size:12px">此邮件仅用于测试，不代表有新比赛公布。</div>
              </td></tr>
            </table>
          </td></tr>
        </table>
      </body>
    </html>
    """
    plain_body = (
        "WCA Watch 邮件配置测试成功\n\n"
        "测试邮件已正常送达，SMTP 发信配置工作正常。\n"
        "此邮件仅用于测试，不代表有新比赛公布。"
    )
    send_email(subject, body, plain_body)
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
