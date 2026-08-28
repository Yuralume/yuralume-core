"""The chat prompt, one section at a time.

``default.py`` builds a frozen :class:`~.context.PromptSectionContext`
and hands it to :func:`~.registry.assemble`; everything else about the
prompt lives here:

``context.py``
    the typed snapshot every section reads;
``order.py``
    the single table that fixes section order;
``registry.py``
    ``PromptSection``, the registry, the mutual-exclusion resolvers, and
    the assembler;
``identity/state/schedule/story/tools/vision/dialogue.py``
    the renderers themselves, grouped by domain, each ending in a
    ``SECTIONS`` tuple that binds its blocks to names in the table;
``text.py``
    the handful of strings and helpers shared across those groups.

Adding a block is three edits: a renderer, a ``SECTIONS`` entry, and a
name in ``order.py`` — the structural tests in
``tests/unit/prompt_sections`` fail loudly if you skip one of them.
"""
