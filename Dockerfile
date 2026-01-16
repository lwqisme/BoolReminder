# 使用Python 3.13作为基础镜像
FROM python:3.13-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 安装Rust工具链（用于编译longbridge）
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# 复制requirements文件
COPY requirements.txt .

# 安装Python依赖（使用RUSTFLAGS绕过兼容性问题）
RUN RUSTFLAGS="-A dependency_on_unit_never_type_fallback" pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建必要的目录和日志目录
RUN mkdir -p config report notify web scheduler logs

# 暴露Flask端口
EXPOSE 5000

# 设置启动命令
CMD ["python", "run.py"]
