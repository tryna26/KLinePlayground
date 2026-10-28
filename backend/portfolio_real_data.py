"""Source-reviewed admission for a small real bank pool, never a blanket bypass.

The reviewed 2024 window ends before Ping An's 13 June dividend record date.
Other securities/windows still require the existing trusted evidence import.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import StringIO
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unicodedata

import pandas as pd
import requests

from backend.portfolio_models import serialize
from backend.replay_calendar import load_public_calendar
from backend.replay_models import ActionHistory, Evidence, ReplayDataError, SecurityHistory, day, utc_now, validate_bars
from backend.replay_storage import content_hash, read_cache, write_cache
from backend.sina_source import SinaDailySource


START, END, VERSION = "2024-01-02", "2024-06-12", "reviewed-bank-2024-h1-v1"
BANKS = {"600000":("浦发银行","1999-11-10"), "000001":("平安银行","1991-04-03"), "600036":("招商银行","2002-04-09")}
REFERENCES = {
    "600000":"https://news.spdb.com.cn/investor_relation/periodic_report/202408/P020240819612520255318.pdf",
    "000001":"https://epaper.cs.com.cn/zgzqb/html/2024-06/06/nw.D110000zgzqb_20240606_5-B050.htm",
    "600036":"https://www.sse.com.cn/disclosure/listedinfo/announcement/c/new/2024-08-30/600036_20240830_BGYA.pdf",
}
_prepare_lock = threading.Lock()


def _request(url, *, params=None, headers=None, post=False):
    with requests.Session() as session:
        session.trust_env = False
        response = session.request("POST" if post else "GET", url, params=params,
            headers={"User-Agent":"Mozilla/5.0", **(headers or {})}, timeout=(6,15))
        response.raise_for_status()
        if "sina.com.cn" in url:
            response.encoding = "gb18030"
        return response


def cninfo_request(endpoint, code):
    from akshare.stock.stock_dividend_cninfo import _get_file_content_ths
    from backend.local_js import call_local_script
    key = call_local_script(_get_file_content_ths(), "getResCode1")
    response = _request("https://webapi.cninfo.com.cn/api/"+endpoint, post=True,
        params={"scode":code,"sdate":"2024-01-01","edate":"2024-06-30"},
        headers={"Accept-Enckey":key,"Origin":"https://webapi.cninfo.com.cn",
                 "Referer":"https://webapi.cninfo.com.cn/","X-Requested-With":"XMLHttpRequest"})
    data = response.json()
    records = data.get("records")
    if (not isinstance(records,list) or data.get("resultcode") != 200
            or int(data.get("count",-1)) != len(records) or int(data.get("total",len(records))) != len(records)):
        raise ReplayDataError(f"{code}：巨潮资料缺失、分页不完整或返回格式改变")
    return data


def sina_company_records(code):
    info_url=f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CorpInfo/stockid/{code}.phtml"
    actions_url=f"https://vip.stock.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/{code}.phtml"
    info = pd.read_html(StringIO(_request(info_url).text))[3].fillna("")
    old_names = [str(row[1]) for row in info.values if "证券简称更名历史" in str(row[0])]
    frames = pd.read_html(StringIO(_request(actions_url).text))
    dividends, rights = frames[12].fillna(""), frames[13].fillna("")
    return {"old_names":old_names, "dividends":dividends.astype(str).values.tolist(),
            "rights":rights.astype(str).values.tolist(), "urls":[info_url,actions_url]}


def review_facts(code, facts, calendar):
    """Deterministic gate for the individually reviewed bank/window definitions."""
    name, listed = BANKS[code]
    profile = facts["profile"]["records"]
    if len(profile)!=1 or profile[0].get("ASECCODE")!=code or profile[0].get("ASECNAME")!=name or profile[0].get("F006D")!=listed:
        raise ReplayDataError(f"{code}：公司身份、名称或上市日期与已复核证券不一致")
    old = facts["sina"]["old_names"]
    if not old or any(not str(value).strip() for value in old):
        raise ReplayDataError(f"{code}：缺少独立股票曾用名记录，不能核验特殊状态")
    # Check complete former-name records from BOTH sources, not just today's name.
    all_names = old + [profile[0].get("F002V") or "", name]
    if any(re.search(r"ST|退市|退$|\*",unicodedata.normalize("NFKC",value).upper()) for value in all_names):
        raise ReplayDataError(f"{code}：曾用名涉及特殊状态，需单独逐日期核验")
    event_dates = []
    for event in facts["dividends"]["records"]:
        dates = [day(event[key]) for key in ("F018D","F020D","F023D","F025D") if event.get(key)]
        if not dates:
            raise ReplayDataError(f"{code}：巨潮存在缺少实施日期的公司行为")
        event_dates += dates
    sina_dates = []
    for row in facts["sina"]["dividends"]:
        if len(row)<8:
            raise ReplayDataError(f"{code}：新浪分红表结构变化")
        # Pending proposals aren't executed actions; require the implemented rows.
        if row[4] == "实施":
            dates = [day(row[i]) for i in (5,6,7) if re.fullmatch(r"\d{4}-\d{2}-\d{2}",row[i])]
            if not dates:
                raise ReplayDataError(f"{code}：新浪已实施分红缺少日期")
            event_dates += dates
            sina_dates += dates
        elif row[4] not in {"预案","股东大会预案","取消","未实施"}:
            if row[4] != "不分配" or any(float(row[i]) != 0 for i in (1,2,3)):
                raise ReplayDataError(f"{code}：新浪分红进度无法确认")
    rights = facts["sina"]["rights"]
    if not rights:
        raise ReplayDataError(f"{code}：未取得配股表，不能把空请求当作无事件")
    if rights[0][0] != "暂时没有数据！":
        for row in rights:
            dates = [day(value[:10]) for value in row[4:9] if re.match(r"\d{4}-\d{2}-\d{2}",value)]
            if not dates:
                raise ReplayDataError(f"{code}：配股事件日期不完整")
            event_dates += dates
    if any(day(START)<=value<=day(END) for value in event_dates):
        raise ReplayDataError(f"{code}：核验区间含公司行为，不能沿用无事件窗口")
    # Each source must bracket the window with actual implemented dividends.
    independent = facts["dividends"]["records"]
    dates = [day(row["F020D"]) for row in independent if row.get("F020D")]
    if any(not values or min(values)>=day(START) or max(values)<=day(END) for values in (dates,sina_dates)):
        raise ReplayDataError(f"{code}：分红历史未覆盖核验窗口前后")
    if not facts["capital"]["records"]:
        raise ReplayDataError(f"{code}：股本核验资料为空")
    for row in facts["capital"]["records"]:
        if row.get("SECCODE")!=code:
            raise ReplayDataError("股本资料证券不匹配")
        if not set(str(row.get("F002V","")).split(",")) <= {"定期报告","可转债转股"}:
            raise ReplayDataError(f"{code}：股本变动涉及未复核原因")
        # Third-party bond conversion doesn't grant/replace the trainee's shares.
    bars = validate_bars(code,facts["quotes"],START,END)
    expected = set(calendar.between(START,END))
    if {bar.date for bar in bars}!=expected or any(bar.volume<=0 for bar in bars):
        raise ReplayDataError(f"{code}：真实行情未逐日覆盖公共日历，不能假定缺口是停牌")
    return bars


def _collect(code):
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs={"profile":pool.submit(cninfo_request,"sysapi/p_sysapi1133",code),
              "dividends":pool.submit(cninfo_request,"sysapi/p_sysapi1139",code),
              "capital":pool.submit(cninfo_request,"stock/p_stock2215",code),
              "sina":pool.submit(sina_company_records,code),
              "quotes":pool.submit(SinaDailySource().fetch,code,START,END)}
        return {key:future.result() for key,future in jobs.items()}


def _write_manifest(path, payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent,prefix=".review-",suffix=".tmp")
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as stream:
            json.dump(payload,stream,ensure_ascii=False,allow_nan=False,sort_keys=True)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def prepare_real_data(data_dir):
    """Publish all three admissions only after the complete review succeeds."""
    if not _prepare_lock.acquire(blocking=False):
        raise ReplayDataError("真实数据正在更新，请等待当前更新完成")
    try:
        root=Path(data_dir)/"portfolio_reviewed"
        calendar=load_public_calendar(data_dir)
        with ThreadPoolExecutor(max_workers=3) as pool:
            collected=list(pool.map(_collect,BANKS))
        results={code:(facts,review_facts(code,facts,calendar)) for code,facts in zip(BANKS,collected)}
        path=root/"history.json"
        old=json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {"schema_version":1,"securities":{},"actions":{},"names":{},"receipts":{}}
        if old.get("schema_version")!=1:
            raise ReplayDataError("现有真实准入文件版本不兼容，未覆盖")
        now=utc_now()
        for code,(facts,bars) in results.items():
            receipt={"version":VERSION,"stock":code,"start":START,"end":END,"fetched_at":now,
                     "facts":facts,"quotes":[bar.to_dict() for bar in bars],"reference":REFERENCES[code]}
            checksum=content_hash(receipt)
            receipt_path=f"receipts/{code}-{checksum}.json"
            write_cache(root/receipt_path,receipt)
            evidence=Evidence(START,END,"巨潮公司概况/历史分红/股本 + 新浪曾用名/分红配股 + 独立公共日历及逐日真实行情",VERSION,
                f"{REFERENCES[code]}；源资料核验回执 SHA256={checksum}；仅限该银行的无权益事件窗口",now)
            old["securities"][code]=serialize(SecurityHistory(code,BANKS[code][1],evidence))
            old["actions"][code]=serialize(ActionHistory(code,evidence))
            old.setdefault("names",{})[code]=BANKS[code][0]
            old.setdefault("receipts",{})[code]={"path":receipt_path,"sha256":checksum}
        old["updated_at"]=now
        _write_manifest(path,old)
        return {"source":"sina","start":START,"end":END,"stocks":[{"stock":c,"name":n[0]} for c,n in BANKS.items()],"updated_at":now,
                "note":"真实未复权日线已接通；仅开放已复核的三只银行股、2024-01-02 至 2024-06-12。"}
    except (requests.RequestException,ValueError,KeyError,IndexError,TypeError,OSError) as exc:
        if isinstance(exc,ReplayDataError):raise
        raise ReplayDataError("真实数据更新或来源核验失败，未扩大交易范围；请稍后重试") from exc
    finally:
        _prepare_lock.release()


class ReviewedSinaSource:
    """Read exactly the checked real observations, with integrity validation."""
    name="sina-reviewed"
    version=VERSION
    synthetic=False

    def __init__(self,root,receipts):
        self.root,self.receipts=Path(root).resolve(),receipts
        self.version = VERSION + ":" + content_hash(receipts)

    def fetch(self,code,start,end):
        record=self.receipts[code]
        path=(self.root/record["path"]).resolve()
        if not path.is_relative_to(self.root):
            raise ReplayDataError("真实行情回执路径无效")
        payload=read_cache(path)
        if (content_hash(payload)!=record["sha256"] or payload["stock"]!=code
                or payload["version"]!=VERSION or not payload["start"]<=start<=end<=payload["end"]):
            raise ReplayDataError("真实行情回执不匹配或已损坏")
        return [row for row in payload["quotes"] if start<=row["date"]<=end]
