# BOLL筛选系统部署文档

## 概述

本系统用于自动分析自选列表中的股票BOLL指标，筛选接近上下轨的股票，并通过邮件和Web界面提供通知。

## 系统架构

- **Flask Web应用**: 提供Web界面查看结果、更新token、手动触发分析
- **定时任务调度器**: 每天北京时间11:00自动执行分析
- **邮件通知服务**: 通过SMTP发送HTML格式的报告
- **Docker容器**: 便于部署和管理

## 首次部署

### 1. 服务器环境要求

- Linux服务器（推荐Ubuntu 20.04+）
- Docker和Docker Compose已安装
- Git已安装

### 2. 克隆代码

```bash
git clone <your-repo-url> BoolReminder
cd BoolReminder
```

### 3. 配置设置

```bash
# 复制配置模板
cp config/config.yaml.example config/config.yaml

# 编辑配置文件
nano config/config.yaml
```

填写以下配置：

- **LongBridge配置**: `app_key`, `app_secret`, `access_token`
- **邮件配置**: SMTP服务器信息、发件人、收件人
- **Web配置**: `secret_key`（随机字符串）、`update_password`（Token更新页面密码）
- **调度配置**: 时区和执行时间（默认11:00）

### 4. 构建和启动

```bash
# 构建Docker镜像
docker-compose build

# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f
```

### 5. 访问Web界面

打开浏览器访问: `http://服务器IP:5000`

## 更新部署

当代码更新后，在服务器上执行：

```bash
# 方式1: 使用部署脚本（推荐）
./deploy.sh

# 方式2: 手动执行
git pull
docker-compose build
docker-compose down
docker-compose up -d
```

## 配置文件管理

### 配置文件位置

- `config/config.yaml`: 实际配置文件（**不提交到Git**）
- `config/config.yaml.example`: 配置模板（提交到Git）

### 更新Token

有两种方式更新token：

1. **通过Web界面**（推荐）:
   - 访问 `http://服务器IP:5000/update-token`
   - 输入密码和新的token
   - 点击更新

2. **直接编辑配置文件**:
   ```bash
   nano config/config.yaml
   # 修改 access_token 字段
   # 重启容器: docker-compose restart
   ```

## 功能说明

### Web界面

- **首页 (`/`)**: 显示最新分析结果（HTML表格）
- **更新Token (`/update-token`)**: 更新LongBridge access_token
- **API接口**:
  - `GET /api/result`: 获取最新结果（JSON）
  - `POST /api/trigger`: 手动触发分析
  - `POST /api/update-token`: 更新token（需要密码）

### 定时任务

- 每天北京时间11:00自动执行分析
- 自动生成HTML报告
- 自动发送邮件通知

### 邮件通知

- 邮件格式: HTML表格
- 发送时机: 定时任务执行后
- 收件人: 配置文件中指定的邮箱列表

## 维护操作

### 查看日志

```bash
# 查看所有日志
docker-compose logs -f

# 查看最近100行
docker-compose logs --tail=100
```

### 重启服务

```bash
docker-compose restart
```

### 停止服务

```bash
docker-compose down
```

### 更新代码

```bash
./deploy.sh
```

## 故障排查

### 1. 容器无法启动

- 检查配置文件是否存在: `ls -la config/config.yaml`
- 查看错误日志: `docker-compose logs`
- 检查端口是否被占用: `netstat -tuln | grep 5000`

### 2. 邮件发送失败

- 检查SMTP配置是否正确
- 确认SMTP服务器支持TLS
- 查看应用日志中的错误信息

### 3. Token过期

- 通过Web界面更新token
- 或直接编辑配置文件后重启容器

### 4. 定时任务不执行

- 检查时区配置: `schedule.timezone` 应为 `Asia/Shanghai`
- 查看调度器日志
- 确认容器时间正确: `docker-compose exec boll-reminder date`

## 安全建议

1. **生产环境**:
   - 使用HTTPS（通过Nginx反向代理）
   - 设置强密码
   - 定期更新token

2. **配置文件安全**:
   - 不要将 `config/config.yaml` 提交到Git
   - 使用环境变量存储敏感信息（可选）

3. **访问控制**:
   - 考虑添加IP白名单
   - 使用防火墙限制端口访问

## 环境变量支持

可以通过环境变量覆盖配置（在docker-compose.yml中设置）：

```yaml
environment:
  - LONGBRIDGE_APP_KEY=your_key
  - LONGBRIDGE_APP_SECRET=your_secret
  - LONGBRIDGE_ACCESS_TOKEN=your_token
```

## 联系支持

如遇问题，请检查：
1. 应用日志: `docker-compose logs`
2. 配置文件格式是否正确（YAML语法）
3. LongBridge API是否正常
4. 网络连接是否正常
