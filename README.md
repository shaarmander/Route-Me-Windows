# Route Me for Windows

Route Me is a desktop map and saved-road planner for WoW: Forever. Setup imports
the artwork and coordinates from your local WoW installation and installs the
Wayfinder position and heading addon. The Windows package was tested with
Forever 1.60.1.70959. It supports 50 zone and city maps plus three overviews.

[Download Route Me for Windows x64](https://github.com/shaarmander/Route-Me-Windows/releases/latest)

## Install

1. Install WoW: Forever through Battle.net and launch it once.
2. Install [64-bit Python 3.13 for Windows](https://www.python.org/downloads/windows/).
   Keep the Python launcher, pip, and Tcl/Tk selected. Setup downloads the
   app's Python dependencies on the first run.
3. Download `Route-Me-Windows-x64.zip` from the release. Extract it into a
   folder you can write to, such as `Documents\Route Me`. Run
   **Setup Route Me.cmd** from the extracted folder. If setup asks where WoW
   is installed, choose the folder containing `_forever_`.
4. Restart WoW, enable **Wayfinder** in the AddOns list, and log in. Open
   **Route Me** from the Start menu or run **Launch Route Me.cmd**. Click
   **Connect** to show your live location.

Choose **Browse maps** to inspect zones, or **Draw roads** to save your own
roads. Forever routes use saved roads only. In the app, **Click destination**
can start movement immediately. Backspace or Esc stops movement. Check
Blizzard's terms before using automatic in-game movement.
[Blizzard End User License Agreement](https://www.blizzard.com/en-us/legal/08b946df-660a-40e4-a072-1fbde65173b1/blizzard-end-user-license-agreement)

After a WoW patch, run setup again from the same folder to import the installed
build and carry your saved roads forward. Your roads, maps, and settings are
stored in the extracted Route Me folder.

This project is independent of Blizzard Entertainment.
