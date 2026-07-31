FROM node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3

WORKDIR /app

ENV NODE_ENV=production

COPY sidecars/whatsapp/package*.json ./
RUN npm ci --omit=dev --ignore-scripts \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx

COPY sidecars/whatsapp/src ./src

RUN mkdir -p /data/auth /data/media \
    && chown -R node:node /data /app

USER node

EXPOSE 32190

CMD ["node", "src/main.js"]
