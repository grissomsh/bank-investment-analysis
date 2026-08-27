---
name: bank-investment-analysis
description: A股银行股/银行ETF 定量投资分析系统 — 四层框架（数据层→行业温度→个股五维评分→组合建议）。行业层用板块PB历史分位+股息率-10Y国债利差+动量合成温度分决定ETF配置节奏；个股层对42家上市银行做同业截面打分（盈利30%/资产质量25%/成长15%/资本10%/估值20%）并施加不良率/拨备覆盖率一票否决降档。自动拉取东财F10银行专项财务(净息差/不良率/拨备覆盖率/资本充足率)、估值史、中证官方指数估值、国债收益率，输出控制台表格+HTML报告+JSON。当用户需要分析银行股、给银行股评分排名、判断银行ETF当前是否值得买入、查询银行板块估值分位或股债性价比时使用；也适用于评分权重与阈值的调整讨论。
---

# bank-investment-analysis — A股银行投资定量分析

## 核心文件

- **`scripts/bank_analysis.py`** — 主分析脚本：拉数 → 行业温度 → 个股五维评分 → 表格/HTML/JSON
- **`scripts/bank_universe.py`** — 单点配置：42家银行池 + 模型常量（权重/GATES/映射表）
- **`scripts/bank_data_store.py`** — SQLite 存档（中证官方估值每日累积）
- **`tests/test_scoring.py`** — 打分模型离线回归测试（不触网）
- **`references/bank_framework.md`** — 框架方法论详解（先读这个再动模型常量）

---

## 快速开始

```bash
# 部署（首次）: 建目录 + 复制脚本 + 准备akshare环境
bash setup.sh

# 完整分析（约1~3分钟, 42家×2次接口调用）
cd ~/.bank-skill/scripts
python3 bank_analysis.py                 # 注意: PEP668环境请用 venv 内的 python
```

> 本机约定（同 etf-three-factor）：Homebrew Python 受 PEP 668 限制，setup.sh 会装到 `~/.bank-skill/venv`；已存在 `~/.etf-skill/venv` 时也可直接复用：`~/.etf-skill/venv/bin/python bank_analysis.py`

### 子命令

| 功能 | 命令 |
| --- | --- |
| 环境自检（跑任何分析前建议先执行） | `python3 bank_analysis.py --healthcheck` |
| 附带单只个股详析 | `python3 bank_analysis.py --detail 600036` |
| 不生成 HTML（只要表格+JSON） | `python3 bank_analysis.py --no-html` |
| 查看本地数据库状态 | `python3 bank_analysis.py --stats` |
| 打分模型离线回归 | `python3 tests/test_scoring.py` |

---

## 输出文件

| 文件 | 位置 | 说明 |
| --- | --- | --- |
| HTML报告 | `~/.bank-skill/workspace/银行投资分析.html` | 温度卡 + 全量评分表 + ETF池 |
| JSON数据 | `~/.bank-skill/workspace/银行投资分析.json` | 全部结构化结果 |
| SQLite | `~/.bank-skill/workspace/bank_history.db` | 中证官方PE/股息率逐日累积 |

工作区可用环境变量 `BANK_WORKSPACE` 覆盖。

---

## 模型一页纸

```text
L1 行业温度 = PB分位分×50% + 股债利差分×35% + 动量确认×15%
    ≥75 低估·积极配置 | 55–75 正常定投 | 40–55 持有不加仓 | <40 减持观察

L2 个股基础分 = 盈利30% + 资产质量25% + 成长15% + 资本充足10% + 估值吸引力20%
    （各维度内按指标截面百分位加权; 缺失自动重归一）
一票否决: 不良率>2.0% 或 拨备覆盖率<130% → 强制≤C档; 核心一级<8.5% → 再降一档
档位: A+≥80 / A≥70 / B≥60 / C≥50 / D<50

L3 组合: ETF底仓看温度档位 · 个股卫星取高分池 · 控制大行/股份/城商/农商分散度
```

报告另含现价/涨跌幅/PE-TTM/PE动态(=总市值÷年化归母净利)等行情参考列，
仅作观察，不参与评分（银行股锚定 PB/PB÷ROE，见 framework 文档 §3.2）。

全部权重、阈值、分段映射集中在 `scripts/bank_universe.py`，方法论依据与字段口径坑见 `references/bank_framework.md`。

**改模型常量的纪律**：现行权重为理论结构 + 经验设定，未经统计拟合校准；调整任何权重/阈值前先读 framework 文档 §6，改动后必须重跑 `tests/test_scoring.py`。

---

## 数据源

| 数据 | API | 说明 |
| --- | --- | --- |
| 银行专项财务 | akshare `stock_financial_analysis_indicator_em` | 东财F10, 含NIM/不良/拨备/资本充足率 |
| 个股PB/PE史 | akshare `stock_value_em` | 2018起, 合成板块中位数PB序列 |
| 指数官方估值 | akshare `stock_zh_index_value_csindex` | 仅近20日, 依赖SQLite逐日累积 |
| 日K线 | 腾讯 `web.ifzq.gtimg.cn` | 股票/指数/ETF 通吃 |
| 国债收益率 | akshare `bond_zh_us_rate` | 10Y 用于利差因子 |
| ETF清单 | akshare `fund_etf_spot_em` | 名称含"银行"按规模Top8动态发现 |

---

## 故障排查

```bash
python3 bank_analysis.py --healthcheck   # 五路数据源+DB逐一自检, 失败退出码1
```

- **财务数据大面积失败** → 多为网络/代理问题（东财 datacenter 走代理偶发被风控），可换网络重试；脚本自身每只股票失败会自动重试一次。
- **温度分只剩部分子分** → 对应数据源当日不可得，模型自动降级并权重归一，属正常容错。
- **中证估值"本地累积 N 行"增长缓慢** → 该接口只回吐近20日，需持续每日运行攒历史；板块PB分位不依赖它（由成分股估值史合成），冷启动即可用。
- **单一银行缺某指标**（如个别行不披露拨贷比） → 该子项跳过、维度内权重重归一，该行覆盖度 <100% 属预期行为。

更多细节:

- 方法论/公式推导/口径验证 → `references/bank_framework.md`
