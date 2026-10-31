# Changelog

## 0.2.0 — 2026-10-31

### Dashboard
- New dark dashboard with cyan accents and four metric cards.
- Recent reported-hashrate sparkline and dedicated session-progress panel.
- Color-coded event stream with `SHARE`, `SPEED`, `JOB`, and warning labels.
- Responsive wide, standard, and compact layouts; long screen lines are truncated
  without changing the raw log.
- English UI, controller status messages, demo, tests, and documentation.

### Launching
- Added `START_MINING.bat` for explicit normal-session launching after one-time opt-in.
- Preserved the disabled-by-default public configuration and placeholder GPU UUID.
- No changes to hard power/thermal/time ceilings, recovery ordering, or auto-start behavior.

### Documentation & verification
- Redesigned README with a clearly labeled illustrative dashboard preview.
- Expanded cost, consent, pool-reconnection, and recovery guidance.
- Added layout, speed-history, and temperature-display checks.
- Made the disabled-state test independent of the developer's local opt-in setting.

## Initial publication — 2026-10-31

- Explicit opt-in Windows GPU mining controller with power-only UAC helper.
- Windows Job Object containment, bounded sessions, and original-power recovery journal.
- Rich UI, raw logs, and non-mining tests.
