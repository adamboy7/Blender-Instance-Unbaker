# Blender Instance Unbaker — Face Instance Aligner

A Blender addon that replaces baked (non-instanced) duplicate objects with proper linked instances of a canonical mesh, aligned via face-pair selection.

Exporting and re-importing an instanced object can destroy instance relationships, and while sepetating by loos pieces can re-construct the objects, linked relations are lost resulting in bigger file sizes. This addon helps reverse that: point it at a matching face on the canonical and the baked duplicate, and it spawns a correctly-placed linked instance in one click.

**Blender 3.0+** | Panel: `View3D > Sidebar > Instance Unbaker`

---

## Installation

1. Download `face_instance_align.py`
2. In Blender: `Edit > Preferences > Add-ons > Install`
3. Select the file and enable **Face Instance Aligner**
4. The panel appears in the **Instance Unbaker** tab of the 3D Viewport sidebar (`N`)

---

## Core Use Case

You have a canonical mesh (the "master" you want to keep instancing) and one or more baked duplicates that are supposed to be instances of it but are not. The addon lets you pick a matching face on each and automatically:

- Creates a linked duplicate of the canonical mesh
- Computes the exact world transform so the canonical's chosen face lands precisely on the baked duplicate's chosen face
- Moves the new instance into an **Instanced** collection
- Hides the baked duplicate (viewport + render)

Repeat for each baked duplicate. Each run reuses the stored canonical face, so after the first setup you only need to select one face per duplicate.

---

## Workflow

### Step 1 — Store the canonical face

1. Select your canonical (source) mesh object
2. Enter **Edit Mode** (`Tab`)
3. Switch to **Face Select** mode
4. Select **exactly one face** — pick a face that is easy to identify on the baked duplicates (a flat side, a top face, etc.)
5. In the **Face Align** panel, click **Store Canonical Face**

The panel confirms the stored object name with a checkmark.

### Step 2 — Align each baked duplicate

1. Select a baked duplicate object
2. Enter **Edit Mode** and select **exactly one face** — the face that corresponds to the canonical face you stored
3. Click **Align Instance to Face**

The addon:
- Spawns a linked instance of the canonical mesh
- Aligns it so its stored reference face matches the duplicate's selected face exactly (position, orientation, optionally scale)
- Moves the instance into the **Instanced** collection
- Hides the baked duplicate

Repeat from Step 2 for every remaining duplicate. The stored canonical face persists across operations.

---

## Options

All options are in the **Options** box in the panel. They can be combined freely.

| Option | Default | Description |
|---|---|---|
| **Scale to Fit Face Size** | Off | Uniformly scales the instance so its reference face matches the world-space area of the target face. Useful when duplicates were scaled up or down before baking. |
| **Align Origins** | Off | After face alignment, shifts the instance so its object origin sits at the baked duplicate's origin. The rotation and scale from face alignment are preserved — only the translation changes. Best combined with Scale to Fit. |
| **Fit to Bounding Box** | Off | Applies a per-axis (non-uniform) scale so the instance's world-space bounding box matches the baked duplicate's bounding box. Use this when duplicates have different proportions from the canonical (e.g. same width but taller). Runs after the other two options. |

---

## Utility Buttons

| Button | Description |
|---|---|
| **Focus Canonical & Select Face** | Jumps to the canonical object in Edit Mode with the stored reference face re-selected and the viewport centered on it. Works even if the canonical is currently hidden. |
| **Unhide Tool-Hidden Objects** | Restores visibility (viewport and render) of all baked duplicates hidden during alignment. Use this to inspect or re-align any object. |
| **Clear Canonical Face** | Resets the stored canonical object and reference face so you can start fresh with a different canonical. |

---

## How the Alignment Math Works

Each face is represented as a coordinate frame:

- **Origin** — world-space face center (average of all face vertices)
- **Z-axis** — world-space face normal (using inverse-transpose for correctness under non-uniform scale)
- **X-axis** — direction from the center toward the first vertex, projected onto the face plane (resolves the roll degree of freedom)
- **Y-axis** — `Z × X` (right-hand frame)

Let `Fc_local` = canonical face frame in the canonical object's local space.
Let `Fd_world` = baked duplicate face frame in world space.

The instance world matrix is:

```
M_instance = Fd_world × inv(Fc_local)
```

This places the canonical's reference face exactly on top of the duplicate's reference face in world space.

---

## Notes

- The addon stores the canonical face persistently in the scene's properties, so it survives undo and file saves within the same `.blend`
- New instances are always placed in a top-level **Instanced** collection (created automatically if absent)
- Each instance is named `<canonical_name>_instance`
- The baked duplicate is hidden from both viewport and render; use **Unhide Tool-Hidden Objects** to recover them
- Undo (`Ctrl+Z`) works for all operations
