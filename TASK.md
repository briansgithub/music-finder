# Music Finder — Skip + learning patches + genre filters

## Genre chips (authoritative)

When chips are **ON**, recommendations must come from **ANY** of the enabled tags/chips (**union / OR-include** over the enabled set).

- **All chips enabled by default = unconstrained** (same as today; no genre filter).
- Turning a chip **OFF** removes that tag from the allowed set.
- If the user leaves **at least one chip on**, only suggest tracks that match **at least one** enabled chip (casefold exact tag or cluster membership).
- Do **not** use exclude-list / “disabled means filter out” semantics.
- Multi-tag tracks match if **any** enabled chip hits (OR-include).
- Engine field: `enabled_genres` on `/dials` and `/next`.
  - omitted / JSON `null` = unconstrained (all on).
  - non-empty list = allow-list (union).
  - empty list `[]` = every chip off → no genre matches.
- New chips that cross the dial threshold appear **on** (enabled by default). If the user already constrained the set, newly seen chips are added to the allowed set until toggled off.

## Skip (unchanged)

Consolidate Next + Skip into only **Skip**. Skip POSTs `/rate` with `rating: "skip"` (taste update) and shown confidence, then GETs `/next`. `/not_now` remains a separate postpone with no taste penalty.

## Other learning patches (unchanged)

- Candidate title keyword filters before scoring (hard-drop junk; review-tier live/cover stays).
- Sanitize artist/tag vocabulary for overlap + dials.
- Session suppress after meh/skip on junk-pattern titles.
- Persist `predicted_confidence` on `/rate`.
