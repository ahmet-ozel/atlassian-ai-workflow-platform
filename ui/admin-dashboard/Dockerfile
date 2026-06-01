# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# admin-dashboard — multi-stage Dockerfile (Next.js 14 UI on :3000)
# ---------------------------------------------------------------------------
# Build context = platform root (parent of ui/).
# ---------------------------------------------------------------------------

FROM node:20-alpine AS builder

WORKDIR /build

# Copy the local lib dependency
COPY libs/web-shared/ ./libs/web-shared/

# Copy package files
COPY ui/admin-dashboard/package.json ./

# Fix the file: reference to point to local copy and install
RUN sed -i 's|file:../../libs/web-shared|file:./libs/web-shared|' package.json && npm install

# Copy source
COPY ui/admin-dashboard/tsconfig.json ui/admin-dashboard/next.config.mjs ./
COPY ui/admin-dashboard/app/ ./app/
COPY ui/admin-dashboard/components/ ./components/
COPY ui/admin-dashboard/lib/ ./lib/

RUN npm run build

# ---------------------------------------------------------------------------

FROM node:20-alpine AS runtime

RUN adduser -D -u 10001 appuser

WORKDIR /app

ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

COPY --from=builder --chown=appuser:appuser /build/package.json ./package.json
COPY --from=builder --chown=appuser:appuser /build/next.config.mjs ./next.config.mjs
COPY --from=builder --chown=appuser:appuser /build/.next ./.next
COPY --from=builder --chown=appuser:appuser /build/node_modules ./node_modules
COPY --from=builder --chown=appuser:appuser /build/app ./app
COPY --from=builder --chown=appuser:appuser /build/components ./components
COPY --from=builder --chown=appuser:appuser /build/lib ./lib

USER appuser

EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost:3000/api/health || exit 1

CMD ["npm", "start"]
