# musicrec PWA

Local static client. **Play** opens YouTube Music in a new tab (handoff). Does not stream YouTube.

**Skip** POSTs `/rate` with `skip` (and shown confidence) then loads `/next`. There is no Next button. Genre chips are all-on by default (unconstrained); tap off to leave the allow-list — suggestions then match any remaining chip.

## Run

1. Engine: `cd recommender && ./run.sh serve --host 127.0.0.1 --port 8765`
2. PWA: `cd pwa && npx --yes serve -l 5173` (or any static server) → http://127.0.0.1:5173
3. Open the PWA; the first card loads automatically. **Play in YouTube Music** hands off. **Skip** records a pass.
