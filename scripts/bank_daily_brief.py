#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股银行板块每日背景材料收料器 (bank daily brief)

定位: 为银行股分析提供定性背景素材层 —— 只收料、不做判断;
主报告(bank_analysis.py)保持纯量化, 本脚本产物供人阅读/会话内分析引用。

四模块:
  1. 政策与金融数据 — LPR / 社融增量 / M2·M1 (SQLite 逐日累积, 变动自动标记)
  2. 池内个股公告   — 42家银行 cninfo 公告按类别关键词过滤(资本/分红/股东/治理/业绩)
  3. 板块新闻       — 东财个股新闻(stock_news_em)逐行扫描池内银行
  4. 资金面与研报   — 大宗交易每日明细(池内) + 东财研报评级(池内, 快照内最新)

产物:
  workspace/brief/YYYY-MM-DD.md      人读摘要(要点+四节清单+缺失源说明)
  workspace/brief/raw/YYYY-MM-DD/*.json  各模块原始数据(可追溯)
  workspace/brief.db                 宏观时序累积(LPR/社融/M2)

用法:
    python3 scripts/bank_daily_brief.py            # 收当日(含往前days天)素材
    python3 scripts/bank_daily_brief.py --days 4   # 扩大公告/新闻回看窗口

约90次接口调用(42公告+42新闻+宏观/大宗/研报), 全程约1-2分钟; 单源失败自动降级
并在摘要"缺失源"节标注, 不影响其余模块。
"""

import argparse
import json
import os
import re
import sqlite3
import ssl
import sys
import time
from datetime import datetime, timedelta

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from bank_universe import BANKS
from bank_data_store import WORKSPACE

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

BRIEF_DIR = os.path.join(WORKSPACE, "brief")
RAW_DIR = os.path.join(BRIEF_DIR, "raw")
DB_PATH = os.path.join(WORKSPACE, "brief.db")

NEWS_PER_BANK = 3          # 每家银行最多保留条数(窗口内)
NEWS_TOTAL_CAP = 60
ANN_CAP = 80               # 公告清单上限(按日期降序截断)
RESEARCH_CAP = 30
DZJY_BACKTRACK = 4         # 大宗交易向前回溯的交易日探测天数

# 公告类别 → 标题关键词(命中即归类; 顺序即优先级, 一题只归一类)
ANN_CATEGORIES = [
    ("资本", r"定增|定向增发|向特定对象发行|配股|可转换公司债券|资本债券|永续债|"
             r"二级资本债|注资|增资|发行金融债券|战略投资者|认购协议"),
    ("分红", r"分红|派息|利润分配|权益分派|股东回报规划"),
    ("股东", r"增持|减持|回购|要约收购|权益变动|股权激励"),
    ("治理", r"离任|聘任|辞职|换届|任职资格|高管变更"),
    ("业绩", r"业绩快报|业绩预告|季度报告|半年度报告|年度报告"),
]


# ============================================================
# 纯计算函数(可离线测试)
# ============================================================

def classify_announcement(title):
    """公告标题 → 类别(资本/分红/股东/治理/业绩) 或 None(与投资无关, 丢弃)"""
    clean = re.sub(r"</?em>", "", str(title))
    for cat, pattern in ANN_CATEGORIES:
        if re.search(pattern, clean):
            return cat
    return None


def trim_news(rows, now=None, days=3, per_bank=NEWS_PER_BANK, total_cap=NEWS_TOTAL_CAP):
    """原始新闻行 → 窗口内新闻: 按发布时间降序、每银行限 per_bank 条、总量限 total_cap。
    rows: [{code,name,title,time,src,url}]; time 兼容 'YYYY-MM-DD[ HH:MM:SS]'。"""
    now = now or datetime.now()
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    valid = [r for r in rows if str(r.get("time", ""))[:10] >= cutoff]
    valid.sort(key=lambda r: str(r.get("time", "")), reverse=True)
    kept, counts = [], {}
    for r in valid:
        c = r["code"]
        if counts.get(c, 0) >= per_bank:
            continue
        counts[c] = counts.get(c, 0) + 1
        kept.append(r)
    return kept[:total_cap]


def latest_trade_date(today=None, backtrack=DZJY_BACKTRACK):
    """从 today 起向前回溯 backtrack-1 天的候选日期串(YYYYMMDD)。
    大宗交易明细按日请求, 周末/节假日返回空由调用方逐候选尝试。"""
    today = today or datetime.now()
    return [(today - timedelta(days=i)).strftime("%Y%m%d")
            for i in range(max(1, backtrack))]


def month_key(v):
    """'202603' / '2026年03月份' / '2026年3月' → '2026-03' (排序/显示统一); 解析失败→None"""
    m = re.search(r"(\d{4})\D*(\d{1,2})", str(v))
    return f"{m.group(1)}-{int(m.group(2)):02d}" if m else None


def _esc(s):
    return str(s).replace("|", "/").replace("\n", " ")


# ============================================================
# 数据获取(网络, 单源失败返回空并记录)
# ============================================================

class Brief:
    """收集器: 各 fetch_* 失败时写 self.missing 并返回空数据"""

    def __init__(self, days=3):
        self.days = days
        self.missing = []
        self.now = datetime.now()

    def _fail(self, label, e):
        self.missing.append(f"{label}: {type(e).__name__} {str(e)[:60]}")
        return None

    # ---- 模块1: 宏观与政策数据 ----
    def fetch_macro(self):
        import akshare as ak
        out = {}
        try:
            df = ak.macro_china_lpr()
            df = df.sort_values("TRADE_DATE")
            out["lpr"] = df.tail(2)[["TRADE_DATE", "LPR1Y", "LPR5Y"]].to_dict("records")
            last_d = str(df["TRADE_DATE"].iloc[-1])[:10]
            age = (self.now - datetime.strptime(last_d, "%Y-%m-%d")).days
            if age > 45:
                self.missing.append(f"LPR数据陈旧(接口最新{last_d})")
        except Exception as e:
            self._fail("LPR", e)
        try:
            df = ak.macro_china_shrzgm()
            df["_k"] = df["月份"].map(month_key)
            df = df.dropna(subset=["_k"]).sort_values("_k")
            out["shrzgm"] = df.tail(2)[["_k", "社会融资规模增量", "其中-人民币贷款"]].to_dict("records")
            self._stale_month("社融", df["_k"].iloc[-1])
        except Exception as e:
            self._fail("社融", e)
        try:
            df = ak.macro_china_money_supply()
            df["_k"] = df["月份"].map(month_key)
            df = df.dropna(subset=["_k"]).sort_values("_k")
            cols = {"_k": "_k",
                    "货币和准货币(M2)-数量(亿元)": "m2",
                    "货币和准货币(M2)-同比增长": "m2_yoy",
                    "货币(M1)-数量(亿元)": "m1",
                    "货币(M1)-同比增长": "m1_yoy"}
            out["m2"] = df[list(cols)].rename(columns=cols).tail(2).to_dict("records")
            self._stale_month("M2", df["_k"].iloc[-1])
        except Exception as e:
            self._fail("M2", e)
        try:
            # 社融口径"对实体经济贷款"当月值(金十源), 比 shrzgm 主表更新鲜;
            # 2026-09 实测主表滞后约4个月而本接口同步至上一月, 互为补充
            df = ak.macro_china_new_financial_credit()
            df["_k"] = df["月份"].map(month_key)
            df = df.dropna(subset=["_k"]).sort_values("_k")
            out["rmb_loan"] = df.tail(4)[["_k", "当月", "当月-同比增长"]].to_dict("records")
            self._stale_month("新增贷款", df["_k"].iloc[-1])
        except Exception as e:
            self._fail("新增贷款", e)
        return out

    def _stale_month(self, label, latest_k):
        """月度宏观数据落后当前月超过2个月 → 显性标注(月度数据上月月中发布, 2个月内属正常)"""
        now_k = self.now.year * 12 + self.now.month
        y, m = latest_k.split("-")
        if now_k - (int(y) * 12 + int(m)) > 2:
            self.missing.append(f"{label}数据陈旧(接口最新{latest_k})")

    # ---- 模块2: 池内公告(cninfo, 逐行请求) ----
    def fetch_announcements(self):
        import akshare as ak
        start = (self.now - timedelta(days=self.days)).strftime("%Y%m%d")
        end = self.now.strftime("%Y%m%d")
        rows, failures = [], 0
        for code, meta in BANKS.items():
            got = None
            for attempt in range(2):       # cninfo 对高频请求限流, 失败重试一次
                try:
                    df = ak.stock_zh_a_disclosure_report_cninfo(
                        symbol=code, market="沪深京", keyword="",
                        start_date=start, end_date=end)
                    got = df if df is not None and len(df) else None
                    break
                except Exception:
                    if attempt == 1:
                        failures += 1
                    time.sleep(1.2)
            if got is not None:
                for _, r in got.iterrows():
                    rows.append({"code": code, "name": meta["n"],
                                 "title": str(r.get("公告标题", "")),
                                 "date": _ann_date(r)})
            time.sleep(0.45)
        if failures > len(BANKS) * 0.3:    # 限流导致的静默缺失必须显性化
            self.missing.append(f"公告(cninfo限流, {failures}家失败, 结果不完整)")
        cats = [{"code": r["code"], "name": r["name"], "date": r["date"],
                 "cat": classify_announcement(r["title"]), "title": r["title"]}
                for r in rows]
        cats = [c for c in cats if c["cat"]]
        cats.sort(key=lambda x: (x["date"], x["code"]), reverse=True)
        return cats[:ANN_CAP]

    # ---- 模块3: 板块新闻(东财个股新闻) ----
    def fetch_news(self):
        import akshare as ak
        rows = []
        for code, meta in BANKS.items():
            try:
                df = ak.stock_news_em(symbol=code)
                for _, r in (df.iterrows() if df is not None and len(df) else []):
                    rows.append({"code": code, "name": meta["n"],
                                 "title": str(r.get("新闻标题", "")),
                                 "time": str(r.get("发布时间", ""))[:19],
                                 "src": str(r.get("文章来源", "")),
                                 "url": str(r.get("新闻链接", ""))})
            except Exception:
                pass
            time.sleep(0.25)
        return trim_news(rows, now=self.now, days=self.days)

    # ---- 模块4a: 大宗交易(池内) ----
    def fetch_dzjy(self):
        import akshare as ak
        last_err = None
        for d in latest_trade_date(self.now):
            try:
                df = ak.stock_dzjy_mrmx(symbol="A股", start_date=d, end_date=d)
            except Exception as e:
                last_err = e              # 当日未发布/接口抖动 → 试前一交易日
                time.sleep(0.5)
                continue
            if df is None or len(df) == 0:
                continue
            date_str = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            df = df[df["证券代码"].astype(str).isin(BANKS)]
            if len(df) == 0:
                return {"date": date_str, "rows": []}   # 最新交易日池内无大宗
            return {"date": date_str,
                    "rows": df[["证券代码", "证券简称", "成交价", "折溢率",
                                "成交量", "成交额", "买方营业部", "卖方营业部"]]
                    .to_dict("records")}
        if last_err:
            self._fail("大宗交易", last_err)
        return {"date": None, "rows": []}

    # ---- 模块4b: 研报评级(池内, 接口快照内最新N份) ----
    def fetch_research(self):
        import akshare as ak
        try:
            df = ak.stock_research_report_em()
            # 该接口为全市场滚动快照(约200余行), 银行研报可能数日无新增, 属正常
            df = df[df["股票代码"].astype(str).isin(BANKS)]
            df = df.sort_values("日期", ascending=False).head(RESEARCH_CAP)
            cols = ["日期", "股票代码", "股票简称", "报告名称", "东财评级", "机构",
                    "2026-盈利预测-收益"]
            return df[cols].rename(
                columns={"2026-盈利预测-收益": "2026EPS预测"}).to_dict("records")
        except Exception as e:
            self._fail("研报", e)
            return []


def _ann_date(row):
    """cninfo 行 → 公告日期: 优先日期列, 否则从链接 announcementTime 提取"""
    for col in ("公告日期", "公告时间", "announcementTime"):
        v = row.get(col)
        if v is not None and str(v)[:4].isdigit():
            return str(v)[:10]
    m = re.search(r"announcementTime=([\d-]+)", str(row.get("公告链接", "")))
    return m.group(1)[:10] if m else ""


# ============================================================
# SQLite 宏观时序累积
# ============================================================

def accumulate_macro(data):
    """把本次抓到的 LPR/社融/M2 追加进 brief.db, 返回各表累计行数"""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS macro_lpr(
        date TEXT PRIMARY KEY, lpr1y REAL, lpr5y REAL);
    CREATE TABLE IF NOT EXISTS macro_shrzgm(
        month TEXT PRIMARY KEY, total REAL, rmb_loan REAL);
    CREATE TABLE IF NOT EXISTS macro_m2(
        month TEXT PRIMARY KEY, m2 REAL, m2_yoy REAL, m1 REAL, m1_yoy REAL);
    """)
    for r in data.get("lpr") or []:
        cur.execute("INSERT OR REPLACE INTO macro_lpr VALUES(?,?,?)",
                    (str(r["TRADE_DATE"])[:10],
                     _f(r["LPR1Y"]), _f(r["LPR5Y"])))
    for r in data.get("shrzgm") or []:
        cur.execute("INSERT OR REPLACE INTO macro_shrzgm VALUES(?,?,?)",
                    (r["_k"], _f(r["社会融资规模增量"]), _f(r["其中-人民币贷款"])))
    for r in data.get("m2") or []:
        cur.execute("INSERT OR REPLACE INTO macro_m2 VALUES(?,?,?,?,?)",
                    (r["_k"], _f(r["m2"]), _f(r["m2_yoy"]), _f(r["m1"]), _f(r["m1_yoy"])))
    con.commit()
    counts = {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("macro_lpr", "macro_shrzgm", "macro_m2")}
    con.close()
    return counts


def _f(v):
    try:
        f = float(v)
        return None if f != f else f
    except Exception:
        return None


# ============================================================
# 摘要生成
# ============================================================

def macro_lines(data):
    """宏观 dict → 摘要要点行(变动自动标记 📢)"""
    lines = []
    lpr = data.get("lpr") or []
    if len(lpr) >= 2 and lpr[-1]["LPR1Y"] != lpr[-2]["LPR1Y"]:
        lines.append(f"📢 LPR调整: {lpr[-2]['LPR1Y']}%/{lpr[-2]['LPR5Y']}% → "
                     f"{lpr[-1]['LPR1Y']}%/{lpr[-1]['LPR5Y']}% ({lpr[-1]['TRADE_DATE']})")
    elif lpr:
        lines.append(f"LPR 1Y/5Y = {lpr[-1]['LPR1Y']}%/{lpr[-1]['LPR5Y']}% "
                     f"@{lpr[-1]['TRADE_DATE']} (持平)")
    sr = data.get("shrzgm") or []
    if sr:
        cur, prev = sr[-1], (sr[-2] if len(sr) >= 2 else None)
        delta = (f"(上月{prev['社会融资规模增量']:,.0f}亿)" if prev else "")
        lines.append(f"社融增量 {cur['_k']} = {cur['社会融资规模增量']:,.0f}亿 {delta}".rstrip()
                     + f", 其中人民币贷款 {cur['其中-人民币贷款']:,.0f}亿")
    m2 = data.get("m2") or []
    if m2:
        cur = m2[-1]
        lines.append(f"M2 {cur['_k']} = {cur['m2']:,.0f}亿 (同比{cur['m2_yoy']:+.1f}%)"
                     f" | M1 同比{cur['m1_yoy']:+.1f}%  (M2-M1剪刀差"
                     f" {cur['m2_yoy'] - cur['m1_yoy']:+.1f}pp)")
    rl = data.get("rmb_loan") or []
    if rl:
        segs = [f"{r['_k']} {_f(r['当月']):+,.0f}亿(同比{r['当月-同比增长']:+.0f}%)"
                for r in rl[-3:]]
        lines.append("对实体经济贷款(社融口径)近3月: " + " | ".join(segs))
    return lines


def gen_markdown(date_str, macro, anns, news, dzjy, research, missing, counts):
    L = [f"# 银行板块每日背景材料 {date_str}", ""]
    L.append(f"生成 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜"
             f"公告/新闻回看窗口 {args_days}天｜宏观序列累积 "
             f"LPR {counts['macro_lpr']}期 / 社融 {counts['macro_shrzgm']}期 / "
             f"M2 {counts['macro_m2']}期")
    L.append("")
    L.append("> 定性素材层: 只罗列事实, 不含判断; 配合主报告(量化评分)使用。")
    L.append("")

    L.append("## 要点(机械规则触发)")
    L.append("")
    key_lines = macro_lines(macro)
    if key_lines:
        L += [f"- {ln}" for ln in key_lines]
    else:
        L.append("- (宏观数据今日无更新或抓取失败, 见缺失源)")
    L.append("")

    L.append("## 一、政策与金融数据")
    L.append("")
    L += [f"- {ln}" for ln in macro_lines(macro)] or ["- (无)"]
    L.append("")

    L.append(f"## 二、池内个股公告(近{args_days}天, {len(anns)}条)")
    L.append("")
    if anns:
        L.append("| 日期 | 银行 | 类别 | 标题 |")
        L.append("| --- | --- | --- | --- |")
        L += [f"| {a['date']} | {a['name']} | {a['cat']} | {_esc(a['title'])} |"
              for a in anns]
    else:
        L.append("(窗口内无命中类别的公告)")
    L.append("")

    L.append(f"## 三、板块新闻(近{args_days}天, {len(news)}条)")
    L.append("")
    if news:
        for n in news:
            L.append(f"- [{n['time'][5:16]}][{n['name']}] {_esc(n['title'])}"
                     f"({n['src']})")
    else:
        L.append("(窗口内无新闻)")
    L.append("")

    L.append("## 四、资金面与研报")
    L.append("")
    if dzjy["rows"]:
        L.append(f"### 大宗交易 {dzjy['date']} ({len(dzjy['rows'])}笔)")
        L.append("")
        L.append("| 代码 | 简称 | 成交价 | 折溢率 | 成交量(手) | 成交额(万) | 买方 | 卖方 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in dzjy["rows"]:
            prem = _f(r.get("折溢率"))
            prem_txt = f"{prem:+.2f}%" if prem is not None else "—"
            L.append(f"| {r['证券代码']} | {r['证券简称']} | {r['成交价']} | {prem_txt} | "
                     f"{_f(r['成交量']):,.0f} | {float(r['成交额'])/1e4:,.0f} | "
                     f"{_esc(r['买方营业部'])} | {_esc(r['卖方营业部'])} |")
        L.append("")
    else:
        L.append("### 大宗交易: 窗口内池内无成交")
        L.append("")
    if research:
        L.append(f"### 研报(接口快照内最新, {len(research)}份)")
        L.append("")
        L.append("| 日期 | 银行 | 评级 | 机构 | 2026EPS | 报告 |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        L += [f"| {r['日期']} | {r['股票简称']} | {r['东财评级']} | {r['机构']} | "
              f"{r['2026EPS预测']} | {_esc(r['报告名称'])} |" for r in research]
    else:
        L.append("### 研报: 接口快照内池内无新报告")
    L.append("")

    L.append("## 缺失源")
    L.append("")
    L += [f"- {m}" for m in missing] or ["- (全部数据源正常)"]
    L.append("")
    return "\n".join(L)


args_days = 3     # 由 main() 写回, gen_markdown 引用(脚本级单次运行, 可接受)


def main():
    global args_days
    ap = argparse.ArgumentParser(description="银行板块每日背景材料收料器")
    ap.add_argument("--days", type=int, default=3,
                    help="公告/新闻回看窗口天数(默认3, 覆盖周末)")
    args = ap.parse_args()
    args_days = args.days

    os.makedirs(RAW_DIR, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    b = Brief(days=args.days)
    print(f"📰 银行板块每日背景材料 @ {date_str} (窗口{args_days}天)")

    macro = b.fetch_macro()
    print(f"   宏观: LPR{'✅' if macro.get('lpr') else '❌'} "
          f"社融{'✅' if macro.get('shrzgm') else '❌'} "
          f"M2{'✅' if macro.get('m2') else '❌'}")
    counts = accumulate_macro(macro)

    anns = b.fetch_announcements()
    print(f"   公告: {len(anns)}条命中类别")
    news = b.fetch_news()
    print(f"   新闻: {len(news)}条窗口内")
    dzjy = b.fetch_dzjy()
    print(f"   大宗: {len(dzjy['rows'])}笔" +
          (f" @{dzjy['date']}" if dzjy["date"] else " (近几日无)"))
    research = b.fetch_research()
    print(f"   研报: {len(research)}份(快照内)")

    md = gen_markdown(date_str, macro, anns, news, dzjy, research,
                      b.missing, counts)
    md_path = os.path.join(BRIEF_DIR, f"{date_str}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    raw = {"date": date_str, "macro": macro, "announcements": anns,
           "news": news, "dzjy": dzjy, "research": research,
           "missing": b.missing}
    with open(os.path.join(RAW_DIR, f"{date_str}.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1, default=str)

    print(f"📄 摘要已写入 {md_path}")
    print(f"📄 原始数据 {RAW_DIR}/{date_str}.json | 宏观时序 {DB_PATH}")
    if b.missing:
        print("⚠️ 缺失源: " + "; ".join(b.missing))
    print("⚠️ 本文件为背景素材, 不构成投资建议。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
