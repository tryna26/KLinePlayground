"""Unadjusted Sina daily bars. Decode locally; never execute remote scripts."""

import re
import requests

from backend.replay_models import ReplayDataError, day, stock_code, validate_bars


class SinaDailySource:
    name = "sina"
    version = "sina-unadjusted-shares-v1"
    synthetic = False

    def fetch(self, code, start, end):
        code = stock_code(code)
        first, last = day(start), day(end)
        symbol = ("sh" if code.startswith("6") else "sz") + code
        url = f"https://finance.sina.com.cn/realstock/company/{symbol}/hisdata/klc_kl.js"
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(url, headers={"User-Agent":"Mozilla/5.0", "Referer":"https://finance.sina.com.cn/"}, timeout=(6,15))
                response.raise_for_status()
            match = re.search(r'=\s*"([^"\r\n]+)"\s*;', response.text)
            if not match:
                raise ValueError("missing encoded daily data")
            from akshare.stock.cons import hk_js_decode
            from backend.local_js import call_local_script
            records = call_local_script(hk_js_decode, "d", match.group(1))
            return normalize_sina_records(code, records, first, last)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            raise ReplayDataError(f"{code}：新浪真实日线获取或解析失败，请重试更新真实数据") from exc


def normalize_sina_records(code, records, first, last):
    if not isinstance(records, list) or not records:
        raise ReplayDataError("新浪未返回有效原始日线")
    rows = []
    for row in records:
        value = day(row["date"][:10])
        if first <= value <= last:
            rows.append({"date":str(value), **{field:str(row[field]) for field in ("open","high","low","close","volume")}})
    # Volume is already in shares. No filling, rounding, deduplication or adjustment.
    return [bar.to_dict() for bar in validate_bars(code, rows, first, last)]
