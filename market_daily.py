# -*- coding: utf-8 -*-
"""Daily index collector and Gmail email reporter.

Fetches S&P 500, NASDAQ Composite, and Taiwan Weighted Index data in parallel,
calculates the 120-trading-day moving average, writes CSV/JSON/text reports,
and sends the latest report through Gmail SMTP.

Designed for Python 3.10+ and GitHub Actions.
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
YFINANCE_CACHE_DIR = DATA_DIR / "yfinance_cache"

MA_DAYS = int(os.getenv("MA_DAYS", "120"))
FETCH_PERIOD = os.getenv("FETCH_PERIOD", "1y")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

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


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    YFINANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Avoid multiple worker threads racing to create yfinance's default cache.
    try:
        yf.set_tz_cache_location(str(YFINANCE_CACHE_DIR))
    except Exception as exc:  # noqa: BLE001
        logging.debug("無法設定 yfinance 時區快取: %s", exc)

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
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logging.warning("%s 下載失敗: %s", symbol, exc)
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 5)

    raise RuntimeError(
        "%s 連續失敗 %d 次: %s" % (symbol, MAX_RETRIES, last_error)
    )


def fetch_all_markets() -> tuple[list[MarketResult], list[str]]:
    results: list[MarketResult] = []
    errors: list[str] = []

    with ThreadPoolExecutor(
        max_workers=len(MARKETS),
        thread_name_prefix="market",
    ) as pool:
        futures = {
            pool.submit(fetch_market, symbol, name): (symbol, name)
            for symbol, name in MARKETS.items()
        }

        for future in as_completed(futures):
            symbol, name = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
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


def build_report_text(
    results: list[MarketResult],
    errors: list[str],
) -> str:
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
                "  差距：%s%.2f 點 (%s%.3f%%)"
                % (
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

    return "\n".join(lines)


def write_latest_files(
    results: list[MarketResult],
    errors: list[str],
) -> str:
    payload = {
        "generated_at_taipei": datetime.now(TAIPEI_TZ).isoformat(
            timespec="seconds"
        ),
        "moving_average_days": MA_DAYS,
        "results": [asdict(result) for result in results],
        "errors": errors,
    }
    LATEST_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report_text = build_report_text(results, errors)
    LATEST_TEXT.write_text(report_text, encoding="utf-8")
    print(report_text)
    return report_text


def email_subject(results: list[MarketResult]) -> str:
    dates = sorted({item.market_date for item in results})
    date_label = dates[-1] if dates else datetime.now(TAIPEI_TZ).date().isoformat()
    signals = [
        item.name + " " + item.cross_signal
        for item in results
        if item.cross_signal != "無穿越"
    ]
    signal_suffix = "｜" + "、".join(signals) if signals else ""
    return "市場半年線報告 %s%s" % (date_label, signal_suffix)


def attach_file(message: EmailMessage, path: Path) -> None:
    if not path.exists():
        return

    suffix = path.suffix.lower()
    subtype = {
        ".csv": "csv",
        ".json": "json",
        ".txt": "plain",
        ".log": "plain",
    }.get(suffix, "octet-stream")
    maintype = "text" if suffix in {".csv", ".json", ".txt", ".log"} else "application"

    if maintype == "text":
        content = path.read_text(encoding="utf-8-sig", errors="replace")
        message.add_attachment(
            content,
            subtype=subtype,
            filename=path.name,
        )
    else:
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )


def send_email(report_text: str, results: list[MarketResult]) -> None:
    username = os.getenv("SMTP_USERNAME", "").strip()
    app_password = os.getenv("SMTP_APP_PASSWORD", "").replace(" ", "").strip()
    recipients_raw = os.getenv("EMAIL_TO", username).strip()
    recipients = [
        item.strip()
        for item in recipients_raw.replace(";", ",").split(",")
        if item.strip()
    ]

    if not username:
        raise RuntimeError("未設定 SMTP_USERNAME")
    if not app_password:
        raise RuntimeError("未設定 SMTP_APP_PASSWORD")
    if not recipients:
        raise RuntimeError("未設定 EMAIL_TO")

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "465"))

    message = EmailMessage()
    message["From"] = username
    message["To"] = ", ".join(recipients)
    message["Subject"] = email_subject(results)
    message.set_content(report_text)

    attach_file(message, LATEST_TEXT)
    attach_file(message, LATEST_JSON)
    attach_file(message, SUMMARY_CSV)

    context = ssl.create_default_context()
    logging.info(
        "準備透過 %s:%d 寄信到 %s",
        smtp_host,
        smtp_port,
        ", ".join(recipients),
    )

    with smtplib.SMTP_SSL(
        smtp_host,
        smtp_port,
        context=context,
        timeout=30,
    ) as server:
        server.login(username, app_password)
        server.send_message(message)

    logging.info("Email 寄送成功：%s", ", ".join(recipients))


def main() -> int:
    setup_logging()
    logging.info("每日市場任務開始，半年線=%d 日", MA_DAYS)

    results, errors = fetch_all_markets()
    if not results:
        logging.error("三個市場全部抓取失敗")
        write_latest_files([], errors)
        return 1

    new_rows = append_history(results)
    report_text = write_latest_files(results, errors)

    if env_bool("EMAIL_ENABLED", default=False):
        send_only_when_new = env_bool(
            "EMAIL_SEND_ONLY_WHEN_NEW",
            default=True,
        )
        if send_only_when_new and new_rows == 0:
            logging.info("沒有新交易資料，依設定跳過 Email")
        else:
            try:
                send_email(report_text, results)
            except Exception as exc:  # noqa: BLE001
                logging.exception("Email 寄送失敗: %s", exc)
                return 3
    else:
        logging.info("EMAIL_ENABLED=false，跳過 Email")

    if errors:
        logging.warning(
            "任務部分成功：成功 %d，失敗 %d",
            len(results),
            len(errors),
        )
        return 2

    logging.info("任務完成：成功 %d", len(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
