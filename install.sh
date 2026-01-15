#!/bin/bash
# LongBridge SDK 安装脚本

set -e

echo "正在安装 LongBridge SDK..."

# 检查是否在虚拟环境中
if [ -z "$VIRTUAL_ENV" ]; then
    echo "警告: 未检测到虚拟环境，建议先激活虚拟环境"
    echo "运行: source .venv/bin/activate"
fi

# 检查 Rust 是否安装
if ! command -v cargo &> /dev/null; then
    echo "检测到未安装 Rust，正在安装..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    source "$HOME/.cargo/env"
    echo "Rust 安装完成！"
else
    echo "Rust 已安装: $(cargo --version)"
    # 确保 cargo 在 PATH 中
    if [ -f "$HOME/.cargo/env" ]; then
        source "$HOME/.cargo/env"
    fi
fi

# 安装 longbridge
echo "正在安装 longbridge..."
RUSTFLAGS="-A dependency_on_unit_never_type_fallback" pip install longbridge

echo "安装完成！"
echo ""
echo "验证安装..."
python -c "from longbridge.openapi import QuoteContext; print('✓ longbridge 导入成功！')"
