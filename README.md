# Helix3D

A Fusion 360 add-in that creates a parametric 3D helix sketch curve.

## What it does

Two ways to use it:

- **Outside a sketch** — creates a custom feature ("Helix") wrapping a 3D sketch that holds one fixed spline. Radius, pitch/height/turns (depending on mode), taper and start angle are model parameters visible in the Parameters dialog. Right-click > Edit Feature reopens the dialog.
- **Inside a sketch** — adds the helix straight into the active sketch as a fixed spline, with its definition stored as attributes on the curve. Select the curve, right-click > Edit 3D Helix to change it. Expressions referencing user parameters are stored on the curve and re-evaluated after every command, so the curve follows its parameters while the add-in is running. It is not a custom feature (one can't wrap the sketch you're editing without swallowing it), so the values don't appear as rows in the Parameters dialog.

The curve is a degree-3 non-rational B-spline interpolating sampled helix points with exact end tangents. No solids or surfaces are involved.

Supported modes: Revolutions & Pitch, Revolutions & Height, Height & Pitch, Spiral (flat).

## Installation

1. Download or clone this repository.
2. Copy the `Helix3D` folder into your Fusion 360 add-ins folder:
   - Windows: `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns`
   - Mac: `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns`
3. In Fusion 360, go to **Utilities > Add-Ins**, find Helix3D under the Add-Ins tab, and run it (optionally enable "Run on Startup").
4. The Helix command appears in the Sketch Create and Solid Create panels.

## License

No license specified — all rights reserved by the author unless otherwise agreed.
