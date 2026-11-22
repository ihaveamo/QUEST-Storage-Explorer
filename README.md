QUEST 3 STORAGE EXPLORER - AUTOMATIC LAUNCHER (WINDOWS)
======================================================

AUTHOR: MICHAEL FORDE
VERSION: 2.2
DATE 23/11/26


1. Extract the entire ZIP archive into a writable folder on your PC,
   for example D:\Quest3-Storage-Explorer\ .
2. Connect your Quest 3 over USB. In the headset, enable Developer Mode
   if needed and approve the USB debugging prompt.
3. Double-click Start-Quest3-Explorer.cmd .

On first run, the launcher:
- Checks for a working 64-bit Python 3.10+ and installs a private Python 3.13
  under python-runtime\ if Python is genuinely missing. Does not use winget.
- Downloads Google's official Android Platform-Tools into the local
  platform-tools\ folder if its adb.exe is not already working.
- Creates a local .venv Python environment, reusing installed packages
  when they are visible, and installs Androguard and dependencies if missing.
- Checks there is exactly one connected, authorised Quest.
- Starts quest3_app_explorer.py, which creates an offline interactive HTML
  report and a JSON data snapshot in this folder and opens the browser.

Later runs reuse working components and start the report directly.
If Python is installed in a nonstandard location, set QUEST3_PYTHON to its full python.exe path before launching.
Internet is required only for any components that need installing.

Optional: From cmd.exe or PowerShell, pass arguments via the .cmd launcher:
  .\Start-Quest3-Explorer.cmd --depth 5 --large-file-mb 64
  .\Start-Quest3-Explorer.cmd --all
  .\Start-Quest3-Explorer.cmd --no-large-files

Output:
  Quest3-App-Explorer.html   -- open in any web browser
  Quest3-App-Explorer.json   -- data snapshot for further analysis
  Quest3-App-Labels.json    -- cached human-readable game labels

IMPORTANT: The audit is read-only. It does not delete anything on your Quest.
The main disk audit may take longer on a device with many games and files.

Official downloads:
  Python: https://www.python.org/downloads/
  Google ADB: https://developer.android.com/tools/releases/platform-tools
  Androguard: https://pypi.org/project/androguard/

If a .venv becomes damaged, close the application, delete ONLY the .venv
folder, and double-click the launcher again to rebuild it. If ADB becomes
corrupt, delete ONLY the platform-tools folder and launch again.

The launcher does not make global PATH changes and does not require Administrator.
This version corrects a Windows PowerShell 5.1 Python detection bug in the first release.

Log verbosity:
- Androguard's blue DEBUG messages are hidden by default. INFO, WARNING and
  ERROR messages stay visible in the console.
- To enable debug logs temporarily in PowerShell, run:
    $env:LOGURU_LEVEL = 'DEBUG'
    .\Start-Quest3-Explorer.cmd
- For normal logging again, close that PowerShell window or run:
    $env:LOGURU_LEVEL = 'INFO'

Folder-percentage and Android breakdown fix:
- If /sdcard root size is unavailable, the folder table uses the sum of
  measured, disjoint top-level folders. The table clearly labels this an
  approximate share, not a percentage of total device storage.
- On Quest builds with no recursive `du -d`, a targeted fallback scans
  /sdcard/Android/data, /obb and /media plus their per-package folders.
- Expand Android in the browser table to find which packages use the most.
- /sdcard/Android primarily contains APP data and expansion assets, not the
  Android OS; never delete /sdcard/Android wholesale.
