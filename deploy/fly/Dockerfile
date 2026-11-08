# Build and run image for the Fly.io deployment (docs/FLY-DEPLOYMENT-PLAN.md). One image, two roles:
#   frontend: pando serve   (Git HTTP, packfile URIs, background compaction; needs git for recompression)
#   meta:     pando meta-serve
FROM rust:1.98-bookworm AS build
WORKDIR /src
COPY server/ server/
RUN cd server && cargo build --release --locked

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates python3 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/server/target/release/pando /usr/local/bin/pando
COPY deploy/fly/entrypoint.sh /usr/local/bin/entrypoint.sh
# test-only object-store fault relay (acceptance test T7); started only when PANDO_TEST_RELAY=1
COPY verify/fault/s3relay.py /usr/local/bin/s3relay.py
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
