#!/bin/bash
# 服务器端部署脚本
# 用于更新代码并重启Docker容器

set -e

echo "=========================================="
echo "BOLL筛选系统 - 部署脚本"
echo "=========================================="

# 检查是否在项目目录
if [ ! -f "docker-compose.yml" ]; then
    echo "错误: 请在项目根目录执行此脚本"
    exit 1
fi

# 1. 拉取最新代码
echo ""
echo "步骤 1: 拉取最新代码..."
git pull

# 2. 检查配置文件
echo ""
echo "步骤 2: 检查配置文件..."
if [ ! -f "config/config.yaml" ]; then
    if [ -f "config/config.yaml.example" ]; then
        echo "配置文件不存在，从模板创建..."
        cp config/config.yaml.example config/config.yaml
        echo "请编辑 config/config.yaml 填写配置信息"
        exit 1
    else
        echo "错误: 配置文件模板不存在"
        exit 1
    fi
fi

# 3. 构建/更新Docker镜像
echo ""
echo "步骤 3: 构建Docker镜像..."
docker-compose build

# 4. 重启容器
echo ""
echo "步骤 4: 重启容器..."
docker-compose down
docker-compose up -d

# 5. 显示状态
echo ""
echo "步骤 5: 检查容器状态..."
docker-compose ps

echo ""
echo "=========================================="
echo "部署完成！"
echo "=========================================="
echo ""
echo "查看日志: docker-compose logs -f"
echo "停止服务: docker-compose down"
echo "访问Web界面: http://服务器IP:5000"
echo ""
