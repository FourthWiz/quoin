Frozen copy of the pre-component Stream Deck runtime scripts, byte-identical to
the prototype as first committed. Compatibility tests load these to prove the
new monitor and dashboard still interoperate with a running prototype, and the
later state-migration tests reuse them as input. They are never shipped (the
sdist excludes `quoin/dev/`).
