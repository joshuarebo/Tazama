# TAZAMA web image, multi-stage, OpenShift compatible, no secrets baked in.
#
# Next.js standalone output is what makes the runtime layer small: it traces the modules
# actually imported and copies only those, so the final image carries no node_modules tree
# and no build toolchain.

FROM node:22-bookworm-slim AS deps
WORKDIR /app
# Dependency layer first, so application edits do not re-resolve the tree.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund


FROM node:22-bookworm-slim AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* values are inlined at build time, so they are supplied as build args and
# must never carry a secret. The API base URL is a hostname, not a credential; anything
# sensitive is read by the server at runtime instead.
ARG NEXT_PUBLIC_API_BASE_URL=""
ARG NEXT_PUBLIC_APP_MODE="demo"
ARG NEXT_PUBLIC_MAP_TILE_URL=""
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_PUBLIC_APP_MODE=$NEXT_PUBLIC_APP_MODE \
    NEXT_PUBLIC_MAP_TILE_URL=$NEXT_PUBLIC_MAP_TILE_URL
RUN npm run build


FROM node:22-bookworm-slim AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    TZ=UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 tazama \
    && useradd --uid 10001 --gid 0 --create-home --shell /usr/sbin/nologin tazama

WORKDIR /app

# Same OpenShift constraint as the backend image: the cluster runs an arbitrary UID that is
# not in /etc/passwd and is always in group 0, so files must be group-0 owned and
# group-writable, and USER must be numeric.
COPY --from=builder --chown=10001:0 /app/.next/standalone ./
COPY --from=builder --chown=10001:0 /app/.next/static ./.next/static
COPY --from=builder --chown=10001:0 /app/public ./public
RUN chmod -R g=u /app

USER 10001
EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://127.0.0.1:3000/ || exit 1

CMD ["node", "server.js"]
