FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       calibre \
       python3 \
       python3-pip \
       ca-certificates \
       fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY scripts/ /app/scripts/
COPY recipes/ /app/recipes/

RUN pip3 install --no-cache-dir pypdfium2 Pillow

COPY scripts/news_sync_docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
