FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

ARG KINDLE_SEND_VERSION=v2.0.0-rc-2
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && case "$TARGETARCH" in \
        amd64) KINDLE_ARCH="amd64" ;; \
        arm64) KINDLE_ARCH="arm64" ;; \
        *) echo "Unsupported TARGETARCH: $TARGETARCH" && exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/kindle-send.tar.gz \
        "https://github.com/nikhil1raghav/kindle-send/releases/download/${KINDLE_SEND_VERSION}/kindle-send_${KINDLE_SEND_VERSION#v}_linux_${KINDLE_ARCH}.tar.gz" \
    && tar -xzf /tmp/kindle-send.tar.gz -C /usr/local/bin kindle-send \
    && chmod +x /usr/local/bin/kindle-send \
    && rm -rf /var/lib/apt/lists/* /tmp/kindle-send.tar.gz

COPY api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY api/app /app/app
COPY renderer /app/renderer

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
