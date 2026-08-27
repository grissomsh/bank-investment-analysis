# bank-investment-analysis

A股银行股 / 银行 ETF 定量投资分析系统。回答两个问题：

1. **现在该不该加银行敞口？** → L1 行业温度分（板块 PB 历史分位 + 股息率−10Y 国债利差 + 动量确认）
2. **如果要选个股，选谁？** → L2 对 42 家上市银行做同业截面五维评分 + 资产质量一票否决

数据全自动拉取：东财 F10 银行专项财务指标（净息差/不良率/拨备覆盖率/三级资本充足率等，非通用字段）、个股估值史（2018 起）、中证官网指数估值、腾讯 K 线、10Y 国债收益率、ETF 实时清单。

## Skill 描述

- 数据获取：六路免费数据源，单点失败自动降级
- 行业温度：`PB分位×50% + 股债利差×35% + 动量×15%`，四档配置动作
- 个股评分：盈利 30 / 资产质量 25 / 成长 15 / 资本充足 10 / 估值吸引力 20，截面百分位打分
- 安全机制：不良率>2% 或拨备覆盖率<130% 强制降档；核心一级<8.5% 再降一档
- 输出：控制台表格 + HTML 报告 + JSON；中证官方估值逐日 SQLite 累积

适用场景：分析银行板块估值位置、给银行股评分排名、决定银行 ETF 定投节奏、单只银行深度体检（`--detail`）。

## 项目结构

- `SKILL.md`：skill 入口说明
- `setup.sh`：一键部署脚本
- `scripts/bank_analysis.py`：主流水线
- `scripts/bank_universe.py`：监控池与模型常量（单点定义）
- `scripts/bank_data_store.py`：SQLite 存档模块
- `references/bank_framework.md`：框架方法论详解（DDM 推导 / 权重依据 / 字段口径坑）
- `tests/test_scoring.py`：打分模型离线回归测试

## 快速开始

```bash
# 1. 一键部署
bash setup.sh

# 2. 环境/数据源自检
~/.bank-skill/venv/bin/python ~/.bank-skill/scripts/bank_analysis.py --healthcheck

# 3. 完整分析（1~3分钟）
cd ~/.bank-skill/scripts && ./venv/bin/python bank_analysis.py   # 或复用 ~/.etf-skill/venv
```

常用命令：

- `python3 bank_analysis.py --detail 600036`：附带招行详析
- `python3 bank_analysis.py --no-html`：只要表格与 JSON
- `python3 bank_analysis.py --stats`：本地数据库状态
- `python3 tests/test_scoring.py`：离线回归（46 用例）

## 五维评分速览

| 维度 | 权重 | 核心指标 |
| --- | --- | --- |
| 盈利能力 | 30% | 年化加权ROE · 净息差 · ROA · 成本收入比(反) |
| 资产质量 | 25% | 不良率(反) · 拨备覆盖率 · 拨贷比 |
| 成长性 | 15% | 归母净利同比 · 营收同比 |
| 资本充足 | 10% | 核心一级资本充足率 |
| 估值吸引力 | 20% | PB自身历史分位(反) · 相对板块PB溢价(反) · PB÷年化ROE(反) |

理论依据：`PB = ROE×d/(k−g)` —— ROE 是估值的锚，资产质量是账面价值的可信度开关，股息率与国债的利差是板块层面的配置信号。完整推导见[references/bank_framework.md](references/bank_framework.md)。

## 版权与声明

- 本 skill 为个人量化研究工具，参考了公开的 PB-ROE 银行研究共识框架
- 全部输出不构成投资建议，据此交易风险自负
