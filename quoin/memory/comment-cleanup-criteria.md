# Comment cleanup — category 3 criteria

Category 3 of the pre-PR comment cleanup (`comment_cleanup.py --emit-candidates`) surfaces
defensive over-explanation for an agent to judge, one block at a time, against the four tests
below. A comment block is removed only when **all four** hold:

1. **Addressed-objection shape.** It argues for the code against an alternative, an objection or a
   misreading, rather than stating what the code does or why it must. Marker forms: "this is not
   X", "one might think", "note that this does not", "no, this is not a bug", "contrary to".
   These same forms are the deterministic pre-filter that narrows what reaches this judgment pass
   at all.
2. **No forward-acting information.** A reader modifying this code would do nothing differently if
   the comment were gone. If it names a constraint — an invariant, an API contract, a platform
   quirk, an ordering requirement, a performance bound — it is forward-acting: KEEP.
3. **Self-contained.** It is a whole comment block and no other comment or code refers to it.
4. **No external referent.** It does not cite a bug, CVE, upstream issue, RFC or platform behavior
   a reader cannot re-derive from the code.

**Tie-break, mandatory: any doubt is a KEEP.** Under-removal is the correct failure direction
because a removal here is unconfirmed and recoverable only by reverting the commit.

## Worked examples

| Comment (abridged) | Verdict | Which test decides |
|---|---|---|
| `Ring buffer, not an unbounded list — see simple_claude.invoke ...; both cells share the same streaming shape.` | KEEP | 2 — names a data-structure constraint |
| `Fixed and non-circular: not imported from the taxonomy below, so the taxonomy can diverge ...` | KEEP | 2 — tells a future editor not to import it |
| `A real OS pipe has a usable fd — that's the case this fix targets. A test double ... keeps the pre-fix line-buffered read ...` | KEEP | 2 — explains the branch immediately below it |
| `this insert is required for that import to resolve, not decorative pattern-parity with the twin wrapper.` | KEEP | 2 — the leading half is forward-acting even though the tail is defensive |
| `Deliberately NOT a substring test — the real save note is a prefix.` | KEEP | 2 — states the actual predicate |
| A block whose every sentence answers an objection and names no constraint | REMOVE | 1 and 2 both satisfied |

The honest read of that table: on a typical tree, category 3 will fire rarely. That is the
criteria working, not failing — the deterministic pre-filter is what keeps a rarely-firing pass
cheap.
