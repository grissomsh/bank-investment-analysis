#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股银行投资分析主流水线

四层框架:
  L0 数据层  — 东财F10财务指标(银行专项) + 东财个股估值史 + 中证官网指数估值
               + 腾讯K线 + 10Y国债收益率 + ETF实时清单
  L1 行业层  — 板块PB历史分位 / 股息率-10Y国债利差 / 动量确认 → 行业温度分
  L2 个股层  — 五维评分(盈利30/质量25/成长15/资本10/估值20), 截面分位数打分
               + 资产质量一票否决降档
  L3 组合层  — 温度决定 ETF底仓节奏, 个股评分决定卫星选择, 输出分散度统计

用法:
    python3 bank_analysis.py                     # 完整流水线
    python3 bank_analysis.py --detail 600036     # 附带单只个股详析
    python3 bank_analysis.py --no-html           # 不写HTML
    python3 bank_analysis.py --healthcheck       # 环境自检
    python3 bank_analysis.py --stats             # 本地DB状态

纯计算函数(打分/映射/年化)不触网, 可离线回归: tests/test_scoring.py
"""

import argparse
import json
import math
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from bank_universe import (
    BANKS, DEFAULT_ETFS, GATES, RATING_LEVELS, SECTOR_WEIGHTS, WEIGHTS,
    PB_PCTILE_MAP, PB_PCTILE_FLOOR, PB_PCTILE_CAP,
    SPREAD_MAP, MOMENTUM_DEV_SCALE, MOMENTUM_CLAMP,
    TEMPERATURE_LEVELS, INDEX_TCODE, INDEX_NAME, CSINDEX_CODE,
    secucode, tcode,
)
import bank_data_store
from bank_data_store import WORKSPACE, upsert_csindex, load_csindex

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

JSON_OUT = os.path.join(WORKSPACE, "银行投资分析.json")
HTML_OUT = os.path.join(WORKSPACE, "银行投资分析.html")


# ============================================================
# 纯计算函数（可离线测试）
# ============================================================

def annualize_factor(report_date):
    """财报累计期 → 年化系数。'2026-03-31'→4, 半年→2, 三季→4/3, 年报→1"""
    try:
        m = int(report_date[5:7])
    except Exception:
        return None
    return {3: 4.0, 6: 2.0, 9: 4.0 / 3.0, 12: 1.0}.get(m)


def linear_map(x, breakpoints, floor=None, cap=None):
    """分段线性插值映射到分数。breakpoints=[(x0,s0),...] 按x升序。
    两端沿末段斜率外推; floor/cap 提供时钳制。"""
    if x is None:
        return None
    try:
        if isinstance(x, float) and math.isnan(x):
            return None
    except Exception:
        return None
    xs = [bp[0] for bp in breakpoints]
    ss = [bp[1] for bp in breakpoints]
    if len(xs) < 2:
        s = float(ss[0])
    elif x <= xs[0]:
        s = ss[0] + (x - xs[0]) * ((ss[1] - ss[0]) / (xs[1] - xs[0]))
    elif x >= xs[-1]:
        s = ss[-1] + (x - xs[-1]) * ((ss[-1] - ss[-2]) / (xs[-1] - xs[-2]))
    else:
        s = ss[-1]
        for i in range(len(xs) - 1):
            if xs[i] <= x <= xs[i + 1]:
                frac = (x - xs[i]) / (xs[i + 1] - xs[i])
                s = ss[i] + frac * (ss[i + 1] - ss[i])
                break
    if floor is not None:
        s = max(floor, s)
    if cap is not None:
        s = min(cap, s)
    return s


def hist_pctile(values, x):
    """x 在 values 中的经验百分位(严格小于占比*100)。无效输入返回 None"""
    vals = [v for v in values if v is not None]
    try:
        vals = [v for v in vals if not math.isnan(v)]
    except TypeError:
        pass
    if x is None or not vals:
        return None
    return sum(1 for v in vals if v < x) / len(vals) * 100


def cross_percentile_rank(rows, key, reverse=False):
    """截面内对字段做中间名百分位打分(0..100, 含并列修正)。
    reverse=True 为低优指标(值小者得分高)。返回 {行下标: 分数}"""
    pairs = []
    for i, r in enumerate(rows):
        v = r.get(key)
        if isinstance(v, (int, float)) and not math.isnan(v):
            pairs.append((i, float(v)))
    out = {}
    n = len(pairs)
    if n == 0:
        return out
    for i, v in pairs:
        below = sum(1 for _, w in pairs if w < v)
        ties = sum(1 for _, w in pairs if w == v)
        p = (below + 0.5 * ties) / n * 100
        out[i] = round(100 - p if reverse else p, 1)
    return out


def score_banks(rows, weights=None):
    """五维截面评分(就地补充分数字段)。缺失子项跳过并按可用权重重归一,
    整维度缺失则剔除该维度再重归一总权重。"""
    if weights is None:
        weights = WEIGHTS
    dims = list(weights.keys())
    item_pcts = {}
    for dim in dims:
        for item, cfg in weights[dim]["items"].items():
            item_pcts[(dim, item)] = cross_percentile_rank(rows, item, cfg.get("低优", False))

    total_w_all = sum(weights[d]["w"] for d in dims)
    for i, r in enumerate(rows):
        dim_scores, missing_dims = {}, []
        total_num = total_den = 0.0
        for dim in dims:
            num = den = 0.0
            for item, cfg in weights[dim]["items"].items():
                p = item_pcts[(dim, item)].get(i)
                if p is None:
                    continue
                num += p * cfg["wt"]
                den += cfg["wt"]
            if den <= 0:
                missing_dims.append(dim)
                continue
            ds = num / den
            dim_scores[dim] = round(ds, 1)
            total_num += ds * weights[dim]["w"]
            total_den += weights[dim]["w"]
        total = total_num / total_den if total_den else float("nan")
        cov_w = total_w_all - sum(weights[d]["w"] for d in missing_dims)
        r["维度分"] = dim_scores
        r["缺失维度"] = missing_dims
        r["覆盖度"] = round(cov_w / total_w_all, 3)
        r["基础分"] = round(total, 1)
    return rows


def apply_gates(row, gates=None, levels=None):
    """一票否决与降档(就地写 档位/档位说明/告警)。
    不良率超限或拨备覆盖率不足 → 强制不高于 C 档;
    核心一级资本充足率不足 → 再降一档; 财报陈旧 → 警示标注。"""
    gates = gates or GATES
    levels = levels or RATING_LEVELS
    warns = []

    def lo_index(name):
        return next(k for k, lv in enumerate(levels) if lv[1] == name)

    idx = next((k for k, lv in enumerate(levels) if row["基础分"] >= lv[0]), len(levels) - 1)
    hard_triggered = False

    npl = row.get("npl_ratio")
    cov = row.get("provision_cov")
    cet1 = row.get("cet1")
    if isinstance(npl, (int, float)) and npl > gates["npl_hard"]:
        warns.append(f"不良率{npl:.2f}%>{gates['npl_hard']:.1f}%")
        hard_triggered = True
    if isinstance(cov, (int, float)) and cov < gates["cov_floor"]:
        warns.append(f"拨备覆盖率{cov:.0f}%<{gates['cov_floor']:.0f}%")
        hard_triggered = True
    if hard_triggered:
        idx = max(idx, lo_index(gates["hard_cap_level"]))
    if isinstance(cet1, (int, float)) and cet1 < gates["cet1_floor"]:
        warns.append(f"核心一级{cet1:.2f}%<{gates['cet1_floor']}%,分红能力受限")
        idx = min(idx + 1, len(levels) - 1)
    rd = row.get("_report_date")
    if rd:
        try:
            age = (datetime.now() - datetime.strptime(rd[:10], "%Y-%m-%d")).days
            if age > gates["report_stale_days"]:
                warns.append(f"财报距今{age}天")
        except Exception:
            pass

    row["档位"] = levels[idx][1]
    row["档位说明"] = levels[idx][2]
    row["告警"] = warns
    return row


def sector_temperature(pb_pctile, spread_pts, mom_dev_pct,
                        w=None, pb_map=None, spread_map=None):
    """行业温度分 0-100 与三个子分。任一输入 None 时该因子剔除并重归一权重。
    mom_dev_pct: 指数收盘价相对250日均线的偏离%。"""
    w = w or SECTOR_WEIGHTS
    pb_map = pb_map if pb_map is not None else PB_PCTILE_MAP
    spread_map = spread_map if spread_map is not None else SPREAD_MAP

    s_pb = linear_map(pb_pctile, pb_map, floor=PB_PCTILE_FLOOR, cap=PB_PCTILE_CAP) \
        if pb_pctile is not None else None
    # 利差分钳制到 [0,100]: 利差为负(股息率<国债)时给最低分而非负分
    s_spread = linear_map(spread_pts, spread_map, floor=0, cap=100) \
        if spread_pts is not None else None
    s_pb = round(s_pb, 1) if s_pb is not None else None
    s_spread = round(s_spread, 1) if s_spread is not None else None
    if mom_dev_pct is not None:
        s_mom = round(max(MOMENTUM_CLAMP[0],
                          min(MOMENTUM_CLAMP[1], 50 + mom_dev_pct * MOMENTUM_DEV_SCALE)), 1)
    else:
        s_mom = None

    parts = {"pb": s_pb, "spread": s_spread, "momentum": s_mom}
    keys = [k for k in parts if parts[k] is not None]
    wsum = sum(w[k] for k in keys)
    temp = round(sum(parts[k] * w[k] for k in keys) / wsum, 1) if wsum else None
    return {"温度分": temp,
            "子分": {"PB分位分": s_pb, "利差分": s_spread, "动量分": s_mom},
            "有效因子": keys}


def temp_level(temp):
    for lo, nm, icon in TEMPERATURE_LEVELS:
        if temp is None or temp >= lo:
            return nm, icon
    nm, icon = TEMPERATURE_LEVELS[-1][1], TEMPERATURE_LEVELS[-1][2]
    return nm, icon


# ============================================================
# 数据获取（网络）
# ============================================================

def fetch_kline(tc, limit=320):
    """腾讯日K(qfq): [{date,c,h,l,v}], 失败返回[]"""
    u = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tc},day,,,{limit},qfq"
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        node = d.get("data", {}).get(tc, {})
        k = node.get("day") or node.get("qfqday") or []
        return [{"date": r[0], "c": float(r[2]), "h": float(r[3]), "l": float(r[4]),
                 "v": float(r[5])} for r in k if len(r) >= 6 and r[0]]
    except Exception:
        return []


def ma_dev_pct(klines, n=250):
    """最新收盘相对N日均线的偏离%, 数据不足返回None"""
    closes = [k["c"] for k in klines]
    if len(closes) < n:
        return None
    ma = sum(closes[-n:]) / n
    return (closes[-1] / ma - 1) * 100


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except Exception:
        return None


def parse_financial_row(r):
    """东财F10主要指标一行(dict) → 标准化指标 dict(统一保留位数)。供 fetch_financial 复用"""
    def rd_(x, nd=2):
        v = _num(x)
        return round(v, nd) if v is not None else None

    fdate = str(r.get("REPORT_DATE", ""))[:10]
    fac = annualize_factor(fdate) or 1.0
    roe = _num(r.get("ROEJQ"))
    roa = _num(r.get("ZZCJLL"))
    return {
        "_report_date": fdate,
        "报告期": str(r.get("REPORT_DATE_NAME", fdate)),
        "bps": rd_(r.get("BPS")),
        "roe_annualized": round(roe * fac, 2) if roe is not None else None,
        "roa_annualized": round(roa * fac, 3) if roa is not None else None,
        "nim": rd_(r.get("NET_INTEREST_MARGIN")),
        "cost_income": rd_(r.get("REVENUE_RATIO")),
        "npl_ratio": rd_(r.get("NONPERLOAN")),
        "provision_cov": rd_(r.get("BLDKBBL")),
        "loan_provision": rd_(r.get("LOAN_PROVISION_RATIO")),
        "cet1": rd_(r.get("HXYJBCZL")),
        "profit_yoy": rd_(r.get("PARENTNETPROFITTZ")),
        "revenue_yoy": rd_(r.get("TOTALOPERATEREVETZ")),
    }


def fetch_financial(code, retries=2):
    """东财F10主要指标(含银行专项字段): 返回最新一期 dict 或 None"""
    for attempt in range(retries):
        try:
            import akshare as ak
            df = ak.stock_financial_analysis_indicator_em(
                symbol=secucode(code), indicator="按报告期")
            if df is None or len(df) == 0:
                return None
            return parse_financial_row(df.iloc[0].to_dict())
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(1.0)
    return None


def fetch_valuation_history(code):
    """东财个股估值史(约2018起, 按日期升序整理):
    返回 ({date:pb}, {date,close,pb,pe}) 或 (None,None)"""
    try:
        import akshare as ak
        import pandas as pd
        df = ak.stock_value_em(symbol=code)
        if df is None or len(df) == 0:
            return None, None
        df["市净率"] = pd.to_numeric(df["市净率"], errors="coerce")
        df["PE(TTM)"] = pd.to_numeric(df["PE(TTM)"], errors="coerce")
        df["当日收盘价"] = pd.to_numeric(df["当日收盘价"], errors="coerce")
        df = df.sort_values("数据日期")
        ser = dict(zip(df["数据日期"].astype(str), df["市净率"]))
        last = df.dropna(subset=["市净率"]).iloc[-1]
        cur = {"date": str(last["数据日期"]), "close": float(last["当日收盘价"]),
               "pb": float(last["市净率"]),
               "pe": None if pd.isna(last["PE(TTM)"]) else float(last["PE(TTM)"])}
        return ser, cur
    except Exception:
        return None, None


def _median(vals):
    vs = sorted(vals)
    n = len(vs)
    if n == 0:
        return None
    return vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2


def build_sector_median_pb(per_stock_hist, min_coverage=0.5):
    """由成分股各自 PB 序列合成「板块等权中位数PB」日序列。
    per_stock_hist: {code: {date: pb}}。某日有效样本需≥有历史银行数的
    min_coverage 比例且不少于2家, 否则视为不可信跳过该日。
    返回 [(date, median_pb)] 升序"""
    all_dates = set()
    for ser in per_stock_hist.values():
        if ser:
            all_dates.update(ser.keys())
    codes = [c for c, s in per_stock_hist.items() if s]
    need = max(2, int(round(len(codes) * min_coverage)))
    out = []
    for d in sorted(all_dates):
        vals = [per_stock_hist[c][d] for c in codes if d in per_stock_hist[c]]
        vals = [v for v in vals if v is not None and not math.isnan(v) and v > 0]
        if len(vals) >= need:
            out.append((d, _median(vals)))
    return out


def fetch_csindex_valuation():
    """中证官网指数估值(最近约20交易日): [{'date',pe_static,pe_ttm,yield_1,yield_2}] 升序"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_value_csindex(symbol=CSINDEX_CODE)
        if df is None or len(df) == 0:
            return []
        rows = []
        for _, r in df.iterrows():
            rows.append({"date": str(r["日期"])[:10],
                         "pe_static": _num(r.get("市盈率1")),
                         "pe_ttm": _num(r.get("市盈率2")),
                         "yield_1": _num(r.get("股息率1")),
                         "yield_2": _num(r.get("股息率2"))})
        return sorted(rows, key=lambda x: x["date"])
    except Exception:
        return []


def fetch_treasury_10y():
    """中国10Y国债收益率% 与对应日期"""
    try:
        import akshare as ak
        df = ak.bond_zh_us_rate(start_date="20200101")
        col = "中国国债收益率10年"
        s = df[["日期", col]].dropna()
        if len(s) == 0:
            return None, None
        return float(s[col].iloc[-1]), str(s["日期"].iloc[-1])[:10]
    except Exception:
        return None, None


def _safe_float(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except Exception:
        return None


def fetch_bank_etfs(top_n=8):
    """动态发现名称含'银行'的ETF并按规模取前N; DEFAULT_ETFS 作为兜底锚点"""
    anchor = [{"code": c, "name": m["n"], "price": None, "premium_pct": None,
               "scale_yi": None, "turnover_yi": None} for c, m in DEFAULT_ETFS.items()]
    try:
        import akshare as ak
        df = ak.fund_etf_spot_em()
        sel = df[df["名称"].astype(str).str.contains("银行")].copy()
        sel["_scale"] = sel["总市值"].apply(_safe_float)
        sel = sel.sort_values("_scale", ascending=False).head(top_n)
        out = []
        for _, r in sel.iterrows():
            px, iopv = _safe_float(r.get("最新价")), _safe_float(r.get("IOPV实时估值"))
            out.append({"code": str(r["代码"]), "name": str(r["名称"]), "price": px,
                        "premium_pct": round((px / iopv - 1) * 100, 2) if px and iopv else None,
                        "scale_yi": (_safe_float(r.get("总市值")) or 0) / 1e8,
                        "turnover_yi": (_safe_float(r.get("成交额")) or 0) / 1e8})
        found = {e["code"] for e in out}
        return out + [a for a in anchor if a["code"] not in found]
    except Exception:
        return anchor


# ============================================================
# 流水线
# ============================================================

def run(detail_code=None, make_html=True):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    codes = list(BANKS.keys())
    print(f"🏦 A股银行投资分析 @ {ts}")
    print(f"   银行池 {len(codes)} 家 | 基准指数 {INDEX_NAME}")

    # ---- L1 行情与利率 ----
    idx_kline = fetch_kline(INDEX_TCODE, 320)
    idx_dev250 = ma_dev_pct(idx_kline, 250)
    idx_close = idx_kline[-1]["c"] if idx_kline else None
    idx_close_disp = round(idx_close, 2) if idx_close is not None else None
    print(f"   指数K线 {len(idx_kline)}根 | 收盘 {_fmt(idx_close_disp)} | MA250偏离 {_fmt(idx_dev250, '%')}")

    cs_rows = fetch_csindex_valuation()
    if cs_rows:
        upsert_csindex([(r["date"], r["pe_static"], r["pe_ttm"], r["yield_1"], r["yield_2"])
                        for r in cs_rows])
        print(f"   中证官方估值入库 {len(cs_rows)} 行, 本地累积 {len(load_csindex())} 行")
    y1 = cs_rows[-1]["yield_1"] if cs_rows else None
    ty10, ty_date = fetch_treasury_10y()
    spread = (y1 - ty10) if (y1 is not None and ty10 is not None) else None
    print(f"   指数股息率(近12M) {_fmt(y1, '%')} | 10Y国债 {_fmt(ty10, '%')}@{ty_date} "
          f"| 利差 {_fmt(spread, 'pp')}")

    # ---- L0 成分股数据 ----
    print(f"   拉取 {len(codes)} 家银行的财务指标与估值史 ...")
    fin_ok = val_ok = 0
    raw_rows = []
    for i, c in enumerate(codes, 1):
        fin = fetch_financial(c)
        fin_ok += 1 if fin else 0
        ser, cur = fetch_valuation_history(c)
        val_ok += 1 if (ser and cur) else 0
        raw_rows.append({"code": c, "name": BANKS[c]["n"], "type": BANKS[c]["t"],
                         **(fin or {}), "_val_hist": ser, "_val_cur": cur})
        if i % 10 == 0:
            print(f"   ... {i}/{len(codes)}")
        time.sleep(0.05)
    print(f"   财务指标 {fin_ok}/{len(codes)} | 估值史 {val_ok}/{len(codes)}")
    if fin_ok < len(codes) * 0.6:
        print("❌ 财务数据大面积失败(多为网络/代理问题), 终止本次运行")
        return None

    per_stock_hist = {r["code"]: r["_val_hist"] for r in raw_rows}
    sector_series = build_sector_median_pb(per_stock_hist)
    sector_pb_now = sector_series[-1][1] if sector_series else None
    sector_pb_pctile = hist_pctile([m for _d, m in sector_series], sector_pb_now) \
        if sector_pb_now is not None and len(sector_series) >= 500 else None

    for r in raw_rows:
        ser, cur = r.pop("_val_hist"), r.pop("_val_cur")
        if cur:
            r["close"] = round(cur["close"], 2)
            r["pb"] = round(cur["pb"], 3)
            r["pe_ttm"] = round(cur["pe"], 2) if cur.get("pe") is not None else None
        if ser and cur and cur.get("pb"):
            pb_list = [v for v in ser.values() if v is not None and not math.isnan(v) and v > 0]
            need = min(750, len(pb_list))          # 近3年窗口
            r["pb_self_pctile"] = round(hist_pctile(pb_list[-need:], cur["pb"]), 1) \
                if len(pb_list) >= 250 else None
            r["pb_vs_sector"] = round((cur["pb"] / sector_pb_now - 1) * 100, 1) \
                if sector_pb_now else None
            if r.get("roe_annualized"):
                r["pb_roe"] = round(cur["pb"] / (r["roe_annualized"] / 100), 2)

    # ---- L2 五维评分 + 一票否决 ----
    raw_rows = score_banks(raw_rows)
    raw_rows = [apply_gates(r) for r in raw_rows]
    ranked = sorted(raw_rows, key=lambda r: -(r["基础分"] if r["基础分"] == r["基础分"] else -1))

    # ---- L1 温度 ----
    temp = sector_temperature(sector_pb_pctile, spread, idx_dev250)
    lvl, icon = temp_level(temp["温度分"])
    print(f"\n🌡️ 行业温度 {icon} {temp['温度分']}分 [{lvl}] "
          f"(PB分位分{temp['子分']['PB分位分']} | 利差分{temp['子分']['利差分']} "
          f"| 动量分{temp['子分']['动量分']})")
    print(f"   板块中位数PB {_fmt(sector_pb_now, 'pb')} → 历史分位 {_fmt(sector_pb_pctile, '百分位')}"
          f" (样本{len(sector_series)}日)")

    type_top = {}
    for r in ranked[:10]:
        type_top[r["type"]] = type_top.get(r["type"], 0) + 1

    if detail_code:
        d = next((r for r in raw_rows if r["code"] == detail_code.strip()), None)
        if d:
            print("\n".join(format_detail(d)))
        else:
            print(f"\n⚠️ 未找到 {detail_code} 的可用数据")

    report = {
        "ts": ts, "index": INDEX_TCODE, "index_name": INDEX_NAME,
        "index_close": idx_close, "index_ma250_dev_pct": idx_dev250,
        "sector": {"temp": temp, "level": lvl, "icon": icon,
                   "median_pb": round(sector_pb_now, 4) if sector_pb_now else None,
                   "median_pb_pctile": round(sector_pb_pctile, 1) if sector_pb_pctile is not None else None,
                   "median_pb_days": len(sector_series),
                   "div_yield": y1, "treasury_10y": ty10, "spread_pts": spread,
                   "csindex_archive_rows": len(load_csindex())},
        "banks": [{k: v for k, v in r.items() if not str(k).startswith("_")} for r in ranked],
        "top10_type_mix": type_top,
    }
    report["etfs"] = [
        {**e, "ma250_dev_pct": ma_dev_pct(fetch_kline(tcode(e["code"]), 320), 250)}
        for e in fetch_bank_etfs()]

    os.makedirs(WORKSPACE, exist_ok=True)
    with open(JSON_OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n📄 JSON已写入 {JSON_OUT}")

    print_table(ranked)
    if make_html:
        with open(HTML_OUT, "w", encoding="utf-8") as f:
            f.write(gen_html(report))
        print(f"📄 HTML报告已写入 {HTML_OUT}")
    print("\n⚠️ 本输出仅为量化研究工具的参考信号, 不构成任何投资建议。")
    return report


def _fmt(v, unit=""):
    if v is None:
        return "—"
    if unit == "%":
        return f"{v:+.2f}%"
    if unit == "pp":
        return f"{v:+.2f}pp"
    if unit == "pb":
        return f"{v:.2f}"
    if unit == "百分位":
        return f"{v:.0f}%分位"
    return f"{v}"


def format_detail(d):
    lines = [f"\n🔍 {d['name']}({d['code']}) 详析 — 报告期 {d.get('报告期')} | 收盘 "
             f"{_fmt(d.get('close'))} | PB {_fmt(d.get('pb'))} | PE-TTM {_fmt(d.get('pe_ttm'))}"]
    fields = [("年化加权ROE%", "roe_annualized"), ("年化ROA%", "roa_annualized"),
              ("净息差%", "nim"), ("成本收入比%", "cost_income"),
              ("不良率%", "npl_ratio"), ("拨备覆盖率%", "provision_cov"),
              ("拨贷比%", "loan_provision"), ("核心一级资本充足率%", "cet1"),
              ("营收同比%", "revenue_yoy"), ("归母净利同比%", "profit_yoy"),
              ("每股净资产", "bps"), ("PB自身历史分位", "pb_self_pctile"),
              ("相对板块PB溢价%", "pb_vs_sector"), ("PB÷年化ROE", "pb_roe")]
    for label, key in fields:
        v = d.get(key)
        lines.append(f"   {label:<14}{v if v is not None else '—'}")
    lines.append(f"   维度分: {d.get('维度分')}")
    lines.append(f"   基础分 {d['基础分']} → 档位 {d['档位']}({d.get('档位说明')}) "
                 f"| 权重覆盖度 {d.get('覆盖度')}"
                 + (f" | ⚠️ {'; '.join(d['告警'])}" if d.get("告警") else ""))
    return lines


def _pad(s, width):
    disp = 0
    out = ""
    for ch in str(s):
        out += ch
        disp += 2 if ord(ch) > 127 else 1
    return out + " " * max(0, width - disp)


COLW = [4, 9, 7, 7, 6, 4, 6, 6, 6, 6, 6, 6, 6, 7, 12]


def print_table(ranked):
    print("\n===== 个股五维评分(截面分位打分, 0-100) =====")
    hdr = ["排名", "名称", "代码", "类别", "总分", "档位",
           "盈利", "质量", "成长", "资本", "估值", "PB", "不良%", "覆盖%", "报告期"]
    print(" ".join(_pad(h, w) for h, w in zip(hdr, COLW)))
    for i, r in enumerate(ranked, 1):
        dm = r["维度分"]
        cells = [str(i), r["name"], r["code"], r["type"],
                 f"{r['基础分']:g}", r["档位"]]
        for dim in ["盈利能力", "资产质量", "成长性", "资本充足", "估值吸引力"]:
            v = dm.get(dim)
            cells.append(f"{v:g}" if isinstance(v, (int, float)) else "—")
        cells += [_fmt(r.get("pb"), "pb"), _fmt(r.get("npl_ratio")),
                  _fmt(r.get("provision_cov")), r.get("报告期") or "—"]
        mark = " ⚠️" if r.get("告警") else ""
        print(" ".join(_pad(c, w) for c, w in zip(cells, COLW)) + mark)


# ============================================================
# HTML 报告（token替换, 避免模板转义问题）
# ============================================================

_HTML_TEMPLATE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>A股银行投资分析报告</title>
<style>
 body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:24px;
      color:#26303a;background:#fafbfd;font-size:14px;line-height:1.55}
 .card{background:#fff;border:1px solid #e6eaef;border-radius:12px;padding:18px 22px;margin-bottom:18px;
       box-shadow:0 1px 3px rgba(16,24,40,.04)}
 h1{font-size:21px;margin:0 0 4px} h2{font-size:16px;margin:0 0 12px;color:#1c2833}
 .sub{color:#7a869a;font-size:12px;margin-bottom:14px}
 table{border-collapse:collapse;width:@TABLEW@;font-size:13px}
 th{background:#f2f5f9;text-align:left;padding:7px 8px;border-bottom:2px solid #dde4ec;
    white-space:nowrap;color:#42536b}
 td{padding:6px 8px;border-bottom:1px solid #eef1f5;vertical-align:top}
 tr:hover td{background:#f7fafd}
 .score{font-weight:700;color:#123b6d}
 tr.gate td{background:#fff7ed}
 .warn{color:#c2410c;font-size:11px}
 .period{color:#98a2b3;font-size:11px}
 .mono{font-family:ui-monospace,Menlo,monospace;color:#667085}
 .big{font-size:34px;font-weight:800;color:#123b6d}
 .pill{display:inline-block;padding:2px 10px;border-radius:99px;background:#eff4ff;
       color:#2a4fbf;font-size:13px;margin-left:8px;font-weight:600}
 .grid{display:flex;gap:12px;flex-wrap:wrap;margin-top:10px}
 .kv{flex:1;min-width:150px;background:#f7f9fc;border-radius:10px;padding:10px 14px}
 .kv b{display:block;font-size:19px;color:#193a63}
 .kv span{color:#8391a6;font-size:11px}
 .foot{color:#98a2b3;font-size:11px;text-align:center;margin-top:22px}
</style></head><body>
<h1>🏦 A股银行投资分析 <span class="pill">@ICON@ @TEMP@ 分 · @LEVEL@</span></h1>
<div class="sub">生成时间 @TS@｜基准指数 @IDXNAME@ 收盘 @IDXCLOSE@｜五维权重：盈利30 质量25 成长15 资本10 估值20</div>

<div class="card">
<h2>🌡️ L1 行业温度（ETF配置节奏）</h2>
<div class="grid">
<div class="kv"><span>PB温度 · 板块中位数PB历史分位</span><b>@SPB@ <small style="font-size:11px">@MEDPB@ → @PBPCT@</small></b></div>
<div class="kv"><span>股债性价比 · 股息率−10Y国债</span><b>@SSPREAD@ <small style="font-size:11px">@DIVY@ − @TY10@ = @SPREADPP@</small></b></div>
<div class="kv"><span>动量确认 · 相对MA250偏离</span><b>@SMOM@ <small style="font-size:11px">@IDXDEV@</small></b></div>
<div class="kv"><span>Top10 类别分布</span><b style="font-size:14px">@TYPEMIX@</b></div>
</div>
<p style="color:#7a869a;font-size:12px;margin:8px 0 0">
温度≥75 积极配置 ｜ 55–75 正常定投 ｜ 40–55 持有不加仓 ｜ &lt;40 减持/止盈观察。
中证官网官方PE/股息率每日落库累积(@ARCHIVE@行)，长期将支持官方口径的历史分位。</p>
</div>

<div class="card">
<h2>📊 L2 个股五维评分（⚠️底色行为触发资产质量降档）</h2>
<table><tr><th>#</th><th>银行</th><th>类别</th><th>总分</th><th>档位</th>
<th>维度分</th><th>PB</th><th>不良率</th><th>拨备覆盖率</th><th>相对板块PB</th>
<th>PB自身分位</th><th>告警 / 报告期</th></tr>
@BANKROWS@
</table></div>

<div class="card">
<h2>💰 银行ETF池（名称含“银行”按规模Top8动态发现）</h2>
<table><tr><th>代码</th><th>名称</th><th>现价</th><th>IOPV溢价</th><th>MA250偏离</th>
<th>市值(亿)</th><th>今日成交(亿)</th></tr>
@ETFROWS@
</table></div>

<div class="foot">数据源：东方财富F10(银行专项财务)·东财估值史·中证指数公司·腾讯财经·国债收益率曲线
—— 量化研究工具, 全部输出不构成投资建议。</div>
</body></html>
"""


def gen_html(rep):
    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;")

    sec, temp = rep["sector"], rep["sector"]["temp"]

    bank_rows = []
    for i, r in enumerate(rep["banks"], 1):
        dm = r["维度分"]
        warn = "<br>".join(f"<span class='warn'>⚠ {esc(w)}</span>" for w in r.get("告警") or []) or "—"
        cell = lambda k, fmt="{:.0f}": (
            fmt.format(dm[k]) if isinstance(dm.get(k), (int, float)) else "—")
        pb = lambda v, fmt="{:.2f}": fmt.format(v) if isinstance(v, (int, float)) else "—"
        bank_rows.append(
            "<tr%s><td>%d</td><td><b>%s</b><br><span class='mono'>%s</span></td>"
            "<td>%s</td><td class='score'>%.1f</td><td><b>%s</b></td>"
            "<td style='white-space:nowrap'>盈%s 质%s 成%s 资%s 估%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s<br><span class='period'>%s</span></td></tr>" % (
                " class='gate'" if r.get("告警") else "",
                i, esc(r["name"]), r["code"], esc(r["type"]),
                r["基础分"], esc(r["档位"]),
                cell("盈利能力"), cell("资产质量"), cell("成长性"),
                cell("资本充足"), cell("估值吸引力"),
                pb(r.get("pb")), pb(r.get("npl_ratio"), "{:.2f}%"),
                pb(r.get("provision_cov"), "{:.0f}%"),
                pb(r.get("pb_vs_sector"), "{:+.1f}%"),
                pb(r.get("pb_self_pctile"), "{:.0f}"),
                warn, esc(r.get("报告期") or "—")))

    etf_rows = []
    for e in rep["etfs"]:
        etf_rows.append(
            "<tr><td class='mono'>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
                esc(e["code"]), esc(e["name"]),
                _fmt(e.get("price")), _fmt(e.get("premium_pct"), "%"),
                _fmt(e.get("ma250_dev_pct"), "%"),
                "%.0f" % e["scale_yi"] if e.get("scale_yi") else "—",
                "%.0f" % e["turnover_yi"] if e.get("turnover_yi") else "—"))

    mix = "、".join(f"{k}{v}家" for k, v in rep["top10_type_mix"].items())
    t = temp["温度分"]
    icon = next((ic for lo, _nm, ic in TEMPERATURE_LEVELS if t is not None and t >= lo),
                TEMPERATURE_LEVELS[-1][2])

    html = (_HTML_TEMPLATE
            .replace("@TABLEW@", "100%")
            .replace("@ICON@", icon)
            .replace("@TEMP@", _fmt(t))
            .replace("@LEVEL@", esc(sec["level"]))
            .replace("@TS@", esc(rep["ts"]))
            .replace("@IDXNAME@", esc(rep["index_name"]))
            .replace("@IDXCLOSE@", _fmt(rep.get("index_close")))
            .replace("@SPB@", _fmt(temp["子分"]["PB分位分"]))
            .replace("@MEDPB@", _fmt(sec["median_pb"]))
            .replace("@PBPCT@", _fmt(sec["median_pb_pctile"], "百分位"))
            .replace("@SSPREAD@", _fmt(temp["子分"]["利差分"]))
            .replace("@DIVY@", _fmt(sec["div_yield"], "%"))
            .replace("@TY10@", _fmt(sec["treasury_10y"], "%"))
            .replace("@SPREADPP@", _fmt(sec["spread_pts"], "pp"))
            .replace("@SMOM@", _fmt(temp["子分"]["动量分"]))
            .replace("@IDXDEV@", _fmt(rep.get("index_ma250_dev_pct"), "%"))
            .replace("@TYPEMIX@", esc(mix or "—"))
            .replace("@ARCHIVE@", str(sec["csindex_archive_rows"]))
            .replace("@BANKROWS@", "\n".join(bank_rows))
            .replace("@ETFROWS@", "\n".join(etf_rows)))
    return html


# ============================================================
# CLI
# ============================================================

def healthcheck():
    state = {"ok": True}

    def step(name, fn):
        try:
            print(f"✅ {name}: {fn()}")
        except Exception as e:
            state["ok"] = False
            print(f"❌ {name}: {type(e).__name__} {str(e)[:120]}")

    step("腾讯K线", lambda: f"{len(fetch_kline(INDEX_TCODE, 5))}根")

    def _cs():
        rows = fetch_csindex_valuation()
        if not rows:
            raise RuntimeError("无返回")
        return f"{len(rows)}行, 最近 {rows[-1]['date']}"

    step("akshare 中证指数估值", _cs)
    step("akshare 国债收益率", lambda: fetch_treasury_10y()[0])

    def _etf():
        lst = fetch_bank_etfs()
        if not lst:
            raise RuntimeError("空")
        return f"{len(lst)}只"

    step("akshare ETF清单", _etf)

    def _fin():
        f = fetch_financial("600036")
        if not f or f.get("npl_ratio") is None:
            raise RuntimeError("银行专项字段缺失")
        return f"招行{f['报告期']} 不良率{f['npl_ratio']}% NIM{f['nim']}%"

    step("东财F10银行专项", _fin)
    step("SQLite落库", lambda: (upsert_csindex([]), bank_data_store.DB_PATH)[1])
    return 0 if state["ok"] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="A股银行投资分析（行业温度 + 个股五维评分）")
    ap.add_argument("--detail", metavar="CODE", help="附带单只个股详析, 如 --detail 600036")
    ap.add_argument("--no-html", action="store_true", help="不生成HTML报告")
    ap.add_argument("--healthcheck", action="store_true", help="环境自检")
    ap.add_argument("--stats", action="store_true", help="本地数据库状态")
    args = ap.parse_args()

    if args.healthcheck:
        sys.exit(healthcheck())
    if args.stats:
        s = bank_data_store.stats()
        print(f"DB: {s['db_path']}\ncsindex_value: {s['csindex_rows']} 行 ({s['first']} ~ {s['last']})")
        sys.exit(0)

    rc = run(detail_code=args.detail, make_html=not args.no_html)
    sys.exit(0 if rc else 1)
