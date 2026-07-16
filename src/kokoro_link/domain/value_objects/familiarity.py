"""Familiarity band — the qualitative descriptor for interaction volume
between one character and the operator (Layer 4 of the persona model).

We deliberately avoid an integer "intimacy score": once a number lands
in the prompt the LLM starts arithmetic-reasoning about it ("our score
is 7 so I should act 70% close"), which produces stilted output. The
band stays as a named bucket and renderers translate it to interaction
heat phrases so the LLM treats it as context, not a relationship truth.

This module deliberately does NOT use ``from __future__ import
annotations`` — the ``ClassVar`` singletons pattern relies on
``dataclass`` recognising bare ``ClassVar[...]`` annotations at class
build time, and PEP 563 stringification breaks that detection on
older Pythons (mirrors how ``proactive_outcome.py`` and friends
handle the same trick)."""

from dataclasses import dataclass
from typing import ClassVar


_VALID_VALUES: frozenset[str] = frozenset(
    {"stranger", "acquaintance", "familiar", "close"},
)


@dataclass(frozen=True, slots=True)
class Familiarity:
    """One of four named bands. Compares by ``value``; the class
    attributes below are the canonical singletons callers should use.
    """

    value: str

    STRANGER: "ClassVar[Familiarity]"
    ACQUAINTANCE: "ClassVar[Familiarity]"
    FAMILIAR: "ClassVar[Familiarity]"
    CLOSE: "ClassVar[Familiarity]"

    def __post_init__(self) -> None:
        raw = (self.value or "").strip().lower()
        if raw not in _VALID_VALUES:
            raise ValueError(
                f"Familiarity must be one of {sorted(_VALID_VALUES)}, got {self.value!r}",
            )
        object.__setattr__(self, "value", raw)

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, raw: str) -> "Familiarity":
        return cls(raw)


Familiarity.STRANGER = Familiarity("stranger")
Familiarity.ACQUAINTANCE = Familiarity("acquaintance")
Familiarity.FAMILIAR = Familiarity("familiar")
Familiarity.CLOSE = Familiarity("close")
