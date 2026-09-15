# Autodesk Fusion Helix 3D Sketch Feature

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
4. The **3D Helix** command appears in the Sketch Create and Solid Create panels.

## Usage

### Create a helix as its own feature

1. Click **3D Helix** in the Sketch Create or Solid Create panel (while nothing is being sketched/edited).
2. Optionally select a **Plane** for the helix to sit on (defaults to the XY plane), and optionally a **Center Point** and/or **Start Point** to place and orient it.
3. Pick a **Mode**:
   - *Revolutions & Pitch* — set turns and the axial distance per turn.
   - *Revolutions & Height* — set turns and overall height.
   - *Height & Pitch* — set overall height and axial distance per turn.
   - *Spiral (flat)* — a flat spiral with a start and end radius, no height.
4. Fill in the remaining fields shown for that mode (Radius, Pitch/Height/Revolutions, Taper Angle, Start Angle, End Radius) and choose Right/Left hand for the winding direction.
5. Click OK. The helix appears in the timeline as a **Helix** custom feature, and its values appear as parameters in the Parameters dialog.
6. To change it later, right-click the feature in the timeline or browser and choose **Edit Feature**.

### Add a helix straight into a sketch

1. Start editing (or create) a sketch, then click **3D Helix**.
2. Fill in the same fields as above, plus a **Base plane** for the triad that positions and orients the curve within the sketch.
3. Leave **Finish sketch and create parametric feature** off to keep the helix as a curve inside the current sketch, or turn it on to finish the sketch and wrap it as a Helix feature instead.
4. To edit an in-sketch helix later, select the curve, right-click, and choose **Edit 3D Helix**. Any field values typed as expressions referencing user parameters are re-evaluated automatically whenever the model changes.

## License

MIT — see [LICENSE](LICENSE).
