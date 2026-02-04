FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV QTWEBENGINE_DISABLE_SANDBOX=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       calibre \
       python3 \
       python3-pip \
       ca-certificates \
       fontconfig \
       libopengl0 \
       wget \
       xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh | sh /dev/stdin

WORKDIR /app

COPY scripts/ /app/scripts/
COPY recipes/ /app/recipes/

RUN pip3 install --no-cache-dir --upgrade pypdfium2 Pillow

COPY scripts/news_sync_docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
