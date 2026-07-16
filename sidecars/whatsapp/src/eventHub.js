export class EventHub {
  constructor({ maxHistory = 100 } = {}) {
    this.maxHistory = maxHistory;
    this.historyBySession = new Map();
    this.subscribersBySession = new Map();
  }

  publish(sessionId, event) {
    const normalized = Object.freeze({
      ...event,
      session_id: sessionId,
      emitted_at: event.emitted_at ?? new Date().toISOString(),
    });
    const history = this.historyBySession.get(sessionId) ?? [];
    history.push(normalized);
    if (history.length > this.maxHistory) {
      history.splice(0, history.length - this.maxHistory);
    }
    this.historyBySession.set(sessionId, history);

    const subscribers = this.subscribersBySession.get(sessionId);
    if (!subscribers) {
      return normalized;
    }
    for (const subscriber of subscribers) {
      subscriber(normalized);
    }
    return normalized;
  }

  history(sessionId) {
    return [...(this.historyBySession.get(sessionId) ?? [])];
  }

  subscribe(sessionId, subscriber, { replay = false } = {}) {
    const subscribers = this.subscribersBySession.get(sessionId) ?? new Set();
    subscribers.add(subscriber);
    this.subscribersBySession.set(sessionId, subscribers);
    if (replay) {
      for (const event of this.history(sessionId)) {
        subscriber(event);
      }
    }
    return () => {
      subscribers.delete(subscriber);
      if (subscribers.size === 0) {
        this.subscribersBySession.delete(sessionId);
      }
    };
  }
}
