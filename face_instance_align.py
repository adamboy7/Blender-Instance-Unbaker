"""
Instance Unbaker - Blender Addon
=================================
Replaces baked (non-instanced) duplicate objects with proper linked
instances of a canonical mesh, aligned via face-pair selection.

Blender's "Realize Instances" and similar bake operations destroy instance
relationships, leaving independent mesh copies. This addon reverses that:
pick a matching face on the canonical and on the baked duplicate, and a
correctly-placed linked instance is spawned in one click.

WORKFLOW:
  1. Enter Edit Mode on the CANONICAL object, select exactly ONE face,
     press "Store Canonical Face"
  2. Enter Edit Mode on the BAKED DUPLICATE, select exactly ONE face
     (the corresponding face), press "Align Instance to Face"
  3. A linked duplicate of the canonical object is created, its selected
     face aligned to match the baked duplicate's face, and the baked
     duplicate is hidden.

FACE FRAME CONVENTION:
  Each face is represented as a coordinate frame:
    - Origin : face center (average of all face verts in world space)
    - Z-axis : face normal (world space)
    - X-axis : direction from center toward first vertex, projected onto
               face plane (resolves the roll / tangent DOF)
    - Y-axis : cross(Z, X)  →  right-hand frame

ALIGNMENT MATH:
  Let Fc = canonical face frame (4x4 matrix, world space)
  Let Fd = duplicate face frame (4x4 matrix, world space)

  We want the new instance I such that its face frame equals Fd:
    I * (canonical_object_to_face_offset) = Fd

  canonical_object_to_face_offset = Fc_local (face frame in object space)

  So:
    I = Fd * Fc_local_inv

  where Fc_local is the face frame expressed relative to the canonical
  object's own transform (not world space). This way the instance world
  matrix places its face exactly on top of the duplicate's face.

ORIGIN ALIGNMENT (optional):
  After face-alignment, override just the translation column of M_instance
  with the duplicate's world-space origin. Rotation and scale columns are
  untouched, so face orientation and size correction are preserved.

COLLECTION:
  New instances are moved into a top-level "Instanced" collection
  (created if absent), keeping the outliner tidy.
"""

bl_info = {
    "name": "Instance Unbaker",
    "author": "adamboy7",
    "version": (1, 7, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Instance Unbaker",
    "description": (
        "Reverse baked instances: select matching faces on a canonical mesh "
        "and a baked duplicate to spawn a linked instance with precise "
        "face-to-face alignment, then hide the duplicate."
    ),
    "category": "Object",
}

import math

import bpy
import bmesh
from mathutils import Matrix, Vector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_face_frame_from_edit_mesh(obj):
    """
    Open the bmesh for *obj* (must be in Edit Mode), find the single
    selected face, compute its world-space 4×4 frame matrix and world-space
    area, free the bmesh, and return (matrix, area, None) — or
    (None, None, error_string).

    IMPORTANT: all BMFace/BMVert data is consumed and converted to plain
    mathutils types *before* this function returns, so no BMesh references
    escape. This prevents the 'BMesh data has been removed' crash that
    occurs when a live BMFace reference is used after the bmesh is
    invalidated by Blender's internal GC between operator calls.

    Frame convention:
      Z = world-space face normal
      X = (first_vert − centre) projected onto face plane  (resolves roll)
      Y = Z × X
      origin = face centre in world space

    Area is computed in world space (fan triangulation), so object scale
    is already factored in — suitable for direct ratio comparison.
    """
    if obj is None or obj.type != 'MESH':
        return None, None, "No mesh object active"
    if obj.mode != 'EDIT':
        return None, None, "Object must be in Edit Mode"

    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    selected = [f for f in bm.faces if f.select]
    if len(selected) != 1:
        # Free before early return
        bm.free()
        return None, None, f"Select exactly 1 face (you have {len(selected)} selected)"

    face = selected[0]
    mat_world = obj.matrix_world

    # ── Snapshot all data from the live BMFace into plain Vectors NOW ──
    verts_ws = [mat_world @ Vector(v.co) for v in face.verts]
    normal_local = Vector(face.normal).normalized()

    # Done with bmesh — free it immediately so Blender can't invalidate it
    # under us later in the same call stack.
    bm.free()

    # ── Everything below uses only plain mathutils, no BMesh refs ──

    # Face centre
    centre = Vector((0.0, 0.0, 0.0))
    for v in verts_ws:
        centre += v
    centre /= len(verts_ws)

    # World normal via inverse-transpose — correct for any transform including
    # non-uniform scale.  (For pure rotation/uniform scale this equals
    # mat_world.to_3x3(), but the inverse-transpose is always correct.)
    normal_ws = (mat_world.inverted().transposed().to_3x3() @ normal_local).normalized()

    # Tangent: first-vertex direction projected onto face plane
    raw_tangent = verts_ws[0] - centre
    if raw_tangent.length < 1e-8:
        raw_tangent = verts_ws[1] - centre
    tangent = (raw_tangent - raw_tangent.project(normal_ws)).normalized()

    binormal = normal_ws.cross(tangent).normalized()

    # Build column-major 4×4 frame matrix
    # mathutils.Matrix rows = row vectors, so we transpose to get columns
    frame = Matrix((
        (*tangent,   0.0),
        (*binormal,  0.0),
        (*normal_ws, 0.0),
        (*centre,    1.0),
    )).transposed()

    # ── World-space face area via cross-product fan triangulation ──
    # Triangulate as a fan from verts_ws[0].  Works for convex and most
    # concave faces; matches the area the duplicate's face visually occupies
    # in world space (i.e. object scale is already folded in via mat_world).
    area = 0.0
    v0 = verts_ws[0]
    for i in range(1, len(verts_ws) - 1):
        edge1 = verts_ws[i]     - v0
        edge2 = verts_ws[i + 1] - v0
        area += edge1.cross(edge2).length * 0.5

    return frame, area, None


# ---------------------------------------------------------------------------
# Persistent scene storage
# ---------------------------------------------------------------------------

class FaceAlignProperties(bpy.types.PropertyGroup):
    canonical_object: bpy.props.StringProperty(
        name="Canonical Object",
        description="Name of the canonical (source) mesh object",
        default="",
    )
    # We store the face frame as a flat 16-float string because
    # PropertyGroup doesn't support Matrix directly.
    canonical_face_matrix: bpy.props.StringProperty(
        name="Canonical Face Matrix (flat)",
        default="",
    )
    canonical_face_local_matrix: bpy.props.StringProperty(
        name="Canonical Face Local Matrix (flat)",
        default="",
    )
    canonical_face_area: bpy.props.FloatProperty(
        name="Canonical Face Area",
        description="World-space area of the stored canonical face",
        default=0.0,
        min=0.0,
    )
    scale_to_fit: bpy.props.BoolProperty(
        name="Scale to Fit Face Size",
        description=(
            "Uniformly scale the instance so its reference face matches "
            "the size of the target face"
        ),
        default=False,
    )
    align_origins: bpy.props.BoolProperty(
        name="Align Origins",
        description=(
            "After face alignment, shift the instance so its origin matches "
            "the target object's origin. Best combined with Scale to Fit"
        ),
        default=False,
    )
    fit_to_bounding_box: bpy.props.BoolProperty(
        name="Fit to Bounding Box",
        description=(
            "Stretch the instance per-axis to match the target object's bounding box. "
            "Useful when the target has non-uniform scale. Can be combined with Scale to Fit"
        ),
        default=False,
    )
    status_message: bpy.props.StringProperty(
        name="Status",
        default="No canonical face stored yet.",
    )
    hidden_by_tool: bpy.props.StringProperty(
        name="Hidden By Tool",
        description="Newline-separated names of objects hidden by Align Instance",
        default="",
    )

    def set_matrix(self, mat: Matrix, attr: str):
        flat = [mat[r][c] for r in range(4) for c in range(4)]
        setattr(self, attr, " ".join(f"{v:.10f}" for v in flat))

    def get_matrix(self, attr: str) -> Matrix | None:
        raw = getattr(self, attr, "")
        if not raw:
            return None
        try:
            vals = [float(x) for x in raw.split()]
        except ValueError:
            return None
        if len(vals) != 16:
            return None
        return Matrix([[vals[r * 4 + c] for c in range(4)] for r in range(4)])


# ---------------------------------------------------------------------------
# Collection helper
# ---------------------------------------------------------------------------

def _get_or_create_instanced_collection(scene):
    """
    Return the top-level collection named "Instanced", creating and
    linking it to the scene's root collection if it doesn't exist yet.
    """
    col = bpy.data.collections.get("Instanced")
    if col is None:
        col = bpy.data.collections.new("Instanced")
        scene.collection.children.link(col)
    elif col.name not in scene.collection.children:
        # Collection exists in bpy.data but isn't linked to this scene yet
        scene.collection.children.link(col)
    return col


def _move_to_collection(obj, target_col):
    """
    Unlink *obj* from every collection it currently belongs to, then
    link it into *target_col*. Avoids duplicate-link errors.
    """
    for col in list(obj.users_collection):
        col.objects.unlink(obj)
    target_col.objects.link(obj)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class FACEALIGN_OT_store_canonical(bpy.types.Operator):
    """Store the selected face as the reference face for the canonical mesh. Must be in Edit Mode with exactly one face selected"""
    bl_idname = "face_align.store_canonical"
    bl_label = "Store Canonical Face"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj is not None
            and obj.type == 'MESH'
            and obj.mode == 'EDIT'
        )

    def execute(self, context):
        props = context.scene.face_align_props
        obj = context.active_object

        # Atomically open bmesh, compute frame + area, free bmesh — no live refs escape
        frame_world, face_area, err = _get_face_frame_from_edit_mesh(obj)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}

        # Local frame: face frame relative to object's own transform.
        # Stored so the align step can compute: M_instance = Fd_world @ Fc_local_inv
        frame_local = obj.matrix_world.inverted() @ frame_world

        props.canonical_object = obj.name
        props.canonical_face_area = face_area
        props.set_matrix(frame_world, "canonical_face_matrix")
        props.set_matrix(frame_local, "canonical_face_local_matrix")
        props.status_message = (
            f"✓ Stored canonical face on '{obj.name}'. "
            "Now select the matching face on the baked duplicate."
        )

        self.report({'INFO'}, props.status_message)
        return {'FINISHED'}


class FACEALIGN_OT_align_instance(bpy.types.Operator):
    """Spawn a linked instance of the canonical mesh with its reference face aligned to the selected face on this object, then hide this object"""
    bl_idname = "face_align.align_instance"
    bl_label = "Align Instance to Face"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.face_align_props
        obj = context.active_object
        return (
            bool(props.canonical_object)
            and bool(props.canonical_face_local_matrix)
            and obj is not None
            and obj.type == 'MESH'
            and obj.mode == 'EDIT'
        )

    def execute(self, context):
        props = context.scene.face_align_props

        # --- Validate stored canonical data ---
        canonical_name = props.canonical_object
        if not canonical_name:
            self.report({'ERROR'}, "No canonical face stored. Run 'Store Canonical Face' first.")
            return {'CANCELLED'}

        canonical_obj = bpy.data.objects.get(canonical_name)
        if canonical_obj is None:
            self.report({'ERROR'}, f"Canonical object '{canonical_name}' not found in scene.")
            return {'CANCELLED'}

        Fc_local = props.get_matrix("canonical_face_local_matrix")
        if Fc_local is None:
            self.report({'ERROR'}, "Stored canonical face matrix is invalid. Re-store it.")
            return {'CANCELLED'}

        # --- Get selected face on the baked duplicate ---
        dup_obj = context.active_object
        if dup_obj is canonical_obj:
            self.report({'ERROR'}, "Active object is the canonical object. Select the baked duplicate.")
            return {'CANCELLED'}

        # Atomically open bmesh, compute frame matrix + area, free bmesh.
        # MUST happen before any mode_set() call, which would invalidate
        # the edit-mesh bmesh and cause 'BMFace has been removed' errors.
        Fd_world, dup_area, err = _get_face_frame_from_edit_mesh(dup_obj)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}

        # --- Compute instance world matrix ---
        #
        # We want the instance so that:
        #   instance.matrix_world @ Fc_local = Fd_world
        #
        # Solving for instance.matrix_world:
        #   M_instance = Fd_world @ inv(Fc_local)
        #
        M_instance = Fd_world @ Fc_local.inverted()

        # --- Scale-to-fit ---
        # If enabled and both faces have non-degenerate area, compute the
        # uniform scale factor that makes the canonical face the same size
        # as the duplicate face in world space:
        #   scale = sqrt(dup_area / canonical_area)
        #
        # This is applied as a post-multiply uniform scale in the instance's
        # own local space so the alignment (translation + rotation already
        # encoded in M_instance) is not disturbed.
        #
        # Derivation: M_instance already places the canonical face frame onto
        # Fd_world. To also match face sizes we scale about the face centre
        # (which is the translation column of Fd_world). A uniform scale S
        # about a point P = S_local post-multiplied onto M_instance:
        #   M_final = M_instance @ Matrix.Scale(s, 4)
        # This scales the whole object uniformly in its local axes, which
        # is equivalent to scaling about the object origin — but since the
        # face centre IS the object origin after alignment, it stays put.
        if props.scale_to_fit:
            can_area = props.canonical_face_area
            if can_area > 1e-12 and dup_area > 1e-12:
                s = math.sqrt(dup_area / can_area)
                M_instance = M_instance @ Matrix.Scale(s, 4)

        # --- Exit edit mode before object operations ---
        bpy.ops.object.mode_set(mode='OBJECT')

        # --- Create linked duplicate ---
        # Select only canonical, duplicate it (linked = same mesh data)
        bpy.ops.object.select_all(action='DESELECT')
        canonical_obj.select_set(True)
        context.view_layer.objects.active = canonical_obj
        bpy.ops.object.duplicate(linked=True)

        instance = context.active_object
        instance.name = canonical_obj.name + "_instance"

        # --- Origin alignment (optional) ---
        # Replace only the translation column of M_instance with the
        # duplicate's world-space origin, leaving rotation/scale intact.
        # This is a pure column-3 swap on the 4×4 matrix:
        #   M_final.col[3] = (dup_origin.x, dup_origin.y, dup_origin.z, 1)
        # The rotation+scale 3×3 submatrix (cols 0-2) is unchanged, so
        # face orientation and any scale-to-fit correction are preserved.
        if props.align_origins:
            dup_origin = dup_obj.matrix_world.translation.copy()
            # matrix_world is read-only element-wise; build a new matrix
            M_instance = M_instance.copy()
            M_instance.col[3] = (*dup_origin, 1.0)

        # --- Apply the computed world matrix ---
        instance.matrix_world = M_instance

        # --- Bounding-box fit (optional) ---
        # After placement, compare the world-space AABB of the baked duplicate
        # to that of the instance, then rescale the instance per-axis so both
        # boxes match.
        #
        # Why per-axis (non-uniform)?  The baked mesh may have a different
        # aspect ratio from the canonical (e.g. same width but taller).  A
        # single uniform face-area scale cannot recover that independently.
        #
        # Implementation:
        #   1.  Measure world AABBs of duplicate and (just-placed) instance.
        #   2.  Compute per-axis ratio:  ratio[i] = dup_size[i] / inst_size[i]
        #   3.  Build a non-uniform scale matrix S in the instance's *local*
        #       space:  M_final = M_instance @ S
        #       This leaves position + orientation (cols 0–2 directions)
        #       unchanged — only their magnitudes are rescaled.
        #
        # Edge cases:
        #   • If either AABB has a near-zero extent on an axis (flat / 2-D
        #     face), the ratio for that axis is clamped to 1.0 (no change)
        #     to avoid divide-by-zero or absurd scale spikes.
        #   • The step runs AFTER scale_to_fit and align_origins so all three
        #     options compose safely.
        bb_scale_applied = False
        bbox_ratios = Vector((1.0, 1.0, 1.0))
        if props.fit_to_bounding_box:
            # Decompose first so we have the rotation axes for projection.
            loc, rot, cur_scale = instance.matrix_world.decompose()
            R = rot.to_matrix()
            # Local axes of the instance, expressed as world-space unit vectors.
            # Projecting both objects onto these directions gives per-local-axis
            # extents, so ratio[i] maps directly to cur_scale[i] — correct even
            # when the instance is rotated after face alignment.
            local_axes = [R.col[0].normalized(), R.col[1].normalized(), R.col[2].normalized()]

            def _size_along_axes(obj, axes):
                mat = obj.matrix_world
                verts = [mat @ v.co for v in obj.data.vertices]
                if not verts:
                    return None
                return Vector([
                    max(v.dot(a) for v in verts) - min(v.dot(a) for v in verts)
                    for a in axes
                ])

            dup_size  = _size_along_axes(dup_obj, local_axes)
            inst_size = _size_along_axes(instance, local_axes)

            if dup_size is not None and inst_size is not None:
                ratios = []
                for d, i in zip(dup_size, inst_size):
                    if i > 1e-8 and d > 1e-8:
                        ratios.append(d / i)
                    else:
                        ratios.append(1.0)   # degenerate axis — leave untouched

                bbox_ratios = Vector(ratios)

                new_scale = Vector((
                    cur_scale.x * ratios[0],
                    cur_scale.y * ratios[1],
                    cur_scale.z * ratios[2],
                ))
                S_mat = Matrix.Diagonal(new_scale).to_4x4()
                instance.matrix_world = (
                    Matrix.Translation(loc)
                    @ rot.to_matrix().to_4x4()
                    @ S_mat
                )
                bb_scale_applied = True

        # --- Move instance into "Instanced" collection ---
        instanced_col = _get_or_create_instanced_collection(context.scene)
        _move_to_collection(instance, instanced_col)

        # --- Hide the baked duplicate ---
        dup_obj.hide_set(True)          # viewport hide
        dup_obj.hide_render = True      # render hide too
        props.hidden_by_tool = (props.hidden_by_tool + "\n" + dup_obj.name).strip("\n")

        # --- Select the new instance ---
        bpy.ops.object.select_all(action='DESELECT')
        instance.select_set(True)
        context.view_layer.objects.active = instance

        # --- Build status message ---
        notes = []
        if props.scale_to_fit:
            can_area = props.canonical_face_area
            if can_area > 1e-12 and dup_area > 1e-12:
                s = math.sqrt(dup_area / can_area)
                notes.append(f"face-scaled ×{s:.4f}")
        if props.align_origins:
            notes.append("origin aligned")
        if bb_scale_applied:
            rx, ry, rz = bbox_ratios
            notes.append(f"bbox fit [{rx:.3f}, {ry:.3f}, {rz:.3f}]")
        note_str = f" ({', '.join(notes)})" if notes else ""
        props.status_message = (
            f"✓ '{instance.name}' → Instanced collection, "
            f"hidden '{dup_obj.name}'.{note_str}"
        )
        self.report({'INFO'}, props.status_message)
        return {'FINISHED'}


class FACEALIGN_OT_unhide_all(bpy.types.Operator):
    """Restore visibility of all objects hidden by this tool during alignment"""
    bl_idname = "face_align.unhide_all"
    bl_label = "Unhide Tool-Hidden Objects"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.face_align_props
        names = [n for n in props.hidden_by_tool.split("\n") if n]
        count = 0
        for name in names:
            obj = context.scene.objects.get(name)
            if obj is not None and obj.hide_get():
                obj.hide_set(False)
                obj.hide_render = False
                count += 1
        props.hidden_by_tool = ""
        self.report({'INFO'}, f"Unhid {count} object(s) hidden by this tool.")
        return {'FINISHED'}


class FACEALIGN_OT_clear_canonical(bpy.types.Operator):
    """Reset the stored canonical object and reference face so you can start over"""
    bl_idname = "face_align.clear_canonical"
    bl_label = "Clear Canonical Face"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.face_align_props
        props.canonical_object = ""
        props.canonical_face_matrix = ""
        props.canonical_face_local_matrix = ""
        props.hidden_by_tool = ""
        props.status_message = "Canonical face cleared."
        self.report({'INFO'}, "Canonical face cleared.")
        return {'FINISHED'}



class FACEALIGN_OT_focus_canonical(bpy.types.Operator):
    """Jump to the canonical object in Edit Mode with its stored reference face re-selected and viewport centered on it. Works even if the canonical object is currently hidden"""
    bl_idname = "face_align.focus_canonical"
    bl_label = "Focus Canonical & Select Face"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.face_align_props
        return (
            bool(props.canonical_object)
            and bpy.data.objects.get(props.canonical_object) is not None
        )

    def execute(self, context):
        props = context.scene.face_align_props

        # ── Validate ──
        canonical_name = props.canonical_object
        if not canonical_name:
            self.report({'ERROR'}, "No canonical object stored yet.")
            return {'CANCELLED'}

        canonical_obj = bpy.data.objects.get(canonical_name)
        if canonical_obj is None:
            self.report({'ERROR'}, f"Canonical object '{canonical_name}' not found.")
            return {'CANCELLED'}

        Fc_local = props.get_matrix("canonical_face_local_matrix")

        # ── Exit edit mode on whatever is currently active ──
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        # ── Unhide canonical if hidden, remember state to restore later ──
        was_hidden = canonical_obj.hide_get()
        if was_hidden:
            canonical_obj.hide_set(False)

        # ── Select only the canonical object ──
        bpy.ops.object.select_all(action='DESELECT')
        canonical_obj.select_set(True)
        context.view_layer.objects.active = canonical_obj

        # ── Focus viewport on the selection (Numpad '.') ──
        # view3d.view_selected polls for both a VIEW_3D area AND a WINDOW
        # region — passing only area is not enough in Blender 4.x and raises
        # "expected a view3d region". We must also pass the WINDOW region
        # (the main drawing region, as opposed to HEADER, UI, TOOLS, etc.).
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                win_region = next(
                    (r for r in area.regions if r.type == 'WINDOW'), None
                )
                if win_region is not None:
                    with context.temp_override(area=area, region=win_region):
                        bpy.ops.view3d.view_selected()
                break

        # ── Enter Edit Mode in Face Select mode ──
        bpy.ops.object.mode_set(mode='EDIT')
        context.tool_settings.mesh_select_mode = (False, False, True)  # faces

        # ── Re-select the stored reference face ──
        # Strategy: find the face whose object-space centre is closest to
        # the stored Fc_local translation (col[3] xyz = face centre in
        # object space).  This is index-free and survives minor mesh edits.
        if Fc_local is not None:
            # Stored face centre in object space (translation column of Fc_local)
            stored_centre_local = Fc_local.col[3].xyz

            bm = bmesh.from_edit_mesh(canonical_obj.data)
            bm.faces.ensure_lookup_table()

            # Deselect all first
            for f in bm.faces:
                f.select = False
            bm.select_flush(False)

            # Find closest face centre
            best_face = None
            best_dist_sq = float('inf')
            for f in bm.faces:
                centre = Vector((0.0, 0.0, 0.0))
                for v in f.verts:
                    centre += v.co
                centre /= len(f.verts)
                d = (centre - stored_centre_local).length_squared
                if d < best_dist_sq:
                    best_dist_sq = d
                    best_face = f

            if best_face is not None:
                best_face.select = True
                bm.select_flush(True)
                bmesh.update_edit_mesh(canonical_obj.data, loop_triangles=False)
                # bm is still live inside edit mode — do NOT call bm.free() here
                # (that would corrupt the edit mesh; Blender owns this bmesh)
                face_note = f", re-selected closest face (dist={best_dist_sq**0.5:.4f})"
            else:
                face_note = " (no faces found to select)"
        else:
            face_note = " (no stored face — store one first)"

        props.status_message = f"✓ Focused on '{canonical_name}'{face_note}."
        self.report({'INFO'}, props.status_message)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class FACEALIGN_PT_main(bpy.types.Panel):
    bl_label = "Instance Unbaker"
    bl_idname = "FACEALIGN_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Instance Unbaker"

    def draw(self, context):
        layout = self.layout
        props = context.scene.face_align_props

        # Status box
        box = layout.box()
        box.label(text="Status:", icon='INFO')
        # Word-wrap the status by splitting on newlines
        for line in props.status_message.split("\n"):
            box.label(text=line)

        layout.separator()

        # ── Step 1 ──
        col = layout.column(align=True)
        col.label(text="Canonical Object:", icon='OBJECT_DATA')
        if props.canonical_object:
            col.label(text=f"  Stored: {props.canonical_object}", icon='CHECKMARK')
        else:
            col.label(text="  (none stored)", icon='X')
            col.separator()
            col.label(text="① Select 1 face on a canonical mesh")
            col.label(text="② Select a corresponding non-instanced face")
            col.label(text="③ Press Align Instance to Face")
            col.separator()

        col.operator("face_align.store_canonical", icon='EYEDROPPER')
        col.operator("face_align.align_instance", icon='LINKED')

        layout.separator()

        # ── Options ──
        box2 = layout.box()
        box2.label(text="Options:", icon='SETTINGS')
        box2.prop(props, "scale_to_fit", icon='FULLSCREEN_ENTER')
        box2.prop(props, "align_origins", icon='OBJECT_ORIGIN')
        box2.prop(props, "fit_to_bounding_box", icon='PIVOT_BOUNDBOX')

        layout.separator()

        # ── Utilities ──
        box3 = layout.box()
        box3.label(text="Utilities:", icon='TOOL_SETTINGS')
        box3.operator("face_align.focus_canonical", icon='ZOOM_SELECTED')
        box3.operator("face_align.unhide_all", icon='HIDE_OFF')
        box3.operator("face_align.clear_canonical", icon='TRASH')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

CLASSES = (
    FaceAlignProperties,
    FACEALIGN_OT_store_canonical,
    FACEALIGN_OT_align_instance,
    FACEALIGN_OT_unhide_all,
    FACEALIGN_OT_clear_canonical,
    FACEALIGN_OT_focus_canonical,
    FACEALIGN_PT_main,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.face_align_props = bpy.props.PointerProperty(
        type=FaceAlignProperties
    )


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.face_align_props


if __name__ == "__main__":
    register()
