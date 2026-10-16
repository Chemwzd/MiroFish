FROM python:3.11-slim

# Mainland-China network: default to a reachable PyPI mirror, overridable.
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    ZEPSHIM_DB=/data/zepshim.sqlite3 \
    ZEPSHIM_PORT=8100

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt

COPY *.py ./

VOLUME ["/data"]
EXPOSE 8100

CMD ["python", "app.py"]
