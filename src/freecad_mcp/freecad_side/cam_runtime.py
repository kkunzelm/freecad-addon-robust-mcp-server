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

# Operations FreeCAD keeps in GUI modules without a Create(); they don't need the
# GUI though. add_operation() runs the steps of FreeCAD's GUI command. "sources":
# how many existing operations they take (source_operations).
SPECIAL_OPERATIONS: dict[str, dict[str, str]] = {
    "Comment": {
        "gui": "Path.Op.Gui.Comment",
        "cls": "Comment",
        "vp": "_ViewProviderComment",
        "sources": "none",
        "info": "Comment line in the G-code (property Comment)",
    },
    "Stop": {
        "gui": "Path.Op.Gui.Stop",
        "cls": "Stop",
        "vp": "_ViewProviderStop",
        "sources": "none",
        "info": "Program stop: Stop=Mandatory (M0) or Optional (M1)",
    },
    "Copy": {
        "gui": "Path.Op.Gui.Copy",
        "cls": "ObjectPathCopy",
        "vp": "ViewProviderPathCopy",
        "sources": "one",
        "info": "Linked copy of one operation's path (follows changes)",
    },
    "Array": {
        "gui": "Path.Op.Gui.Array",
        "cls": "ObjectArray",
        "vp": "ViewProviderArray",
        "sources": "many",
        "info": "Linear/polar/point array of operations with one tool controller",
    },
    "SimpleCopy": {
        "model": "Path.Op.Custom",
        "gui": "Path.Op.Gui.Custom",  # only for the view provider
        "sources": "many",
        "info": "Custom op holding a fixed G-code copy of operations",
    },
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
    # new in FreeCAD 26.3
    "Boundary2": {
        "kind": "recipe",
        "gui": "Path.Dressup.Gui.Boundary2",
        "info": "Clip a path to a boundary (inside/outside, offset)",
    },
    "Mirror": {
        "kind": "recipe",
        "gui": "Path.Dressup.Gui.Mirror",
        "info": "Mirror a path about X, Y or both",
    },
    "PlungeMilling": {
        "kind": "recipe",
        "gui": "Path.Dressup.Gui.PlungeMilling",
        "info": "Replace a path by plunge cuts (step over, peck options)",
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
        "(plus Boundary2, Mirror, PlungeMilling since 26.3) exist only in GUI "
        "modules and have no headless Create(); they need the FreeCAD GUI."
    ),
    "output-filename-subpart": (
        "FreeCAD bug: the output file pattern uses %T, %t, %W or %O, but this "
        "FreeCAD's FilenameGenerator replaces them before it knows the section, "
        'so "%S-%t-%d" gives "0--Doc-T1-Tool.nc". Fixed on main in 91ad168ea4 '
        "(issue #31758); needs a FreeCAD version that contains that fix, or an "
        "output pattern without these substitutions."
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


def _machine(name: str) -> Any:
    """The machine definition (.fcm) a job names (FreeCAD 26.3+)."""
    from Machine.models.machine import MachineFactory

    try:
        return MachineFactory.get_machine(name)
    except FileNotFoundError as e:
        raise ValueError(
            f"Machine {name!r} not found in {MachineFactory.get_config_directory()}"
        ) from e


def _list_machines() -> list[dict[str, Any]] | None:
    """Machines in the CAM asset folder; None before FreeCAD 26.3."""
    try:
        from Machine.models.machine import MachineFactory
    except ImportError:
        return None
    out = []
    for name in MachineFactory.list_configurations():
        try:
            m = MachineFactory.get_machine(name)
            out.append({"name": m.name, "post_processor": m.postprocessor_file_name})
        except Exception as e:
            out.append({"name": name, "error": str(e)})
    return out


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


# FreeCAD bookkeeping, not options: expressions are reported per property and
# paths by path_summary()
_INTERNAL_PROPS = {"Proxy", "ExpressionEngine", "_ElementMapVersion", "Path"}


def _describe_props(
    obj: Any, groups: list[str] | None = None, include_hidden: bool = False
) -> list[dict[str, Any]]:
    exprs = dict(obj.ExpressionEngine)
    out = []
    for prop in obj.PropertiesList:
        if prop in _INTERNAL_PROPS:
            continue
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
    for name, s in SPECIAL_OPERATIONS.items():
        if mod == s["gui"] and "cls" in s:  # SimpleCopy is a Custom op
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
    for name, s in SPECIAL_OPERATIONS.items():
        entry = {"type": name, "info": s["info"], "source_operations": s["sources"]}
        try:
            importlib.import_module(s.get("model") or s["gui"])
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
        # 26.3+: new-style posts are only reachable through a machine (Job.Machine),
        # Job.PostProcessor lists the legacy ones
        "machine_post_processors": sorted(Prefs.allEnabledMachinePostProcessors())
        if hasattr(Prefs, "allEnabledMachinePostProcessors")
        else None,
        "machines": _list_machines(),
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


def _library_serializer(path: Any, for_import: bool) -> Any:
    """FreeCAD's tool library serializer for a file extension."""
    from Path.Tool.library.serializers import all_serializers

    ext = path.suffix.lower()
    flag = "can_import" if for_import else "can_export"
    found = [s for s in all_serializers if ext in getattr(s, "extensions", ())]
    usable = [s for s in found if getattr(s, flag, True)]
    if not usable:
        names = sorted(
            e
            for s in all_serializers
            if getattr(s, flag, True)
            for e in getattr(s, "extensions", ())
        )
        what = "import" if for_import else "export"
        hint = f" (FreeCAD can't {what} {ext} files)" if found else ""
        raise ValueError(f"Can't {what} {path.name}{hint}; formats: {names}")
    return usable[0]


def import_tool_library(args: dict[str, Any]) -> dict[str, Any]:
    """Import a tool library file (and its bits) into FreeCAD's tool assets."""
    from Path.Tool import cam_assets
    from Path.Tool.library.models.library import Library

    path = FsPath(args["file_path"])
    if not path.is_file():
        raise ValueError(f"No such file: {path}")
    serializer = _library_serializer(path, for_import=True)
    lib_id = args.get("library_id") or path.stem
    lib_uri = f"toolbitlibrary://{lib_id}"
    if cam_assets.exists(lib_uri) and not args.get("overwrite"):
        raise ValueError(f"Library {lib_id!r} exists; pass overwrite or library_id")
    data = path.read_bytes()
    if hasattr(serializer, "deep_deserialize_with_context"):
        # .fctl: finds bits in the stores or in a Bit/ folder next to Library/
        loaded = serializer.deep_deserialize_with_context(data, path)
    else:
        loaded = serializer.deserialize(data, lib_id, {})
    numbered = [(loaded.get_bit_no_from_bit(b), b) for b in loaded.get_bits()]
    if not numbered:
        raise ValueError(f"{path.name}: no tools found (missing bit files?)")
    lib = Library(
        args.get("label") or getattr(loaded, "label", "") or lib_id, id=lib_id
    )
    added, kept = [], []
    for nr, bit in sorted(numbered, key=lambda nb: nb[0]):
        if bit.get_id().startswith("camotics_tool_"):
            # Camotics files number their bits camotics_tool_<n>: make them
            # unique per library so a second import doesn't replace them
            bit.id = f"{lib_id}_T{nr}"
        bit_uri = f"toolbit://{bit.get_id()}"
        if cam_assets.exists(bit_uri) and not args.get("overwrite"):
            kept.append(bit.get_id())  # same bit already stored: reuse it
        else:
            cam_assets.add(bit)
            added.append(bit.get_id())
        lib.add_bit(bit, nr)
    cam_assets.add(lib)
    return {
        "library": lib_id,
        "label": lib.label,
        "format": serializer.get_label(),
        "tools": [
            {"number": nr, "id": b.get_id(), "label": b.label}
            for nr, b in sorted(
                ((lib.get_bit_no_from_bit(b), b) for b in lib.get_bits()),
                key=lambda nb: nb[0],
            )
        ],
        "bits_added": added,
        "bits_already_stored": kept,
    }


def export_tool_library(args: dict[str, Any]) -> dict[str, Any]:
    """Write a tool library as .fctl, Camotics .json or LinuxCNC .tbl."""
    from Path.Tool import cam_assets

    path = FsPath(args["file_path"])
    serializer = _library_serializer(path, for_import=False)
    want = args["library"]
    lib = next(
        (
            x
            for x in cam_assets.fetch(asset_type="toolbitlibrary", depth=1)
            if want in (x.get_id(), x.label)
        ),
        None,
    )
    if lib is None:
        raise ValueError(f"No tool library {want!r} (id or label)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serializer.serialize(lib))
    return {
        "file": str(path),
        "format": serializer.get_label(),
        "library": lib.get_id(),
        "tools": len(lib.get_bits()),
    }


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


_AXES = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}
_ORIGIN_X = ("min", "max", "center", "keep")
_ORIGIN_Z = ("top", "bottom", "center", "keep")


def _models_bb(job: Any) -> Any:
    bb = FreeCAD.BoundBox()
    for m in job.Model.Group:
        bb.add(m.Shape.BoundBox)
    return bb


def _rotation_for(args: dict[str, Any], job: Any, doc: Any) -> Any:
    """The rotation place_model() applies: rotate {axis, angle} or face_up."""
    rot = args.get("rotate")
    face = args.get("face_up")
    if rot and face:
        raise ValueError("Pass either rotate or face_up, not both")
    if rot:
        axis = rot.get("axis", "z")
        vec = FreeCAD.Vector(*(_AXES[axis.lower()] if isinstance(axis, str) else axis))
        return FreeCAD.Rotation(vec, float(rot["angle"]))
    if face:
        clone = _link_sub_list(doc, job, [{"object": face["object"]}])[0][0]
        sub = clone.Shape.getElement(face["face"])
        if sub.ShapeType != "Face":
            raise ValueError(f"{face['face']} is not a face")
        u0, u1, v0, v1 = sub.ParameterRange
        normal = sub.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        if sub.Orientation == "Reversed":
            normal = normal.negative()
        return FreeCAD.Rotation(normal, FreeCAD.Vector(0, 0, 1))
    return FreeCAD.Rotation()


def place_model(args: dict[str, Any]) -> dict[str, Any]:
    """Turn and move a job's model clones (e.g. flip for the second side)."""
    import Draft

    doc = _doc(args.get("doc_name"))
    job = _job(doc, args.get("job_name"))
    rot = _rotation_for(args, job, doc)
    center = _models_bb(job).Center
    stock = job.Stock
    # stock from base (model bbox + margins) has the Ext* margin properties
    from_base = stock is not None and hasattr(stock, "ExtXneg")
    if not rot.isNull():
        angle = math.degrees(rot.Angle)
        for clone in job.Model.Group:  # as the Job dialog: around the bbox center
            Draft.rotate(clone, angle, center, rot.Axis)
        if stock is not None and not from_base:
            Draft.rotate(stock, angle, center, rot.Axis)
    doc.recompute()
    origin = args.get("origin") or {}
    bb = _models_bb(job)
    move = FreeCAD.Vector(*(args.get("move") or [0, 0, 0]))
    for key, lo, hi, choices in (
        ("x", bb.XMin, bb.XMax, _ORIGIN_X),
        ("y", bb.YMin, bb.YMax, _ORIGIN_X),
        ("z", bb.ZMin, bb.ZMax, _ORIGIN_Z),
    ):
        how = origin.get(key, "keep")
        if how not in choices:
            raise ValueError(f"origin.{key} must be one of {choices}")
        at = {"min": lo, "bottom": lo, "max": hi, "top": hi, "center": (lo + hi) / 2}
        if how != "keep":
            setattr(move, key, getattr(move, key) - at[how])
    if move.Length > 0:
        for clone in job.Model.Group:
            Draft.move(clone, move)
        if stock is not None and not from_base:
            Draft.move(stock, move)
    doc.recompute()
    if from_base:
        # a from-base stock only records the model position when it is created
        set_stock(
            {
                "doc_name": doc.Name,
                "job_name": job.Name,
                "kind": "from_base",
                "extent_neg": [
                    _mm(stock.ExtXneg),
                    _mm(stock.ExtYneg),
                    _mm(stock.ExtZneg),
                ],
                "extent_pos": [
                    _mm(stock.ExtXpos),
                    _mm(stock.ExtYpos),
                    _mm(stock.ExtZpos),
                ],
            }
        )
    doc.recompute()
    bb = _models_bb(job)
    sbb = job.Stock.Shape.BoundBox if job.Stock is not None else None
    return {
        "job": job.Name,
        "rotation": {
            "axis": _safe(rot.Axis),
            "angle": round(math.degrees(rot.Angle), 6),
        },
        "move": _safe(move),
        "model_bound_box": [
            round(v, 4) for v in (bb.XMin, bb.XMax, bb.YMin, bb.YMax, bb.ZMin, bb.ZMax)
        ],
        "stock_bound_box": None
        if sbb is None
        else [
            round(v, 4)
            for v in (sbb.XMin, sbb.XMax, sbb.YMin, sbb.YMax, sbb.ZMin, sbb.ZMax)
        ],
        "stock": "recreated from base" if from_base else "moved with the model",
        "operations_recomputed": [o.Label for o in job.Operations.Group],
    }


def _mm(q: Any) -> float:
    return float(q.Value) if hasattr(q, "Value") else float(q)


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


def _sources(doc: Any, job: Any, op_type: str, names: list[str] | None) -> list[Any]:
    """Validated source operations for Copy, Array and SimpleCopy."""
    want = SPECIAL_OPERATIONS[op_type]["sources"]
    if want == "none":
        if names:
            raise ValueError(f"{op_type} takes no source_operations")
        return []
    srcs = [_obj(doc, n) for n in names or []]
    if not srcs or (want == "one" and len(srcs) != 1):
        n = "exactly one" if want == "one" else "at least one"
        raise ValueError(f"{op_type} needs {n} operation in source_operations")
    for s in srcs:
        if not s.isDerivedFrom("Path::Feature") or s not in job.Operations.Group:
            raise ValueError(f"{s.Label} is not a top-level operation of {job.Label}")
    if op_type == "Array":
        # the conditions of FreeCAD's Array command (IsActive)
        from Path.Base import Util as PathUtil

        tcs = {PathUtil.toolControllerForOp(s) for s in srcs}
        if len(tcs) != 1 or None in tcs:
            raise ValueError(
                "Array: all source operations need the same tool controller"
            )
        if any(PathUtil.coolantModeForOp(s) != "None" for s in srcs):
            raise ValueError("Array: FreeCAD only arrays operations with coolant None")
    return srcs


def _create_special_op(
    doc: Any, job: Any, op_type: str, name: str, srcs: list[Any]
) -> Any:
    """Steps of FreeCAD's GUI command for the operations in SPECIAL_OPERATIONS."""
    spec = SPECIAL_OPERATIONS[op_type]
    if op_type == "SimpleCopy":
        import Path.Op.Custom as PathCustom
        from Path.Base import Util as PathUtil
        from PathScripts import PathUtils

        op = PathCustom.Create(name, parentJob=job)
        op.ToolController = PathUtil.toolControllerForOp(srcs[0])
        op.CoolantMode = PathUtil.coolantModeForOp(srcs[0])
        paths = [PathUtils.getPathWithPlacement(s) for s in srcs]
        op.Gcode = [c.toGCode() for p in paths for c in p.Commands]
        _attach_op_vp(op, spec["gui"])
        return op
    gmod = importlib.import_module(spec["gui"])
    op = doc.addObject("Path::FeaturePython", name)
    getattr(gmod, spec["cls"])(op)
    if spec["sources"] == "one":
        op.Base = srcs[0]
        if hasattr(srcs[0], "ToolController"):
            op.ToolController = srcs[0].ToolController
    elif spec["sources"] == "many":
        op.Base = srcs
    if _gui():
        vp = getattr(gmod, spec["vp"])(op.ViewObject)
        if op.ViewObject.Proxy is None:  # some view providers don't self-assign
            op.ViewObject.Proxy = vp
    job.Proxy.addOperation(op)
    return op


def add_operation(args: dict[str, Any]) -> dict[str, Any]:
    """Add an operation of any registered type to a job."""
    op_type = args["op_type"]
    if op_type in SPECIAL_OPERATIONS:
        return _add_special_operation(args)
    if op_type not in OPERATIONS:
        known = sorted(OPERATIONS) + sorted(SPECIAL_OPERATIONS)
        raise ValueError(f"Unknown op_type {op_type!r}; known: {known}")
    if args.get("source_operations"):
        raise ValueError(f"{op_type} takes no source_operations")
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


def _add_special_operation(args: dict[str, Any]) -> dict[str, Any]:
    """add_operation() for Comment, Stop, Copy, Array and SimpleCopy."""
    op_type = args["op_type"]
    if args.get("tool_controller") or args.get("base") is not None:
        raise ValueError(
            f"{op_type} takes no tool_controller or base"
            + (
                "; it uses the source operations' tool controller"
                if SPECIAL_OPERATIONS[op_type]["sources"] != "none"
                else ""
            )
        )
    doc = _doc(args.get("doc_name"))
    job = _job(doc, args.get("job_name"))
    srcs = _sources(doc, job, op_type, args.get("source_operations"))
    op = _create_special_op(doc, job, op_type, args.get("name") or op_type, srcs)
    if args.get("label"):
        op.Label = args["label"]
    report = _set_props(doc, job, op, args.get("properties"), args.get("expressions"))
    doc.recompute()
    return {
        "name": op.Name,
        "label": op.Label,
        "type": op_type,
        "sources": [s.Name for s in srcs],
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
    # "recipe": the steps of FreeCAD's own GUI command / Create() for this
    # dressup, without its task panel and transaction
    if not _gui():
        raise ValueError(f"{dtype}: " + FREECAD_BUGS["dressup-gui-only"])
    import inspect

    gmod = importlib.import_module(spec["gui"])
    dr = doc.addObject("Path::FeaturePython", f"{dtype}Dressup")
    # ObjectDressup(obj, base) on 26.3 for most of them, ObjectDressup(obj) before
    if len(inspect.signature(gmod.ObjectDressup).parameters) >= 2:
        proxy = gmod.ObjectDressup(dr, base)
    else:
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
    if kind == "operation" and tname not in OPERATIONS | SPECIAL_OPERATIONS:
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
        if kind == "operation" and tname in SPECIAL_OPERATIONS:
            srcs = []
            if SPECIAL_OPERATIONS[tname]["sources"] != "none":
                import Path.Op.Profile as PathProfile

                _select_only(job.Tools.Group[0])
                try:
                    srcs = [PathProfile.Create("Profile", parentJob=job)]
                finally:
                    _select_only(None)
                doc.recompute()
            obj = _create_special_op(doc, job, tname, tname, srcs)
        elif kind == "operation":
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
    # Comment and Stop have no tool controller and no moves by design
    if (not cmds or not cut["Z"]) and "ToolController" in op.PropertiesList:
        warnings.append("no cutting moves (check Base, depths and tool controller)")
    if rapid_low and stock_top is not None:
        warnings.append(
            f"{rapid_low} rapid XY moves below the stock top (Z {round(stock_top, 3):g}); "
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
    # FreeCAD omits F entirely when the tool controller feed is 0
    if 0.0 in feeds or (cut["Z"] and not feeds):
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
            and p not in _INTERNAL_PROPS
            and not p.startswith("_")
            and "Hidden" not in ss.getPropertyStatus(p)
        },
        "machine": getattr(job, "Machine", None) or None,
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


_MAX_CHECK_POINTS = 250_000


def _sim_ops(job: Any, names: list[str] | None, doc: Any) -> list[Any]:
    """Active operations with a tool, in job order (or the named ones)."""
    from Path.Dressup import Utils as PathDressup

    if names:
        ops = [_obj(doc, n) for n in names]
        missing = [o.Label for o in ops if o not in job.Operations.Group]
        if missing:
            raise ValueError(f"Not top-level operations of {job.Label}: {missing}")
    else:
        ops = list(job.Operations.Group)
    out = []
    for op in ops:
        if not getattr(op, "Active", True) or getattr(op, "Path", None) is None:
            continue
        tc = PathDressup.toolController(op)
        if tc is not None and tc.Tool is not None:
            out.append((op, tc))
    return out


def _sim_arc(sim: Any, pos: Any, cmd: Any, ccw: bool, resolution: float) -> Any:
    """An XY arc as short G1 moves (as FreeCAD's simulator does)."""
    cx = pos.Base.x + (cmd.i or 0)
    cy = pos.Base.y + (cmd.j or 0)
    a0 = math.atan2(pos.Base.y - cy, pos.Base.x - cx)
    da = math.atan2(cmd.y - cy, cmd.x - cx) - a0
    da = da % (2 * math.pi) if ccw else -((-da) % (2 * math.pi))
    r = math.hypot(cmd.i or 0, cmd.j or 0)
    n = max(1, math.ceil(math.sqrt(r / resolution * da * da)))
    da /= n
    dz = (cmd.z - pos.Base.z) / n
    cmd.Name = "G1"
    for _k in range(n):
        a0 += da
        cmd.x = cx + r * math.cos(a0)
        cmd.y = cy + r * math.sin(a0)
        cmd.z = pos.Base.z + dz
        pos = sim.ApplyCommand(pos, cmd)
    return pos


def _sim_drill(sim: Any, pos: Any, cmd: Any, first: bool) -> Any:
    """A drill cycle as moves: (to R,) over the hole, down, back to R."""
    import Path

    steps = [Path.Command("G0", {"Z": cmd.r})] if first else []
    steps.append(Path.Command("G0", {"X": cmd.x, "Y": cmd.y, "Z": cmd.r}))
    steps.append(Path.Command("G1", {"X": cmd.x, "Y": cmd.y, "Z": cmd.z}))
    steps.append(Path.Command("G1", {"X": cmd.x, "Y": cmd.y, "Z": cmd.r}))
    for s in steps:
        pos = sim.ApplyCommand(pos, s)
    return pos


def _sim_apply(sim: Any, pos: Any, commands: list[Any], resolution: float) -> Any:
    """Feed one operation's commands to PathSim like FreeCAD's simulator.

    XY arcs become short G1 moves, drill cycles become moves.
    """
    import Constants  # CAM's G-code name lists (Mod/CAM/Constants.py, 26.3+)

    moves = Constants.GCODE_MOVE_RAPID + Constants.GCODE_MOVE_MILL
    drills = Constants.GCODE_MOVE_DRILL + Constants.GCODE_DRILL_EXTENDED
    first_drill = True
    for i, cmd in enumerate(commands):
        if cmd.Name in moves:
            first_drill = True
            arc = cmd.Name in Constants.GCODE_MOVE_ARC
            j = i
            while arc and cmd.z is None:  # arcs inherit Z from earlier moves
                j -= 1
                cmd.z = commands[j].z if j >= 0 else 0
            if arc and (cmd.k or 0) == 0:
                ccw = cmd.Name in Constants.GCODE_MOVE_CCW
                pos = _sim_arc(sim, pos, cmd, ccw, resolution)
            else:
                pos = sim.ApplyCommand(pos, cmd)
        if cmd.Name in Constants.GCODE_CYCLE_CANCEL:
            first_drill = True
        if cmd.Name in drills:
            pos = _sim_drill(sim, pos, cmd, first_drill)
            first_drill = False
    return pos


def _top_z(mesh: Any, x: float, y: float, z_from: float) -> float | None:
    hit = mesh.nearestFacetOnRay((x, y, z_from), (0, 0, -1))
    return next(iter(hit.values()))[2] if hit else None


def _compare_with_model(
    result: Any, job: Any, step: float, tol: float, z_from: float
) -> dict[str, Any]:
    """Compare the simulated top surface with the model's, seen from above."""
    import MeshPart

    gouges: list[tuple[float, float, float]] = []
    left_max = 0.0
    checked = 0
    for model in job.Model.Group:
        mesh = MeshPart.meshFromShape(Shape=model.Shape, LinearDeflection=step / 4)
        mb = model.Shape.BoundBox
        nx = int(mb.XLength / step)
        ny = int(mb.YLength / step)
        while nx * ny > _MAX_CHECK_POINTS:
            step *= 1.25
            nx, ny = int(mb.XLength / step), int(mb.YLength / step)
        xs = [mb.XMin + (ix + 0.5) * step for ix in range(nx)]
        ys = [mb.YMin + (iy + 0.5) * step for iy in range(ny)]
        top = [[_top_z(mesh, x, y, z_from) for y in ys] for x in xs]
        k = 2  # the voxel result is off by up to one cell at walls: keep 2 away
        for ix, x in enumerate(xs):
            for iy, y in enumerate(ys):
                zm = top[ix][iy]
                if zm is None:
                    continue
                around = [
                    top[jx][jy] if 0 <= jx < nx and 0 <= jy < ny else None
                    for jx, jy in (
                        (ix + k, iy),
                        (ix - k, iy),
                        (ix, iy + k),
                        (ix, iy - k),
                    )
                ]
                if any(z is None or abs(z - zm) > tol for z in around):
                    continue  # near the outline or a step of the model
                checked += 1
                zr = _top_z(result, x, y, z_from)
                zr = job.Stock.Shape.BoundBox.ZMin if zr is None else zr
                if zr < zm - tol:
                    gouges.append((x, y, zm - zr))
                else:
                    left_max = max(left_max, zr - zm)
    gouges.sort(key=lambda g: -g[2])
    return {
        "checked_points": checked,
        "grid_step": round(step, 4),
        "gouge": {
            "found": bool(gouges),
            "points": len(gouges),
            "area_mm2": round(len(gouges) * step * step, 2),
            "max_depth": round(gouges[0][2], 3) if gouges else 0.0,
            "worst": [
                {"x": round(x, 2), "y": round(y, 2), "depth": round(d, 3)}
                for x, y, d in gouges[:10]
            ],
        },
        "material_left_on_part_max": round(left_max, 3),
    }


def simulate(args: dict[str, Any]) -> dict[str, Any]:
    """Cut the stock along the job's toolpaths (FreeCAD's voxel simulator)."""
    import time

    import Mesh
    import PathSimulator
    from PathScripts import PathUtils

    doc = _doc(args.get("doc_name"))
    job = _job(doc, args.get("job_name"))
    if job.Stock is None:
        raise ValueError(f"{job.Label} has no stock")
    doc.recompute()
    ops = _sim_ops(job, args.get("operations"), doc)
    if not ops:
        raise ValueError("No active operation with a tool controller to simulate")
    stock = job.Stock.Shape
    sb = stock.BoundBox
    maxlen = max(sb.XLength, sb.YLength)
    res = float(args.get("resolution") or min(0.5, max(0.05, maxlen / 400)))
    t0 = time.time()
    sim = PathSimulator.PathSim()
    sim.BeginSimulation(stock, res)
    rot = FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), 0)  # as FreeCAD's simulator
    pos = FreeCAD.Placement(FreeCAD.Vector(0, 0, sb.ZMax), rot)
    for op, tc in ops:
        shape = tc.Tool.Shape
        if shape.isNull() or not shape.isValid():
            raise ValueError(f"Tool shape of {tc.Label} is invalid")
        sim.SetToolShape(shape, 0.05 * res / (0.01 * maxlen))  # GUI: 0.05*accuracy
        pos = _sim_apply(
            sim, pos, list(PathUtils.getPathWithPlacement(op).Commands), res
        )
    outer, inner = sim.GetResultMesh()
    result = Mesh.Mesh(outer)
    result.addMesh(inner)
    sim_time = time.time() - t0
    out: dict[str, Any] = {
        "operations": [op.Label for op, _tc in ops],
        "resolution": round(res, 4),
        "stock_volume": round(stock.Volume, 2),
        "remaining_volume": round(result.Volume, 2),
        "removed_volume": round(stock.Volume - result.Volume, 2),
        "simulation_seconds": round(sim_time, 2),
        "notes": [
            "Voxel simulation seen from above: material is removed along Z only "
            "(3-axis); feeds, tool holder and collisions are not simulated."
        ],
    }
    if args.get("compare_with_model", True):
        t1 = time.time()
        out["model_check"] = _compare_with_model(result, job, res, res, sb.ZMax + 10.0)
        out["model_check"]["seconds"] = round(time.time() - t1, 2)
    if args.get("result_file"):
        path = FsPath(args["result_file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        result.write(str(path))
        out["result_file"] = str(path)
    if args.get("show_result"):
        feat = doc.addObject("Mesh::Feature", "SimulationResult")
        feat.Mesh = result
        feat.Label = f"{job.Label} simulated stock"
        out["result_object"] = feat.Name
        if _gui() and job.Stock.ViewObject is not None:
            job.Stock.ViewObject.Visibility = False
        doc.recompute()
    return out


def check_job(args: dict[str, Any]) -> dict[str, Any]:
    """FreeCAD's job check ("Sanity"): findings, summary, optional HTML report."""
    import tempfile

    from Path.Main.Sanity import ImageBuilder
    from Path.Main.Sanity.Sanity import CAMSanity

    doc = _doc(args.get("doc_name"))
    job = _job(doc, args.get("job_name"))
    doc.recompute()
    report_file = args.get("report_file")
    with tempfile.TemporaryDirectory() as tmp:
        target = FsPath(report_file) if report_file else FsPath(tmp) / "report.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        # built like CAMSanity.validate_job(); images (3D views of the GUI) only
        # on request, the report then embeds them
        sanity = object.__new__(CAMSanity)
        sanity.job = job
        sanity.output_file = str(target)
        sanity.filelocation = str(target.parent)
        sanity.image_builder = (
            ImageBuilder.ImageBuilderFactory.get_image_builder(str(target.parent))
            if args.get("images")
            else ImageBuilder.DummyImageBuilder(str(target.parent))
        )
        sanity.data = sanity.summarize()
        squawks, critical = sanity.get_all_squawks()
        if report_file:
            target.write_text(sanity.get_output_report(), encoding="utf-8")
    run = sanity.data.get("runData", {})
    findings = [
        {
            "level": s.get("squawkType"),
            "message": s.get("Note"),
            "source": s.get("Operator"),
        }
        for s in squawks
    ]
    return {
        "job": job.Name,
        "ok": not critical,
        "critical": len(critical),
        "findings": findings,
        "summary": {
            "cycle_time": run.get("cycletotal"),
            "job_min_z": run.get("jobMinZ"),
            "job_max_z": run.get("jobMaxZ"),
            "operations": [
                {
                    k: _safe(v)
                    for k, v in op.items()
                    if k in ("opName", "minZ", "maxZ", "cycleTime", "coolantMode")
                }
                for op in run.get("operations", [])
            ],
            "stock": {
                k: _safe(v)
                for k, v in sanity.data.get("stockData", {}).items()
                if k not in ("squawkData", "baseImage")
            },
        },
        "report_file": str(target) if report_file else None,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def post_process(args: dict[str, Any]) -> dict[str, Any]:
    """Run the job's post processor without dialogs and write the G-code."""
    from Path.Post.Processor import PostProcessorFactory

    doc = _doc(args.get("doc_name"))
    job = _job(doc, args.get("job_name"))
    machine_name, post_name = _select_post(job, args)
    doc.recompute()
    post = PostProcessorFactory.get_post_processor(job, post_name)
    if post is None or not hasattr(post, "export"):  # 26.3 returns a CAMError
        raise ValueError(f"Post processor {post_name!r} could not be loaded: {post}")
    # export2() is the machine flow; it never opens a dialog
    sections = post.export2() if machine_name else post.export()
    if not sections:
        raise ValueError(
            "Post processor returned nothing (argument error? see the FreeCAD report view)"
        )
    extension = post.get_file_extension() if machine_name else None
    output = args.get("output_file")
    files: list[str] = []
    if args.get("use_job_output"):
        if output:
            raise ValueError("Pass either output_file or use_job_output, not both")
        files = _write_job_output(job, sections, extension)
    elif output:
        files = _write_output_file(
            FsPath(output), sections, args.get("split_files", True)
        )
    gcode = "".join(g for _n, g in sections if g)
    out: dict[str, Any] = {
        "machine": machine_name or None,
        "post_processor": post_name,
        "args": None if machine_name else job.PostProcessorArgs,
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


def _select_post(job: Any, args: dict[str, Any]) -> tuple[str, str]:
    """(machine name or "", post processor name) for a post_process call."""
    machine_name = getattr(job, "Machine", "")  # FreeCAD 26.3+
    if machine_name:
        # machine flow: the machine names the post processor and holds its
        # options; Job.PostProcessor/PostProcessorArgs are not used
        if args.get("post_processor") or args.get("post_args") is not None:
            raise ValueError(
                f"Job {job.Label} uses machine {machine_name!r}, which sets the post "
                "processor and its options; change the machine (or clear "
                "Job.Machine) instead of passing post_processor/post_args"
            )
        post_name = _machine(machine_name).postprocessor_file_name
        if not post_name:
            raise ValueError(f"Machine {machine_name!r} names no post processor")
        return machine_name, post_name
    if args.get("post_processor"):
        job.PostProcessor = args["post_processor"]
    if not job.PostProcessor:
        raise ValueError("No post processor set on the job; pass post_processor")
    if args.get("post_args") is not None:
        job.PostProcessorArgs = args["post_args"]
    # legacy posts open a modal G-code editor in the GUI unless told otherwise
    if "--no-show-editor" not in job.PostProcessorArgs:
        job.PostProcessorArgs = (job.PostProcessorArgs + " --no-show-editor").strip()
    return "", job.PostProcessor


def _write_output_file(out_path: Any, sections: list[Any], split: bool) -> list[str]:
    """Write to the given file, or one <name>_<section> file per section."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if len(sections) == 1 or not split:
        out_path.write_text("".join(g for _n, g in sections if g))
        return [str(out_path)]
    files = []
    for name, g in sections:
        path = (
            out_path.with_name(f"{out_path.stem}_{name}{out_path.suffix}")
            if name
            else out_path
        )
        path.write_text(g or "")
        files.append(str(path))
    return files


def _write_job_output(
    job: Any, sections: list[Any], extension: str | None = None
) -> list[str]:
    """Write the sections where FreeCAD's Post Process command would.

    Uses FreeCAD's FilenameGenerator with the job's output pattern
    (Job.PostProcessorOutputFile), so the names match the GUI. In the machine
    flow the post processor's file extension replaces the pattern's, as in
    FreeCAD. Existing files are overwritten.
    """
    import re

    from Path.Post.Utils import FilenameGenerator

    generator = (
        FilenameGenerator(job=job, file_extension=extension)
        if extension
        else FilenameGenerator(job=job)
    )
    if re.search(r"%[TtWO]", job.PostProcessorOutputFile or "") and not hasattr(
        generator, "filename_template"
    ):
        raise RuntimeError(FREECAD_BUGS["output-filename-subpart"])
    names = generator.generate_filenames()
    files = []
    for subpart, gcode in sections:
        if gcode is None:  # the post processor asked for no file
            continue
        generator.set_subpartname("" if subpart == "allitems" else subpart)
        path = FsPath(next(names))
        if not path.is_absolute():
            raise ValueError(
                f"The job's output pattern {job.PostProcessorOutputFile!r} gives the "
                f"relative path {str(path)!r} (an unsaved document has no folder "
                "for %D); save the document or pass output_file"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        # line ends as in FreeCAD's Post Process command: a leading "\n\n" or a
        # "\r" means write unchanged, otherwise "\n" becomes the system's line end
        if gcode.startswith("\n\n"):
            path.write_text(gcode[2:], newline="")
        elif "\r" in gcode:
            path.write_text(gcode, newline="")
        else:
            path.write_text(gcode)
        files.append(str(path))
    return files


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
        # The fifth argument is a list of operation names, not a bool.
        attrs[PathJob.JobTemplate.SetupSheet] = job.SetupSheet.Proxy.templateAttributes(
            True, True, True, True, None
        )
    path.write_text(json.dumps(attrs, sort_keys=True, indent=2))
    return {"file": str(path), "keys": sorted(attrs)}
