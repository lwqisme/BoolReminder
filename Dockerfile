FROM python:3.13-slim

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y curl \
    && rm -rf /var/lib/apt/lists/*
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install --no-cache-dir --index-url https://mirrors.cloud.tencent.com/pypi/simple -r requirements.txt \
    || pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt \
    || pip install --no-cache-dir --index-url https://mirrors.aliyun.com/pypi/simple -r requirements.txt

COPY . .

RUN mkdir -p config report report/drawdown notify web scheduler logs data trade_sync

EXPOSE 5000

CMD ["python", "run.py"]
