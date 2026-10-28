"""Portfolio raw quotes, cache provenance, and evidence-based daily states."""

from dataclasses import dataclass
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from typing import Protocol

from backend.replay_models import (
    DailyBar, DataStatus, QuoteState, ReplayDataError, SecurityHistory,
    day, stock_code, timestamp, utc_now, validate_bars,
)
from backend.replay_storage import content_hash, read_cache, write_cache
from backend.tencent_source import fetch_raw_rows


class DailyQuoteSource(Protocol):
    name: str
    version: str
    synthetic: bool

    def fetch(self, code: str, start: str, end: str) -> list[dict]: ...


class TencentDailySource:
    name = "tencent"
    version = "tencent-raw-mainboard-v1"
    synthetic = False

    def fetch(self, code, start, end):
        rows = fetch_raw_rows(code, start, end)
        return [{"date": row[0], "open": row[1], "close": row[2], "high": row[3],
                 "low": row[4], "volume": str(_lots_to_shares(row[5]))} for row in rows]


def _lots_to_shares(value):
    from backend.replay_models import number
    return number(value) * 100


@dataclass(frozen=True)
class QuoteBundle:
    stock: str
    start: date
    end: date
    bars: tuple[DailyBar, ...]
    source: str
    source_version: str
    updated_at: str
    data_version: str
    synthetic: bool


class QuoteRepository:
    """Internal unrestricted store; NEVER pass this object to charts or clients.

    Cache key includes source/version, stock and exact request range. Each file
    retains its own fetch time. A one-day repair cannot freshen an older range.
    Normal cache age is 24h; callers can pin historical caches with max_age=None.
    Failed refreshes never overwrite a previously valid artifact.
    """

    def __init__(self, cache_dir, source: DailyQuoteSource | None = None, *, max_age=timedelta(hours=24)):
        self.source = source or TencentDailySource()
        self.cache_dir = Path(cache_dir) / "portfolio_replay" / "quotes"
        self.max_age = max_age
        self._attempted = set()
        self._observations = []

    def _record(self, code, start, end, bundle=None, error=None):
        self._observations.append((code, start, end, bundle, error))
        return bundle

    def _key(self, code, start, end):
        return content_hash([self.source.name, self.source.version, code, str(start), str(end)])

    def prime(self, code, start, end, *, refresh=False, offline=False):
        """Prepare a bounded range. Missing bars remain unknown, never suspension."""
        code, start, end = stock_code(code), day(start), day(end)
        if end < start:
            raise ReplayDataError("结束日期不能早于开始日期")
        key = self._key(code, start, end)
        path = self.cache_dir / f"{key}.json"
        if not refresh and path.exists():
            try:
                payload = read_cache(path)
                identity = (payload["stock"], payload["requested_start"], payload["requested_end"],
                            payload["source"], payload["source_version"], payload["synthetic"])
                if identity != (code, str(start), str(end), self.source.name, self.source.version, self.source.synthetic):
                    raise ReplayDataError("缓存来源或请求范围不匹配")
                if payload["price_basis"] != "unadjusted" or payload["volume_unit"] != "share":
                    raise ReplayDataError("缓存价格口径或成交量单位不匹配")
                updated = timestamp(payload["updated_at"])
                age = datetime.now(timezone.utc) - updated
                if age < timedelta(0) or (self.max_age is not None and age > self.max_age):
                    raise ReplayDataError("行情缓存已过期，需要重新获取")
                bars = validate_bars(code, payload["bars"], start, end)
                if payload["data_version"] != content_hash([bar.to_dict() for bar in bars]):
                    raise ReplayDataError("行情内容版本不匹配")
                bundle = QuoteBundle(code, start, end, bars, self.source.name, self.source.version,
                                     payload["updated_at"], payload["data_version"], self.source.synthetic)
                return self._record(code, start, end, bundle)
            except (ReplayDataError, KeyError, TypeError):
                if offline:
                    return self._record(code, start, end, error=(DataStatus.CACHE_MISSING, "行情缓存损坏或过期，离线状态无法补取"))
        if offline:
            return self._record(code, start, end, error=(DataStatus.CACHE_MISSING, "缺少有效行情缓存，请联网补取"))
        try:
            rows = self.source.fetch(code, str(start), str(end))
        except ReplayDataError:
            return self._record(code, start, end, error=(DataStatus.INVALID, "行情未通过校验，禁止成交；请核对来源后刷新"))
        except Exception:
            return self._record(code, start, end, error=(DataStatus.DOWNLOAD_FAILED, "行情下载失败，禁止按旧价格成交；请刷新重试"))
        try:
            bars = validate_bars(code, rows, start, end)
        except ReplayDataError:
            return self._record(code, start, end, error=(DataStatus.INVALID, "行情未通过校验，禁止成交；请核对来源后刷新"))
        updated_at = utc_now()
        version = content_hash([bar.to_dict() for bar in bars])
        payload = {"stock": code, "requested_start": str(start), "requested_end": str(end),
                   "actual_start": str(bars[0].date) if bars else None,
                   "actual_end": str(bars[-1].date) if bars else None,
                   "source": self.source.name, "source_version": self.source.version,
                   "synthetic": self.source.synthetic, "updated_at": updated_at,
                   "price_basis": "unadjusted", "volume_unit": "share", "data_version": version,
                   "bars": [bar.to_dict() for bar in bars]}
        try:
            write_cache(path, payload)
        except OSError:
            return self._record(code, start, end, error=(DataStatus.CACHE_MISSING, "行情缓存写入失败，未建立可复现数据版本"))
        bundle = QuoteBundle(code, start, end, bars, self.source.name, self.source.version, updated_at, version, self.source.synthetic)
        # Explicit successful refresh permits missing dates to be retried.
        self._attempted.difference_update({pair for pair in self._attempted if pair[0] == code and start <= pair[1] <= end})
        return self._record(code, start, end, bundle)

    def quote(self, code, value, calendar, security: SecurityHistory | None, *, repair=True, synthetic=False):
        code, value = stock_code(code), day(value)

        def state(status, reason, bar=None, bundle=None):
            status_evidence = security.evidence if security is not None and security.stock == code else None
            return QuoteState(code, value, status, reason, bar,
                              bundle.source if bundle else None, bundle.updated_at if bundle else None,
                              bundle.data_version if bundle else None,
                              status_evidence.source if status_evidence else None,
                              str(status_evidence.version) if status_evidence else None)

        if self.source.synthetic and not synthetic:
            return state(DataStatus.UNSUPPORTED, "合成数据仅限显式测试模式使用")
        calendar.require_range(value, value, synthetic=synthetic)
        if not calendar.is_open(value):
            return state(DataStatus.MARKET_CLOSED, "公共交易日历显示该日休市")
        if security is None or security.stock != code or not security.evidence.covers(value, value, synthetic=synthetic):
            return state(DataStatus.UNKNOWN, "该日上市、停牌及特殊历史状态尚未核实，禁止成交")
        if value < security.listed_on:
            return state(DataStatus.NOT_LISTED, "该日尚未上市")
        if security.delisted_on is not None and value >= security.delisted_on:
            return state(DataStatus.DELISTED, "该日已退市")
        if any(a <= value <= b for a, b, _ in security.special_periods):
            return state(DataStatus.UNSUPPORTED, "该日属于尚未支持的特殊交易状态")
        if value in security.suspended:
            return state(DataStatus.SUSPENDED, "历史状态记录已确认停牌，禁止成交")

        def latest():
            observation = next((item for item in reversed(self._observations)
                                if item[0] == code and item[1] <= value <= item[2]), None)
            return observation[3:] if observation else (None, None)

        bundle, error = latest()
        bar = next((bar for bar in bundle.bars if bar.date == value), None) if bundle else None
        # A failed explicit refresh invalidates execution even if old prices exist.
        if repair and (bar is None or error) and (code, value) not in self._attempted:
            self.prime(code, value, value, refresh=True)
            self._attempted.add((code, value))
            bundle, error = latest()
            bar = next((bar for bar in bundle.bars if bar.date == value), None) if bundle else None
        if error:
            return state(*error)
        if bar is None:
            if bundle is None:
                return state(DataStatus.CACHE_MISSING, "缺少行情缓存，请补取并校验后重试")
            return state(DataStatus.UNKNOWN, "补取后仍无该日行情，原因不明；不能直接判为停牌，禁止成交", bundle=bundle)
        if not calendar.is_open(bar.date):
            return state(DataStatus.INVALID, "行情日期与公共交易日历冲突")
        if bar.volume == 0:
            return state(DataStatus.NO_TRADES, "该日成交量为零，禁止模拟成交", bar, bundle)
        return state(DataStatus.TRADING, "有效未复权日线；成交仍须通过订单规则校验", bar, bundle)
