# EVE ISK Overlay

A lightweight, always-on-top overlay that reads EVE Online game logs and
displays bounty income in real time.

## Features

- Rolling ISK/hour estimate with a configurable time window
- Optional ESS delayed-payout estimation
- Session total and elapsed session time
- Per-character totals read from each log's `Listener:` header
- Compact M/B ISK formatting
- Rolling-rate trend graph
- Configurable Windows, Linux, Wine, and Steam Proton log paths
- Automatic detection of active UTF-8 and UTF-16 game logs
- Support for multiple simultaneously running clients

## Requirements

- Windows or Linux
- Python 3.10 or newer with Tkinter
- EVE game logging enabled for every account to be tracked

No third-party Python packages are required to run from source.

## Download

Windows and Linux builds are published from GitHub Releases.

If you want to build manually, run:

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed --name EVE-ISK-Overlay eve_isk_overlay.py
```

The generated executable will be in:

```text
dist\EVE-ISK-Overlay.exe
```

On Linux, install Tkinter first if your distribution does not include it:

```bash
sudo apt install python3-tk
```

Then build:

```bash
python3 -m pip install pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed --name eve-isk-overlay eve_isk_overlay.py
```

The generated executable will be in:

```text
dist/eve-isk-overlay
```

## Run from source

Open PowerShell in the project directory and run:

```powershell
python eve_isk_overlay.py
```

On Linux, the source file can also be run directly:

```bash
chmod +x eve_isk_overlay.py
./eve_isk_overlay.py
```

## Configuration

On first start, the app creates `config.ini` next to the script or executable if
it does not already exist.

The config uses Python `configparser` format:

```ini
[logs]
directories =
    C:\Users\YourName\Documents\EVE\logs\Gamelogs
```

Linux users can replace that with a Linux path. For Steam Proton EVE installs,
the path normally looks like:

```ini
[logs]
directories =
    /home/your-user/.local/share/Steam/steamapps/compatdata/8500/pfx/drive_c/users/steamuser/Documents/EVE/logs/Gamelogs
```

If your Proton prefix has a different Windows user folder, use that folder name
under `drive_c/users/`.

You can watch multiple log folders by putting one path per line:

```ini
[logs]
directories =
    C:\Users\YourName\Documents\EVE\logs\Gamelogs
    /home/your-user/.local/share/Steam/steamapps/compatdata/8500/pfx/drive_c/users/steamuser/Documents/EVE/logs/Gamelogs
```

The app supports normal Windows paths, Linux paths, `~`, `$HOME`, and Windows
environment variables such as `%USERPROFILE%`.

Files modified during the last 60 seconds are shown as active. Recent inactive
logs are also loaded so entries inside the selected rolling window are not
missed after restarting the overlay.

Only bounty lines containing `added to next bounty payout` are counted. Change
the rolling period with the **Last ... min** field. For example, 10 million ISK
earned over 10 minutes is displayed as `60.00 M ISK/h`.

ESS estimation is enabled by default. At 100%, a logged immediate payment of
60,000 ISK is estimated as 100,000 ISK. The adjustable range is 100-200%, and
only the delayed 40% share is scaled. Session and per-character totals always
show raw logged ISK and are not changed by the ESS modifier.

## Release

Maintainers can publish new Windows and Linux executable builds by pushing a
version tag:

```powershell
git tag v0.1.0
git push origin v0.1.0
```

The GitHub Actions workflow builds `EVE-ISK-Overlay.exe` on Windows and a
Linux `eve-isk-overlay` tarball, then attaches both to the matching GitHub
Release.

## Privacy

All processing happens locally. The app reads game logs from the configured EVE
log directories and does not send data over the network. `config.ini` is ignored
by Git so personal paths are not committed accidentally.
