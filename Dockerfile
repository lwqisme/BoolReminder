FROM python:3.13-slim

WORKDIR /app

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y curl build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN RUSTFLAGS="-A dependency_on_unit_never_type_fallback" pip install --no-cache-dir --index-url https://pypi.org/simple -r requirements.txt \
    || RUSTFLAGS="-A dependency_on_unit_never_type_fallback" pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

COPY . .

RUN mkdir -p config report report/drawdown notify web scheduler logs data trade_sync

EXPOSE 5000

CMD ["python", "run.py"]
