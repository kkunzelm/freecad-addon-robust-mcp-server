"""CAM workbench tools for FreeCAD Robust MCP Server.

These tools build and inspect CAM jobs (stock, tool controllers, operations,
dressups) and post-process them to G-code, without opening any dialog.

They are generic: every operation and dressup type of the CAM workbench is
reachable through ``cam_add_operation``/``cam_add_dressup``, and every option
through ``cam_describe_type``/``cam_get_properties``/``cam_set_properties``,
which read FreeCAD's own property metadata (types, enumerations, tooltips).

All FreeCAD-side code lives in ``freecad_mcp/freecad_side/cam_runtime.py``.
Each tool sends that module plus one function call; arguments travel as JSON,
so no user value is ever pasted into Python source.
"""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from freecad_mcp.tools.utils import wrap_with_transaction

_RUNTIME_PATH = (
    Path(__file__).resolve().parent.parent / "freecad_side" / "cam_runtime.py"
)


def _runtime_source() -> str:
    """Return the FreeCAD-side CAM runtime source.

    Returns:
        The source code of ``cam_runtime.py``. Read on every call so edits in
        a development checkout apply after reconnecting the MCP server.
    """
    return _RUNTIME_PATH.read_text(encoding="utf-8")


def build_cam_code(
    func: str,
    args: dict[str, Any],
    transaction: str | None = None,
) -> str:
    """Build the Python code that runs one CAM runtime function in FreeCAD.

    Args:
        func: Name of a function defined in ``cam_runtime.py``.
        args: JSON-serializable arguments passed to that function as a dict.
        transaction: Undo transaction name for modifying calls, None for
            read-only calls.

    Returns:
        Python source for the bridge's ``execute_python``.

    Example:
        >>> code = build_cam_code("inspect_job", {"job_name": "Job"})
    """
    payload = json.dumps(args)
    call = f"import json as _json\n_result_ = {func}(_json.loads({payload!r}))\n"
    if transaction:
        doc_name = args.get("doc_name")
        doc_expr = (
            f"FreeCAD.getDocument({doc_name!r})"
            if doc_name
            else "FreeCAD.ActiveDocument"
        )
        call = wrap_with_transaction(call, transaction, doc_expr=doc_expr)
    return _runtime_source() + "\n\n" + call


def _drop_none(args: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in args.items() if v is not None}


def _error_message(result: Any) -> str:
    """Shorten a FreeCAD traceback to its final error line plus the traceback."""
    tb = result.error_traceback or "CAM operation failed"
    last = tb.strip().splitlines()[-1] if tb.strip() else tb
    return f"{last}\n\n{tb}"


def register_cam_tools(mcp: Any, get_bridge: Callable[[], Awaitable[Any]]) -> None:
    """Register CAM workbench tools with the Robust MCP Server.

    Args:
        mcp: The FastMCP (Robust MCP Server) instance.
        get_bridge: Async function to get the active bridge.

    Example:
        Register CAM tools with an MCP server::

            from freecad_mcp.tools.cam import register_cam_tools

            register_cam_tools(mcp, get_bridge)
    """

    async def run(
        func: str, args: dict[str, Any], transaction: str | None = None
    ) -> Any:
        bridge = await get_bridge()
        result = await bridge.execute_python(
            build_cam_code(func, _drop_none(args), transaction)
        )
        if result.success:
            return result.result
        raise ValueError(_error_message(result))

    @mcp.tool()
    async def cam_get_capabilities() -> dict[str, Any]:
        """List what the CAM workbench offers in the connected FreeCAD.

        Use this first. It reports every operation and dressup type with its
        availability, the enabled post processors, job templates (job_*.json),
        the tool asset path, and known FreeCAD CAM issues the tools detect.

        Returns:
            Dictionary with freecad_version, gui, operations, dressups,
            stock_kinds, post_processors, job_templates, default_job_template,
            asset_path, freecad_home, local_patches, known_freecad_issues.
        """
        return await run("capabilities", {})

    @mcp.tool()
    async def cam_list_tool_library(library: str | None = None) -> dict[str, Any]:
        """List FreeCAD's tool libraries, their tool bits and the tool shapes.

        Tool bits from a library can be used in ``cam_add_tool_controller``
        with ``tool={"library_bit": "<id>"}``. Shapes (endmill, drill, v-bit,
        ...) can be used to define a new bit with ``tool={"shape": ...}``.

        Args:
            library: Only list this library (id or label). All if None.

        Returns:
            Dictionary with libraries (id, label, bits with number, id, label,
            shape, parameters, attributes) and shapes.
        """
        return await run("list_tools", {"library": library})

    @mcp.tool()
    async def cam_analyze_model(
        object_name: str,
        doc_name: str | None = None,
        face_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """List a model's faces and drillable holes, to choose operation Base faces.

        Args:
            object_name: Model object (name or unique label).
            doc_name: Document. Uses the active document if None.
            face_types: Only list these surface types, e.g. ["Plane", "Cylinder"].

        Returns:
            Dictionary with bound_box, volume, valid, faces (face name, surface
            type, area, z range, normal for planes, radius/center/axis for
            cylinders) and drillable_holes (element, diameter, center).
        """
        return await run(
            "analyze_model",
            {
                "object_name": object_name,
                "doc_name": doc_name,
                "face_types": face_types,
            },
        )

    @mcp.tool()
    async def cam_create_job(
        model_objects: list[str],
        doc_name: str | None = None,
        label: str | None = None,
        template: str | None = None,
        model_offset: list[float] | None = None,
        stock: dict[str, Any] | None = None,
        default_tool_controller: bool = True,
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a CAM job for one or more model objects, without the job dialog.

        The job works on clones of the models (Job.Model group); the originals
        are not changed. Machine/material defaults belong in a job template
        (see ``cam_export_job_template``).

        Args:
            model_objects: Objects to machine (names or unique labels).
            doc_name: Document. Uses the active document if None.
            label: Job label.
            template: Path to a job_*.json template. None uses FreeCAD's
                default template preference; "" uses no template.
            model_offset: [x, y, z] added to the clones' placement, e.g. to put
                the top of the part at Z0. Stock is then re-created from it.
            stock: Stock definition, same keys as ``cam_set_stock``.
            default_tool_controller: Without a template FreeCAD adds a 5 mm end
                mill tool controller. False removes it.
            properties: Job property values, e.g. {"PostProcessor": "linuxcnc"}.

        Returns:
            The job description from ``cam_inspect_job``.
        """
        return await run(
            "create_job",
            {
                "model_objects": model_objects,
                "doc_name": doc_name,
                "label": label,
                "template": template,
                "model_offset": model_offset,
                "stock": stock,
                "default_tool_controller": default_tool_controller,
                "properties": properties,
            },
            "Create CAM Job",
        )

    @mcp.tool()
    async def cam_set_stock(
        kind: str = "from_base",
        job_name: str | None = None,
        doc_name: str | None = None,
        extent_neg: list[float] | None = None,
        extent_pos: list[float] | None = None,
        size: list[float] | None = None,
        radius: float | None = None,
        height: float | None = None,
        placement: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Replace a job's stock.

        Args:
            kind: "from_base" (model bounding box plus margins), "box" or
                "cylinder".
            job_name: Job. Optional if the document has exactly one job.
            doc_name: Document. Uses the active document if None.
            extent_neg: from_base margins [x, y, z] on the negative sides (mm).
            extent_pos: from_base margins [x, y, z] on the positive sides (mm).
            size: box [length, width, height] (mm).
            radius: cylinder radius (mm).
            height: cylinder height (mm).
            placement: {"base": [x, y, z], "axis": [x, y, z], "angle": deg}.

        Returns:
            Dictionary with stock name, kind and bound_box.
        """
        return await run(
            "set_stock",
            {
                "kind": kind,
                "job_name": job_name,
                "doc_name": doc_name,
                "extent_neg": extent_neg,
                "extent_pos": extent_pos,
                "size": size,
                "radius": radius,
                "height": height,
                "placement": placement,
            },
            "Set CAM Stock",
        )

    @mcp.tool()
    async def cam_add_tool_controller(
        tool: dict[str, Any],
        job_name: str | None = None,
        doc_name: str | None = None,
        tool_number: int | None = None,
        label: str | None = None,
        spindle_speed: float | None = None,
        spindle_dir: str | None = None,
        horiz_feed: str | None = None,
        vert_feed: str | None = None,
        properties: dict[str, Any] | None = None,
        expressions: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        """Add a tool controller (tool bit plus speeds and feeds) to a job.

        Args:
            tool: One of
                {"library_bit": "<bit id from cam_list_tool_library>"},
                {"bit_file": "/path/to/bit.fctb"}, or
                {"shape": "endmill", "name": "6mm EM",
                 "parameters": {"Diameter": "6 mm", "Flutes": 4, ...},
                 "attributes": {...}}.
            job_name: Job. Optional if the document has exactly one job.
            doc_name: Document. Uses the active document if None.
            tool_number: T number. Next free number if None.
            label: Controller label. "TC: <tool label>" if None.
            spindle_speed: Spindle speed in rpm.
            spindle_dir: "Forward", "Reverse" or "None".
            horiz_feed: Feed with unit, e.g. "800 mm/min". A bare number is mm/s.
            vert_feed: Plunge feed with unit, e.g. "200 mm/min".
            properties: Any further tool controller properties.
            expressions: Property expressions, e.g. {"SpindleSpeed": "..."}.

        Returns:
            Dictionary with name, label, tool_number, tool, diameter, properties.
        """
        return await run(
            "add_tool_controller",
            {
                "tool": tool,
                "job_name": job_name,
                "doc_name": doc_name,
                "tool_number": tool_number,
                "label": label,
                "spindle_speed": spindle_speed,
                "spindle_dir": spindle_dir,
                "horiz_feed": horiz_feed,
                "vert_feed": vert_feed,
                "properties": properties,
                "expressions": expressions,
            },
            "Add Tool Controller",
        )

    @mcp.tool()
    async def cam_remove_tool_controller(
        tool_controller: str,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Remove a tool controller (and its tool bit) that no operation uses.

        Args:
            tool_controller: Tool controller (name or unique label).
            doc_name: Document. Uses the active document if None.

        Returns:
            Dictionary with removed (label) and remaining tool controllers.
        """
        return await run(
            "remove_tool_controller",
            {"tool_controller": tool_controller, "doc_name": doc_name},
            "Remove Tool Controller",
        )

    @mcp.tool()
    async def cam_add_operation(
        op_type: str,
        job_name: str | None = None,
        tool_controller: str | None = None,
        base: list[dict[str, Any]] | None = None,
        properties: dict[str, Any] | None = None,
        expressions: dict[str, str | None] | None = None,
        name: str | None = None,
        label: str | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Add any CAM operation to a job.

        Use ``cam_get_capabilities`` for the types and ``cam_describe_type``
        for every option of a type. Depth properties are expressions by
        default; giving a value replaces the expression.

        Args:
            op_type: Operation type, e.g. "Profile", "Pocket", "PocketShape",
                "Adaptive", "MillFace", "Helix", "Drilling", "Tapping",
                "ThreadMilling", "Engrave", "Vcarve", "Deburr", "Slot",
                "Surface", "Waterline", "Probe", "Custom".
            job_name: Job. Optional if the document has exactly one job.
            tool_controller: Tool controller (name or label). Required when
                the job has more than one.
            base: Features to machine, [{"object": "<model>", "subelements":
                ["Face3", "Edge7"]}]. Original model names are mapped to the
                job's clones. None keeps FreeCAD's default (e.g. Drilling picks
                all holes, Profile the model outline); [] clears it.
            properties: Property values, e.g. {"Side": "Outside",
                "FinalDepth": -5, "StepDown": "1.5 mm"}.
            expressions: Property expressions; None as value removes one.
            name: Internal object name. The type if None.
            label: Display label.
            doc_name: Document. Uses the active document if None.

        Returns:
            Dictionary with name, label, type, what was set, and a path summary
            (commands, cut bound box, feeds, cycle time, warnings, notes).
        """
        return await run(
            "add_operation",
            {
                "op_type": op_type,
                "job_name": job_name,
                "tool_controller": tool_controller,
                "base": base,
                "properties": properties,
                "expressions": expressions,
                "name": name,
                "label": label,
                "doc_name": doc_name,
            },
            f"Add {op_type} Operation",
        )

    @mcp.tool()
    async def cam_add_dressup(
        dressup_type: str,
        base_operation: str,
        properties: dict[str, Any] | None = None,
        expressions: dict[str, str | None] | None = None,
        tag_count: int | None = None,
        label: str | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Add a dressup to an operation (or to another dressup).

        The dressup replaces the operation in the job. If the connected FreeCAD
        has the known bug where the base operation stays in the job (it would
        be cut twice), the call fails and is undone instead of working around
        it; see ``cam_get_capabilities`` known_freecad_issues.

        Args:
            dressup_type: "Tags", "Boundary", "Dogbone", "Array", "LeadInOut",
                "RampEntry", "Dragknife", "AxisMap" or "ZCorrect".
            base_operation: Operation or dressup to dress (name or label).
            properties: Property values, e.g. {"Width": 6, "Height": 1.5}.
            expressions: Property expressions.
            tag_count: Tags only: number of holding tags to generate.
            label: Display label.
            doc_name: Document. Uses the active document if None.

        Returns:
            Dictionary with name, label, type, base, what was set and a path
            summary.
        """
        return await run(
            "add_dressup",
            {
                "dressup_type": dressup_type,
                "base_operation": base_operation,
                "properties": properties,
                "expressions": expressions,
                "tag_count": tag_count,
                "label": label,
                "doc_name": doc_name,
            },
            f"Add {dressup_type} Dressup",
        )

    @mcp.tool()
    async def cam_describe_type(
        type_name: str,
        kind: str = "operation",
        groups: list[str] | None = None,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        """List every option of an operation or dressup type with its defaults.

        Builds the type on a placeholder model in a hidden temporary document,
        reads its properties and closes the document again.

        Args:
            type_name: Operation or dressup type (see cam_get_capabilities).
            kind: "operation" or "dressup".
            groups: Only these property groups, e.g. ["Depth", "Operation"].
            include_hidden: Include properties FreeCAD hides in the editor.

        Returns:
            Dictionary with kind, type and properties (name, type, group,
            default, doc, enum values, expression, read_only).
        """
        return await run(
            "describe_type",
            {
                "type": type_name,
                "kind": kind,
                "groups": groups,
                "include_hidden": include_hidden,
            },
        )

    @mcp.tool()
    async def cam_get_properties(
        object_name: str,
        groups: list[str] | None = None,
        include_hidden: bool = False,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Read all properties of a CAM object with their metadata.

        Works for jobs, stock, setup sheets, tool controllers, operations and
        dressups.

        Args:
            object_name: Object (name or unique label).
            groups: Only these property groups.
            include_hidden: Include properties FreeCAD hides in the editor.
            doc_name: Document. Uses the active document if None.

        Returns:
            Dictionary with name, label, kind, type and properties (name, type,
            group, value, doc, enum values, expression, read_only).
        """
        return await run(
            "get_properties",
            {
                "object_name": object_name,
                "groups": groups,
                "include_hidden": include_hidden,
                "doc_name": doc_name,
            },
        )

    @mcp.tool()
    async def cam_set_properties(
        object_name: str,
        values: dict[str, Any] | None = None,
        expressions: dict[str, str | None] | None = None,
        recompute: bool = True,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Set properties of any CAM object, with validation.

        Enumeration values are checked against the allowed list, read-only
        properties are refused, and a plain value replaces an expression.
        Quantities take units: "5 mm", "800 mm/min", "90 °".

        Args:
            object_name: Object (name or unique label).
            values: Property values. Link lists use [{"object", "subelements"}],
                links an object name, vectors [x, y, z].
            expressions: Property expressions; None as value removes one.
            recompute: Recompute the document afterwards.
            doc_name: Document. Uses the active document if None.

        Returns:
            Dictionary with what was set and, for operations and dressups, a
            path summary.
        """
        return await run(
            "set_properties",
            {
                "object_name": object_name,
                "values": values,
                "expressions": expressions,
                "recompute": recompute,
                "doc_name": doc_name,
            },
            "Set CAM Properties",
        )

    @mcp.tool()
    async def cam_set_operation_order(
        order: list[str],
        job_name: str | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Reorder the operations of a job (this is the G-code order).

        Args:
            order: Every top-level operation/dressup of the job, in the new order.
            job_name: Job. Optional if the document has exactly one job.
            doc_name: Document. Uses the active document if None.

        Returns:
            Dictionary with the new order (labels).
        """
        return await run(
            "set_operation_order",
            {"order": order, "job_name": job_name, "doc_name": doc_name},
            "Reorder CAM Operations",
        )

    @mcp.tool()
    async def cam_inspect_job(
        job_name: str | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Describe a job: models, stock, tool controllers, operations, post.

        Every operation includes a path summary with plausibility warnings
        (no cutting moves, feed 0, rapid moves below the stock top, cuts
        outside the stock) and notes (e.g. cuts below the stock bottom).

        Args:
            job_name: Job. Optional if the document has exactly one job.
            doc_name: Document. Uses the active document if None.

        Returns:
            Dictionary with models, stock, tool_controllers, operations,
            setup_sheet, post processor settings, fixtures and cycle_time.
        """
        return await run("inspect_job", {"job_name": job_name, "doc_name": doc_name})

    @mcp.tool()
    async def cam_inspect_path(
        object_name: str,
        gcode_lines: int = 0,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Summarize the toolpath of one operation or dressup.

        Args:
            object_name: Operation or dressup (name or unique label).
            gcode_lines: Also return up to this many lines of its path as
                G-code (FreeCAD internal units: mm, mm/s). 0 returns none.
            doc_name: Document. Uses the active document if None.

        Returns:
            Dictionary with commands, by_command, feeds_mm_per_min, cycle_time,
            cut_bound_box, warnings, notes and optionally gcode.
        """
        return await run(
            "inspect_path",
            {
                "object_name": object_name,
                "gcode_lines": gcode_lines,
                "doc_name": doc_name,
            },
        )

    @mcp.tool()
    async def cam_post_process(
        job_name: str | None = None,
        post_processor: str | None = None,
        post_args: str | None = None,
        output_file: str | None = None,
        split_files: bool = True,
        use_job_output: bool = False,
        return_lines: int = 0,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Post-process a job to G-code and optionally write it to a file.

        "--no-show-editor" is always added, so FreeCAD never opens its G-code
        editor dialog. Check generated G-code in a simulator before running it
        on a machine.

        Args:
            job_name: Job. Optional if the document has exactly one job.
            post_processor: Post processor name, e.g. "linuxcnc", "grbl". The
                job's setting if None (see cam_get_capabilities).
            post_args: Post processor arguments (replaces the job's setting).
            output_file: File to write. Nothing is written if None.
            split_files: When the job splits its output (Job.SplitOutput),
                write one file per section as <name>_<section><ext>.
            use_job_output: Write the files exactly where FreeCAD's Post
                Process command would: folder and names from the job's output
                pattern (Job.PostProcessorOutputFile, e.g. "%D/%S-%t-%d.nc"),
                one file per section when the job splits its output. Existing
                files are overwritten. Not together with output_file. Needs a
                saved document when the pattern uses %D.
            return_lines: Also return up to this many G-code lines.
            doc_name: Document. Uses the active document if None.

        Returns:
            Dictionary with post_processor, args, sections, lines, files,
            cycle_time and optionally gcode.
        """
        return await run(
            "post_process",
            {
                "job_name": job_name,
                "post_processor": post_processor,
                "post_args": post_args,
                "output_file": output_file,
                "split_files": split_files,
                "use_job_output": use_job_output,
                "return_lines": return_lines,
                "doc_name": doc_name,
            },
            "Post Process CAM Job",
        )

    @mcp.tool()
    async def cam_export_job_template(
        file_path: str,
        job_name: str | None = None,
        include_tool_controllers: bool = True,
        include_stock: bool = True,
        include_setup_sheet: bool = True,
        include_post: bool = True,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Save a job's settings as a FreeCAD job template (job_*.json).

        Templates are how FreeCAD keeps machine and material defaults: tool
        controllers with speeds and feeds, setup sheet heights and expressions,
        stock margins and the post processor. Use one with cam_create_job.

        Args:
            file_path: Target file; the name must start with "job_" and end
                with ".json" so FreeCAD finds it.
            job_name: Job. Optional if the document has exactly one job.
            include_tool_controllers: Store the tool controllers.
            include_stock: Store the stock definition.
            include_setup_sheet: Store the setup sheet.
            include_post: Store post processor, arguments and output file.
            doc_name: Document. Uses the active document if None.

        Returns:
            Dictionary with file and the stored top-level keys.
        """
        return await run(
            "export_job_template",
            {
                "file_path": file_path,
                "job_name": job_name,
                "include_tool_controllers": include_tool_controllers,
                "include_stock": include_stock,
                "include_setup_sheet": include_setup_sheet,
                "include_post": include_post,
                "doc_name": doc_name,
            },
        )
