#!/usr/bin/env bash
# ============================================================
# 银行投资分析 Skill 安装脚本
# 流程: git clone 本仓库(任意位置) → bash setup.sh
#       脚本会把 SKILL.md / scripts/ / references/ 安装到
#       Claude Code 的 skills 目录: ~/.claude/skills/bank-investment-analysis
#       之后在 Claude Code 里对话即可使用, 也可以命令行直接跑
# 运行产物(HTML/JSON/SQLite/reports)统一落在安装目录的 workspace/
# 环境变量: CLAUDE_SKILLS_DIR 覆盖 skills 根目录, BANK_PYPI_MIRROR 覆盖 pip 镜像
# ============================================================
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_ROOT="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
TARGET="$SKILLS_ROOT/bank-investment-analysis"
PYPI_MIRROR="${BANK_PYPI_MIRROR:-https://mirrors.aliyun.com/pypi/simple/}"
VENV_DIR="$TARGET/venv"

echo "🏦 银行投资分析 Skill 安装"
echo "  源:   $SOURCE_DIR"
echo "  目标: $TARGET"
echo ""

# 1. 安装 Skill 文件 (workspace 保留, 幂等覆盖)
mkdir -p "$TARGET/workspace" "$TARGET/scripts" "$TARGET/references"
cp "$SOURCE_DIR/SKILL.md" "$TARGET/"
cp "$SOURCE_DIR"/scripts/*.py "$TARGET/scripts/"
cp "$SOURCE_DIR"/references/*.md "$TARGET/references/"
echo "✅ 已安装: SKILL.md + scripts/ + references/ (workspace/ 保留原数据)"

# 2. python + akshare 环境
#    优先级: 安装目录venv > 复用其他skill的venv > 系统 > pip安装 > 自建venv (PEP668兜底)
PY_BIN="python3"
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ 未找到 python3, 请先安装 Python 3.7+"
    exit 1
fi
echo "✅ Python: $(python3 --version)"

if [ -x "$VENV_DIR/bin/python" ] && \
   "$VENV_DIR/bin/python" -c "import akshare" 2>/dev/null; then
    PY_BIN="$VENV_DIR/bin/python"
    echo "✅ 使用已装虚拟环境: $VENV_DIR"
elif [ -x "$HOME/.etf-skill/venv/bin/python" ] && \
     "$HOME/.etf-skill/venv/bin/python" -c "import akshare" 2>/dev/null; then
    PY_BIN="$HOME/.etf-skill/venv/bin/python"
    echo "✅ 复用已有虚拟环境: $HOME/.etf-skill/venv"
elif python3 -c "import akshare" 2>/dev/null; then
    AK_VER=$(python3 -c "import akshare; print(getattr(akshare, '__version__', '?'))" 2>/dev/null || echo "?")
    echo "✅ akshare 已安装 (v$AK_VER)"
elif pip3 install -i "$PYPI_MIRROR" akshare 2>/dev/null; then
    echo "✅ akshare 安装完成"
else
    echo "📦 pip3 受限 (PEP 668 外部管理环境等), 改用独立虚拟环境 ..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip -q -i "$PYPI_MIRROR"
    "$VENV_DIR/bin/pip" install -q -i "$PYPI_MIRROR" akshare
    PY_BIN="$VENV_DIR/bin/python"
    echo "✅ akshare 已装入虚拟环境: $VENV_DIR"
fi

echo ""
echo "=================================================="
echo "🎉 安装完成! 后续命令里的 python 均指: $PY_BIN"
echo ""
echo "Claude Code 对话式 (推荐): 重启 Claude Code 后直接说"
echo "  \"跑一下今天的银行分析\" / \"给招商银行做个体检\""
echo ""
echo "命令行方式:"
echo "  $PY_BIN $TARGET/scripts/bank_analysis.py --healthcheck   # 环境自检 (推荐先跑)"
echo "  $PY_BIN $TARGET/scripts/bank_analysis.py                 # 完整分析 (收盘后)"
echo "  $PY_BIN $TARGET/scripts/bank_analysis.py --detail 600036 # 单只详析"
echo "  $PY_BIN $TARGET/scripts/bank_analysis.py --report 601128 # 下载年报/中报"
echo ""
echo "报告与数据: $TARGET/workspace/"
echo "回归测试:   python3 tests/test_scoring.py (在源码仓库目录)"
