# Voluntary Farming v0.2.0 — Dashboard refresh

An English-first release with a redesigned Rich terminal dashboard and clearer setup.

## Highlights
- Dark metric cards, cyan accents, and a reported-hashrate sparkline.
- Dedicated session timer/progress panel and color-coded event stream.
- Responsive wide, standard, and compact terminal layouts.
- English UI, status messages, README, and changelog.
- `START_MINING.bat` for repeat launches after one-time opt-in.
- Generated, clearly labeled demo preview in the README.

## Before running
Mining remains **disabled by default** in this release. Configure your GPU UUID,
review the visible reward address and costs, obtain/verify the official miner,
then explicitly opt in. `trial` is real mining, not the harmless UI demo.
The operator pays electricity; the default reward recipient is the author.

No changes to power, temperature, or time ceilings; no miner binaries, autostart,
automatic downloads, or automatic process restarts are included.

## Verification
51 non-mining tests passed on Windows, covering supervision, recovery, containment,
streaming, Unicode, blocked rendering, and responsive layouts. These tests do not
guarantee third-party miner safety, payouts, profitability, or hardware safety.
