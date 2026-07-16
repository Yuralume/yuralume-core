FROM node:22-bookworm-slim

WORKDIR /app

ENV NODE_ENV=production

COPY sidecars/whatsapp/package*.json ./
RUN npm ci --omit=dev --ignore-scripts

COPY sidecars/whatsapp/src ./src

RUN mkdir -p /data/auth /data/media \
    && chown -R node:node /data /app

USER node

EXPOSE 32190

CMD ["node", "src/main.js"]
