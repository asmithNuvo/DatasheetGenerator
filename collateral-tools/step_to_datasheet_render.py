"""
step_to_datasheet_render.py

Reads a mechanical STEP model (typically the output of gds_to_step.py) and
renders preliminary datasheet-style images (isometric + top-down).

Color convention (matching the source STEP coloring used upstream):
    - 'red'-ish solid color  -> gold finish   (metallic)
    - any other color / none -> grey plastic  (matte, optionally translucent)

Pipeline:
    load STEP (with per-solid colors via XCAF)
      -> classify each solid's color
      -> tessellate each solid to a mesh
      -> assign a pyrender material per solid
      -> build a flat-lit scene
      -> render isometric + top views offscreen
      -> save PNGs

NOTE ON HEADLESS RENDERING:
    pyrender needs an OpenGL context. On a headless box (CI/container) set the
    platform BEFORE pyrender is imported. This script defaults to 'egl'.
    Override with the PYOPENGL_PLATFORM env var ('egl' or 'osmesa').
"""

import os

import sys

# Select the OpenGL backend BEFORE pyrender/OpenGL is imported.
#   Linux (often headless): EGL is the right offscreen backend.
#   Windows / macOS: leave it unset so pyrender uses its default pyglet
#     context (a hidden window). Windows has no standard EGL library, so
#     forcing EGL there raises "Unable to load EGL library".
# Override explicitly by setting PYOPENGL_PLATFORM in the environment
# (e.g. 'osmesa' for a truly headless box without a GPU).
if "PYOPENGL_PLATFORM" not in os.environ and sys.platform.startswith("linux"):
    os.environ["PYOPENGL_PLATFORM"] = "egl"

import argparse

import numpy as np
import trimesh
import pyrender
import imageio.v2 as imageio

import cadquery as cq
from cadquery import Shape

# OpenCascade (OCP) bindings for reading per-solid colors out of the STEP.
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDF import TDF_LabelSequence
from OCP.Quantity import Quantity_Color
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_SOLID
from OCP.IFSelect import IFSelect_RetDone


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

# Material appearance (baseColorFactor is linear RGBA, 0-1).
GOLD_BASE_COLOR = [0.83, 0.69, 0.22, 1.0]
GREY_BASE_COLOR = [0.63, 0.63, 0.63, 1.0]

# Set < 1.0 for the "semi-transparent grey plastic" look. 1.0 = fully opaque.
GREY_ALPHA = 1.0

# Tessellation fidelity. Larger = coarser/faster ("crude" datasheet look).
LINEAR_DEFLECTION = 0.1
ANGULAR_DEFLECTION = 0.3

# Render defaults.
DEFAULT_RESOLUTION = (1200, 1200)
BACKGROUND_RGBA = [1.0, 1.0, 1.0, 1.0]   # white
FIT_MARGIN = 1.25                        # padding around the model in frame

# Lighting. Tuned to keep highlights from clipping (gold reads as gold, not pale
# yellow). Raise AMBIENT for flatter/brighter, raise KEY/FILL for more contrast.
AMBIENT_LIGHT = 0.30          # uniform base fill, 0-1
KEY_LIGHT_INTENSITY = 1.6     # main directional light
FILL_LIGHT_INTENSITY = 0.6    # softer fill lights (x2) to lift shadows


# --------------------------------------------------------------------------- #
# STEP loading (geometry + color)
# --------------------------------------------------------------------------- #

def _color_of(shape, color_tool):
    """Return (r, g, b) in 0-1 for a TopoDS_Shape, or None if uncolored."""
    c = Quantity_Color()
    for ctype in (XCAFDoc_ColorType.XCAFDoc_ColorSurf,
                  XCAFDoc_ColorType.XCAFDoc_ColorGen):
        if color_tool.GetColor(shape, ctype, c):
            return (c.Red(), c.Green(), c.Blue())
    return None


def read_step_shapes_and_colors(path):
    """
    Read a STEP file via the XCAF reader, preserving per-solid colors.

    Returns a list of (TopoDS_Shape solid, color-or-None) tuples.
    """
    doc = TDocStd_Document(TCollection_ExtendedString("XCAF"))
    reader = STEPCAFControl_Reader()
    reader.SetColorMode(True)
    reader.SetNameMode(True)

    if reader.ReadFile(path) != IFSelect_RetDone:
        raise RuntimeError(f"STEPCAFControl_Reader failed to read: {path}")
    reader.Transfer(doc)

    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    free = TDF_LabelSequence()
    shape_tool.GetFreeShapes(free)

    results = []
    for i in range(1, free.Length() + 1):
        root = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main()).GetShape_s(free.Value(i))
        explorer = TopExp_Explorer(root, TopAbs_SOLID)
        while explorer.More():
            solid = explorer.Current()
            results.append((solid, _color_of(solid, color_tool)))
            explorer.Next()
    return results


def load_solids(path):
    """
    Load solids with colors. Falls back to a plain (colorless) import if the
    XCAF color path fails for any reason -- everything then renders grey.
    """
    try:
        solids = read_step_shapes_and_colors(path)
        if solids:
            return solids
        print("No solids found via XCAF; falling back to plain import.",
              file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - intentional broad fallback
        print(f"XCAF color read failed ({exc}); falling back to plain import.",
              file=sys.stderr)

    wp = cq.importers.importStep(path)
    return [(s.wrapped, None) for s in wp.solids().vals()]


# --------------------------------------------------------------------------- #
# Color classification + materials
# --------------------------------------------------------------------------- #

def is_red(color):
    """True if a 0-1 RGB color reads as 'red' (the gold-finish flag upstream)."""
    if color is None:
        return False
    r, g, b = color
    return r > 0.5 and g < 0.4 and b < 0.4


def material_for(color):
    """Build a pyrender material: metallic gold for red, matte grey otherwise."""
    if is_red(color):
        return pyrender.MetallicRoughnessMaterial(
            baseColorFactor=GOLD_BASE_COLOR,
            metallicFactor=0.9,
            roughnessFactor=0.35,
        )
    grey = list(GREY_BASE_COLOR)
    grey[3] = GREY_ALPHA
    return pyrender.MetallicRoughnessMaterial(
        baseColorFactor=grey,
        metallicFactor=0.1,
        roughnessFactor=0.7,
        alphaMode="BLEND" if GREY_ALPHA < 1.0 else "OPAQUE",
    )


# --------------------------------------------------------------------------- #
# Tessellation
# --------------------------------------------------------------------------- #

def tessellate(solid, lin_defl=LINEAR_DEFLECTION, ang_defl=ANGULAR_DEFLECTION):
    """Tessellate a single TopoDS_Shape solid into a trimesh.Trimesh."""
    verts, tris = Shape(solid).tessellate(lin_defl, ang_defl)
    if not verts or not tris:
        return None
    vertices = np.array([(v.x, v.y, v.z) for v in verts], dtype=np.float64)
    faces = np.array(tris, dtype=np.int64)
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


# --------------------------------------------------------------------------- #
# Scene + camera
# --------------------------------------------------------------------------- #

def build_scene(meshes_and_materials):
    """Assemble per-solid meshes into a flat-lit pyrender scene."""
    scene = pyrender.Scene(
        ambient_light=[AMBIENT_LIGHT] * 3,
        bg_color=BACKGROUND_RGBA,
    )
    for tm, mat in meshes_and_materials:
        # smooth=False keeps facets crisp -> the slightly flat datasheet look.
        mesh = pyrender.Mesh.from_trimesh(tm, material=mat, smooth=False)
        scene.add(mesh)

    # One key light plus two dimmer fills: enough shaping to read form without
    # clipping highlights on the gold/white surfaces.
    lights = (
        ((1, 1, 2), KEY_LIGHT_INTENSITY),
        ((-1, -1, 1), FILL_LIGHT_INTENSITY),
        ((-1, 1, 1), FILL_LIGHT_INTENSITY),
    )
    for direction, intensity in lights:
        light = pyrender.DirectionalLight(color=[1.0, 1.0, 1.0], intensity=intensity)
        scene.add(light, pose=_look_at((0, 0, 0), direction, (0, 0, 1)))
    return scene


def _look_at(target, from_dir, up):
    """
    4x4 camera pose looking from `target + from_dir` toward `target`.

    Built in pyrender/OpenGL convention (camera looks down its local -Z).
    `from_dir` only needs a direction here; magnitude is normalized.
    """
    target = np.asarray(target, dtype=np.float64)
    eye = target + np.asarray(from_dir, dtype=np.float64)
    up = np.asarray(up, dtype=np.float64)

    forward = target - eye
    forward /= np.linalg.norm(forward)

    z = -forward
    x = np.cross(up, z)
    if np.linalg.norm(x) < 1e-9:          # up parallel to view dir
        up = np.array([0.0, 1.0, 0.0])
        x = np.cross(up, z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)

    pose = np.eye(4)
    pose[:3, 0] = x
    pose[:3, 1] = y
    pose[:3, 2] = z
    pose[:3, 3] = eye
    return pose


def camera_pose_for_view(bounds, view, yfov=np.deg2rad(35.0)):
    """
    Compute a camera pose framing the model from its bounding box.

    bounds: (2, 3) array of [min_xyz, max_xyz]
    view:   'isometric' or 'top'
    """
    bmin, bmax = np.asarray(bounds[0]), np.asarray(bounds[1])
    center = (bmin + bmax) / 2.0
    radius = np.linalg.norm(bmax - bmin) / 2.0

    # Distance so the bounding sphere fits within the vertical FOV, plus margin.
    distance = (radius / np.tan(yfov / 2.0)) * FIT_MARGIN

    if view == "top":
        direction = np.array([0.0, 0.0, 1.0])
        up = np.array([0.0, 1.0, 0.0])
    elif view == "isometric":
        direction = np.array([1.0, 1.0, 1.0])
        direction /= np.linalg.norm(direction)
        up = np.array([0.0, 0.0, 1.0])
    else:
        raise ValueError(f"Unknown view: {view!r}")

    return _look_at(center, direction * distance, up), yfov


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_view(scene, bounds, view, resolution):
    """Render one view to an RGBA uint8 array."""
    pose, yfov = camera_pose_for_view(bounds, view)
    camera = pyrender.PerspectiveCamera(yfov=yfov, aspectRatio=resolution[0] / resolution[1])
    cam_node = scene.add(camera, pose=pose)

    renderer = pyrender.OffscreenRenderer(*resolution)
    try:
        color, _ = renderer.render(scene, flags=pyrender.RenderFlags.RGBA)
    finally:
        renderer.delete()
        scene.remove_node(cam_node)
    return color


def render_datasheet_images(step_path, output_prefix, resolution=DEFAULT_RESOLUTION,
                            views=("isometric", "top")):
    """Top-level pipeline: STEP file -> one PNG per requested view."""
    solids = load_solids(step_path)

    meshes_and_materials = []
    all_vertices = []
    for solid, color in solids:
        tm = tessellate(solid)
        if tm is None:
            continue
        meshes_and_materials.append((tm, material_for(color)))
        all_vertices.append(tm.vertices)

    if not meshes_and_materials:
        raise RuntimeError(f"No renderable geometry produced from {step_path}")

    stacked = np.vstack(all_vertices)
    bounds = np.array([stacked.min(axis=0), stacked.max(axis=0)])

    scene = build_scene(meshes_and_materials)

    outputs = []
    for view in views:
        image = render_view(scene, bounds, view, resolution)
        out_path = f"{output_prefix}_{view}.png"
        imageio.imwrite(out_path, image)
        outputs.append(out_path)
        print(f"wrote {out_path}")
    return outputs


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Render preliminary datasheet images from a mechanical STEP model."
    )
    parser.add_argument("step_file", help="Input STEP file (e.g. gds_to_step.py output)")
    parser.add_argument(
        "output_prefix",
        help="Output path prefix; views are suffixed (e.g. PREFIX_isometric.png)",
    )
    parser.add_argument(
        "--resolution", type=int, nargs=2, metavar=("W", "H"),
        default=list(DEFAULT_RESOLUTION),
        help="Output resolution in pixels (default: 1200 1200)",
    )
    parser.add_argument(
        "--views", nargs="+", choices=["isometric", "top"],
        default=["isometric", "top"],
        help="Which views to render (default: isometric top)",
    )
    args = parser.parse_args()

    render_datasheet_images(
        args.step_file,
        args.output_prefix,
        resolution=tuple(args.resolution),
        views=tuple(args.views),
    )


if __name__ == "__main__":
    main()