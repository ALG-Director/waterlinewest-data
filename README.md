# WaterLineWest Data — Phase 2A

This starter repository tests one automated value for the Snowpack module:

- **USGS site:** 09380000 — Colorado River at Lees Ferry, AZ
- **USGS parameter:** 00060 — Discharge, cubic feet per second
- **Published file:** `docs/snowpack-status.json`

The GitHub Action runs `scripts/update_lees_ferry.py`, updates only the `lees_ferry_flow` indicator, and commits the changed JSON back into the repository.

## First setup

1. Create a new GitHub repository named `waterlinewest-data`.
2. Upload these folders/files into the repository:
   - `docs/index.html`
   - `docs/snowpack-status.json`
   - `scripts/update_lees_ferry.py`
   - `.github/workflows/update-snowpack.yml`
   - `README.md`
3. In GitHub, open **Settings → Pages**.
4. Under **Build and deployment**, set:
   - Source: **Deploy from a branch**
   - Branch: **main**
   - Folder: **/docs**
5. Save.
6. In GitHub, open **Actions → Update Snowpack Status → Run workflow**.
7. After it finishes, check this URL pattern:

```text
https://YOUR-GITHUB-USER.github.io/waterlinewest-data/snowpack-status.json
```

Do not switch the Squarespace module to this GitHub URL until that JSON URL opens in a browser.

## What changes automatically?

Phase 2A updates only this section:

```json
"lees_ferry_flow": {
  "display_value": "...",
  "note": "cfs",
  "timestamp": "Observed ...",
  "source_name": "USGS Water Data"
}
```

The snowpack, CBRFC forecast, and Reclamation values remain curated manually until each official source is tested.
