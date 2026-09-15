# Autodesk Fusion Helix 3D Sketch Feature

An Autodesk Fusion add-in that creates parametric 3D helix, spiral and along-a-path sketch curves.

![Every mode the add-in builds](docs/images/all-modes.png)

Fusion's own Coil command makes a solid. This one only ever makes a sketch curve, so you can sweep it, pattern it, use it as a path, or hand it to CAM, without a body you have to delete afterwards. The curve is a degree-3 non-rational B-spline through sampled helix points with exact end tangents, 24 samples per turn. Radial error measures well under a micron.

## What it can make

### Revolutions & Pitch

Turns plus the axial distance per turn. The example is 4 turns at 8 mm pitch, 10 mm radius.

![Helix from revolutions and pitch](docs/images/revolutions-and-pitch.png)

### Revolutions & Height

Turns plus overall height, and the pitch falls out of those. 5 turns over 40 mm here.

![Helix from revolutions and height](docs/images/revolutions-and-height.png)

### Height & Pitch

Overall height plus pitch, and the number of turns falls out. 45 mm tall at 9 mm pitch.

![Helix from height and pitch](docs/images/height-and-pitch.png)

### Spiral (flat)

A flat spiral with a start and an end radius, no height. 3 mm out to 20 mm over 4 turns.

![Flat spiral](docs/images/spiral-flat.png)

### Taper

Any of the helix modes can taper. Give it an angle:

![Helix tapered by angle](docs/images/taper-by-angle.png)

Or switch the Taper dropdown to By end radius and give it the radius you actually want to finish on, which saves working the angle out from the height. This one runs 6 mm to 18 mm:

![Helix tapered by end radius](docs/images/taper-by-end-radius.png)

### Left hand

Direction flips the winding without touching anything else.

![Left hand helix](docs/images/left-hand.png)

### Path & Pitch

Winds the helix around a curve you pick instead of a straight axis. Pitch is measured along the path, so it stays even around bends. 6 mm pitch, 4 mm radius, wound along a spline:

![Helix wound along a spline at a fixed pitch](docs/images/path-and-pitch.png)

### Path & Revolutions

Same thing, but you say how many turns to fit along the path rather than the pitch. 24 turns round a circle:

![Helix wound around a circle for a set number of turns](docs/images/path-and-revolutions.png)

### It is a real curve

Nothing above is a picture of a coil. Sweep a section along one and you get a body like any other:

![A spring swept along one of the helix curves](docs/images/swept-spring.png)

## Installation

1. Download or clone this repository.
2. Put its contents in a folder called `Helix3D` inside your Fusion add-ins folder:
   - Windows: `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns`
   - Mac: `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns`

   Rule number one: the folder has to be named `Helix3D`, matching `Helix3D.py` and `Helix3D.manifest`. Fusion will not see the add-in if the cloned folder keeps its repository name.
3. In Fusion, go to **Utilities > Add-Ins**, find Helix3D on the Add-Ins tab, and run it. Tick "Run on Startup" if you want it back after a restart.
4. The **3D Helix** command appears in the Sketch Create and Solid Create panels.

IMPORTANT: if you change the manifest, restart Fusion completely. Fusion only reads it at startup, and a half-loaded add-in fails in ways that look like a bug in the command.

## Using it

The command behaves differently depending on whether you are in a sketch when you start it, so pick the one that suits what you are doing.

### As a timeline feature

Start the command with nothing being edited. You get a **Helix** feature in the timeline, wrapping a 3D sketch that holds the curve, and its values appear as rows in the Parameters dialog like any other feature.

1. Click **3D Helix** in the Sketch Create or Solid Create panel.
2. Pick a **Mode**.
3. Optionally pick a **Plane** for it to sit on. Leave it empty and it uses the XY plane, or the sketch plane belonging to the centre point you picked.
4. Optionally pick a **Center Point** to put the axis through, and a **Start Point** to say where the curve begins. A start point drives the radius, the start angle and the height offset, so those fields hide while it is set.
5. Fill in the fields for that mode, choose right or left hand, and click OK.
6. To change it later, right-click the feature in the timeline or the browser and choose **Edit Feature**.

NOTE: Mode and the Taper dropdown are greyed out when you edit an existing feature. Fusion fixes which parameters a custom feature owns at the moment it is created and gives no way to add or remove them afterwards, so switching between a taper angle and an end radius means making a new helix.

### Straight into a sketch

Start the command while you are editing a sketch and the curve goes into that sketch instead, with its definition stored on the curve.

1. Start or edit a sketch, then click **3D Helix**.
2. Position it either with the **Placement** triad and its **Base plane** dropdown, or by picking a **Center Point** and **Start Point**. Set both points and the triad disappears, because the points now decide everything it was deciding.
3. Leave **Finish sketch and create parametric feature** off to keep the curve in the sketch. Turn it on to close the sketch and wrap it as a Helix feature instead.
4. To edit one later, select the curve, right-click, and choose **Edit 3D Helix**.

An in-sketch helix follows its inputs the same way a feature does: type an expression that references a user parameter, or drag the point or path it is built on, and the curve rebuilds when the command finishes. The catch is that this only happens while the add-in is running, because the add-in is what re-evaluates it. The values also do not appear in the Parameters dialog, since the curve is not a feature. If you want rows in the Parameters dialog, use the timeline feature instead.

## Modes at a glance

| Mode | You give it | It works out |
| --- | --- | --- |
| Revolutions & Pitch | Turns, pitch | Height |
| Revolutions & Height | Turns, height | Pitch |
| Height & Pitch | Height, pitch | Turns |
| Spiral (flat) | Start radius, end radius, turns | A flat spiral |
| Path & Pitch | A path, pitch | Turns along the path |
| Path & Revolutions | A path, turns | Pitch along the path |

Radius, start angle and direction apply to all of them. Taper applies to everything except the flat spiral, which already works in start and end radii.

## Worth knowing

- The path modes take one curve at a time, a sketch curve or a body edge. Chained curves are not supported yet, so a path made of several joined segments needs to be one curve.
- Wind a helix around a tight corner with a radius larger than the corner and it will pass through itself. That is the geometry, not a bug, but the add-in does not warn you about it.
- A centre or start point has to come earlier in the timeline than the helix. Points that come later will not highlight when you try to pick them.
- Developed against Fusion 2705.1.15 on Windows. It should be fine on Mac, I just have not tested it there.

## License

MIT, see [LICENSE](LICENSE).
