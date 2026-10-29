"""Trusted backend evidence import; HTTP clients cannot assert completeness."""

import json
from pathlib import Path

from backend.portfolio_replay import PortfolioReplay
from backend.replay_calendar import load_public_calendar
from backend.replay_data import QuoteRepository, TencentDailySource
from backend.replay_models import ActionHistory, CorporateAction, Evidence, ReplayDataError, SecurityHistory


def replay_factory(data_dir, *, allow_demo=False, stock_names=None, data_manager=None):
    def manager():
        nonlocal data_manager
        if data_manager is None:
            from backend.data_manager import DataManager
            data_manager = DataManager(str(data_dir))
        return data_manager

    def build(params):
        if params["data_source"] in {"tencent", "akshare", "xtdata", "mootdx", "offline"}:
            from backend.portfolio_practice import build_practice
            return build_practice(data_dir, manager(), params)
        if params["data_source"] == "synthetic-practice":
            if not allow_demo:
                raise ReplayDataError("合成数据仅可在显式启用的开发验收模式使用")
            from backend.portfolio_demo import practice_replay
            return practice_replay(Path(data_dir) / "portfolio_practice", params)
        if params["data_source"] == "synthetic-demo":
            if not allow_demo:
                raise ReplayDataError("合成数据仅可在显式启用的开发验收模式使用")
            from tests.replay_fixture import fixture
            _, calendar, securities, actions, _, quotes = fixture(Path(data_dir) / "portfolio_demo")
            return PortfolioReplay(params["stocks"], params["start_date"], params["end_date"], calendar,
                                   quotes, securities, actions, synthetic=True, accounting=True)
        if params["data_source"] not in {"tencent", "sina"}:
            raise ReplayDataError("不支持的组合数据源")
        path = Path(data_dir) / "portfolio_reviewed" / "history.json"
        if not path.is_file():
            raise ReplayDataError("该真实区间缺少后端核验的历史状态及公司行为完整性证明，暂不能创建组合训练")
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if raw["schema_version"] != 1:
                raise ValueError("schema")
            securities, actions = {}, {}
            for code in params["stocks"]:
                security, action = raw["securities"][code], raw["actions"][code]
                securities[code] = SecurityHistory(**{**security, "evidence": Evidence(**security["evidence"])})
                actions[code] = ActionHistory(code, Evidence(**action["evidence"]),
                                             tuple(CorporateAction(**item) for item in action["events"]))
                if securities[code].stock != code or any(e.scope != "reviewed" for e in (securities[code].evidence, actions[code].evidence)):
                    raise ValueError("evidence")
                if not securities[code].evidence.covers(params["start_date"], params["end_date"]):
                    raise ReplayDataError(f"{code}：历史状态核验范围不覆盖请求区间")
                actions[code].require_supported(params["start_date"], params["end_date"], accounting=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise ReplayDataError("后端核验文件不完整或版本不受支持，不能使用浏览器声明代替") from exc
        calendar = load_public_calendar(data_dir)
        source = TencentDailySource()
        if params["data_source"] == "sina":
            from backend.portfolio_real_data import ReviewedSinaSource
            source = ReviewedSinaSource(path.parent, raw.get("receipts", {}))
            if any(code not in source.receipts for code in params["stocks"]):
                raise ReplayDataError("所选股票尚未接入新浪核验行情，请先更新真实数据")
        quotes = QuoteRepository(data_dir, source)
        return PortfolioReplay(params["stocks"], params["start_date"], params["end_date"], calendar,
                               quotes, securities, actions, accounting=True)
    def options(query="", source="tencent"):
        from backend.replay_models import stock_code
        from backend.portfolio_rules import FeeSchedule
        sources = [{"id": row["value"], "name": row["label"]}
                   for row in manager().get_available_sources() if row["available"]]
        # Legacy reviewed runs remain restorable, but new exercises use the
        # same source choices as the single-stock setup.
        if allow_demo:
            sources.append({"id": "synthetic-demo", "name": "合成演示 · 非真实证券历史"})
            sources.append({"id": "synthetic-practice", "name": "合成练习 · 多月均线与自动播放"})
        if source != "sina" and source not in {item["id"] for item in sources}:
            raise ReplayDataError("该数据源未启用；合成演示需以 --portfolio-demo 启动")
        names = (stock_names() if stock_names else manager().stock_names) or {}
        if not names:
            cached_names = Path(data_dir) / "stock_names.json"
            if cached_names.is_file():
                try:
                    names = json.loads(cached_names.read_text(encoding="utf-8-sig"))
                    if not isinstance(names, dict):
                        names = {}
                except ValueError:
                    names = {}
        coverage, reviewed_names, updated_at = {}, {}, None
        if source == "synthetic-practice":
            from backend.portfolio_demo import NAMES, START, END
            names = NAMES
            coverage = {code: {"start": START, "end": END} for code in names}
        elif source == "synthetic-demo":
            names = {"600000": "合成甲", "000001": "合成乙", "600036": "合成丙"}
            coverage = {code: {"start": "2024-01-02", "end": "2024-01-10"} for code in names}
        else:
            path = Path(data_dir) / "portfolio_reviewed" / "history.json"
            if path.is_file():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8-sig"))
                    reviewed_names = raw.get("names", {})
                    updated_at = raw.get("updated_at")
                    for code, security in raw.get("securities", {}).items():
                        if source == "sina" and code not in raw.get("receipts", {}):
                            continue
                        a = security["evidence"]
                        b = raw["actions"][code]["evidence"]
                        if a["scope"] == b["scope"] == "reviewed":
                            coverage[code] = {"start": max(a["start"], b["start"]),
                                              "end": min(a["end"], b["end"])}
                except (ValueError, KeyError, TypeError):
                    pass  # Preview/create performs strict validation; catalog is advisory.
            # Do not leak the developer fixture's SYNTHETIC names into real data.
            names = {code:name for code,name in names.items() if not str(name).startswith(("SYNTHETIC-", "合成", "练习"))}
            names = {**{code: code for code in coverage}, **names, **reviewed_names}
            if source == "sina":
                from backend.portfolio_real_data import BANKS
                names = {**{code:value[0] for code,value in BANKS.items()}, **names}
            else:
                coverage, updated_at = {}, None
        candidates = dict(names)
        if len(query) == 6 and query.isdigit():
            candidates.setdefault(query, query)
        rows = []
        for code, name in sorted(candidates.items()):
            if query and query.lower() not in (code + str(name)).lower():
                continue
            try:
                stock_code(code)
                reason = "" if code in coverage else ("开始时自动加载所选日期的行情" if source != "sina" else "尚无已核验历史区间")
            except ReplayDataError as exc:
                reason = str(exc)
            rows.append({"stock": code, "name": str(name), "coverage": coverage.get(code), "reason": reason})
            if len(rows) == 50:
                break
        periods = FeeSchedule().periods
        suggested = {"start":max(row["start"] for row in coverage.values()),
                     "end":min(row["end"] for row in coverage.values())} if coverage else None
        return {"sources": sources, "stocks": rows, "suggested_range":suggested, "updated_at":updated_at, "fee_range": {
            "start": str(periods[0].start), "end": str(periods[-1].end)},
            "note": "可自由选择股票和日期，开始时自动加载日线；非交易日起止将对齐区间内交易日。"}

    def preview(params):
        from backend.portfolio_engine import PortfolioEngine
        if params["data_source"] in {"tencent", "akshare", "xtdata", "mootdx", "offline"}:
            rows = []
            for code in params["stocks"]:
                try:
                    build({**params, "stocks": [code]})
                    rows.append({"stock": code, "supported": True, "reason": "行情已就绪"})
                except ReplayDataError as exc:
                    rows.append({"stock": code, "supported": False, "reason": str(exc)})
            return {"supported": bool(rows) and all(row["supported"] for row in rows), "stocks": rows,
                    "start_date": params["start_date"], "end_date": params["end_date"], "data_source": params["data_source"]}
        rows = []
        for code in params["stocks"]:
            try:
                replay = build({**params, "stocks": [code]})
                # Inspect the whole requested coverage internally; publish no prices.
                probe = replay._engine_fork()
                probe._engine_seek(probe._engine_end_date())
                engine = PortfolioEngine(replay, params["initial_cash"])
                engine.close()
                rows.append({"stock": code, "supported": True, "reason": "所选区间校验通过"})
            except ReplayDataError as exc:
                rows.append({"stock": code, "supported": False, "reason": str(exc)})
        return {"supported": all(row["supported"] for row in rows), "stocks": rows,
                "start_date": params["start_date"], "end_date": params["end_date"],
                "data_source": params["data_source"]}

    from backend.portfolio_real_data import prepare_real_data
    build.options, build.preview = options, preview
    build.prepare_real_data = lambda: prepare_real_data(data_dir)
    return build
