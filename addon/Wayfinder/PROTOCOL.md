# Wayfinder read-only overlay feedback

Wayfinder contains no roads, route planner, destination click handlers or movement
controls. The external Python app renders saved roads and routes in a Windows
layered window. The addon reports the visible world map canvas and viewport so
the desktop drawings follow zoom, pan and map changes without image matching.

Version 4 has 80 columns, each with a color header and eight black/white bits.
The compact panel is 160 by 18 physical pixels; large mode is 320 by 36. Integers
are little endian. The nominal update interval is 50 ms, subject to game frames.
Offsets are zero based:

| Offset | Bytes | Meaning |
| --- | --- | --- |
| 0 | 2 | RM magic |
| 2 | 1 | Protocol version: 4 |
| 3 | 1 | Bits: 0 position valid, 1 stealth, 2 world valid, 3 map viewport valid |
| 4 | 2 | Heartbeat counter, wrapping at 65536 |
| 6 | 2 | Player UI map ID |
| 8, 10 | 2 each | Player normalized X/Y times 65535 |
| 12 | 2 | Clockwise heading in hundredths of a degree |
| 16 | 24 | Player zone name, UTF-8, zero padded |
| 40, 44, 48 | 4 each | Signed world X/Y/Z in hundredths of a yard |
| 52 | 2 | Continent ID |
| 54 | 4 | Client build |
| 58 | 2 | Displayed UI map ID, independent of player zone |
| 60, 62, 64, 66 | 2 each | Signed physical canvas left/top/right/bottom |
| 68, 70, 72, 74 | 2 each | Signed physical visible viewport left/top/right/bottom |
| 78 | 1 | Sum of bytes 0 through 77 modulo 251 |
| 79 | 1 | Sum of byte value times one-based index, modulo 251 |

Unused bytes are zero. Rectangles use the game client top-left as origin. Canvas
layout coordinates convert to pixels using effective scale times physical
screen height divided by 768, before flipping the vertical origin. This handles
large windows and UI scaling without introducing a vertical offset. See
[Blizzard PixelUtil](https://github.com/Gethe/wow-ui-source/blob/live/Interface/AddOns/Blizzard_SharedXML/PixelUtil.lua).
Canvas coordinates may be negative while zoomed or panned. The decoder intersects the
reported clip with the canvas and game window; it does not draw outside them.
Out-of-range or unavailable geometry clears only the viewport-valid bit. A closed
or hidden map reports no viewport. No map pins, click handlers, exported Lua data,
or route drawings are loaded by the addon.

World Z remains unverified (`world_z_valid=False`); navigation continues to infer
ground with the existing checks. Decoding still accepts old 64-column versions
for position tracking, but overlays require version 4. Version 3 destination
requests are ignored and can no longer start trips.

The external reader uses one background capture at a time while idle and reuses
movement packets during a trip. Missing or frozen viewport data older than 400 ms
hides the overlay. The overlay never takes foreground focus. It is click-through
normally; holding Alt enables its desktop selection surface. A click uses the
displayed map assignment, expires in two seconds while waiting for modifiers to
be released, and is discarded on focus loss, dragging, changed geometry or an
existing trip. The desktop planner then refreshes and validates live position.
