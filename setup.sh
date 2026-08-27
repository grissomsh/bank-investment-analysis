#!/usr/bin/env bash
# ============================================================
# 银行投资分析 skill 一键部署脚本
# 用法: bash setup.sh
# 功能: 建目录 → 复制脚本 → 准备akshare环境 → 打印使用说明
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_HOME="${BANK_SKILL_HOME:-$HOME/.bank-skill}"
SCRIPTS_DIR="$SKILL_HOME/scripts"
WORKSPACE_DIR="$SKILL_HOME/workspace"
PYPI_MIRROR="${BANK_PYPI_MIRROR:-https://mirrors.aliyun.com/pypi/simple/}"

echo "🏦 银行投资分析 skill 部署"
echo "  目标目录: $SKILL_HOME"
echo ""

mkdir -p "$SCRIPTS_DIR" "$WORKSPACE_DIR"
echo "✅ 脚本目录: $SCRIPTS_DIR"
echo "✅ 工作区:   $WORKSPACE_DIR"

cp "$SCRIPT_DIR/scripts/bank_analysis.py" "$SCRIPTS_DIR/"
cp "$SCRIPT_DIR/scripts/bank_universe.py" "$SCRIPTS_DIR/"
cp "$SCRIPT_DIR/scripts/bank_data_store.py" "$SCRIPTS_DIR/"
echo "✅ 脚本已复制: bank_analysis.py + bank_universe.py + bank_data_store.py"

if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ 未找到 python3, 请先安装 Python 3.7+"
    exit 1
fi
echo "✅ Python: $(python3 --version)"

# akshare 环境: 复用已有的 etf-skill venv > 系统 > 自建 venv (PEP668 兜底)
VENV_DIR="$SKILL_HOME/venv"
PY_BIN="python3"
if [ -x "$HOME/.etf-skill/venv/bin/python" ] && \
   "$HOME/.etf-skill/venv/bin/python" -c "import akshare" 2>/dev/null; then
    PY_BIN="$HOME/.etf-skill/venv/bin/python"
    echo "✅ 复用已有虚拟环境: $HOME/.etf-skill/venv"
elif [ -x "$VENV_DIR/bin/python" ]; then
    PY_BIN="$VENV_DIR/bin/python"
    echo "✅ 使用虚拟环境: $VENV_DIR"
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
echo "🎉 部署完成! 常用命令:"
echo "  cd $SCRIPTS_DIR"
echo "  $PY_BIN bank_analysis.py --healthcheck   # 环境自检 (推荐先跑)"
echo "  $PY_BIN bank_analysis.py                 # 完整分析"
echo "  $PY_BIN bank_analysis.py --detail 600036 # 附带单只详析"
echo "  回归测试: python3 tests/test_scoring.py (在源码目录)"
