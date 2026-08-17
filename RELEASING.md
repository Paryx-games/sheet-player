# Shipping an update

## One-time setup

1. Create the `paryx-games/piano-player` repo on GitHub (public - the
   app fetches `version.json` unauthenticated via raw.githubusercontent.com,
   which only works on public repos).
2. Commit `version.json` to the repo root, on the `main` branch.

If you use a different repo name/org, or a different branch than
`main`, update `VERSION_MANIFEST_URL` in `updater.py` to match -
that URL is the one thing that has to stay in sync by hand.

## Every time you ship a new version

1. **Bump `CURRENT_VERSION` in `main.py`** to the new version, e.g.
   `"1.1.0"`. This is what gets compared against `version.json` -
   forgetting this step means the app you just built still thinks
   it's the old version and will immediately offer to "update" to
   itself.

2. **Build the exe**: `pyinstaller main.spec` (see BUILD.md).

3. **Create a GitHub Release** with a tag matching the version
   (e.g. `v1.1.0`), and upload `dist/PianoPlayer.exe` as a release
   asset.

4. **Update `version.json`** in the repo and push to `main`:
   ```json
   {
     "version": "1.1.0",
     "download_url": "https://github.com/paryx-games/sheet-player/releases/download/v1.1.0/PianoPlayer.exe",
     "changelog": "whatever changed, shown in the update dialog",
     "mandatory": false
   }
   ```
   `download_url` must point at the exact release asset URL - GitHub
   generates this automatically once you upload the file to the
   release, copy it from there rather than typing it by hand.

5. Done. Anyone running an older version sees the update dialog next
   time they launch.

## The `mandatory` flag

Set `"mandatory": true` when you need to force everyone off a broken
build - it removes the "skip"/"remind me later" buttons entirely and
disables closing the dialog, leaving only "update now". Use sparingly;
it's a hard interruption.

## Order matters

Push `version.json` **after** the release/asset exists, not before -
if the manifest claims a version is available before the download URL
is live, anyone who happens to check in that window gets a 404 on
download.
