# audiobookshelf-tray

A system tray app that plays audiobooks from your Audiobookshelf server in the background, audio only.

It talks to the Audiobookshelf REST API directly and plays the stream through mpv, so there is no browser involved. Runs on Windows, macOS and Linux as a single Python script.

## Features

- Tray icon toggles Play/Pause on left-click (middle-click on GNOME), and the book icon shows the state (grey idle, blue loading, green playing, amber paused)
- First Play asks for your server URL, then your username and password, and saves them locally
- If you typed an address without http:// or https://, https:// is assumed
- Play resumes your most recent in-progress book on the server from its saved position, even if you last listened on another device
- With nothing in progress, Play opens a search box so you can find a book by title
- Right-click menu: Play/Pause, Search, Next, Previous, Now Playing, Quit
- Next and Previous jump to the next and previous chapter
- Now Playing shows a popup with the cover art, title and author
- Progress syncs back to the server every few seconds and on pause, chapter jumps and quit, so other Audiobookshelf clients pick up where you left off
- Books made of several audio files play straight through as one book
- Recovers on its own from stalls, errors, dropped connections, expired sessions and expired logins, then resumes at the same position
- Remembers the last book and position between launches
- Icon style follows the system light or dark theme, or can be set by hand from the Icon style menu

## Quick start

```
python3 audiobookshelf-tray.py
```

On first run the script creates a `.venv` next to itself, installs the Python packages there, installs mpv and the other system pieces with your package manager (Homebrew, apt, dnf, pacman, zypper), and relaunches itself inside the venv. On Windows it also tries to download libmpv automatically. Linux package installs use sudo and need a terminal. Run `python3 audiobookshelf-tray.py --setup` to do only the setup and exit.

Then left-click the tray icon:

1. Type your server address, for example `books.example.com`
2. Type your Audiobookshelf username and password
3. Search for a book by title and it starts playing

After that, clicking Play simply resumes the last book. Use Sign out in the menu to forget the server and login, and Sign in (or Play) to enter them again.

Your password is stored in `state.json` in the folder listed under Files, readable only by your account where the OS supports it. The app uses it to sign in again whenever the server expires your login.

## Manual install

You need Python 3.9 or newer, mpv's library (libmpv), and tkinter for the dialogs.

### Windows

1. Install Python from python.org (tkinter is included).
2. Get libmpv: from the Windows builds linked on https://mpv.io/installation download the `mpv-dev` archive for x86_64 and extract `libmpv-2.dll` (or `mpv-2.dll`) into the same folder as `audiobookshelf-tray.py`.
3. In a terminal inside the repo folder:

```
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw.exe audiobookshelf-tray.py
```

### macOS

```
brew install mpv python-tk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python audiobookshelf-tray.py
```

On macOS the menu bar icon opens the menu on right-click or control-click, and left-click toggles Play/Pause.

### Linux

Debian and Ubuntu:

```
sudo apt install mpv libmpv2 python3-venv python3-tk python3-gi gir1.2-ayatanaappindicator3-0.1
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python audiobookshelf-tray.py
```

Fedora:

```
sudo dnf install mpv-libs python3-tkinter python3-gobject libayatana-appindicator-gtk3
```

Arch:

```
sudo pacman -S mpv tk python-gobject libayatana-appindicator
```

On older Ubuntu releases the package is `libmpv1` instead of `libmpv2`. GNOME needs the AppIndicator extension to show tray icons. On GNOME, left-click always opens the menu because the shell's tray extension never forwards it, so middle-click the icon to toggle Play/Pause. Desktops using the plain X11 tray (KDE, XFCE, MATE and others) get the left-click toggle.

## Check that it works

```
python audiobookshelf-tray.py --selftest
```

After you have signed in once, this logs in, opens a book, plays it silently for a few seconds, closes the session and prints PASS or FAIL. Use the Python from your venv.

## Start at login

The easy way, on any OS:

```
python3 audiobookshelf-tray.py --install-service
python3 audiobookshelf-tray.py --remove-service
```

Install registers a login item (Windows Run key, macOS LaunchAgent, Linux autostart entry). Remove undoes it. Do not move the folder afterward.

## Files

State, log and settings live here:

- Windows: `%APPDATA%\audiobookshelf-tray`
- macOS: `~/Library/Application Support/audiobookshelf-tray`
- Linux: `~/.config/audiobookshelf-tray`

`state.json` holds the server, login, last book and position. `audiobookshelf-tray.log` records errors and recoveries. Set `ABS_TRAY_HOME` to use another folder.

## Stop and restart

```
python3 audiobookshelf-tray.py --stop
python3 audiobookshelf-tray.py --restart
```

`--stop` quits the running copy. `--restart` quits it if running, then starts a fresh one.

## Notes

- Playback uses Audiobookshelf playback sessions, so listening time and progress show up in the server like any other client.
- Search covers all of your book libraries and plays the closest title match.
- Only one copy runs at a time.
