# GBC Emulator — WiFi Pineapple Pager

A Game Boy Color emulator for the [Hak5 WiFi Pineapple Pager](https://hak5.org).

Play any  or  ROM in your browser while your pager acts as the controller and info display.

## How it works

1. Run the payload on your pager
2. The pager screen shows the server URL (e.g. )
3. Open that URL in any browser on the same network
4. Pick a ROM from the list or upload your own  /  file
5. The Game Boy Color emulator runs at full speed in the browser (JavaScript)
6. Your pager physical buttons control the game in real-time

## Features

- **Full GBC emulation** — SM83 CPU (all opcodes), GBC colour palettes, MBC3, PPU with background/window/sprites, Timer, Joypad
- **Any ROM** — load  /  files from the pager or upload directly from the browser
- **Physical button control** — D-pad, Green (A), Red (B) forwarded from pager to emulator
- **On-screen gamepad** — touch / mouse buttons for mobile browsers
- **Pager info display** — shows game title and controls on the pager LCD (not raw frames)
- **Clean exit** — Stop Server button in browser or hold RED button 1 second on pager

## Installation

1. Copy the  folder to your pager:
   

2. Add your ROM files ( / ) to that directory.  
    (from the Hak5 Pager SDK) must also be present.

3. The payload will appear under **Games** on your pager.

## Controls

### Pager buttons

| Button | GBC action |
|--------|-----------|
| D-pad  | D-pad |
| Green  | A |
| Red    | B |

### Keyboard (browser)

| Key | Action |
|-----|--------|
| Z | A |
| X | B |
| Enter | START |
| Shift / Q | SELECT |
| Arrow keys | D-pad |

## Stopping the server

| Method | How |
|--------|-----|
| Browser | Click **Stop Server** button (always visible) |
| Pager | Hold **RED** button for 1 second |

## File overview

| File | Purpose |
|------|---------|
|  | Pager payload entry point |
|  | Python HTTP server — serves UI, lists/uploads ROMs, handles button polling |
|  | Self-contained GBC emulator (no dependencies, ~800 lines JS) |
|  | Python ctypes wrapper for  hardware control |

## Requirements

- WiFi Pineapple Pager running OpenWRT
- Python 3 (pre-installed)
-  from the Hak5 Pager SDK in the payload directory
- At least one  or  ROM file

## Also on Hak5 payload library

This payload was submitted to the official Hak5 payload library:  
https://github.com/hak5/wifipineapplepager-payloads/pull/303
