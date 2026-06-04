name: Probe Snowpack Basins (manual, read-only)

# Run on demand from the Actions tab. This job ONLY prints a report to the log.
# It writes nothing and commits nothing — safe to run anytime.
on:
  workflow_dispatch:

jobs:
  probe:
    runs-on: ubuntu-latest
    steps:
      - name: Check out repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.x'

      - name: Run snowpack basin probe
        run: python scripts/probe_snowpack_basins.py
