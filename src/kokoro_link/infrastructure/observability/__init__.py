"""Observability infrastructure — turn recording, latency / token capture.

Sister of ``infrastructure/persistence/sa_turn_record_*``: the recorder
here is the *write path* invoked by application services on every LLM
turn. Repository implementations live alongside the other SA repos so
all DB-touching code stays in one place.
"""
