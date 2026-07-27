"""External-chat (Hosted Official LINE Channel, Line-H) application services.

LH2 turn state-machine helpers. This package holds pure functions and
orchestration for the ``source="line"`` external chat turn path; the
persistence-layer foundation (receipts + canonical hash) lands first and is
consumed by the turn service in a later slice.
"""
