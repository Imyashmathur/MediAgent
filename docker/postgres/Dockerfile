FROM postgres:16-alpine
# Keep the same Alpine PostgreSQL family as the existing development volume.
RUN apk add --no-cache --virtual .pgvector-build build-base git && \
    git clone --branch v0.8.7 --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector && \
    make -C /tmp/pgvector OPTFLAGS="" with_llvm=no && \
    make -C /tmp/pgvector install with_llvm=no && \
    rm -rf /tmp/pgvector && apk del .pgvector-build
