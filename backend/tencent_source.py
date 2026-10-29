"""Tencent daily bars, with bounded requests and matching adjusted prices."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import time

import pandas as pd
import requests


URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get"
FALLBACK_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


class TencentSourceUnavailable(RuntimeError):
    """Both official endpoints failed at the HTTP/transport level."""


def fetch_raw_rows(stock_code, start_date, end_date):
    """Lossless raw rows for portfolio validation; never request adjusted data.

    Keep this separate from the legacy bundle normalizer, which drops malformed
    dates/duplicates. Portfolio callers must see and reject those records.
    Tencent main-board daily volume is in lots (100 shares).
    """
    if len(stock_code) != 6 or not stock_code.isascii() or not stock_code.isdigit():
        raise ValueError("股票代码必须是六位数字")
    if stock_code.startswith(("60", "68", "69")):
        symbol = "sh" + stock_code
    elif stock_code.startswith(("00", "30")):
        symbol = "sz" + stock_code
    else:
        raise ValueError("腾讯行情支持沪深 A 股，其他市场请选择其他数据源")
    start, end = pd.Timestamp(start_date).normalize(), pd.Timestamp(end_date).normalize()
    if end < start:
        raise ValueError("结束日期不能早于开始日期")
    rows = []
    cursor = start
    while cursor <= end:
        boundary = min(pd.Timestamp(year=cursor.year + 1, month=12, day=31), end)
        rows.extend(_request_rows(symbol, cursor.strftime("%Y-%m-%d"),
                                  boundary.strftime("%Y-%m-%d"), ""))
        cursor = boundary + pd.Timedelta(days=1)
    return rows


def _request_rows(symbol, first, last, adjustment):
    error = None
    transport_errors = 0
    for attempt, endpoint in enumerate((URL, FALLBACK_URL)):
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(
                    endpoint, params={"param": f"{symbol},day,{first},{last},640,{adjustment}"},
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                    timeout=(6, 15),
                )
                response.raise_for_status()
                payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError(payload.get("msg") or "腾讯行情接口返回错误")
            security = payload.get("data", {}).get(symbol, {})
            key = "qfqday" if adjustment else "day"
            # The raw portfolio path requires the raw key, even for empty data.
            if not adjustment and key not in security:
                raise RuntimeError("腾讯未返回未复权日线字段")
            rows = security.get(key)
            if adjustment and rows is None:
                rows = security.get("day", [])
            if not isinstance(rows, list):
                raise ValueError("腾讯日线格式无效")
            if len(rows) >= 640:
                raise RuntimeError("行情返回达到条数上限，无法确认历史区间完整性")
            if any(not isinstance(row, list) or len(row) < 6 for row in rows):
                raise ValueError("腾讯日线字段不足")
            return [row[:6] for row in rows]
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            error = exc
            if isinstance(exc, requests.RequestException):
                transport_errors += 1
            if attempt == 0 and not isinstance(exc, requests.HTTPError):
                time.sleep(0.5)
    error_type = TencentSourceUnavailable if transport_errors == 2 else RuntimeError
    raise error_type(f"腾讯行情获取失败（{symbol}，{first} 至 {last}）：{error}") from error


def fetch_bundle(stock_code, start_date, end_date=None):
    if stock_code.startswith(("60", "68", "69")):
        symbol = "sh" + stock_code
    elif stock_code.startswith(("00", "30")):
        symbol = "sz" + stock_code
    else:
        raise ValueError("腾讯备用源目前支持沪深股票，请选择其他数据源获取该股票。")

    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date or datetime.now().date()).normalize()
    if end < start:
        raise ValueError("结束日期不能早于开始日期")

    # Two calendar years fit within the endpoint's 640-bar limit.
    segments = []
    cursor = start
    while cursor <= end:
        boundary = min(pd.Timestamp(year=cursor.year + 1, month=12, day=31), end)
        segments.append((cursor.strftime("%Y-%m-%d"), boundary.strftime("%Y-%m-%d")))
        cursor = boundary + pd.Timedelta(days=1)

    def fetch_segment(job):
        first, last, adjustment = job
        return adjustment, _request_rows(symbol, first, last, adjustment)

    jobs = [(first, last, adj) for first, last in segments for adj in ("", "qfq")]
    raw_rows, adjusted_rows = [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for adjustment, rows in pool.map(fetch_segment, jobs):
            (adjusted_rows if adjustment else raw_rows).extend(rows)

    columns = ["date", "open", "close", "high", "low", "volume"]

    def normalize(rows):
        frame = pd.DataFrame(rows, columns=columns)
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        for column in columns[1:]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=columns).sort_values("date").drop_duplicates("date")
        frame = frame.loc[frame["date"].between(start, end)].reset_index(drop=True)
        if not frame.empty:
            valid = (
                (frame["low"] <= frame[["open", "close"]].min(axis=1) + 1e-6)
                & (frame["high"] + 1e-6 >= frame[["open", "close"]].max(axis=1))
                & (frame["volume"] >= 0)
            )
            if not valid.all():
                raise ValueError("腾讯返回的行情未通过价格与成交量校验")
        return frame

    daily = normalize(raw_rows)
    if daily.empty:
        return {"kline": None, "factor": None}
    adjusted = normalize(adjusted_rows)
    matched = daily[["date", "close"]].merge(
        adjusted[["date", "close"]], on="date", suffixes=("", "_adjusted"), how="left"
    )
    if matched["close_adjusted"].isna().any() or (matched["close"] <= 0).any():
        raise ValueError("原始行情和复权行情日期不匹配，未写入不完整数据")
    factor = matched[["date"]].copy()
    factor["factor"] = matched["close_adjusted"] / matched["close"]
    return {"kline": daily, "factor": factor}
