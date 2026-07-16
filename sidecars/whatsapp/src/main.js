import pino from "pino";
import { loadConfig } from "./config.js";
import { EventHub } from "./eventHub.js";
import { MediaStore } from "./mediaStore.js";
import { createServer } from "./server.js";
import { SessionManager } from "./sessionManager.js";

const config = loadConfig();
const logger = pino({ level: config.logLevel });
const eventHub = new EventHub({ maxHistory: config.maxEventHistory });
const mediaStore = new MediaStore({ rootDir: config.mediaDir, baseUrl: config.baseUrl });
const sessionManager = new SessionManager({
  authRootDir: config.authDir,
  mediaStore,
  eventHub,
  logger,
  reconnectDelayMs: config.reconnectDelayMs,
});
const server = createServer({ config, sessionManager, eventHub, mediaStore, logger });

server.listen(config.port, config.host, () => {
  logger.info(
    {
      host: config.host,
      port: config.port,
      baseUrl: config.baseUrl,
      authDir: config.authDir,
      mediaDir: config.mediaDir,
    },
    "WhatsApp sidecar listening",
  );
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    logger.info({ signal }, "Stopping WhatsApp sidecar");
    server.close(() => process.exit(0));
  });
}
