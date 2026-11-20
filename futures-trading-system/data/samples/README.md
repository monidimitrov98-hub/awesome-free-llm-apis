# Sample data — SYNTHETIC

`SYNTHETIC_MES_1m.csv` and `SYNTHETIC_MNQ_1m.csv` are **randomly generated** (fixed seed)
by `scripts/generate_synthetic_data.py`. They are **not real market prices** and must not be
used to evaluate any strategy. Their only purpose is to exercise the pipeline.

- 7 CME Globex sessions (trade dates 2025-03-10 to 2025-03-18), 1-minute bars, 1380 bars each.
- Timestamps are naive `America/New_York` local times (bar open). The first session opens
  on 2025-03-09, the day US daylight saving time began, which exercises DST handling.
- Contract symbols `MESH25` / `MNQH25`; prices on the 0.25 tick grid; some overnight bars
  have zero volume on purpose.
- Every file starts with a `# SYNTHETIC DATA` comment line and the file names start with `SYNTHETIC_`.
  The pipeline detects this and prints a warning banner.

Regenerate with `python scripts/generate_synthetic_data.py`.
