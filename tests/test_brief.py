#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日背景材料收料器离线测试(不触网)

用法:
    python3 tests/test_brief.py            # 全部通过退出码0
"""

import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))

from bank_daily_brief import classify_announcement, trim_news, \
    latest_trade_date, month_key

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
print("\n[1] classify_announcement 公告分类")
check("定增→资本", classify_announcement("2026年向特定对象发行A股股票预案") == "资本")
check("永续债→资本", classify_announcement("发行无固定期限资本债券获批复") == "资本")
check("权益分派→分红", classify_announcement("2025年中期利润分配方案公告") == "分红")
check("减持→股东", classify_announcement("股东集中竞价减持股份计划公告") == "股东")
check("辞职→治理", classify_announcement("关于行长辞职的公告") == "治理")
check("中报→业绩", classify_announcement("2026年半年度报告") == "业绩")
check("优先级: 定增优先于业绩", classify_announcement("向特定对象发行股票暨2026年度报告说明") == "资本")
check("诉讼类→None丢弃", classify_announcement("涉及诉讼的进展公告") is None)
check("募集说明书→资本(资本工具落地环节, 保留)", classify_announcement("可转换公司债券募集说明书") == "资本")
check("em标签剥离", classify_announcement("2026年半<em>年度报告</em>") == "业绩")
check("空标题→None", classify_announcement("") is None)

# ------------------------------------------------------------
print("\n[2] trim_news 新闻窗口裁剪")
now = datetime(2026, 9, 14, 12, 0, 0)
rows = [
    {"code": "600036", "name": "招商银行", "title": "新", "time": "2026-09-14 10:00:00"},
    {"code": "600036", "name": "招商银行", "title": "旧", "time": "2026-09-01 10:00:00"},
    {"code": "601128", "name": "常熟银行", "title": "a", "time": "2026-09-13 09:00:00"},
    {"code": "601128", "name": "常熟银行", "title": "b", "time": "2026-09-12 09:00:00"},
    {"code": "601128", "name": "常熟银行", "title": "c", "time": "2026-09-11 09:00:00"},
    {"code": "601128", "name": "常熟银行", "title": "d", "time": "2026-09-11 08:00:00"},
]
kept = trim_news(rows, now=now, days=3, per_bank=2, total_cap=10)
check("窗口外(>3天)剔除", all(r["title"] != "旧" for r in kept))
check("每银行限额2条", sum(1 for r in kept if r["code"] == "601128") == 2)
check("窗口内降序排列", kept[0]["title"] == "新")
kept_cap = trim_news(rows + [{"code": "601398", "name": "工商银行",
                              "title": "x", "time": "2026-09-14 09:00:00"}],
                     now=now, days=3, per_bank=2, total_cap=4)
check("总量cap生效", len(kept_cap) == 4)
check("空输入安全", trim_news([], now=now) == [])

# ------------------------------------------------------------
print("\n[3] latest_trade_date 交易日候选")
cand = latest_trade_date(datetime(2026, 9, 14), backtrack=4)
check("首候选=当日", cand[0] == "20260914")
check("回溯4天", len(cand) == 4 and cand[-1] == "20260911")
check("最小回溯1天", latest_trade_date(datetime(2026, 9, 14), backtrack=1) == ["20260914"])

# ------------------------------------------------------------
print("\n[4] month_key 月份键归一")
check("YYYYMM→YYYY-MM", month_key("202603") == "2026-03")
check("中文月→YYYY-MM", month_key("2026年03月份") == "2026-03")
check("非法→None", month_key("无") is None)

print(f"\n===== 结果: {PASS} PASS / {FAIL} FAIL =====")
sys.exit(1 if FAIL else 0)
