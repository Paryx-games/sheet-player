# Building and shipping the installer

This app is now distributed as an Inno Setup installer, not as a loose
PyInstaller EXE. The built app still starts from a PyInstaller bundle, but
actual updates and end-user distribution happen through the installer.

Must be run on Windows. PyInstaller does not cross-compile.

## 1. Set up a clean environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Using a clean venv avoids accidentally bundling unrelated packages into the
app and bloating the installer.

## 2. Build the app bundle

```powershell
pyinstaller main.spec
```

This produces the app payload used by the installer, usually at:

```text
dist\PianoPlayer.exe
```

## 3. Build the installer

From the installer folder, compile the Inno Setup script:

```powershell
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\PianoPlayer.iss
```

This produces the installable artifact:

```text
installer\PianoPlayerSetup.exe
```

That is the file to upload to the GitHub Release and the file the updater
downloads.

## 4. Release flow

1. Bump `CURRENT_VERSION` in `main.py`.
2. Update `version.json` in the repo root.
3. Build the PyInstaller app bundle.
4. Compile the installer with ISCC.
5. Create a GitHub Release with a tag matching the version.
6. Upload `installer\PianoPlayerSetup.exe` as the release asset.
7. Push the updated `version.json` after the asset exists.

The updater checks `version.json`, downloads the installer asset, silently
uninstalls the old app, runs the new installer, and relaunches the app.

## 5. Verify before release

Check these specifically before publishing a release:

- [ ] App launches normally and shows the main UI
- [ ] Admin overlay appears correctly when launched without elevation
- [ ] "request admin" triggers the UAC prompt and relaunches as admin
- [ ] F6/F7/F8 hotkeys still work in the bundled app
- [ ] Window/exe picker lists real running processes
- [ ] A test sheet actually plays and reaches the target window
- [ ] The generated installer installs cleanly
- [ ] The installer can uninstall a previous version silently

## 6. Troubleshooting

**Hotkeys (F6/F7/F8) do nothing in the built exe:**
The `keyboard` package often needs its bundled resources to be included with
the PyInstaller build. If this happens, run the app from a terminal first so
errors are visible before the window closes.

**The app still updates by replacing the EXE in place:**
That is the old behavior. The current installer-based updater intentionally
avoids replacing the running executable and instead performs a clean
uninstall/install.

**PyInstaller or pywin32 errors:**
Try reinstalling the dependencies and rerunning the build:

```powershell
python -m pip install --upgrade pywin32
python .venv\Scripts\pywin32_postinstall.py -install
pyinstaller main.spec
```

**Windows SmartScreen warnings:**
Unsigned release binaries will often trigger SmartScreen warnings. That is
normal for an indie-built installer and not an indication that the app is
broken.
