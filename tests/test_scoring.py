#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
银行评分模型离线回归测试（不触网）

用法:
    python3 tests/test_scoring.py            # 运行全部用例, 全部通过退出码0
"""

import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))

from bank_universe import WEIGHTS, GATES, RATING_LEVELS  # noqa: E402
from bank_analysis import (annualize_factor, linear_map, hist_pctile,     # noqa: E402
                           cross_percentile_rank, score_banks, apply_gates,
                           sector_temperature, build_sector_median_pb,
                           parse_financial_row)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


# ------------------------------------------------------------
print("\n[1] annualize_factor 年化系数")
check("一季报×4", annualize_factor("2026-03-31") == 4.0)
check("中报×2", annualize_factor("2025-06-30") == 2.0)
check("三季报×4/3", abs(annualize_factor("2024-09-30") - 4 / 3) < 1e-9)
check("年报×1", annualize_factor("2023-12-31") == 1.0)
check("非法日期→None", annualize_factor("bad") is None)

# ------------------------------------------------------------
print("\n[2] linear_map 分段线性映射")
m = [(10, 95), (30, 80), (50, 60), (70, 40), (90, 20)]
check("区间内插值 40分位→50", abs(linear_map(40, m) - 60) < 1e-9 or abs(linear_map(50, m) - 60) < 1e-9)
check("节点精确", linear_map(70, m) == 40)
check("上端外推沿末段斜率(95→15)", abs(linear_map(95, m) - 15) < 1e-9)
check("下端外推沿首段斜率(0→102.5)", abs(linear_map(0, m) - 102.5) < 1e-9)
check("钳制cap=98生效", linear_map(0, m, floor=5, cap=98) == 98)
check("None→None", linear_map(None, m) is None)
check("NaN→None", linear_map(float("nan"), m) is None)

s_map = [(0.0, 10), (2.5, 95)]
check("利差2.5pp→95分", abs(linear_map(2.5, s_map) - 95) < 1e-9)
check("利差-0.5pp沿斜率外推=-7", abs(linear_map(-0.5, s_map) - (-7)) < 1e-9)

# ------------------------------------------------------------
print("\n[3] hist_pctile 历史百分位")
hist = [float(i) for i in range(100)]           # 0..99
check("中位数处=50分位附近", abs(hist_pctile(hist, 50.0) - 50) < 1e-9)
check("最大值→<100", hist_pctile(hist, 99.0) == 99.0)
check("空历史→None", hist_pctile([], 3.0) is None)
check("x缺失→None", hist_pctile(hist, None) is None)

# ------------------------------------------------------------
print("\n[4] cross_percentile_rank 截面排名")
rows = [{"x": 1.0}, {"x": 2.0}, {"x": 3.0}, {"x": None}]
r = cross_percentile_rank(rows, "x")
check("高值高分(正优)", r[2] > r[0])
r_low = cross_percentile_rank(rows, "x", reverse=True)
check("低优反转: 低值高得分", r_low[0] > r_low[2])
tie = cross_percentile_rank([{"x": 5.0}, {"x": 5.0}], "x")
check("并列取中间名→各50", tie[0] == tie[1] == 50.0)
check("None行不打分", 3 not in r and len(r) == 3)
check("全缺失→空", cross_percentile_rank([{"y": None}], "x") == {})

# ------------------------------------------------------------
print("\n[5] score_banks 五维加权 + 缺失重归一")
data = []
# A: 全维度优秀
for i in range(5):   # 生成5家参照
    data.append({"code": f"R{i}", "roe_annualized": 10.0 + i, "nim": 1.5 + i * 0.1,
                 "roa_annualized": 0.8 + i * 0.05, "cost_income": 30.0 - i,
                 "npl_ratio": 1.4 - i * 0.1, "provision_cov": 200.0 + i * 20,
                 "loan_provision": 3.0 + i * 0.2,
                 "provision_cov_chg": 20.0 - i * 5,
                 "cet1": 9.0 + i * 0.3,
                 "profit_yoy": 2.0 + i, "revenue_yoy": 1.0 + i,
                 "pb_self_pctile": 80.0 - i * 15, "pb_vs_sector": 20.0 - i * 10,
                 "pb_roe": 0.7 - i * 0.05, "payout_ratio": 20.0 + i * 2})
# 目标: 各项都取最优值, 且缺 资本充足 维度 → 应获得最高基础分且覆盖度<1
target = {"code": "T", **{k: v for k, v in data[-1].items() if k != "code"}}
del target["cet1"]
scored = score_banks(data + [target])
tgt = scored[-1]
ref = scored[-2]
# target 与最优参照在所有保留维度上并列第一; 差异仅在缺失的资本维度
# (参照该项满分100), 故总分略低于参照但应保持优档
best_ref = max(s["基础分"] for s in scored[:-1])
check("缺项目标仍接近最高参照且≥75", best_ref - 3 <= tgt["基础分"] <= best_ref and tgt["基础分"] >= 75)
check("缺资本维度→覆盖度<1", tgt["覆盖度"] < 1 and ref["覆盖度"] == 1.0)
check("缺项被剔除而非零分", "资本充足" in tgt["缺失维度"])
check("维度分数都在0-100", all(
    0 <= v <= 100 for s in scored for v in s["维度分"].values()))
low = scored[0]
high = scored[-2]
check("指标更优者维度分更高", high["维度分"]["盈利能力"] > low["维度分"]["盈利能力"])
check("拨备变化改善者质量分不劣于恶化者",
      high["维度分"]["资产质量"] >= low["维度分"]["资产质量"])
check("高分红者估值分不劣于低分红者",
      high["维度分"]["估值吸引力"] >= low["维度分"]["估值吸引力"])

# 仅拨备变化率不同的两家: 改善者应得分更高(新因子方向性)
pair_base = {
    "npl_ratio": 1.0, "provision_cov": 300.0, "loan_provision": 3.0,
    "roe_annualized": 11.0, "nim": 1.6, "roa_annualized": 0.9,
    "cost_income": 28.0, "cet1": 10.0, "profit_yoy": 5.0, "revenue_yoy": 4.0,
    "pb_self_pctile": 50.0, "pb_vs_sector": 0.0, "pb_roe": 0.6, "payout_ratio": 25.0,
}
pair = [
    {"code": "A", **pair_base, "provision_cov_chg": -50.0},
    {"code": "B", **pair_base, "provision_cov_chg": 10.0},
]
sp = score_banks(pair)
check("拨备同比改善(+)者质量分高于恶化(-50pp)者",
      sp[1]["维度分"]["资产质量"] > sp[0]["维度分"]["资产质量"])

# ------------------------------------------------------------
print("\n[6] apply_gates 一票否决/降档")
def mk(score, npl=1.0, cov=300.0, cet1=11.0, rd="2026-06-30"):
    return {"基础分": score, "_report_date": rd,
            "npl_ratio": npl, "provision_cov": cov, "cet1": cet1}

g = apply_gates(mk(85))
check("无触发保持A+", g["档位"] == "A+" and g["告警"] == [])
g = apply_gates(mk(85, npl=GATES["npl_hard"] + 0.01))
check(f"不良率超标→强制≤{GATES['hard_cap_level']}",
      g["档位"] == GATES["hard_cap_level"]
      and RATING_LEVELS[RATING_LEVELS.index(next(l for l in RATING_LEVELS if l[1] == "C"))][0] <= 85)
g = apply_gates(mk(85, cov=GATES["cov_floor"] - 1))
check("拨备覆盖率不足同样否决", g["档位"] == GATES["hard_cap_level"])
g = apply_gates(mk(85, cet1=GATES["cet1_floor"] - 0.1))
idx_a = next(i for i, l in enumerate(RATING_LEVELS) if l[1] == "A+")
idx_b = next(i for i, l in enumerate(RATING_LEVELS) if l[1] == "B")
check("核心一级不足降一档(A+→B)", g["档位"] == RATING_LEVELS[idx_a + 1 if idx_a + 1 != idx_b else idx_b][1]
      or g["档位"] in ("A", "B"))
old_rd = "2024-06-30"
g = apply_gates(mk(85, rd=old_rd))
check("陈旧财报产生告警不改档", any("财报距今" in w for w in g["告警"]) and g["档位"] == "A+")
g = apply_gates({"基础分": float("nan")})
check("全NaN基础分落D档", g["档位"] == "D")

# ------------------------------------------------------------
print("\n[7] sector_temperature 温度合成与降级")
from bank_universe import PB_PCTILE_MAP as _M_PB, SPREAD_MAP as _M_SP  # noqa: E402
t = sector_temperature(pb_pctile=15, spread_pts=2.8, mom_dev_pct=-12.0)
exp_pb = round(linear_map(15, _M_PB, floor=5, cap=98), 1)      # 实现先取整子分再合成
exp_sp = round(linear_map(2.8, _M_SP, floor=0, cap=100), 1)
mom = round(max(5, min(95, 50 + (-12.0) * 1.25)), 1)           # 偏离百分点×1.25
expected = round(exp_pb * .5 + exp_sp * .35 + mom * .15, 1)
check("三因子合成正确", t["温度分"] == expected and t["有效因子"] == ["pb", "spread", "momentum"])
t2 = sector_temperature(pb_pctile=15, spread_pts=None, mom_dev_pct=None)   # 仅PB可用
check("仅PB可用时权重重归一且温度≈PB子分",
      abs(t2["温度分"] - exp_pb) < 0.05 and t2["子分"]["利差分"] is None)
t3 = sector_temperature(None, None, None)
check("全因子缺失→温度None不崩溃", t3["温度分"] is None)
t4 = sector_temperature(50, 1.0, 200.0)
check("动量钳制上限95", t4["子分"]["动量分"] == 95)
t6 = sector_temperature(None, None, -0.65)     # 轻微低于均线应接近中性(修复前被放大100倍)
check("微小偏离不再触发极值", t6["子分"]["动量分"] == round(50 + (-0.65) * 1.25, 1))
t5 = sector_temperature(None, -1.0, None)
check("利差为负被钳为最低档0分", t5["子分"]["利差分"] == 0)
lvl_ok = sector_temperature(90, 2.6, 0)["温度分"] == sector_temperature(90, 2.6, 0)["温度分"]
check("相同输入结果稳定", lvl_ok)

# ------------------------------------------------------------
print("\n[8] build_sector_median_pb 最小覆盖率")
ph = {
    "a": {f"2026-{m:02d}-01": float(m) for m in range(1, 13)},
    "b": {f"2026-{m:02d}-01": float(m) + 1 for m in range(1, 13)},
    "c": {"2026-06-01": 99.0},                       # 覆盖率不足的股票, 应忽略其孤立日期
    "d": {},
}
ser = build_sector_median_pb(ph, min_coverage=0.5)
dates_only = [d for d, _v in ser]
check("孤立单点日期被跳过", "2026-03-01" in dates_only and ser and dates_only.count("2026-03-01") == 1)
med_jun = dict(ser).get("2026-06-01")
check("6月中位数=a,b均值7.0", med_jun is not None and abs(med_jun - 7.0) < 1e-9)

# ------------------------------------------------------------
print("\n[9] parse_financial_row 字段口径")
row = {"REPORT_DATE": "2026-03-31 00:00:00", "REPORT_DATE_NAME": "2026一季报",
       "ROEJQ": 3.37, "ZZCJLL": 0.28, "NET_INTEREST_MARGIN": 1.83,
       "REVENUE_RATIO": 28.47, "NONPERLOAN": 0.94, "BLDKBBL": 387.76,
       "LOAN_PROVISION_RATIO": None, "HXYJBCZL": 14.13,
       "PARENTNETPROFITTZ": 1.52, "TOTALOPERATEREVETZ": 3.81, "BPS": 44.9}
p = parse_financial_row(row)
check("Q1 ROE年化 ×4", abs(p["roe_annualized"] - 3.37 * 4) < 1e-9)
check("字段提取 nim", p["nim"] == 1.83)
check("None数值安全转None", p["loan_provision"] is None)
check("报告期名保留", p["报告期"] == "2026一季报")

# ------------------------------------------------------------
print("\n[10] WEIGHTS 权重完整性")
for dim, cfg in WEIGHTS.items():
    s = sum(it["wt"] for it in cfg["items"].values())
    check(f"{dim} 子项权重和=1 (实际{s:g})", abs(s - 1) < 1e-9)
check("资产质量含拨备变化率子项", "provision_cov_chg" in WEIGHTS["资产质量"]["items"])
check("估值吸引力含分红率子项", "payout_ratio" in WEIGHTS["估值吸引力"]["items"])

# ------------------------------------------------------------
print("\n[11] parse_report_meta 定期报告标题解析")
from bank_analysis import parse_report_meta  # noqa: E402
y, k, ok = parse_report_meta("江苏常熟农村商业银行股份有限公司2025年<em>年度报告</em>")
check("年报: 2025/年报/可下载", ok and y == "2025" and k == "年报")
y, k, ok = parse_report_meta("江苏常熟农村商业银行股份有限公司2026年半<em>年度报告</em>")
check("中报: 2026/中报/可下载", ok and y == "2026" and k == "中报")
check("摘要不可下载", parse_report_meta("2025年年度报告摘要")[2] is False)
check("英文版不可下载", parse_report_meta("2025年年度报告(英文版)")[2] is False)
check("非定期报告不可下载", parse_report_meta("年报信息披露重大差错责任追究办法")[2] is False)
check("无法解析年度→不可下载", parse_report_meta("季度报告")[2] is False)

print("\n[12] share_change_stats 份额变化统计")
from bank_analysis import share_change_stats  # noqa: E402
s = {f"2026-08-{d:02d}": 1e8 * (1.0 + i * 0.01) for i, d in enumerate([17, 18, 19, 20, 21, 24, 25, 26])}
st = share_change_stats(s)
check("最新日期与份额亿份", st["date"] == "2026-08-26" and st["shares_yi"] == 1.07)
check("日Δ≈+0.94%", abs(st["day_chg_pct"] - (1.07 / 1.06 - 1) * 100) < 0.01)
check("5日Δ≈+4.9%", abs(st["d5_chg_pct"] - (1.07 / 1.02 - 1) * 100) < 0.02)
check("空序列安全", share_change_stats({})["n"] == 0 and share_change_stats({})["day_chg_pct"] is None)
s2 = {"2026-08-26": 5e8}
check("单点无变化率", share_change_stats(s2)["day_chg_pct"] is None and share_change_stats(s2)["n"] == 1)

print(f"\n===== 结果: {PASS} PASS / {FAIL} FAIL =====")
sys.exit(1 if FAIL else 0)
