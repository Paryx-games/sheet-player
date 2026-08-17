# Building PianoPlayer.exe

Must be run **on Windows** - PyInstaller packages for whatever OS it's
running on, it doesn't cross-compile. Building this on Mac/Linux
produces a Mac/Linux binary, not a working .exe.

## 1. Set up a clean environment

```
uv venv
.venv\Scripts\activate
uv pip install -r requirements.txt
```

Using a clean venv (not your everyday one) avoids accidentally bundling
unrelated packages into the exe and bloating it.

## 2. Build

```
pyinstaller main.spec
```

Output lands in `dist\PianoPlayer.exe`. First build will take a minute
or two - PyInstaller is tracing every import your script touches.

## 3. Verify before sharing

Don't just double-click and assume it's fine - test these specifically,
since they're the parts most likely to silently break in a bundled exe
even though they worked fine when run as a plain script:

- [ ] App launches with no console window flashing behind it
- [ ] Admin overlay shows correctly when launched normally (non-elevated)
- [ ] "request admin" button actually triggers the UAC prompt and relaunches
- [ ] F6/F7/F8 global hotkeys work (this is the one most likely to break -
      if hotkeys silently do nothing, the `keyboard` package's bundled
      data didn't get included right, see troubleshooting below)
- [ ] Window/exe picker dropdown populates with real running processes
- [ ] A test sheet actually plays and reaches a target window

## 4. Distribute

`dist\PianoPlayer.exe` is the only file people need - it's fully
self-contained (Python interpreter + all deps baked in). Nothing else
from `dist\` or `build\` needs to be shared.

Heads up for whoever you send it to: Windows SmartScreen will very
likely flag an unsigned, unknown .exe from an unrecognized publisher -
that's expected for any indie-built exe without a paid code-signing
certificate, not a sign anything's wrong. They'll need to click
"More info" -> "Run anyway". Worth telling them that upfront so they
don't assume it's broken or malicious.

## Troubleshooting

**Hotkeys (F6/F7/F8) don't work in the built exe, but did as a script:**
The `keyboard` package needs its own data files bundled, which the spec
file handles - but if you still hit this, run from a terminal instead
of double-clicking (`dist\PianoPlayer.exe` from an open cmd window) so
you can see if it prints an import/file-not-found error before the
window closes.

**Antivirus deletes or quarantines the exe:**
PyInstaller onefile exes get false-positived by some AV engines fairly
often - it's a known pattern (self-extracting bundled binary), not
specific to this app. If it's a problem for people you're sharing with,
the folder-mode build (`--onedir` instead of `--onefile`) triggers this
less often, at the cost of shipping a folder instead of one file.

**"win32api could not be found" or similar pywin32 errors:**
Means pywin32 wasn't fully picked up by the hook. Try:
```
pip install pywin32 --force-reinstall
python .venv\Scripts\pywin32_postinstall.py -install
```
then rebuild.
