import { BaileysSession } from "./baileysSession.js";
import { normalizeSessionId } from "./sessionId.js";

export class SessionManager {
  constructor({ authRootDir, mediaStore, eventHub, logger, reconnectDelayMs }) {
    this.authRootDir = authRootDir;
    this.mediaStore = mediaStore;
    this.eventHub = eventHub;
    this.logger = logger;
    this.reconnectDelayMs = reconnectDelayMs;
    this.sessions = new Map();
  }

  get(sessionId) {
    const safeSessionId = normalizeSessionId(sessionId);
    let session = this.sessions.get(safeSessionId);
    if (!session) {
      session = new BaileysSession({
        sessionId: safeSessionId,
        authRootDir: this.authRootDir,
        mediaStore: this.mediaStore,
        eventHub: this.eventHub,
        logger: this.logger.child({ sessionId: safeSessionId }),
        reconnectDelayMs: this.reconnectDelayMs,
      });
      this.sessions.set(safeSessionId, session);
    }
    return session;
  }

  async start(sessionId) {
    const session = this.get(sessionId);
    await session.start();
    return session;
  }
}
