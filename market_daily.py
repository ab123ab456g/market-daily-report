# -*- coding: utf-8 -*-
"""Daily index collector and email reporter.

Fetches S&P 500, NASDAQ Composite, and Taiwan Weighted Index data in parallel,
calculates the 120-trading-day moving average, writes CSV/JSON/text reports,
and optionally emails the report through Gmail SMTP.

Designed for Python 3.10+ and a daily scheduler such as GitHub Actions or
PythonAnywhere Scheduled Tasks.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import smtplib
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("MARKET_DATA_DIR", str(BASE_DIR / "data")))
SUMMARY_CSV = DATA_DIR / "market_history.csv"
LATEST_JSON = DATA_DIR / "latest_report.json"
LATEST_TEXT = DATA_DIR / "latest_report.txt"
LOG_FILE = DATA_DIR / "market_daily.log"

MA_DAYS = int(os.getenv("MA_DAYS", "120"))
FETCH_PERIOD = os.getenv("FETCH_PERIOD", "1y")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# Gmail SMTP settings. Password must be a Google App Password, not the normal
# Google Account password.
DEFAULT_EMAIL_TO = "ab123ab456g@gmail.com"
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "").replace(" ", "").strip()
EMAIL_TO_RAW = os.getenv("EMAIL_TO", "").strip() or DEFAULT_EMAIL_TO
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "每日市場半年線").strip()

MARKETS = {
    "^GSPC": "S&P 500",
    "^IXIC": "NASDAQ Composite",
    "^TWII": "Taiwan Weighted Index",
}

CSV_FIELDS = [
    "run_time_taipei",
    "market_date",
    "symbol",
    "name",
    "open",
    "close",
    "ma120",
    "difference_points",
    "difference_percent",
    "position",
    "cross_signal",
]


@dataclass(frozen=True)
class MarketResult:
    run_time_taipei: str
    market_date: str
    symbol: str
    name: str
    open: float
    close: float
    ma120: float
    difference_points: float
    difference_percent: float
    position: str
    cross_signal: str


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


EMAIL_ENABLED = env_bool("EMAIL_ENABLED", True)
EMAIL_SEND_ONLY_WHEN_NEW = env_bool("EMAIL_SEND_ONLY_WHEN_NEW", True)
EMAIL_ATTACH_CSV = env_bool("EMAIL_ATTACH_CSV", True)
EMAIL_ATTACH_JSON = env_bool("EMAIL_ATTACH_JSON", True)


def setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


def normalize_history(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise RuntimeError("下載結果為空")

    required = {"Open", "Close"}
    missing = required.difference(frame.columns)
    if missing:
        raise RuntimeError("缺少必要欄位: %s" % ", ".join(sorted(missing)))

    clean = frame.loc[:, ["Open", "Close"]].copy()
    clean = clean.dropna(subset=["Open", "Close"])
    clean = clean.sort_index()

    if len(clean) < MA_DAYS + 1:
        raise RuntimeError(
            "有效交易日只有 %d 筆，至少需要 %d 筆"
            % (len(clean), MA_DAYS + 1)
        )

    clean["MA"] = clean["Close"].rolling(MA_DAYS).mean()
    clean = clean.dropna(subset=["MA"])

    if len(clean) < 2:
        raise RuntimeError("計算均線後資料不足")

    return clean


def determine_cross(
    previous_close: float,
    previous_ma: float,
    close: float,
    ma: float,
) -> str:
    if previous_close <= previous_ma and close > ma:
        return "向上突破半年線"
    if previous_close >= previous_ma and close < ma:
        return "向下跌破半年線"
    return "無穿越"


def fetch_market(symbol: str, name: str) -> MarketResult:
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logging.info("下載 %s (%s)，第 %d 次", name, symbol, attempt)
            frame = yf.Ticker(symbol).history(
                period=FETCH_PERIOD,
                interval="1d",
                auto_adjust=False,
                actions=False,
                repair=True,
                timeout=20,
            )
            clean = normalize_history(frame)
            previous = clean.iloc[-2]
            latest = clean.iloc[-1]

            open_price = float(latest["Open"])
            close_price = float(latest["Close"])
            ma = float(latest["MA"])
            diff = close_price - ma
            diff_pct = (diff / ma) * 100.0
            position = "半年線之上" if close_price >= ma else "半年線之下"
            cross = determine_cross(
                float(previous["Close"]),
                float(previous["MA"]),
                close_price,
                ma,
            )

            market_date = pd.Timestamp(clean.index[-1]).date().isoformat()
            run_time = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")

            return MarketResult(
                run_time_taipei=run_time,
                market_date=market_date,
                symbol=symbol,
                name=name,
                open=round(open_price, 2),
                close=round(close_price, 2),
                ma120=round(ma, 2),
                difference_points=round(diff, 2),
                difference_percent=round(diff_pct, 3),
                position=position,
                cross_signal=cross,
            )
        except Exception as exc:  # noqa: BLE001 - retry boundary
            last_error = exc
            logging.warning("%s 下載失敗: %s", symbol, exc)
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 5)

    raise RuntimeError("%s 連續失敗 %d 次: %s" % (symbol, MAX_RETRIES, last_error))


def fetch_all_markets() -> tuple[list[MarketResult], list[str]]:
    results: list[MarketResult] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=len(MARKETS), thread_name_prefix="market") as pool:
        futures = {
            pool.submit(fetch_market, symbol, name): (symbol, name)
            for symbol, name in MARKETS.items()
        }

        for future in as_completed(futures):
            symbol, name = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - aggregate failures
                message = "%s (%s): %s" % (name, symbol, exc)
                logging.error(message)
                errors.append(message)

    order = {symbol: index for index, symbol in enumerate(MARKETS)}
    results.sort(key=lambda item: order[item.symbol])
    return results, errors


def load_existing_keys(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()

    keys: set[tuple[str, str]] = set()
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            market_date = row.get("market_date")
            symbol = row.get("symbol")
            if market_date and symbol:
                keys.add((market_date, symbol))
    return keys


def append_history(results: Iterable[MarketResult]) -> int:
    existing_keys = load_existing_keys(SUMMARY_CSV)
    rows = [
        asdict(result)
        for result in results
        if (result.market_date, result.symbol) not in existing_keys
    ]

    if not rows:
        logging.info("沒有新的交易日資料需要寫入 CSV")
        return 0

    file_exists = SUMMARY_CSV.exists()
    with SUMMARY_CSV.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)

    logging.info("新增 %d 筆資料到 %s", len(rows), SUMMARY_CSV)
    return len(rows)


def build_report_text(results: list[MarketResult], errors: list[str]) -> str:
    generated_at = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    lines = [
        "每日市場半年線報告",
        "產生時間（台北）：%s" % generated_at,
        "半年線定義：%d 個交易日收盤價平均" % MA_DAYS,
        "",
    ]

    for result in results:
        sign = "+" if result.difference_points >= 0 else ""
        lines.extend(
            [
                "%s (%s)" % (result.name, result.symbol),
                "  市場日期：%s" % result.market_date,
                "  開盤：%.2f" % result.open,
                "  收盤：%.2f" % result.close,
                "  半年線：%.2f" % result.ma120,
                "  差距：%s%.2f 點 (%s%.3f%%)" % (
                    sign,
                    result.difference_points,
                    sign,
                    result.difference_percent,
                ),
                "  判斷：%s；%s" % (result.position, result.cross_signal),
                "",
            ]
        )

    if errors:
        lines.append("錯誤：")
        lines.extend("  - " + error for error in errors)

    if not results and not errors:
        lines.append("本次沒有市場資料。")

    return "\n".join(lines)


def write_latest_files(
    results: list[MarketResult],
    errors: list[str],
    report_text: str,
) -> None:
    payload = {
        "generated_at_taipei": datetime.now(TAIPEI_TZ).isoformat(timespec="seconds"),
        "moving_average_days": MA_DAYS,
        "results": [asdict(result) for result in results],
        "errors": errors,
    }
    LATEST_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    LATEST_TEXT.write_text(report_text, encoding="utf-8")
    print(report_text)


def parse_recipients(raw: str) -> list[str]:
    recipients = [item.strip() for item in raw.replace(";", ",").split(",")]
    return [item for item in recipients if item]


def build_email_subject(results: list[MarketResult], errors: list[str]) -> str:
    dates = sorted({result.market_date for result in results})
    date_text = dates[-1] if dates else datetime.now(TAIPEI_TZ).date().isoformat()

    if errors and not results:
        status = "抓取失敗"
    elif errors:
        status = "部分成功"
    elif any(result.cross_signal != "無穿越" for result in results):
        status = "出現半年線穿越"
    else:
        status = "每日更新"

    return "[%s] 市場半年線報告｜%s" % (status, date_text)


def attach_file(message: EmailMessage, path: Path, subtype: str) -> None:
    if not path.exists():
        return
    message.add_attachment(
        path.read_bytes(),
        maintype="application" if subtype == "json" else "text",
        subtype=subtype,
        filename=path.name,
    )


def send_email_report(
    results: list[MarketResult],
    errors: list[str],
    report_text: str,
) -> bool:
    if not EMAIL_ENABLED:
        logging.info("EMAIL_ENABLED=false，略過寄信")
        return False

    recipients = parse_recipients(EMAIL_TO_RAW)
    if not recipients:
        logging.warning("沒有設定 EMAIL_TO，略過寄信")
        return False

    if not SMTP_USERNAME or not SMTP_APP_PASSWORD:
        logging.warning(
            "未設定 SMTP_USERNAME 或 SMTP_APP_PASSWORD，報告已產生但不寄信"
        )
        return False

    message = EmailMessage()
    message["Subject"] = build_email_subject(results, errors)
    message["From"] = "%s <%s>" % (EMAIL_FROM_NAME, SMTP_USERNAME)
    message["To"] = ", ".join(recipients)
    message.set_content(report_text, subtype="plain", charset="utf-8")

    if EMAIL_ATTACH_CSV:
        attach_file(message, SUMMARY_CSV, "csv")
    if EMAIL_ATTACH_JSON:
        attach_file(message, LATEST_JSON, "json")

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(
            SMTP_HOST,
            SMTP_PORT,
            context=context,
            timeout=30,
        ) as server:
            server.login(SMTP_USERNAME, SMTP_APP_PASSWORD)
            server.send_message(message)
        logging.info("市場報告已寄送到 %s", ", ".join(recipients))
        return True
    except Exception:  # noqa: BLE001 - email boundary
        logging.exception("寄送市場報告失敗")
        return False


def main() -> int:
    setup_logging()
    logging.info("每日市場任務開始，半年線=%d 日", MA_DAYS)

    results, errors = fetch_all_markets()
    added_count = append_history(results) if results else 0
    report_text = build_report_text(results, errors)
    write_latest_files(results, errors, report_text)

    should_send = (
        not EMAIL_SEND_ONLY_WHEN_NEW
        or added_count > 0
        or bool(errors)
    )
    if should_send:
        send_email_report(results, errors, report_text)
    else:
        logging.info("沒有新交易日資料且沒有錯誤，本次不重複寄信")

    if not results:
        logging.error("三個市場全部抓取失敗")
        return 1
    if errors:
        logging.warning("任務部分成功：成功 %d，失敗 %d", len(results), len(errors))
        return 2

    logging.info("任務完成：成功 %d", len(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
