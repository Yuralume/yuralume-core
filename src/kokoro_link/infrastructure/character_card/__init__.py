"""Character card (.lumecard) packaging infrastructure.

A character card is a single zip container that bundles a character's
*portable static settings* (A-layer) plus optional arc-template
blueprints, for sharing / preset characters / marketplace install.

This package holds the pure-IO concerns:

- :mod:`packager` — read/write the zip container (manifest + stage
  images + arc-template YAMLs), with a zip-slip guard on unpack.
- :mod:`arc_template_yaml` — serialise an ``ArcTemplate`` entity back to
  the same YAML shape the bundled-pack loader reads, so an exported
  template round-trips through the existing loader on import.

Deployment-bound settings (provider/profile routing, voice, LoRAs) and
runtime accumulation (state, memory, persona, schedule, feed, ...) are
deliberately *not* part of a card — see ``docs/CHARACTER_CARD_PLAN.md``.
"""
