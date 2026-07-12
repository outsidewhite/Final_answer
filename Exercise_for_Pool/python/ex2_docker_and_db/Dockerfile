FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# 課題2-1で確認できるように、Ubuntu20.04上にPython3.8とMySQLクライアントを用意する。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        default-mysql-client \
        python3.8 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python3.8 -m pip install --no-cache-dir -r requirements.txt

COPY 2-2.py .

CMD ["sleep", "infinity"]
