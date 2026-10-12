"""CAM workbench helpers that run *inside* FreeCAD.

This file is never imported by the MCP server. The CAM tools in
``freecad_mcp.tools.cam`` read it as text and send it to FreeCAD together
with a single function call, so all FreeCAD API usage lives here, in one
lintable module, instead of being spread over f-string snippets.

Design notes:

- Everything is generic: operations and dressups are looked up in the
  registries below, and their options are read and written through
  FreeCAD's own property metadata (type, group, enumeration, tooltip).
- Nothing may open a dialog. Where FreeCAD's CAM code would ask the user
  (tool controller choice, G-code editor, task panels), the helpers avoid
  that code path.
- Functions take one JSON-compatible dict and return JSON-compatible data.

Targets FreeCAD 1.1 (CAM workbench, ``Path`` package).
"""

# Imports of CAM modules are local to each function: FreeCAD loads them lazily.
# pathlib.Path is imported as FsPath because "Path" is FreeCAD's CAM package.

from __future__ import annotations

import importlib
import math
from pathlib import Path as FsPath
from typing import Any

import FreeCAD

try:
    import FreeCADGui
except ImportError:  # FreeCADCmd / headless
    FreeCADGui = None


def _gui() -> bool:
    return bool(FreeCAD.GuiUp) and FreeCADGui is not None


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

# type name -> (model module, GUI module). The GUI module provides
# ``Command.res`` for the view provider, which draws the toolpath.
OPERATIONS: dict[str, tuple[str, str]] = {
    "Profile": ("Path.Op.Profile", "Path.Op.Gui.Profile"),
    "Pocket": ("Path.Op.Pocket", "Path.Op.Gui.Pocket"),
    "PocketShape": ("Path.Op.PocketShape", "Path.Op.Gui.PocketShape"),
    "Adaptive": ("Path.Op.Adaptive", "Path.Op.Gui.Adaptive"),
    "MillFace": ("Path.Op.MillFace", "Path.Op.Gui.MillFace"),
    "Helix": ("Path.Op.Helix", "Path.Op.Gui.Helix"),
    "Drilling": ("Path.Op.Drilling", "Path.Op.Gui.Drilling"),
    "Tapping": ("Path.Op.Tapping", "Path.Op.Gui.Tapping"),
    "ThreadMilling": ("Path.Op.ThreadMilling", "Path.Op.Gui.ThreadMilling"),
    "Engrave": ("Path.Op.Engrave", "Path.Op.Gui.Engrave"),
    "Vcarve": ("Path.Op.Vcarve", "Path.Op.Gui.Vcarve"),
    "Deburr": ("Path.Op.Deburr", "Path.Op.Gui.Deburr"),
    "Slot": ("Path.Op.Slot", "Path.Op.Gui.Slot"),
    "Surface": ("Path.Op.Surface", "Path.Op.Gui.Surface"),
    "Waterline": ("Path.Op.Waterline", "Path.Op.Gui.Waterline"),
    "Probe": ("Path.Op.Probe", "Path.Op.Gui.Probe"),
    "Custom": ("Path.Op.Custom", "Path.Op.Gui.Custom"),
}

OPERATION_INFO: dict[str, str] = {
    "Profile": "Contour around the model, faces or edges (inside/outside)",
    "Pocket": "Clear 3D pockets selected by faces",
    "PocketShape": "Clear 2.5D pockets from faces or outlines",
    "Adaptive": "Adaptive (trochoidal) clearing or profiling",
    "MillFace": "Face the top of the stock or model",
    "Helix": "Helical interpolation of holes (e.g. counterbores)",
    "Drilling": "Drill cycles G81/G82/G83/G73/G85 on holes",
    "Tapping": "Tapping cycles G84/G74",
    "ThreadMilling": "Mill internal/external threads",
    "Engrave": "Follow edges/ShapeStrings at depth",
    "Vcarve": "V-carving with a V-bit",
    "Deburr": "Chamfer/deburr edges with a chamfer bit",
    "Slot": "Slots between features or along edges",
    "Surface": "3D surface (needs OpenCamLib)",
    "Waterline": "3D waterline (needs OpenCamLib)",
    "Probe": "Probe grid for Z correction",
    "Custom": "Literal G-code lines",
}

# Dressups. "create": the module has a Create(base) that adds the dressup to the
# job ("vp" names the GUI view provider class to attach, if Create doesn't).
# "recipe": FreeCAD 1.1 has no Create() for these; their GUI command builds them
# with the same steps add_dressup() runs (see FREECAD_BUGS["dressup-gui-only"]).
DRESSUPS: dict[str, dict[str, str]] = {
    "Tags": {
        "kind": "create",
        "module": "Path.Dressup.Tags",
        "gui": "Path.Dressup.Gui.Tags",
        "vp": "PathDressupTagViewProvider",
        "info": "Holding tabs on a profile",
    },
    "Boundary": {
        "kind": "create",
        "module": "Path.Dressup.Boundary",
        "gui": "Path.Dressup.Gui.Boundary",
        "vp": "DressupPathBoundaryViewProvider",
        "info": "Clip a path to a boundary solid (default: the stock)",
    },
    "Dogbone": {
        "kind": "create",
        "module": "Path.Dressup.Gui.DogboneII",
        "info": "Dogbone/T-bone relief in inside corners",
    },
    "Array": {
        "kind": "create",
        "module": "Path.Dressup.Array",
        "gui": "Path.Dressup.Gui.Array",
        "vp": "DressupArrayViewProvider",
        "info": "Repeat a path in a linear/polar array",
    },
    "LeadInOut": {
        "kind": "recipe",
        "gui": "Path.Dressup.Gui.LeadInOut",
        "setup": "1",
        "info": "Lead-in/lead-out moves for profiles",
    },
    "RampEntry": {
        "kind": "recipe",
        "gui": "Path.Dressup.Gui.RampEntry",
        "setup": "1",
        "info": "Ramp instead of plunge into the material",
    },
    "Dragknife": {
        "kind": "recipe",
        "gui": "Path.Dressup.Gui.Dragknife",
        "info": "Corner handling for drag knives",
    },
    "AxisMap": {
        "kind": "recipe",
        "gui": "Path.Dressup.Gui.AxisMap",
        "info": "Map a linear axis to a rotary axis",
    },
    "ZCorrect": {
        "kind": "recipe",
        "gui": "Path.Dressup.Gui.ZCorrect",
        "info": "Correct Z from a probe file",
    },
}

# Known FreeCAD CAM bugs the tools detect. We don't work around them: the
# fix belongs in FreeCAD, and the tools report it instead of hiding it.
FREECAD_BUGS: dict[str, str] = {
    "dressup-base-op": (
        "FreeCAD bug: the dressup left its base operation in the job, so the post "
        "processor would emit both (e.g. a profile cut through its holding tags). "
        "Most dressups only remove the base in their GUI view provider. Fixed by "
        "passing removeBefore=True to Job.addOperation() in every dressup; needs a "
        "FreeCAD version that contains that fix."
    ),
    "dressup-gui-only": (
        "FreeCAD design issue: LeadInOut, RampEntry, Dragknife, AxisMap and ZCorrect "
        "(plus Boundary2, Mirror, PlungeMilling on main) exist only in GUI modules "
        "and have no headless Create(); they need the FreeCAD GUI."
    ),
}

STOCK_KINDS = ("from_base", "box", "cylinder")


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _doc(name: str | None) -> Any:
    doc = FreeCAD.getDocument(name) if name else FreeCAD.ActiveDocument
    if doc is None:
        raise ValueError("No document (none active and no doc_name given)")
    # CAM's Create() functions all use FreeCAD.ActiveDocument
    if FreeCAD.ActiveDocument is not doc:
        FreeCAD.setActiveDocument(doc.Name)
        if _gui():
            FreeCADGui.ActiveDocument = FreeCADGui.getDocument(doc.Name)
    return doc


def _obj(doc: Any, name: str) -> Any:
    obj = doc.getObject(name)
    if obj is None:
        found = doc.getObjectsByLabel(name)
        obj = found[0] if len(found) == 1 else None
    if obj is None:
        raise ValueError(f"Object not found: {name!r} (name or unique label)")
    return obj


def _job_of(obj: Any) -> Any:
    from PathScripts import PathUtils

    job = PathUtils.findParentJob(obj)
    if job is None:
        raise ValueError(f"{obj.Label} does not belong to a CAM job")
    return job


def _job(doc: Any, name: str | None) -> Any:
    if name:
        job = _obj(doc, name)
        if not _is_job(job):
            raise ValueError(f"{job.Label} is not a CAM job")
        return job
    jobs = [o for o in doc.Objects if _is_job(o)]
    if len(jobs) != 1:
        raise ValueError(f"{len(jobs)} jobs in {doc.Name}; pass job_name")
    return jobs[0]


def _is_job(obj: Any) -> bool:
    import Path.Main.Job as PathJob

    return isinstance(getattr(obj, "Proxy", None), PathJob.ObjectJob)


def _safe(v: Any) -> Any:  # noqa: PLR0911 - one return per value type
    """Convert property values to JSON/XML-RPC-safe data."""
    if v is None or isinstance(v, bool | str):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, int):
        return v if -(2**31) <= v < 2**31 else str(v)
    if isinstance(v, FreeCAD.Units.Quantity):
        # str() is exact and locale independent (UserString rounds and may use
        # a decimal comma); feeds are stored in mm/s but read better in mm/min
        if v.Unit == _VELOCITY:
            return f"{round(v.getValueAs('mm/min').Value, 6):g} mm/min"
        return str(v)
    if isinstance(v, FreeCAD.Vector):
        return [round(v.x, 6), round(v.y, 6), round(v.z, 6)]
    if isinstance(v, FreeCAD.Placement):
        axis = v.Rotation.Axis
        return {
            "base": _safe(v.Base),
            "axis": _safe(axis),
            "angle": round(math.degrees(v.Rotation.Angle), 6),
        }
    if isinstance(v, list | tuple):
        return [_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _safe(x) for k, x in v.items()}
    if hasattr(v, "Name") and hasattr(v, "TypeId"):
        return v.Name
    return str(v)


_VELOCITY = FreeCAD.Units.Unit("mm/s")


def _placement(spec: Any) -> Any:
    if spec is None:
        return None
    base = FreeCAD.Vector(*spec.get("base", [0, 0, 0]))
    rot = FreeCAD.Rotation(
        FreeCAD.Vector(*spec.get("axis", [0, 0, 1])), spec.get("angle", 0.0)
    )
    return FreeCAD.Placement(base, rot)


def _link_sub_list(doc: Any, job: Any, spec: list[dict[str, Any]]) -> list[Any]:
    """[{object, subelements}] -> [(obj, [subs])], originals mapped to job clones."""
    out = []
    for item in spec:
        obj = _obj(doc, item["object"])
        if job is not None and obj not in job.Model.Group:
            clone = job.Proxy.resourceClone(job, obj)
            if clone is None:
                raise ValueError(f"{obj.Label} is not a model of job {job.Label}")
            obj = clone
        out.append((obj, list(item.get("subelements", []))))
    return out


def _convert(doc: Any, job: Any, obj: Any, prop: str, value: Any) -> Any:  # noqa: PLR0911
    """Convert a JSON value to what the property expects."""
    ptype = obj.getTypeIdOfProperty(prop)
    if value is None:
        return None
    if ptype in ("App::PropertyLinkSubList", "App::PropertyLinkSubListGlobal"):
        return _link_sub_list(doc, job, value)
    if ptype in (
        "App::PropertyLink",
        "App::PropertyLinkGlobal",
        "App::PropertyLinkChild",
    ):
        return _obj(doc, value)
    if ptype in ("App::PropertyLinkList", "App::PropertyLinkListGlobal"):
        return [_obj(doc, n) for n in value]
    if ptype in ("App::PropertyVector", "App::PropertyVectorDistance"):
        return FreeCAD.Vector(*value)
    if ptype == "App::PropertyVectorList":
        return [FreeCAD.Vector(*v) for v in value]
    if ptype == "App::PropertyPlacement":
        return _placement(value)
    return value  # numbers, strings with units ("5 mm", "800 mm/min"), enums, bools


def _set_props(
    doc: Any,
    job: Any,
    obj: Any,
    values: dict[str, Any] | None,
    expressions: dict[str, str | None] | None,
) -> dict[str, Any]:
    """Set values and expressions. A plain value replaces an existing expression."""
    report: dict[str, Any] = {
        "set": [],
        "expressions_cleared": [],
        "expressions_set": [],
    }
    props = set(obj.PropertiesList)
    exprs = dict(obj.ExpressionEngine)
    for prop, expr in (expressions or {}).items():
        if prop not in props:
            raise ValueError(f"{obj.Label} has no property {prop!r}")
        obj.setExpression(prop, expr)
        report["expressions_set" if expr else "expressions_cleared"].append(prop)
    for prop, value in (values or {}).items():
        if prop not in props:
            raise ValueError(f"{obj.Label} has no property {prop!r}")
        if "ReadOnly" in obj.getPropertyStatus(prop):
            raise ValueError(f"{obj.Label}.{prop} is read-only")
        if prop in exprs and prop not in (expressions or {}):
            obj.setExpression(prop, None)
            report["expressions_cleared"].append(prop)
        enums = _enums(obj, prop)
        if enums is not None and value not in enums:
            raise ValueError(f"{obj.Label}.{prop}: {value!r} not in {enums}")
        setattr(obj, prop, _convert(doc, job, obj, prop, value))
        report["set"].append(prop)
    return report


def _enums(obj: Any, prop: str) -> list[str] | None:
    if obj.getTypeIdOfProperty(prop) != "App::PropertyEnumeration":
        return None
    try:
        return list(obj.getEnumerationsOfProperty(prop))
    except Exception:
        return None


def _describe_props(
    obj: Any, groups: list[str] | None = None, include_hidden: bool = False
) -> list[dict[str, Any]]:
    exprs = dict(obj.ExpressionEngine)
    out = []
    for prop in obj.PropertiesList:
        status = obj.getPropertyStatus(prop)
        hidden = "Hidden" in status
        if hidden and not include_hidden:
            continue
        group = obj.getGroupOfProperty(prop)
        if groups and group not in groups:
            continue
        try:
            value = _safe(getattr(obj, prop))
        except Exception as e:  # some properties can't be read in every state
            value = f"<unreadable: {e}>"
        item: dict[str, Any] = {
            "name": prop,
            "type": obj.getTypeIdOfProperty(prop),
            "group": group,
            "value": value,
            "doc": obj.getDocumentationOfProperty(prop),
        }
        enums = _enums(obj, prop)
        if enums is not None:
            item["enum"] = enums
        if prop in exprs:
            item["expression"] = exprs[prop]
        if "ReadOnly" in status:
            item["read_only"] = True
        if hidden:
            item["hidden"] = True
        out.append(item)
    return out


def _select_only(obj: Any) -> None:
    # Op Create() takes a selected tool controller instead of asking via dialog
    if _gui():
        FreeCADGui.Selection.clearSelection()
        if obj is not None:
            FreeCADGui.Selection.addSelection(obj)


def _attach_op_vp(op: Any, gui_module: str) -> None:
    if not _gui() or getattr(op.ViewObject, "Proxy", None) is not None:
        return
    import Path.Op.Gui.Base as OpGui

    gmod = importlib.import_module(gui_module)
    op.ViewObject.Proxy = OpGui.ViewProvider(op.ViewObject, gmod.Command.res)


def _op_kind(obj: Any) -> str:
    if _is_job(obj):
        return "job"
    proxy = getattr(obj, "Proxy", None)
    mod = type(proxy).__module__ if proxy is not None else ""
    if mod.startswith("Path.Dressup"):
        return "dressup"
    if mod.startswith("Path.Op"):
        return "operation"
    if mod.startswith("Path.Tool.Controller"):
        return "tool_controller"
    return "other"


def _type_name(obj: Any) -> str:
    proxy = getattr(obj, "Proxy", None)
    mod = type(proxy).__module__ if proxy is not None else ""
    for name, (m, _g) in OPERATIONS.items():
        if mod == m:
            return name
    for name, d in DRESSUPS.items():
        if mod in (d.get("module"), d.get("gui")):
            return name
    return mod or obj.TypeId


# ---------------------------------------------------------------------------
# Capabilities and discovery
# ---------------------------------------------------------------------------


def capabilities(_args: dict[str, Any]) -> dict[str, Any]:
    """Report CAM types, post processors, templates and known FreeCAD issues."""
    import Path.Preferences as Prefs

    ops = []
    for name, (mod, _gmod) in OPERATIONS.items():
        entry: dict[str, Any] = {"type": name, "info": OPERATION_INFO.get(name, "")}
        try:
            importlib.import_module(mod)
            entry["available"] = True
        except Exception as e:
            entry["available"] = False
            entry["reason"] = str(e).splitlines()[0]
        ops.append(entry)
    dressups = []
    for name, d in DRESSUPS.items():
        entry = {"type": name, "info": d["info"]}
        try:
            importlib.import_module(d.get("module") or d["gui"])
            entry["available"] = True
        except Exception as e:
            entry["available"] = False
            entry["reason"] = str(e).splitlines()[0]
        dressups.append(entry)
    templates = []
    for path in Prefs.searchPaths():
        if path and FsPath(path).is_dir():
            templates += sorted(str(f) for f in FsPath(path).glob("job_*.json"))
    return {
        "freecad_version": ".".join(FreeCAD.Version()[:3]),
        "gui": _gui(),
        "operations": ops,
        "dressups": dressups,
        "stock_kinds": list(STOCK_KINDS),
        "post_processors": sorted(Prefs.allEnabledPostProcessors()),
        "job_templates": templates,
        "default_job_template": Prefs.defaultJobTemplate() or None,
        "asset_path": str(Prefs.getAssetPath()),
        "freecad_home": FreeCAD.getHomePath(),
        "local_patches": _local_patches(),
        "known_freecad_issues": FREECAD_BUGS,
    }


def _local_patches() -> str | None:
    """Content of PATCHED_FROM written by the dev setup's update script, if any."""
    marker = FsPath(FreeCAD.getHomePath()).parent / "PATCHED_FROM"
    try:
        return marker.read_text().strip()
    except OSError:
        return None


def list_tools(args: dict[str, Any]) -> dict[str, Any]:
    """Tool libraries, tool bits and shapes from FreeCAD's tool asset store."""
    from Path.Tool import cam_assets

    want = args.get("library")
    out: dict[str, Any] = {"libraries": [], "shapes": []}
    for lib in cam_assets.fetch(asset_type="toolbitlibrary", depth=1):
        if want and want not in (lib.get_id(), lib.label):
            continue
        bits = []
        for bit in lib.get_bits():
            entry = {
                "number": lib.get_bit_no_from_bit(bit),
                "id": bit.get_id(),
                "label": bit.label,
                "shape": bit.get_shape_name(),
            }
            try:
                entry["parameters"] = {
                    k: _safe(v) for k, v in bit.to_dict().get("parameter", {}).items()
                }
                attrs = bit.to_dict().get("attribute", {})
                if attrs:
                    entry["attributes"] = {k: _safe(v) for k, v in attrs.items()}
            except Exception as e:
                entry["error"] = str(e)
            bits.append(entry)
        out["libraries"].append({"id": lib.get_id(), "label": lib.label, "bits": bits})
    shapes: set[str] = set()
    for store in ("local", "builtin"):  # user shapes and the ones shipped with FreeCAD
        shapes.update(
            u.asset_id for u in cam_assets.list_assets("toolbitshape", store=store)
        )
    out["shapes"] = sorted(shapes)
    return out


def analyze_model(args: dict[str, Any]) -> dict[str, Any]:
    """Faces of a model with the data needed to pick Base features."""
    from Path.Base import Drillable

    doc = _doc(args.get("doc_name"))
    obj = _obj(doc, args["object_name"])
    shape = obj.Shape
    faces = []
    kinds = set(args.get("face_types") or [])
    for i, f in enumerate(shape.Faces, 1):
        t = type(f.Surface).__name__
        if kinds and t not in kinds:
            continue
        bb = f.BoundBox
        item: dict[str, Any] = {
            "face": f"Face{i}",
            "surface": t,
            "area": round(f.Area, 3),
            "z": [round(bb.ZMin, 3), round(bb.ZMax, 3)],
        }
        if t == "Plane":
            n = f.normalAt(0, 0)
            item["normal"] = [round(n.x, 4), round(n.y, 4), round(n.z, 4)]
        elif t == "Cylinder":
            s = f.Surface
            item.update(
                radius=round(s.Radius, 4), center=_safe(s.Center), axis=_safe(s.Axis)
            )
        faces.append(item)
    holes = []
    try:
        for base, sub in Drillable.getDrillableTargets(obj):
            f = base.Shape.getElement(sub)
            holes.append(
                {
                    "element": sub,
                    "diameter": round(2 * f.Surface.Radius, 4),
                    "center": _safe(f.Surface.Center),
                }
            )
    except Exception as e:
        holes = [{"error": str(e)}]
    bb = shape.BoundBox
    return {
        "object": obj.Name,
        "label": obj.Label,
        "valid": shape.isValid(),
        "volume": round(shape.Volume, 3),
        "bound_box": [
            round(v, 4) for v in (bb.XMin, bb.XMax, bb.YMin, bb.YMax, bb.ZMin, bb.ZMax)
        ],
        "faces": faces,
        "drillable_holes": holes,
        "face_count": len(shape.Faces),
    }


# ---------------------------------------------------------------------------
# Job, stock, tool controllers
# ---------------------------------------------------------------------------


def create_job(args: dict[str, Any]) -> dict[str, Any]:
    """Create a job for model objects (see tools.cam.cam_create_job)."""
    import Path.Main.Job as PathJob
    import Path.Preferences as Prefs

    doc = _doc(args.get("doc_name"))
    models = [_obj(doc, n) for n in args["model_objects"]]
    template = args.get("template")
    if template is None:
        template = Prefs.defaultJobTemplate() or None
    elif template == "":
        template = None
    if _gui():
        import Path.Main.Gui.Job as PathJobGui

        job = PathJobGui.Create(models, template, openTaskPanel=False)
    else:
        job = PathJob.Create("Job", models, template)
    if args.get("label"):
        job.Label = args["label"]
    offset = args.get("model_offset")
    if offset:
        for clone in job.Model.Group:
            clone.Placement.Base = clone.Placement.Base + FreeCAD.Vector(*offset)
    doc.recompute()
    if args.get("stock"):
        set_stock({"doc_name": doc.Name, "job_name": job.Name, **args["stock"]})
    elif offset:
        # stock from base records the model placement only at creation
        set_stock({"doc_name": doc.Name, "job_name": job.Name, "kind": "from_base"})
    if not args.get("default_tool_controller", True) and not template:
        # without a template FreeCAD always adds a 5 mm end mill controller
        for tc in list(job.Tools.Group):
            _remove_tc(doc, job, tc)
    if args.get("properties"):
        _set_props(doc, job, job, args["properties"], None)
    doc.recompute()
    return inspect_job({"doc_name": doc.Name, "job_name": job.Name})


def set_stock(args: dict[str, Any]) -> dict[str, Any]:
    """Replace a job's stock (see tools.cam.cam_set_stock)."""
    import Path.Main.Stock as PathStock

    doc = _doc(args.get("doc_name"))
    job = _job(doc, args.get("job_name"))
    kind = args.get("kind", "from_base")
    placement = _placement(args.get("placement"))
    if kind == "from_base":
        neg = FreeCAD.Vector(*args.get("extent_neg", [1, 1, 1]))
        pos = FreeCAD.Vector(*args.get("extent_pos", [1, 1, 1]))
        new = PathStock.CreateFromBase(job, neg, pos, placement)
    elif kind == "box":
        new = PathStock.CreateBox(job, FreeCAD.Vector(*args["size"]), placement)
    elif kind == "cylinder":
        new = PathStock.CreateCylinder(job, args["radius"], args["height"], placement)
    else:
        raise ValueError(f"kind must be one of {STOCK_KINDS}")
    old = job.Stock
    job.Stock = new
    if old is not None and old is not new:
        doc.removeObject(old.Name)
    doc.recompute()
    bb = new.Shape.BoundBox
    return {
        "stock": new.Name,
        "kind": kind,
        "bound_box": [
            round(v, 4) for v in (bb.XMin, bb.XMax, bb.YMin, bb.YMax, bb.ZMin, bb.ZMax)
        ],
    }


def _remove_tc(doc: Any, job: Any, tc: Any) -> None:
    users = [o.Label for o in tc.InList if _op_kind(o) == "operation"]
    if users:
        raise ValueError(f"{tc.Label} is used by {users}")
    tool = tc.Tool
    job.Tools.removeObject(tc)
    doc.removeObject(tc.Name)
    if tool is not None and not tool.InList:
        doc.removeObject(tool.Name)


def add_tool_controller(args: dict[str, Any]) -> dict[str, Any]:
    """Add a tool bit and its controller to a job."""
    import Path.Tool.Controller as PathTC
    from Path.Tool import cam_assets
    from Path.Tool.toolbit import ToolBit

    doc = _doc(args.get("doc_name"))
    job = _job(doc, args.get("job_name"))
    tool_spec = args["tool"]
    if "library_bit" in tool_spec:
        bit = cam_assets.get(f"toolbit://{tool_spec['library_bit']}")
    elif "bit_file" in tool_spec:
        bit = ToolBit.from_file(tool_spec["bit_file"])
    elif "shape" in tool_spec:
        bit = ToolBit.from_dict(
            {
                "name": tool_spec.get("name") or tool_spec["shape"],
                "shape": tool_spec["shape"]
                if tool_spec["shape"].endswith(".fcstd")
                else tool_spec["shape"] + ".fcstd",
                "parameter": tool_spec.get("parameters", {}),
                "attribute": tool_spec.get("attributes", {}),
            }
        )
    else:
        raise ValueError("tool needs one of: library_bit, bit_file, shape")
    tool = bit.attach_to_doc(doc=doc)
    number = args.get("tool_number") or job.Proxy.nextToolNumber()
    label = args.get("label") or f"TC: {tool.Label}"
    tc = PathTC.Create(label, tool, number)
    job.Proxy.addToolController(tc)
    values = {
        k: v
        for k, v in {
            "SpindleSpeed": args.get("spindle_speed"),
            "SpindleDir": args.get("spindle_dir"),
            "HorizFeed": args.get("horiz_feed"),
            "VertFeed": args.get("vert_feed"),
        }.items()
        if v is not None
    }
    values.update(args.get("properties") or {})
    _set_props(doc, job, tc, values, args.get("expressions"))
    if _gui() and tool.ViewObject is not None:
        tool.ViewObject.Visibility = False  # the bit's 3D model sits at the origin
    doc.recompute()
    return {
        "name": tc.Name,
        "label": tc.Label,
        "tool_number": tc.ToolNumber,
        "tool": tool.Label,
        "diameter": _safe(tool.Diameter) if hasattr(tool, "Diameter") else None,
        "properties": _describe_props(tc),
    }


def remove_tool_controller(args: dict[str, Any]) -> dict[str, Any]:
    """Remove an unused tool controller and its tool bit."""
    doc = _doc(args.get("doc_name"))
    tc = _obj(doc, args["tool_controller"])
    job = _job_of(tc)
    label = tc.Label
    _remove_tc(doc, job, tc)
    doc.recompute()
    return {"removed": label, "remaining": [t.Label for t in job.Tools.Group]}


# ---------------------------------------------------------------------------
# Operations and dressups
# ---------------------------------------------------------------------------


def add_operation(args: dict[str, Any]) -> dict[str, Any]:
    """Add an operation of any registered type to a job."""
    op_type = args["op_type"]
    if op_type not in OPERATIONS:
        raise ValueError(f"Unknown op_type {op_type!r}; known: {sorted(OPERATIONS)}")
    mod_name, gui_name = OPERATIONS[op_type]
    mod = importlib.import_module(mod_name)
    doc = _doc(args.get("doc_name"))
    job = _job(doc, args.get("job_name"))
    tc = _obj(doc, args["tool_controller"]) if args.get("tool_controller") else None
    if tc is None and len(job.Tools.Group) > 1:
        raise ValueError("Job has several tool controllers; pass tool_controller")
    tc = tc or (job.Tools.Group[0] if job.Tools.Group else None)
    if tc is None:
        raise ValueError("Job has no tool controller; add one first")

    _select_only(tc)
    try:
        op = mod.Create(args.get("name") or op_type, parentJob=job)
    finally:
        _select_only(None)
    op.ToolController = tc
    if args.get("label"):
        op.Label = args["label"]
    values = dict(args.get("properties") or {})
    if args.get("base") is not None:
        values["Base"] = args["base"]
        if "Disabled" in op.PropertiesList and "Disabled" not in values:
            values["Disabled"] = []
    _attach_op_vp(op, gui_name)
    report = _set_props(doc, job, op, values, args.get("expressions"))
    doc.recompute()
    return {
        "name": op.Name,
        "label": op.Label,
        "type": op_type,
        "set": report,
        "path": path_summary(op, job),
    }


def _create_dressup(dtype: str, base: Any, job: Any, doc: Any) -> Any:
    spec = DRESSUPS[dtype]
    if spec["kind"] == "create":
        dr = importlib.import_module(spec["module"]).Create(base)
        if dr is None:
            raise ValueError(
                f"FreeCAD refused to create a {dtype} dressup on {base.Label}"
            )
        if _gui() and spec.get("vp") and getattr(dr.ViewObject, "Proxy", None) is None:
            gmod = importlib.import_module(spec["gui"])
            getattr(gmod, spec["vp"])(dr.ViewObject)
            if dr.ViewObject.Proxy is None:  # some view providers don't self-assign
                dr.ViewObject.Proxy = getattr(gmod, spec["vp"])(dr.ViewObject)
        return dr
    # "recipe": exactly the steps of FreeCAD's own GUI command for this dressup
    if not _gui():
        raise ValueError(f"{dtype}: " + FREECAD_BUGS["dressup-gui-only"])
    gmod = importlib.import_module(spec["gui"])
    dr = doc.addObject("Path::FeaturePython", f"{dtype}Dressup")
    proxy = gmod.ObjectDressup(dr)
    dr.Base = base
    job.Proxy.addOperation(dr, base, True)
    if spec.get("setup"):
        proxy.setup(dr)
    gmod.ViewProviderDressup(dr.ViewObject)
    if dr.ViewObject.Proxy is None:
        dr.ViewObject.Proxy = gmod.ViewProviderDressup(dr.ViewObject)
    base.ViewObject.Visibility = False
    return dr


def add_dressup(args: dict[str, Any]) -> dict[str, Any]:
    """Dress an operation; fails if FreeCAD leaves the base op in the job."""
    dtype = args["dressup_type"]
    if dtype not in DRESSUPS:
        raise ValueError(f"Unknown dressup_type {dtype!r}; known: {sorted(DRESSUPS)}")
    doc = _doc(args.get("doc_name"))
    base = _obj(doc, args["base_operation"])
    job = _job_of(base)
    if base not in job.Operations.Group:
        raise ValueError(
            f"{base.Label} is not a top-level operation of {job.Label} "
            "(already dressed?); dress the outermost dressup instead"
        )

    dr = _create_dressup(dtype, base, job, doc)
    if base in job.Operations.Group:
        # raising aborts the MCP transaction, so the half-made dressup is undone
        raise RuntimeError(FREECAD_BUGS["dressup-base-op"])
    if args.get("label"):
        dr.Label = args["label"]
    report = _set_props(doc, job, dr, args.get("properties"), args.get("expressions"))
    doc.recompute()
    if dtype == "Tags" and args.get("tag_count"):
        dr.Proxy.generateTags(dr, int(args["tag_count"]))
        doc.recompute()
    return {
        "name": dr.Name,
        "label": dr.Label,
        "type": dtype,
        "base": base.Name,
        "set": report,
        "path": path_summary(dr, job),
    }


def describe_type(args: dict[str, Any]) -> dict[str, Any]:
    """Create a throwaway job to list every property of an op or dressup type."""
    import Part

    kind = args.get("kind", "operation")
    tname = args["type"]
    if kind == "operation" and tname not in OPERATIONS:
        raise ValueError(f"Unknown operation {tname!r}")
    if kind == "dressup" and tname not in DRESSUPS:
        raise ValueError(f"Unknown dressup {tname!r}")
    prev = FreeCAD.ActiveDocument
    doc = FreeCAD.newDocument("MCP_CAM_describe", hidden=True)
    try:
        box = doc.addObject("Part::Feature", "Box")
        box.Shape = Part.makeBox(20, 20, 10)
        doc.recompute()
        FreeCAD.setActiveDocument(doc.Name)
        import Path.Main.Job as PathJob

        job = PathJob.Create("Job", [box])
        doc.recompute()
        if kind == "operation":
            mod = importlib.import_module(OPERATIONS[tname][0])
            _select_only(job.Tools.Group[0])
            try:
                obj = mod.Create(tname, parentJob=job)
            finally:
                _select_only(None)
        else:
            import Path.Op.Profile as PathProfile

            _select_only(job.Tools.Group[0])
            try:
                base = PathProfile.Create("Profile", parentJob=job)
            finally:
                _select_only(None)
            doc.recompute()
            obj = _create_dressup(tname, base, job, doc)
        props = _describe_props(
            obj, args.get("groups"), args.get("include_hidden", False)
        )
        for p in props:  # values computed from the placeholder box mean nothing
            p["default"] = p.pop("value")
        return {"kind": kind, "type": tname, "properties": props}
    finally:
        FreeCAD.closeDocument(doc.Name)
        if prev is not None and prev.Name in FreeCAD.listDocuments():
            FreeCAD.setActiveDocument(prev.Name)


def get_properties(args: dict[str, Any]) -> dict[str, Any]:
    """Return all properties of a CAM object with their metadata."""
    doc = _doc(args.get("doc_name"))
    obj = _obj(doc, args["object_name"])
    return {
        "name": obj.Name,
        "label": obj.Label,
        "kind": _op_kind(obj),
        "type": _type_name(obj),
        "properties": _describe_props(
            obj, args.get("groups"), args.get("include_hidden", False)
        ),
    }


def set_properties(args: dict[str, Any]) -> dict[str, Any]:
    """Set validated property values and expressions on a CAM object."""
    doc = _doc(args.get("doc_name"))
    obj = _obj(doc, args["object_name"])
    try:
        job = obj if _is_job(obj) else _job_of(obj)
    except ValueError:
        job = None
    report = _set_props(doc, job, obj, args.get("values"), args.get("expressions"))
    if args.get("recompute", True):
        doc.recompute()
    out: dict[str, Any] = {"name": obj.Name, "label": obj.Label, "set": report}
    if _op_kind(obj) in ("operation", "dressup") and job is not None:
        out["path"] = path_summary(obj, job)
    return out


def set_operation_order(args: dict[str, Any]) -> dict[str, Any]:
    """Reorder the top-level operations of a job."""
    doc = _doc(args.get("doc_name"))
    job = _job(doc, args.get("job_name"))
    current = list(job.Operations.Group)
    wanted = [_obj(doc, n) for n in args["order"]]
    missing = [o.Label for o in current if o not in wanted]
    extra = [o.Label for o in wanted if o not in current]
    if missing or extra:
        raise ValueError(
            f"order must list every operation exactly once; missing {missing}, unknown {extra}"
        )
    job.Operations.Group = wanted
    doc.recompute()
    return {"order": [o.Label for o in job.Operations.Group]}


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

_MOVES = ("G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03")
_CYCLES = ("G73", "G74", "G81", "G82", "G83", "G84", "G85", "G86", "G89")


def path_summary(  # noqa: PLR0912 - one pass over the commands, many cases
    op: Any, job: Any = None, gcode_lines: int = 0
) -> dict[str, Any]:
    """Statistics and plausibility warnings for one operation/dressup."""
    path = getattr(op, "Path", None)
    if path is None:
        return {"error": "object has no Path"}
    cmds = path.Commands
    pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    counts: dict[str, int] = {}
    feeds: set[float] = set()
    cut: dict[str, list[float]] = {"X": [], "Y": [], "Z": []}
    rapid_low = 0
    stock_top = None
    if job is not None and job.Stock is not None:
        stock_top = job.Stock.Shape.BoundBox.ZMax
    retract_r = False  # G98 (back to the initial Z) unless G99 is active
    for c in cmds:
        name = c.Name
        counts[name] = counts.get(name, 0) + 1
        p = c.Parameters
        if name in ("G98", "G99"):
            retract_r = name == "G99"
        if "F" in p and name not in ("G0", "G00"):  # G0 carries the rapid rate
            feeds.add(round(p["F"] * 60.0, 3))  # Path stores mm/s
        if name in _CYCLES:
            # drill cycles end at R (G99) or the initial Z (G98), not at the bottom
            for k in "XY":
                if k in p:
                    pos[k] = p[k]
            cut["X"].append(pos["X"])
            cut["Y"].append(pos["Y"])
            if "Z" in p:
                cut["Z"].append(p["Z"])
            if retract_r and "R" in p:
                pos["Z"] = p["R"]
            continue
        if name in _MOVES:
            start = dict(pos)
            for k in "XYZ":
                if k in p:
                    pos[k] = p[k]
            if name in ("G0", "G00"):
                moved_xy = start["X"] != pos["X"] or start["Y"] != pos["Y"]
                low = min(start["Z"], pos["Z"])
                if stock_top is not None and moved_xy and low < stock_top - 1e-6:
                    rapid_low += 1
            else:
                for k in "XYZ":
                    cut[k].append(pos[k])
    out: dict[str, Any] = {
        "commands": len(cmds),
        "by_command": counts,
        "feeds_mm_per_min": sorted(feeds),
        "cycle_time": getattr(op, "CycleTime", None),
    }
    if cut["Z"]:
        out["cut_bound_box"] = [
            round(min(cut["X"]), 3),
            round(max(cut["X"]), 3),
            round(min(cut["Y"]), 3),
            round(max(cut["Y"]), 3),
            round(min(cut["Z"]), 3),
            round(max(cut["Z"]), 3),
        ]
    warnings = []
    notes = []
    if not cmds or not cut["Z"]:
        warnings.append("no cutting moves (check Base, depths and tool controller)")
    if rapid_low:
        warnings.append(
            f"{rapid_low} rapid XY moves below the stock top (Z {stock_top:g}); "
            "verify they stay in material that is already removed"
        )
    if job is not None and cut["Z"] and job.Stock is not None:
        sb = job.Stock.Shape.BoundBox
        if min(cut["Z"]) < sb.ZMin - 1e-6:
            notes.append(
                f"cuts {round(sb.ZMin - min(cut['Z']), 3)} mm below the stock bottom"
            )
        if (
            min(cut["X"]) < sb.XMin - 1e-6
            or max(cut["X"]) > sb.XMax + 1e-6
            or min(cut["Y"]) < sb.YMin - 1e-6
            or max(cut["Y"]) > sb.YMax + 1e-6
        ):
            warnings.append("cutting moves outside the stock XY bounds")
    if 0.0 in feeds:
        warnings.append("feed rate 0 in path (tool controller feeds not set?)")
    out["warnings"] = warnings
    out["notes"] = notes
    if gcode_lines:
        text = path.toGCode().splitlines()
        out["gcode"] = text[:gcode_lines]
        out["gcode_truncated"] = len(text) > gcode_lines
    return out


def inspect_job(args: dict[str, Any]) -> dict[str, Any]:
    """Describe a job: models, stock, tool controllers, operations, post."""
    doc = _doc(args.get("doc_name"))
    job = _job(doc, args.get("job_name"))
    sb = job.Stock.Shape.BoundBox if job.Stock is not None else None
    ops = []
    for op in job.Operations.Group:
        entry: dict[str, Any] = {
            "name": op.Name,
            "label": op.Label,
            "kind": _op_kind(op),
            "type": _type_name(op),
            "active": getattr(op, "Active", True),
        }
        tc = getattr(op, "ToolController", None)
        if tc is not None:
            entry["tool_controller"] = tc.Label
        for prop in (
            "StartDepth",
            "FinalDepth",
            "StepDown",
            "SafeHeight",
            "ClearanceHeight",
        ):
            if hasattr(op, prop):
                entry[prop] = _safe(getattr(op, prop))
        if _op_kind(op) == "dressup":
            entry["base"] = (
                op.Base.Name if getattr(op, "Base", None) is not None else None
            )
        entry["path"] = path_summary(op, job)
        ops.append(entry)
    tcs = [
        {
            "name": tc.Name,
            "label": tc.Label,
            "tool_number": tc.ToolNumber,
            "tool": tc.Tool.Label if tc.Tool else None,
            "diameter": _safe(getattr(tc.Tool, "Diameter", None)) if tc.Tool else None,
            "spindle_speed": tc.SpindleSpeed,
            "spindle_dir": tc.SpindleDir,
            "horiz_feed": _safe(tc.HorizFeed),
            "vert_feed": _safe(tc.VertFeed),
        }
        for tc in job.Tools.Group
    ]
    ss = job.SetupSheet
    return {
        "name": job.Name,
        "label": job.Label,
        "doc": doc.Name,
        "models": [
            {
                "clone": m.Name,
                "label": m.Label,
                "placement": _safe(m.Placement),
                "bound_box": [
                    round(v, 4)
                    for v in (
                        m.Shape.BoundBox.XMin,
                        m.Shape.BoundBox.XMax,
                        m.Shape.BoundBox.YMin,
                        m.Shape.BoundBox.YMax,
                        m.Shape.BoundBox.ZMin,
                        m.Shape.BoundBox.ZMax,
                    )
                ],
            }
            for m in job.Model.Group
        ],
        "stock": None
        if sb is None
        else {
            "name": job.Stock.Name,
            "type": _type_name(job.Stock),
            "bound_box": [
                round(v, 4)
                for v in (sb.XMin, sb.XMax, sb.YMin, sb.YMax, sb.ZMin, sb.ZMax)
            ],
        },
        "tool_controllers": tcs,
        "operations": ops,
        "setup_sheet": {
            p: _safe(getattr(ss, p))
            for p in ss.PropertiesList
            if ss.getGroupOfProperty(p) not in ("Base",)
            and not p.startswith("_")
            and "Hidden" not in ss.getPropertyStatus(p)
        },
        "post_processor": job.PostProcessor,
        "post_processor_args": job.PostProcessorArgs,
        "post_processor_output_file": job.PostProcessorOutputFile,
        "fixtures": list(job.Fixtures),
        "order_output_by": job.OrderOutputBy,
        "cycle_time": job.CycleTime,
    }


def inspect_path(args: dict[str, Any]) -> dict[str, Any]:
    """Summarize the toolpath of one operation or dressup."""
    doc = _doc(args.get("doc_name"))
    obj = _obj(doc, args["object_name"])
    job = _job_of(obj)
    return {
        "name": obj.Name,
        "label": obj.Label,
        **path_summary(obj, job, int(args.get("gcode_lines", 0))),
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def post_process(args: dict[str, Any]) -> dict[str, Any]:
    """Run the job's post processor without dialogs and write the G-code."""
    from Path.Post.Processor import PostProcessorFactory

    doc = _doc(args.get("doc_name"))
    job = _job(doc, args.get("job_name"))
    if args.get("post_processor"):
        job.PostProcessor = args["post_processor"]
    if not job.PostProcessor:
        raise ValueError("No post processor set on the job; pass post_processor")
    post_args = args.get("post_args")
    if post_args is not None:
        job.PostProcessorArgs = post_args
    # legacy posts open a modal G-code editor in the GUI unless told otherwise
    if "--no-show-editor" not in job.PostProcessorArgs:
        job.PostProcessorArgs = (job.PostProcessorArgs + " --no-show-editor").strip()
    doc.recompute()
    post = PostProcessorFactory.get_post_processor(job, job.PostProcessor)
    if post is None:
        raise ValueError(f"Post processor {job.PostProcessor!r} could not be loaded")
    sections = post.export()
    if sections is None:
        raise ValueError(
            "Post processor returned nothing (argument error? see the FreeCAD report view)"
        )
    output = args.get("output_file")
    files = []
    if output:
        out_path = FsPath(output)
        if len(sections) == 1 or not args.get("split_files", True):
            out_path.write_text("".join(g for _n, g in sections if g))
            files.append(str(out_path))
        else:
            for name, g in sections:
                path = (
                    out_path.with_name(f"{out_path.stem}_{name}{out_path.suffix}")
                    if name
                    else out_path
                )
                path.write_text(g or "")
                files.append(str(path))
    gcode = "".join(g for _n, g in sections if g)
    out: dict[str, Any] = {
        "post_processor": job.PostProcessor,
        "args": job.PostProcessorArgs,
        "sections": [n for n, _g in sections],
        "lines": gcode.count("\n"),
        "files": files,
        "cycle_time": job.CycleTime,
    }
    max_lines = int(args.get("return_lines", 0))
    if max_lines:
        lines = gcode.splitlines()
        out["gcode"] = lines[:max_lines]
        out["gcode_truncated"] = len(lines) > max_lines
    return out


def export_job_template(args: dict[str, Any]) -> dict[str, Any]:
    """Write a job_*.json template (tool controllers, setup sheet, stock, post)."""
    import json

    import Path.Main.Job as PathJob
    import Path.Main.Stock as PathStock

    doc = _doc(args.get("doc_name"))
    job = _job(doc, args.get("job_name"))
    path = FsPath(args["file_path"])
    if not (path.name.startswith("job_") and path.suffix == ".json"):
        raise ValueError("FreeCAD only finds templates named job_*.json")
    attrs = job.Proxy.templateAttrs(job)
    if not args.get("include_post", True):
        for key in (
            PathJob.JobTemplate.PostProcessor,
            PathJob.JobTemplate.PostProcessorArgs,
            PathJob.JobTemplate.PostProcessorOutputFile,
        ):
            attrs.pop(key, None)
    if args.get("include_tool_controllers", True):
        attrs[PathJob.JobTemplate.ToolController] = [
            tc.Proxy.templateAttrs(tc) for tc in job.Tools.Group
        ]
    if args.get("include_stock", True) and job.Stock is not None:
        attrs[PathJob.JobTemplate.Stock] = PathStock.TemplateAttributes(job.Stock)
    if args.get("include_setup_sheet", True):
        attrs[PathJob.JobTemplate.SetupSheet] = job.SetupSheet.Proxy.templateAttributes(
            True, True, True, True, True
        )
    path.write_text(json.dumps(attrs, sort_keys=True, indent=2))
    return {"file": str(path), "keys": sorted(attrs)}
