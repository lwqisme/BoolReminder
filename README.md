# BoolReminder - BOLL指标筛选系统

自动分析自选列表中的股票BOLL指标，筛选接近上下轨的股票，并通过邮件和Web界面提供通知。

## 功能特性

- 📊 **自动分析**: 每天北京时间11:00自动分析自选列表中的股票
- 📧 **邮件通知**: 自动发送HTML格式的分析报告
- 🌐 **Web界面**: 查看最新结果、更新token、手动触发分析
- 🔄 **Token管理**: 通过Web界面方便地更新LongBridge token
- 🐳 **Docker部署**: 一键部署，易于维护

## 快速开始

### 本地开发

1. **安装依赖**:
   ```bash
   # 安装Rust（如果需要）
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
   source "$HOME/.cargo/env"
   
   # 安装Python依赖
   RUSTFLAGS="-A dependency_on_unit_never_type_fallback" pip install -r requirements.txt
   ```

2. **配置设置**:
   ```bash
   cp config/config.yaml.example config/config.yaml
   # 编辑 config/config.yaml 填写配置
   ```

3. **运行**:
   ```bash
   python run.py
   ```

### Docker部署

详细部署说明请参考 [DEPLOYMENT.md](DEPLOYMENT.md)

**快速部署**:
```bash
# 1. 配置
cp config/config.yaml.example config/config.yaml
nano config/config.yaml

# 2. 构建和启动
docker-compose up -d

# 3. 查看日志
docker-compose logs -f
```

## 配置说明

配置文件: `config/config.yaml`

主要配置项：
- **longbridge**: LongBridge API配置（app_key, app_secret, access_token）
- **email**: SMTP邮件配置
- **web**: Web服务配置（端口、密钥、更新密码）
- **schedule**: 定时任务配置（时区、执行时间）

详细配置说明请参考 `config/config.yaml.example`

## Web界面

启动后访问: `http://localhost:5000`

功能：
- **首页**: 查看最新分析结果
- **更新Token**: `/update-token` - 更新LongBridge access_token
- **手动触发**: 点击"手动触发分析"按钮

## 定时任务

- 默认执行时间: 每天北京时间11:00
- 自动生成HTML报告
- 自动发送邮件通知

## 邮件通知

- 格式: HTML表格
- 内容: 完整的BOLL分析结果
- 收件人: 配置文件中指定的邮箱列表

## 项目结构

```
BoolReminder/
├── config/              # 配置管理
│   ├── config_manager.py
│   └── config.yaml.example
├── report/              # HTML报告生成
│   └── html_generator.py
├── notify/              # 邮件通知
│   └── email_sender.py
├── web/                 # Flask Web应用
│   └── app.py
├── scheduler/           # 定时任务
│   └── task_scheduler.py
├── watchlist_boll_filter.py  # 主分析逻辑
├── run.py              # 启动脚本
├── Dockerfile          # Docker镜像
├── docker-compose.yml  # Docker Compose配置
└── deploy.sh          # 部署脚本
```

## 更新部署

使用Git部署到远程服务器：

```bash
# 在服务器上执行
./deploy.sh
```

或手动：
```bash
git pull
docker-compose build
docker-compose down
docker-compose up -d
```

## 依赖

- Python 3.13+
- Rust工具链（用于编译longbridge）
- Docker和Docker Compose（用于部署）

Python包依赖见 `requirements.txt`

## 文档

- [部署文档](DEPLOYMENT.md) - 详细的部署和使用说明
- [配置模板](config/config.yaml.example) - 配置文件示例

## 许可证

MIT License
