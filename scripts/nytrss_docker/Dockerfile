FROM haskell:9.6

RUN apt-get update \
    && apt-get install -y --no-install-recommends libsqlite3-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY nytrss_src/ /app/

RUN cabal update \
    && cabal install --lib HandsomeSoup feed file-embed http-conduit sqlite-simple xml-conduit

RUN ghc -threaded ./Main.hs -o /usr/local/bin/nytrss -Wall

WORKDIR /data
VOLUME ["/data"]

ENV NYTRSS_COOKIE_FILE=/data/nyt_cookies.txt

CMD ["nytrss"]
