from __future__ import annotations







from copy import deepcopy



from datetime import datetime, timezone



import hashlib



import json



import math



import os



from pathlib import Path
from types import SimpleNamespace



import re



import shutil
import subprocess



import uuid







from ..audit import AuditLogger



from ..config import EDAConfig



from ..security import PathGuard











_REF_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9_#]*[0-9]+[A-Za-z0-9_#]*|#[A-Za-z]+[0-9]+)$")



_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")



_ALLOWED_LABEL_KINDS = {"local", "global", "hierarchical"}











class SExpressionError(ValueError):



    pass











class SchematicEngine:



    """SCH-first engine for Actuator.







    The engine has two deliberately separate layers:



      * Circuit IR: safe JSON mutations with revision history.



      * KiCad schematic inspection: read-only parsing of .kicad_sch.







    Writing .kicad_sch is intentionally NOT implemented until a KiCad/symbol-library



    backend can round-trip and externally verify the generated file.



    """







    SCHEMA = "chatgpt-actuator/schematic-ir/v1"







    def __init__(self, config: EDAConfig, audit: AuditLogger, guard: PathGuard) -> None:



        self.config = config



        self.audit = audit



        self.guard = guard







    def _require_read(self) -> None:



        if not self.config.enabled:



            raise PermissionError("EDA tools are disabled.")



        if not self.config.allow_read:



            raise PermissionError("EDA read/analysis operations are disabled.")







    def _require_edit(self) -> None:



        if not self.config.enabled:



            raise PermissionError("EDA tools are disabled.")



        if not self.config.allow_edit:



            raise PermissionError("EDA edit operations are disabled.")







    @staticmethod



    def _now() -> str:



        return datetime.now(timezone.utc).isoformat()







    @staticmethod



    def _safe_name(name: str) -> str:



        cleaned = _SAFE_NAME_RE.sub("-", str(name).strip()).strip(".-")



        if not cleaned:



            raise ValueError("name must contain at least one safe filename character.")



        return cleaned[:120]







    @staticmethod



    def _finite(value: float | int, field: str) -> float:



        result = float(value)



        if not math.isfinite(result):



            raise ValueError(f"{field} must be finite.")



        return result







    @staticmethod



    def _normalize_rotation(value: float | int) -> float:



        rotation = float(value) % 360.0



        allowed = (0.0, 90.0, 180.0, 270.0)



        if not any(abs(rotation - item) < 1e-9 for item in allowed):



            raise ValueError("rotation must be one of 0, 90, 180, or 270 degrees.")



        return rotation







    def plan_create(self, project_dir: str, name: str, title: str = "") -> dict:



        self._require_edit()



        root = self.guard.resolve_existing(project_dir)



        if not root.is_dir():



            raise NotADirectoryError(str(root))



        safe_name = self._safe_name(name)



        folder = self.guard.resolve_target(root / ".actuator" / "schematic")



        folder.mkdir(parents=True, exist_ok=True)



        folder = self.guard.resolve_existing(folder)



        target = self.guard.resolve_target(folder / f"{safe_name}.sch-ir.json")



        if target.exists():



            raise FileExistsError(str(target))







        now = self._now()



        data = {



            "schema": self.SCHEMA,



            "name": str(name).strip(),



            "title": str(title),



            "revision": 1,



            "status": "draft",



            "created_at": now,



            "updated_at": now,



            "components": [],



            "connections": [],



            "markers": [],



            "materialized_schematic": None,



            "verification": None,



        }



        self._atomic_write(target, data)



        self.audit.record("eda_sch_plan_create", path=str(target), name=data["name"])



        return self._summary(target, data)







    def plan_get(self, ir_path: str) -> dict:



        self._require_read()



        path, data = self._load_ir(ir_path)



        self.audit.record("eda_sch_plan_get", path=str(path), revision=data["revision"])



        return {"path": str(path), "ir": data, "validation": self._validate_data(data)}







    def component(

        self,

        ir_path: str,

        action: str,

        reference: str,

        *,

        lib_id: str | None = None,

        value: str | None = None,

        footprint: str | None = None,

        x: float | None = None,

        y: float | None = None,

        rotation: float | None = None,

        multi_unit: bool | None = None,

        unit_spacing: float | None = None,

        component_type: str | None = None,

    ) -> dict:

        self._require_edit()

        path, data = self._load_ir(ir_path)

        action = str(action).strip().casefold()

        reference = str(reference).strip()

        if not reference:

            raise ValueError("reference must not be empty.")



        components = data["components"]

        index = next(

            (

                i for i, component in enumerate(components)

                if str(component["reference"]).casefold() == reference.casefold()

            ),

            None,

        )



        if action == "add":

            if index is not None:

                raise ValueError(f"Component reference already exists: {reference}")

            if not _REF_RE.match(reference):

                raise ValueError("reference must look like R1, C3, U2, J1, #PWR0101, etc.")



            lib_id = str(lib_id or "").strip()

            if ":" not in lib_id:

                raise ValueError("lib_id must use KiCad LIBRARY:SYMBOL form.")

            if x is None or y is None:

                raise ValueError("x and y are required when adding a component.")



            inferred_type = (
                "power_flag"
                if lib_id.casefold() == "power:pwr_flag"
                else (
                    "power"
                    if lib_id.casefold().startswith("power:")
                    or reference.casefold().startswith("#pwr")
                    else "component"
                )
            )

            normalized_type = str(component_type or inferred_type).strip().casefold()

            if normalized_type not in {"component", "power", "power_flag"}:

                raise ValueError("component_type must be component, power, or power_flag.")

            is_multi = bool(multi_unit) if multi_unit is not None else False

            if normalized_type in {"power", "power_flag"}:

                is_multi = False

                footprint = ""



            spacing = 25.4 if unit_spacing is None else self._finite(unit_spacing, "unit_spacing")

            if spacing <= 0:

                raise ValueError("unit_spacing must be positive.")



            component = {

                "id": str(uuid.uuid4()),

                "reference": reference,

                "lib_id": lib_id,

                "value": str(value or ""),

                "footprint": str(footprint or ""),

                "position": {

                    "x": self._finite(x, "x"),

                    "y": self._finite(y, "y"),

                },

                "rotation": self._normalize_rotation(0 if rotation is None else rotation),

                "component_type": normalized_type,

                "multi_unit": is_multi,

                "unit_spacing": spacing,

            }

            components.append(component)

            change = {"action": "add", "component": deepcopy(component)}



        elif action == "update":

            if index is None:

                raise KeyError(f"Component not found: {reference}")

            component = components[index]



            if lib_id is not None:

                candidate = str(lib_id).strip()

                if ":" not in candidate:

                    raise ValueError("lib_id must use KiCad LIBRARY:SYMBOL form.")

                component["lib_id"] = candidate

            if value is not None:

                component["value"] = str(value)

            if footprint is not None:

                component["footprint"] = str(footprint)

            if x is not None:

                component["position"]["x"] = self._finite(x, "x")

            if y is not None:

                component["position"]["y"] = self._finite(y, "y")

            if rotation is not None:

                component["rotation"] = self._normalize_rotation(rotation)

            if multi_unit is not None:

                component["multi_unit"] = bool(multi_unit)

            if unit_spacing is not None:

                spacing = self._finite(unit_spacing, "unit_spacing")

                if spacing <= 0:

                    raise ValueError("unit_spacing must be positive.")

                component["unit_spacing"] = spacing

            if component_type is not None:

                normalized_type = str(component_type).strip().casefold()

                if normalized_type not in {"component", "power", "power_flag"}:

                    raise ValueError("component_type must be component, power, or power_flag.")

                component["component_type"] = normalized_type



            normalized_type = str(

                component.get(

                    "component_type",

                    "power_flag"
                    if str(component.get("lib_id", "")).casefold() == "power:pwr_flag"
                    else (
                        "power"
                        if str(component.get("lib_id", "")).casefold().startswith("power:")
                        or reference.casefold().startswith("#pwr")
                        else "component"
                    ),

                )

            ).casefold()

            component["component_type"] = normalized_type

            component.setdefault("multi_unit", False)

            component.setdefault("unit_spacing", 25.4)

            if normalized_type in {"power", "power_flag"}:

                component["footprint"] = ""

                component["multi_unit"] = False



            change = {"action": "update", "component": deepcopy(component)}



        elif action == "remove":

            if index is None:

                raise KeyError(f"Component not found: {reference}")

            data["markers"] = [

                marker

                for marker in data["markers"]

                if not (

                    marker.get("kind") == "no_connect"

                    and str(marker.get("reference", "")).casefold() == reference.casefold()

                )

            ]

            data["connections"] = [

                connection

                for connection in data["connections"]

                if str(connection["from"]["reference"]).casefold() != reference.casefold()

                and str(connection["to"]["reference"]).casefold() != reference.casefold()

            ]

            del components[index]

            change = {"action": "remove", "reference": reference}

        else:

            raise ValueError("action must be add, update, or remove.")



        self._commit_mutation(path, data, "component")

        self.audit.record(

            "eda_sch_component",

            path=str(path),

            operation=action,

            reference=reference,

        )

        return {

            "path": str(path),

            "revision": data["revision"],

            "change": change,

            "validation": self._validate_data(data),

        }



    def power(
        self,
        ir_path: str,
        action: str,
        power_name: str,
        *,
        reference: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict:
        """Add/remove a KiCad power symbol or PWR_FLAG in Circuit IR."""
        self._require_edit()
        action = str(action).strip().casefold()
        power_name = str(power_name).strip()
        if not power_name:
            raise ValueError("power_name must not be empty.")

        if action == "add":
            _, data = self._load_ir(ir_path)
            used = {
                str(component.get("reference", "")).casefold()
                for component in data["components"]
            }
            is_flag = power_name.casefold() == "pwr_flag"
            prefix = "#FLG" if is_flag else "#PWR"
            if reference is None:
                for number in range(101, 10000):
                    candidate = f"{prefix}{number:04d}"
                    if candidate.casefold() not in used:
                        reference = candidate
                        break
                else:
                    raise RuntimeError("Unable to allocate a power reference.")
            if x is None or y is None:
                raise ValueError("x and y are required when adding a power symbol.")
            return self.component(
                ir_path,
                "add",
                str(reference),
                lib_id=f"power:{power_name}",
                value=power_name,
                footprint="",
                x=x,
                y=y,
                rotation=0,
                multi_unit=False,
                component_type="power_flag" if is_flag else "power",
            )

        if action == "remove":
            if not reference:
                raise ValueError("reference is required when removing a power symbol.")
            return self.component(ir_path, "remove", str(reference))

        raise ValueError("action must be add or remove.")


    def auto_layout(
        self,
        ir_path: str,
        *,
        start_x: float = 50.0,
        start_y: float = 50.0,
        column_spacing: float = 35.0,
        row_spacing: float = 25.0,
        power_offset: float = 15.0,
    ) -> dict:
        """Apply collision-aware directed layout for schematic readability."""
        self._require_edit()
        path, data = self._load_ir(ir_path)
        if not data["components"]:
            raise ValueError("Schematic IR has no components to layout.")

        start_x = self._finite(start_x, "start_x")
        start_y = self._finite(start_y, "start_y")
        column_spacing = self._finite(column_spacing, "column_spacing")
        row_spacing = self._finite(row_spacing, "row_spacing")
        power_offset = self._finite(power_offset, "power_offset")
        if column_spacing <= 0 or row_spacing <= 0 or power_offset <= 0:
            raise ValueError("Layout spacing values must be positive.")

        by_ref = {
            str(component["reference"]).casefold(): component
            for component in data["components"]
        }

        def component_kind(component: dict) -> str:
            explicit = str(component.get("component_type", "")).casefold()
            lib_id = str(component.get("lib_id", "")).casefold()
            reference = str(component.get("reference", "")).casefold()
            if explicit == "power_flag" or lib_id == "power:pwr_flag":
                return "power_flag"
            if (
                explicit == "power"
                or (lib_id.startswith("power:") and lib_id != "power:pwr_flag")
                or reference.startswith("#pwr")
            ):
                return "power"
            return "component"

        normal_refs = [
            ref for ref, component in by_ref.items()
            if component_kind(component) == "component"
        ]
        normal_set = set(normal_refs)
        undirected = {ref: set() for ref in normal_refs}
        directed_out = {ref: set() for ref in normal_refs}
        indegree = {ref: 0 for ref in normal_refs}

        for connection in data["connections"]:
            left = str(connection["from"]["reference"]).casefold()
            right = str(connection["to"]["reference"]).casefold()
            if left in normal_set and right in normal_set and left != right:
                undirected[left].add(right)
                undirected[right].add(left)
                if right not in directed_out[left]:
                    directed_out[left].add(right)
                    indegree[right] += 1

        def rank(ref: str) -> tuple:
            raw = str(by_ref[ref]["reference"]).upper()
            match = re.match(r"[A-Z]+", raw)
            prefix = match.group(0) if match else raw
            prefix_rank = {
                "J": 0, "P": 0, "U": 1, "Q": 2,
                "R": 3, "C": 3, "L": 3, "D": 4,
            }.get(prefix, 5)
            return (prefix_rank, raw)

        visited: set[str] = set()
        groups: list[list[str]] = []
        for ref in sorted(normal_refs, key=rank):
            if ref in visited:
                continue
            queue = [ref]
            visited.add(ref)
            group: list[str] = []
            while queue:
                current = queue.pop(0)
                group.append(current)
                for neighbor in sorted(undirected[current], key=rank):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)
            groups.append(group)

        placements: dict[str, dict[str, float]] = {}
        group_y = start_y
        for group in groups:
            group_set = set(group)
            roots = [ref for ref in group if indegree.get(ref, 0) == 0]
            root = min(roots or group, key=rank)
            depths = {root: 0}
            queue = [root]
            while queue:
                current = queue.pop(0)
                for neighbor in sorted(
                    [r for r in directed_out.get(current, set()) if r in group_set],
                    key=rank,
                ):
                    candidate = depths[current] + 1
                    if neighbor not in depths or candidate < depths[neighbor]:
                        depths[neighbor] = candidate
                        queue.append(neighbor)

            queue = sorted(depths, key=lambda ref: (depths[ref], rank(ref)))
            seen_fill = set(queue)
            while queue:
                current = queue.pop(0)
                for neighbor in sorted(undirected[current], key=rank):
                    if neighbor not in group_set or neighbor in seen_fill:
                        continue
                    depths[neighbor] = depths[current] + 1
                    seen_fill.add(neighbor)
                    queue.append(neighbor)
            for ref in group:
                depths.setdefault(ref, 0)

            columns: dict[int, list[str]] = {}
            for ref, depth in depths.items():
                columns.setdefault(depth, []).append(ref)
            max_rows = 1
            for depth in sorted(columns):
                refs = sorted(columns[depth], key=rank)
                max_rows = max(max_rows, len(refs))
                for row, ref in enumerate(refs):
                    placements[ref] = {
                        "x": start_x + depth * column_spacing,
                        "y": group_y + row * row_spacing,
                    }
            group_y += max_rows * row_spacing + row_spacing

        # Net indexes for placing virtual symbols near the relevant normal endpoint.
        net_virtuals: dict[str, list[str]] = {}
        net_normals: dict[str, list[str]] = {}
        ref_nets: dict[str, list[str]] = {}
        for connection in data["connections"]:
            net_name = str(connection.get("net_name", "")).strip()
            if not net_name:
                continue
            for side in ("from", "to"):
                ref = str(connection[side]["reference"]).casefold()
                ref_nets.setdefault(ref, []).append(net_name)
                if ref in normal_set:
                    net_normals.setdefault(net_name, []).append(ref)
                elif ref in by_ref:
                    net_virtuals.setdefault(net_name, []).append(ref)

        def rect(cx: float, cy: float, width: float, height: float):
            return (
                cx - width / 2.0,
                cy - height / 2.0,
                cx + width / 2.0,
                cy + height / 2.0,
            )

        def overlap(left, right, margin: float = 0.0) -> bool:
            return not (
                left[2] + margin <= right[0]
                or right[2] + margin <= left[0]
                or left[3] + margin <= right[1]
                or right[3] + margin <= left[1]
            )

        occupied: list[tuple[float, float, float, float]] = []
        for ref in normal_refs:
            if ref not in placements:
                continue
            pos = placements[ref]
            component = by_ref[ref]
            lib_id = str(component.get("lib_id", "")).casefold()
            rotation = float(component.get("rotation", 0.0) or 0.0) % 360.0
            if lib_id in {"device:r", "device:c", "device:l"}:
                w, h = (10.0, 8.5) if rotation in {90.0, 270.0} else (8.5, 10.0)
            elif "led" in lib_id or "diode" in lib_id:
                w, h = (10.0, 9.0)
            else:
                w, h = (12.0, 10.0)
            occupied.append(rect(pos["x"], pos["y"], w, h))

        collision_rejections = 0

        def candidate_score(
            candidate: tuple[float, float],
            *,
            width: float,
            height: float,
            preferred: tuple[float, float],
            anchor: tuple[float, float],
        ) -> float:
            nonlocal collision_rejections
            box = rect(candidate[0], candidate[1], width, height)
            hits = sum(1 for other in occupied if overlap(box, other, 1.0))
            if hits:
                collision_rejections += hits
            collision_cost = hits * 10000.0
            preference_cost = (
                abs(candidate[0] - preferred[0])
                + abs(candidate[1] - preferred[1])
            ) * 4.0
            wire_cost = (
                abs(candidate[0] - anchor[0])
                + abs(candidate[1] - anchor[1])
            ) * 0.35
            return collision_cost + preference_cost + wire_cost

        def choose_candidate(
            candidates: list[tuple[float, float]],
            *,
            width: float,
            height: float,
            preferred: tuple[float, float],
            anchor: tuple[float, float],
        ) -> tuple[float, float]:
            chosen = min(
                candidates,
                key=lambda point: candidate_score(
                    point,
                    width=width,
                    height=height,
                    preferred=preferred,
                    anchor=anchor,
                ),
            )
            occupied.append(rect(chosen[0], chosen[1], width, height))
            return chosen

        # Place named power symbols first. Candidate selection includes the
        # visible power-value text in its collision box.
        for ref in sorted(by_ref):
            component = by_ref[ref]
            if component_kind(component) != "power":
                continue
            nets = ref_nets.get(ref, [])
            normal_candidates: list[str] = []
            for net_name in nets:
                normal_candidates.extend(net_normals.get(net_name, []))
            normal_candidates = [n for n in normal_candidates if n in placements]
            anchor_pos = placements[normal_candidates[0]] if normal_candidates else {
                "x": start_x, "y": group_y
            }
            anchor = (anchor_pos["x"], anchor_pos["y"])
            value = str(component.get("value", "")).strip().upper()
            downward = (
                value in {"GND", "GNDA", "GNDD", "VSS", "VEE"}
                or value.startswith("-")
                or value.endswith("_GND")
            )
            if downward:
                preferred = (
                    anchor[0] + power_offset * 1.20,
                    anchor[1],
                )
                candidates = [
                    preferred,
                    (anchor[0] + power_offset * 1.40, anchor[1]),
                    (anchor[0] + power_offset * 1.20, anchor[1] + power_offset * 0.20),
                    (anchor[0] + power_offset * 1.40, anchor[1] + power_offset * 0.20),
                ]
            else:
                preferred = (
                    anchor[0] - power_offset * 1.20,
                    anchor[1],
                )
                candidates = [
                    preferred,
                    (anchor[0] - power_offset * 1.40, anchor[1]),
                    (anchor[0] - power_offset * 1.20, anchor[1] - power_offset * 0.20),
                    (anchor[0] - power_offset * 1.40, anchor[1] - power_offset * 0.20),
                ]
            width = max(8.0, len(value) * 1.5 + 3.0)
            x, y = choose_candidate(
                candidates,
                width=width,
                height=8.0,
                preferred=preferred,
                anchor=anchor,
            )
            placements[ref] = {"x": x, "y": y}

        # Place PWR_FLAG away from the signal chain. Its collision rectangle
        # includes the visible "PWR_FLAG" value, which is the main source of
        # visual overlap in compact schematics.
        for ref in sorted(by_ref):
            component = by_ref[ref]
            if component_kind(component) != "power_flag":
                continue
            nets = ref_nets.get(ref, [])
            rail_ref = None
            for net_name in nets:
                for candidate in net_virtuals.get(net_name, []):
                    if candidate != ref and component_kind(by_ref[candidate]) == "power":
                        rail_ref = candidate
                        break
                if rail_ref:
                    break

            normal_candidates = []
            for net_name in nets:
                normal_candidates.extend(net_normals.get(net_name, []))
            normal_candidates = [n for n in normal_candidates if n in placements]
            anchor_pos = placements[normal_candidates[0]] if normal_candidates else {
                "x": start_x, "y": group_y
            }
            anchor = (anchor_pos["x"], anchor_pos["y"])

            rail_value = ""
            if rail_ref and rail_ref in by_ref:
                rail_value = str(by_ref[rail_ref].get("value", "")).strip().upper()
            downward = (
                rail_value in {"GND", "GNDA", "GNDD", "VSS", "VEE"}
                or rail_value.startswith("-")
                or rail_value.endswith("_GND")
            )
            if downward:
                preferred = (
                    anchor[0] + power_offset * 0.50,
                    anchor[1] + power_offset * 0.50,
                )
                candidates = [
                    preferred,
                    (anchor[0] + power_offset * 0.35, anchor[1] + power_offset * 0.50),
                    (anchor[0] + power_offset * 0.65, anchor[1] + power_offset * 0.50),
                    (anchor[0] + power_offset * 0.50, anchor[1] + power_offset * 0.65),
                ]
            else:
                preferred = (
                    anchor[0] - power_offset * 0.50,
                    anchor[1] - power_offset * 0.50,
                )
                candidates = [
                    preferred,
                    (anchor[0] - power_offset * 0.35, anchor[1] - power_offset * 0.50),
                    (anchor[0] - power_offset * 0.65, anchor[1] - power_offset * 0.50),
                    (anchor[0] - power_offset * 0.50, anchor[1] - power_offset * 0.65),
                ]

            x, y = choose_candidate(
                candidates,
                width=5.0,
                height=5.0,
                preferred=preferred,
                anchor=anchor,
            )
            placements[ref] = {"x": x, "y": y}

        for ref, position in placements.items():
            component = by_ref[ref]
            component["position"] = {
                "x": float(position["x"]),
                "y": float(position["y"]),
            }
            kind = component_kind(component)
            component.setdefault(
                "component_type",
                "power" if kind == "power" else
                "power_flag" if kind == "power_flag" else
                "component",
            )
            component.setdefault("multi_unit", False)
            component.setdefault("unit_spacing", 25.4)

        data["layout"] = {
            "strategy": "connectivity-v3.3",
            "start_x": start_x,
            "start_y": start_y,
            "column_spacing": column_spacing,
            "row_spacing": row_spacing,
            "power_offset": power_offset,
            "collision_rejections": collision_rejections,
            "applied_at": self._now(),
        }
        self._commit_mutation(path, data, "auto_layout")
        self.audit.record(
            "eda_sch_auto_layout",
            path=str(path),
            revision=data["revision"],
            components=len(placements),
            strategy="connectivity-v3.3",
            collision_rejections=collision_rejections,
        )
        return {
            "path": str(path),
            "revision": data["revision"],
            "strategy": "connectivity-v3.3",
            "placements": {
                by_ref[ref]["reference"]: position
                for ref, position in placements.items()
            },
            "collision_rejections": collision_rejections,
            "validation": self._validate_data(data),
        }


    def connection(



        self,



        ir_path: str,



        action: str,



        *,



        connection_id: str | None = None,



        from_reference: str | None = None,



        from_pin: str | None = None,



        to_reference: str | None = None,



        to_pin: str | None = None,



        net_name: str = "",



    ) -> dict:



        self._require_edit()



        path, data = self._load_ir(ir_path)



        action = str(action).strip().casefold()







        if action == "add":



            required = [from_reference, from_pin, to_reference, to_pin]



            if any(value is None or not str(value).strip() for value in required):



                raise ValueError("from_reference, from_pin, to_reference, and to_pin are required.")



            refs = {c["reference"].casefold() for c in data["components"]}



            if str(from_reference).casefold() not in refs:



                raise KeyError(f"Component not found: {from_reference}")



            if str(to_reference).casefold() not in refs:



                raise KeyError(f"Component not found: {to_reference}")



            connection = {



                "id": str(uuid.uuid4()),



                "from": {"reference": str(from_reference), "pin": str(from_pin)},



                "to": {"reference": str(to_reference), "pin": str(to_pin)},



                "net_name": str(net_name),



            }



            endpoint_key = self._connection_endpoint_key(connection)



            for existing in data["connections"]:



                if self._connection_endpoint_key(existing) == endpoint_key:



                    raise ValueError("An equivalent connection already exists.")



            data["connections"].append(connection)



            change = {"action": "add", "connection": deepcopy(connection)}



        elif action == "remove":



            connection_id = str(connection_id or "").strip()



            if not connection_id:



                raise ValueError("connection_id is required for remove.")



            before = len(data["connections"])



            data["connections"] = [c for c in data["connections"] if c["id"] != connection_id]



            if len(data["connections"]) == before:



                raise KeyError(f"Connection not found: {connection_id}")



            change = {"action": "remove", "connection_id": connection_id}



        else:



            raise ValueError("action must be add or remove.")







        self._commit_mutation(path, data, "connection")



        self.audit.record("eda_sch_connection", path=str(path), operation=action)



        return {"path": str(path), "revision": data["revision"], "change": change, "validation": self._validate_data(data)}







    def marker(



        self,



        ir_path: str,



        action: str,



        kind: str,



        *,



        marker_id: str | None = None,



        name: str | None = None,



        label_kind: str = "local",



        x: float | None = None,



        y: float | None = None,



        reference: str | None = None,



        pin: str | None = None,



    ) -> dict:



        self._require_edit()



        path, data = self._load_ir(ir_path)



        action = str(action).strip().casefold()



        kind = str(kind).strip().casefold()



        if kind not in {"label", "no_connect"}:



            raise ValueError("kind must be label or no_connect.")







        if action == "add":



            if kind == "label":



                label_kind = str(label_kind).strip().casefold()



                if label_kind not in _ALLOWED_LABEL_KINDS:



                    raise ValueError("label_kind must be local, global, or hierarchical.")



                if not str(name or "").strip() or x is None or y is None:



                    raise ValueError("name, x, and y are required for labels.")



                item = {



                    "id": str(uuid.uuid4()),



                    "kind": "label",



                    "label_kind": label_kind,



                    "name": str(name).strip(),



                    "position": {"x": self._finite(x, "x"), "y": self._finite(y, "y")},



                }



            else:



                if not str(reference or "").strip() or not str(pin or "").strip():



                    raise ValueError("reference and pin are required for no_connect.")



                refs = {c["reference"].casefold() for c in data["components"]}



                if str(reference).casefold() not in refs:



                    raise KeyError(f"Component not found: {reference}")



                item = {



                    "id": str(uuid.uuid4()),



                    "kind": "no_connect",



                    "reference": str(reference),



                    "pin": str(pin),



                }



            data["markers"].append(item)



            change = {"action": "add", "marker": deepcopy(item)}



        elif action == "remove":



            marker_id = str(marker_id or "").strip()



            if not marker_id:



                raise ValueError("marker_id is required for remove.")



            before = len(data["markers"])



            data["markers"] = [item for item in data["markers"] if item["id"] != marker_id]



            if len(data["markers"]) == before:



                raise KeyError(f"Marker not found: {marker_id}")



            change = {"action": "remove", "marker_id": marker_id}



        else:



            raise ValueError("action must be add or remove.")







        self._commit_mutation(path, data, "marker")



        self.audit.record("eda_sch_marker", path=str(path), operation=action, kind=kind)



        return {"path": str(path), "revision": data["revision"], "change": change, "validation": self._validate_data(data)}







    def validate(self, ir_path: str) -> dict:



        self._require_read()



        path, data = self._load_ir(ir_path)



        result = self._validate_data(data)



        self.audit.record("eda_sch_validate", path=str(path), ok=result["ok"], errors=len(result["errors"]))



        return {"path": str(path), "revision": data["revision"], **result}







    def rollback(self, ir_path: str, revision: int) -> dict:



        self._require_edit()



        path, current = self._load_ir(ir_path)



        revision = int(revision)



        if revision < 1:



            raise ValueError("revision must be at least 1.")



        history = path.parent / ".history"



        candidate = history / f"{path.stem}.rev{revision:06d}.json"



        candidate = self.guard.resolve_existing(candidate)



        restored = json.loads(candidate.read_text(encoding="utf-8"))



        self._validate_ir_shape(restored)



        self._snapshot(path, current)



        restored["revision"] = int(current["revision"]) + 1



        restored["updated_at"] = self._now()



        restored["status"] = "draft"



        restored["verification"] = None



        self._atomic_write(path, restored)



        self.audit.record("eda_sch_rollback", path=str(path), source_revision=revision, new_revision=restored["revision"])



        return {"path": str(path), "rolled_back_from": revision, "revision": restored["revision"], "validation": self._validate_data(restored)}







    def inspect_kicad(self, schematic_path: str) -> dict:



        self._require_read()



        path = self.guard.resolve_existing(schematic_path)



        if not path.is_file() or path.suffix.casefold() != ".kicad_sch":



            raise ValueError("schematic_path must be an existing .kicad_sch file.")



        text = path.read_text(encoding="utf-8-sig")



        root = self._parse_sexpr(text)



        if not root or root[0] != "kicad_sch":



            raise SExpressionError("Root token is not kicad_sch.")







        direct = [item for item in root[1:] if isinstance(item, list) and item]



        counts: dict[str, int] = {}



        for item in direct:



            token = str(item[0])



            counts[token] = counts.get(token, 0) + 1







        components = []



        for node in direct:



            if node[0] != "symbol":



                continue



            props = self._properties(node)



            components.append(



                {



                    "reference": props.get("Reference", ""),



                    "value": props.get("Value", ""),



                    "footprint": props.get("Footprint", ""),



                    "lib_id": self._child_atom(node, "lib_id") or "",



                    "uuid": self._child_atom(node, "uuid") or "",



                }



            )







        unresolved_refs = [c for c in components if not c["reference"] or c["reference"].endswith("?")]



        missing_footprints = [



            c["reference"] for c in components



            if c["reference"] and not c["reference"].startswith("#") and not c["footprint"]



        ]



        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()



        result = {



            "path": str(path),



            "sha256": sha256,



            "size_bytes": path.stat().st_size,



            "format": "kicad_sch",



            "version": self._child_atom(root, "version"),



            "generator": self._child_atom(root, "generator"),



            "counts": {



                "symbols": counts.get("symbol", 0),



                "wires": counts.get("wire", 0),



                "junctions": counts.get("junction", 0),



                "labels": counts.get("label", 0),



                "global_labels": counts.get("global_label", 0),



                "hierarchical_labels": counts.get("hierarchical_label", 0),



                "no_connects": counts.get("no_connect", 0),



                "sheets": counts.get("sheet", 0),



            },



            "components": components,



            "static_checks": {



                "unresolved_references": unresolved_refs,



                "missing_footprints": missing_footprints,



            },



        }



        self.audit.record("eda_sch_inspect", path=str(path), symbols=len(components))



        return result








    @staticmethod
    def _sexpr_atom(value) -> str:
        getter = getattr(value, "value", None)
        if callable(getter):
            try:
                return str(getter())
            except Exception:
                pass
        return str(value)

    def _symbol_unit_pin_map(self, lib_id: str) -> dict[int, set[str]]:
        """Read unit->pin mapping from the installed KiCad symbol definition."""
        from kicad_sch_api.library.cache import get_symbol_cache

        symbol_def = get_symbol_cache().get_symbol(str(lib_id))
        if symbol_def is None:
            raise RuntimeError(f"KiCad symbol not found: {lib_id}")

        mapping: dict[int, set[str]] = {}
        raw = getattr(symbol_def, "raw_kicad_data", None) or []
        for child in raw:
            if not isinstance(child, list) or len(child) < 2:
                continue
            if self._sexpr_atom(child[0]) != "symbol":
                continue
            child_name = self._sexpr_atom(child[1])
            match = re.search(r"_(\d+)_(\d+)$", child_name)
            if not match:
                continue
            unit = int(match.group(1))
            if unit <= 0:
                continue
            pins = mapping.setdefault(unit, set())
            for item in child[2:]:
                if not isinstance(item, list) or not item:
                    continue
                if self._sexpr_atom(item[0]) != "pin":
                    continue
                for field in item[1:]:
                    if (
                        isinstance(field, list)
                        and len(field) >= 2
                        and self._sexpr_atom(field[0]) == "number"
                    ):
                        pins.add(self._sexpr_atom(field[1]))
                        break

        if bool(getattr(symbol_def, "units", 1)) and int(getattr(symbol_def, "units", 1)) > 1:
            expected_units = int(symbol_def.units)
            missing = [
                unit for unit in range(1, expected_units + 1)
                if not mapping.get(unit)
            ]
            if missing:
                raise RuntimeError(
                    f"Could not resolve pin mapping for {lib_id} units: {missing}"
                )
        return mapping

    def _placed_pin_position(
        self,
        schematic,
        placed_components: dict[str, object],
        unit_pin_maps: dict[str, dict[int, set[str]]],
        reference: str,
        pin: str,
    ):
        """Resolve a pin on the correct unit instance.

        kicad-sch-api 0.5.6 has a coordinate quirk: Component.get_pin_position()
        can mirror Y for some symbols, while Schematic.get_component_pin_position()
        returns the correct KiCad orientation but anchors every multi-unit pin to
        the first unit instance. For multi-unit parts we therefore take the
        schematic-level point and translate it by the target-unit placement delta.
        """
        key = str(reference).casefold()
        placed = placed_components.get(key)
        if placed is None:
            raise RuntimeError(f"Placed component not found: {reference}")

        virtual_point = getattr(placed, "_actuator_virtual_point", None)
        if virtual_point is not None:
            return virtual_point

        base_point = schematic.get_component_pin_position(
            str(reference), str(pin)
        )
        if base_point is None:
            raise RuntimeError(f"Cannot locate pin: {reference}.{pin}")

        if not hasattr(placed, "get_all_units"):
            # kicad-sch-api 0.5.6 uses opposite quarter-turn direction in
            # Component.get_pin_position() vs Schematic.get_component_pin_position().
            # KiCad's saved 90/270-degree symbol transform matches the component-level
            # result, while 0/180 matches the schematic-level result.
            rotation = float(getattr(placed, "rotation", 0.0) or 0.0) % 360.0
            if abs(rotation - 90.0) < 1e-9 or abs(rotation - 270.0) < 1e-9:
                try:
                    rotated_point = placed.get_pin_position(str(pin))
                except Exception:
                    rotated_point = None
                if rotated_point is not None:
                    return rotated_point
            return base_point

        mapping = unit_pin_maps.get(key, {})
        candidates = sorted(
            unit
            for unit, pins in mapping.items()
            if str(pin) in pins
        )
        if len(candidates) != 1:
            raise RuntimeError(
                f"Pin {reference}.{pin} does not map uniquely to a multi-unit "
                f"instance; candidate units={candidates}."
            )

        base_unit = placed.get_unit(1)
        target_unit = placed.get_unit(candidates[0])
        if base_unit is None or target_unit is None:
            raise RuntimeError(
                f"Multi-unit component {reference} is missing unit placement "
                f"1 or {candidates[0]}."
            )

        dx = float(target_unit.position.x) - float(base_unit.position.x)
        dy = float(target_unit.position.y) - float(base_unit.position.y)

        point_type = type(base_point)
        try:
            return point_type(
                float(base_point.x) + dx,
                float(base_point.y) + dy,
            )
        except Exception:
            class _Point:
                pass
            point = _Point()
            point.x = float(base_point.x) + dx
            point.y = float(base_point.y) + dy
            return point

    @staticmethod
    def _point_xy(point) -> tuple[float, float]:
        return (float(point.x), float(point.y))

    @staticmethod
    def _snap_route(value: float, grid: float = 2.54) -> float:
        if grid <= 0:
            return float(value)
        return round(float(value) / grid) * grid

    @staticmethod
    def _same_point(
        left: tuple[float, float],
        right: tuple[float, float],
        *,
        eps: float = 1e-6,
    ) -> bool:
        return abs(left[0] - right[0]) <= eps and abs(left[1] - right[1]) <= eps

    def _emit_wire_segment(
        self,
        schematic,
        start: tuple[float, float],
        end: tuple[float, float],
        segments: set[tuple[tuple[float, float], tuple[float, float]]],
    ) -> bool:
        """Emit one unique orthogonal segment, ignoring zero-length duplicates."""
        if self._same_point(start, end):
            return False
        if abs(start[0] - end[0]) > 1e-6 and abs(start[1] - end[1]) > 1e-6:
            raise RuntimeError(f"Non-orthogonal route segment requested: {start} -> {end}")

        a = (round(float(start[0]), 6), round(float(start[1]), 6))
        b = (round(float(end[0]), 6), round(float(end[1]), 6))
        key = (a, b) if a <= b else (b, a)
        if key in segments:
            return False

        wire_id = schematic.add_wire(start=start, end=end)
        if not wire_id:
            raise RuntimeError(
                f"kicad-sch-api could not create orthogonal wire segment {start} -> {end}"
            )
        segments.add(key)
        return True

    def _add_orthogonal_wire(
        self,
        schematic,
        source_point,
        dest_point,
        *,
        grid: float = 2.54,
        segments: set[tuple[tuple[float, float], tuple[float, float]]] | None = None,
        forbidden_points: list[tuple[float, float]] | None = None,
    ) -> tuple[list[tuple[float, float]], tuple[float, float]]:
        """Add a compact Manhattan route with at most one bend when possible."""
        sx, sy = self._point_xy(source_point)
        dx, dy = self._point_xy(dest_point)
        shared = segments if segments is not None else set()

        if self._same_point((sx, sy), (dx, dy)):
            raise RuntimeError("Orthogonal route collapsed to a zero-length path.")

        eps = 1e-6
        if abs(sx - dx) <= eps or abs(sy - dy) <= eps:
            path = [(sx, sy), (dx, dy)]
        elif abs(dx - sx) >= abs(dy - sy):
            # Prefer left-to-right signal flow: travel horizontally first.
            bend = (dx, sy)
            path = [(sx, sy), bend, (dx, dy)]
        else:
            bend = (sx, dy)
            path = [(sx, sy), bend, (dx, dy)]

        if forbidden_points:
            def clear(candidate):
                for a,b in zip(candidate,candidate[1:]):
                    for x,y in forbidden_points:
                        if (min(a[0],b[0])-eps<=x<=max(a[0],b[0])+eps and
                            min(a[1],b[1])-eps<=y<=max(a[1],b[1])+eps):return False
                return True
            candidates=[path,[(sx,sy),(sx,dy),(dx,dy)]]
            for channel in [self._snap_route((sx+dx)/2,grid=grid/2),min(sx,dx)-2*grid,max(sx,dx)+2*grid]:
                candidates.append([(sx,sy),(channel,sy),(channel,dy),(dx,dy)])
            for channel in [self._snap_route((sy+dy)/2,grid=grid/2),min(sy,dy)-2*grid,max(sy,dy)+2*grid]:
                candidates.append([(sx,sy),(sx,channel),(dx,channel),(dx,dy)])
            path=next((candidate for candidate in candidates if clear(candidate)),None)
            if path is None:raise RuntimeError('No bounded route avoids unrelated symbol pins')

        compact = [path[0]]
        for point in path[1:]:
            if not self._same_point(point, compact[-1]):
                compact.append(point)

        for a, b in zip(compact, compact[1:]):
            self._emit_wire_segment(schematic, a, b, shared)

        longest = max(
            zip(compact, compact[1:]),
            key=lambda pair: abs(pair[1][0] - pair[0][0]) + abs(pair[1][1] - pair[0][1]),
        )
        label_point = (
            (longest[0][0] + longest[1][0]) / 2.0,
            (longest[0][1] + longest[1][1]) / 2.0,
        )
        return compact, label_point

    @staticmethod
    def _hide_saved_property(path: Path, property_name: str) -> int:
        """Inject KiCad-native (hide yes) into saved property blocks."""
        source = path.read_text(encoding="utf-8")
        needle = f'(property "{property_name}"'
        cursor = 0
        changed = 0
        output: list[str] = []

        while True:
            start = source.find(needle, cursor)
            if start < 0:
                output.append(source[cursor:])
                break

            output.append(source[cursor:start])
            depth = 0
            in_string = False
            escaped = False
            end = None
            for pos in range(start, len(source)):
                ch = source[pos]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end = pos + 1
                        break
            if end is None:
                raise RuntimeError(
                    f"Could not parse saved KiCad property block: {property_name}"
                )

            block = source[start:end]
            if "(hide yes)" not in block:
                effects_at = block.find("(effects")
                if effects_at >= 0:
                    line_start = block.rfind("\n", 0, effects_at) + 1
                    indent = block[line_start:effects_at]
                    block = (
                        block[:line_start]
                        + indent
                        + "(hide yes)\n"
                        + block[line_start:]
                    )
                    changed += 1
            output.append(block)
            cursor = end

        if changed:
            path.write_text("".join(output), encoding="utf-8")
        return changed

    @staticmethod
    def _layout_rect_overlap(
        left: tuple[float, float, float, float],
        right: tuple[float, float, float, float],
        margin: float = 0.0,
    ) -> bool:
        return not (
            left[2] + margin <= right[0]
            or right[2] + margin <= left[0]
            or left[3] + margin <= right[1]
            or right[3] + margin <= left[1]
        )

    @staticmethod
    def _layout_text_box(
        x: float,
        y: float,
        text: str,
        *,
        height: float = 1.8,
    ) -> tuple[float, float, float, float]:
        width = max(2.8, len(str(text)) * 1.35 + 0.8)
        return (
            x - width / 2.0,
            y - height / 2.0,
            x + width / 2.0,
            y + height / 2.0,
        )

    @staticmethod
    def _layout_symbol_box(component: dict) -> tuple[float, float, float, float]:
        x = float(component["position"]["x"])
        y = float(component["position"]["y"])
        lib_id = str(component.get("lib_id", "")).casefold()
        kind = str(component.get("component_type", "")).casefold()
        rotation = float(component.get("rotation", 0.0) or 0.0) % 360.0

        if kind == "power_flag" or lib_id == "power:pwr_flag":
            width, height = 3.5, 4.0
        elif kind == "power" or (lib_id.startswith("power:") and lib_id != "power:pwr_flag"):
            width, height = 5.0, 5.0
        elif lib_id in {"device:r", "device:c", "device:l"}:
            width, height = (8.5, 3.5) if rotation in {90.0, 270.0} else (3.5, 8.5)
        elif "led" in lib_id or "diode" in lib_id:
            width, height = (7.5, 5.5)
        else:
            width, height = (9.0, 7.0)
        return (
            x - width / 2.0,
            y - height / 2.0,
            x + width / 2.0,
            y + height / 2.0,
        )

    def _apply_component_property_layout(
        self,
        placed,
        component: dict,
        occupied: list[tuple[float, float, float, float]] | None = None,
        *,
        normal_mid_y: float | None = None,
    ) -> dict:
        """Place visible properties using bbox collision scoring.

        KiCad property angles are relative to symbol rotation. For quarter-turn
        symbols, using the same property angle cancels the symbol transform and
        renders Reference/Value horizontally in SVG/GUI.
        """
        occupied = occupied if occupied is not None else []
        kind = str(component.get("component_type", "")).casefold()
        lib_id = str(component.get("lib_id", "")).casefold()
        reference = str(component.get("reference", ""))
        value = str(component.get("value", ""))
        is_flag = kind == "power_flag" or lib_id == "power:pwr_flag"
        is_power = (
            kind == "power"
            or (
                lib_id.startswith("power:")
                and lib_id != "power:pwr_flag"
            )
            or reference.casefold().startswith("#pwr")
        )

        stats = {
            "reference": False,
            "value": False,
            "footprint_hidden": False,
            "collision_candidates_rejected": 0,
            "property_rotation": None,
        }

        x = float(placed.position.x)
        y = float(placed.position.y)
        rotation = float(getattr(placed, "rotation", component.get("rotation", 0.0)) or 0.0) % 360.0

        def score(boxes, preference_rank: int) -> float:
            hits = 0
            for box in boxes:
                for other in occupied:
                    if self._layout_rect_overlap(box, other, 0.7):
                        hits += 1
            stats["collision_candidates_rejected"] += hits
            return hits * 10000.0 + preference_rank * 10.0

        if is_flag:
            mid_y = normal_mid_y if normal_mid_y is not None else y
            outward = -1.0 if y <= mid_y else 1.0
            candidates = [
                (x, y + outward * 3.5),
                (x - 3.0, y + outward * 3.5),
                (x + 3.0, y + outward * 3.5),
                (x, y + outward * 5.5),
            ]
            ranked = []
            for index, point in enumerate(candidates):
                box = self._layout_text_box(point[0], point[1], value)
                ranked.append((score([box], index), point, box))
            _, chosen, box = min(ranked, key=lambda item: item[0])
            try:
                placed.set_property_effects(
                    "Value",
                    {
                        "position": chosen,
                        "rotation": 0.0,
                        "visible": True,
                    },
                )
                stats["value"] = True
                stats["property_rotation"] = 0.0
                occupied.append(box)
            except Exception:
                pass
            return stats

        if is_power:
            # Preserve KiCad's conventional +5V/GND text placement.
            return stats

        body = self._layout_symbol_box(component)
        body_half_h = (body[3] - body[1]) / 2.0
        text_gap = 1.8
        first_offset = body_half_h + text_gap
        stack_gap = 2.5

        # Quarter-turn symbols need an equal property angle to appear horizontal.
        property_rotation = rotation if rotation in {90.0, 270.0} else 0.0
        stats["property_rotation"] = property_rotation

        candidates = [
            (
                (x, y - first_offset),
                (x, y + first_offset),
            ),
            (
                (x, y - first_offset),
                (x, y - first_offset - stack_gap),
            ),
            (
                (x, y + first_offset),
                (x, y + first_offset + stack_gap),
            ),
            (
                (x - 2.5, y - first_offset),
                (x - 2.5, y + first_offset),
            ),
            (
                (x + 2.5, y - first_offset),
                (x + 2.5, y + first_offset),
            ),
        ]
        ranked = []
        for index, (ref_pos, value_pos) in enumerate(candidates):
            ref_box = self._layout_text_box(ref_pos[0], ref_pos[1], reference)
            value_box = self._layout_text_box(value_pos[0], value_pos[1], value)
            pair_score = score([ref_box, value_box], index)
            if self._layout_rect_overlap(ref_box, value_box, 0.5):
                pair_score += 10000.0
            ranked.append((pair_score, ref_pos, value_pos, ref_box, value_box))

        _, ref_pos, value_pos, ref_box, value_box = min(
            ranked, key=lambda item: item[0]
        )
        common = {
            "rotation": property_rotation,
            "visible": True,
        }
        try:
            placed.set_property_effects(
                "Reference",
                {**common, "position": ref_pos},
            )
            stats["reference"] = True
        except Exception:
            pass
        try:
            placed.set_property_effects(
                "Value",
                {**common, "position": value_pos},
            )
            stats["value"] = True
        except Exception:
            pass
        if stats["reference"]:
            occupied.append(ref_box)
        if stats["value"]:
            occupied.append(value_box)
        return stats


    @staticmethod
    def _component_is_power(component: dict | None) -> bool:
        if not component:
            return False
        kind = str(component.get("component_type", "")).casefold()
        lib_id = str(component.get("lib_id", "")).casefold()
        reference = str(component.get("reference", "")).casefold()
        return (
            kind == "power"
            or lib_id.startswith("power:")
            and kind != "power_flag"
            or reference.startswith("#pwr")
        )

    def _net_has_named_power_symbol(
        self,
        net_name: str,
        endpoints: list[dict],
        components_by_ref: dict[str, dict],
    ) -> bool:
        wanted = str(net_name).strip().casefold()
        if not wanted:
            return False
        for endpoint in endpoints:
            component = components_by_ref.get(str(endpoint["reference"]).casefold())
            if not self._component_is_power(component):
                continue
            value = str(component.get("value", "")).strip().casefold()
            lib_name = str(component.get("lib_id", "")).split(":", 1)[-1].strip().casefold()
            if wanted in {value, lib_name}:
                return True
        return False

    def _route_endpoint_tree(
        self,
        schematic,
        points: list[tuple[float, float]],
        *,
        grid: float = 2.54,
        segments: set[tuple[tuple[float, float], tuple[float, float]]] | None = None,
    ) -> tuple[tuple[float, float], list[tuple[float, float]]]:
        """Connect 3+ points with one Manhattan trunk and short orthogonal branches."""
        if len(points) < 3:
            raise ValueError("_route_endpoint_tree requires at least three points.")
        shared = segments if segments is not None else set()

        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        x_span = max(xs) - min(xs)
        y_span = max(ys) - min(ys)

        def median(values: list[float]) -> float:
            ordered = sorted(values)
            mid = len(ordered) // 2
            if len(ordered) % 2:
                return ordered[mid]
            return (ordered[mid - 1] + ordered[mid]) / 2.0

        graph_edges: list[tuple[tuple[float, float], tuple[float, float]]] = []

        if y_span > x_span:
            trunk_x = self._snap_route(median(xs), grid)
            taps = [(trunk_x, p[1]) for p in points]
            ordered_axis = sorted({round(t[1], 6) for t in taps})
            for a_y, b_y in zip(ordered_axis, ordered_axis[1:]):
                a, b = (trunk_x, a_y), (trunk_x, b_y)
                if self._emit_wire_segment(schematic, a, b, shared):
                    graph_edges.append((a, b))
            for point, tap in zip(points, taps):
                if self._emit_wire_segment(schematic, point, tap, shared):
                    graph_edges.append((point, tap))
            label_point = (
                trunk_x,
                (min(ordered_axis) + max(ordered_axis)) / 2.0,
            )
        else:
            trunk_y = self._snap_route(median(ys), grid)
            taps = [(p[0], trunk_y) for p in points]
            ordered_axis = sorted({round(t[0], 6) for t in taps})
            for a_x, b_x in zip(ordered_axis, ordered_axis[1:]):
                a, b = (a_x, trunk_y), (b_x, trunk_y)
                if self._emit_wire_segment(schematic, a, b, shared):
                    graph_edges.append((a, b))
            for point, tap in zip(points, taps):
                if self._emit_wire_segment(schematic, point, tap, shared):
                    graph_edges.append((point, tap))
            label_point = (
                (min(ordered_axis) + max(ordered_axis)) / 2.0,
                trunk_y,
            )

        # Junctions are explicit only at graph nodes where three or more wire
        # segments meet. This makes branch intent visible and avoids gratuitous dots.
        degree: dict[tuple[float, float], int] = {}
        for a, b in graph_edges:
            aa = (round(a[0], 6), round(a[1], 6))
            bb = (round(b[0], 6), round(b[1], 6))
            degree[aa] = degree.get(aa, 0) + 1
            degree[bb] = degree.get(bb, 0) + 1

        junctions: list[tuple[float, float]] = []
        for node, count in sorted(degree.items()):
            if count >= 3:
                schematic.junctions.add(position=node)
                junctions.append(node)

        return label_point, junctions

    def _route_connections_by_net(
        self,
        schematic,
        data: dict,
        placed_components: dict[str, object],
        unit_pin_maps: dict[str, dict[int, set[str]]],
    ) -> dict:
        """Route Circuit IR by electrical net instead of by individual edge."""
        components_by_ref = {
            str(component["reference"]).casefold(): component
            for component in data.get("components", [])
        }
        named_groups: dict[str, list[dict]] = {}
        unnamed: list[dict] = []
        for connection in data.get("connections", []):
            name = str(connection.get("net_name", "")).strip()
            if name:
                named_groups.setdefault(name, []).append(connection)
            else:
                unnamed.append(connection)

        emitted: set[tuple[tuple[float, float], tuple[float, float]]] = set()
        labels_added: list[str] = []
        junction_count = 0
        pin_points={}
        for reference,placed in placed_components.items():
            pin_numbers={str(pin.number) for pin in getattr(placed,'pins',[])}
            for pins in unit_pin_maps.get(reference,{}).values():pin_numbers.update(pins)
            for pin in pin_numbers:
                point=self._placed_pin_position(schematic,placed_components,unit_pin_maps,str(components_by_ref[reference]["reference"]),pin)
                pin_points[(reference.casefold(),pin)]=self._point_xy(point)

        def forbidden(allowed):
            return [point for key,point in pin_points.items() if key not in allowed]

        def endpoint(reference: str, pin: str) -> dict:
            point = self._placed_pin_position(
                schematic,
                placed_components,
                unit_pin_maps,
                reference,
                pin,
            )
            component = components_by_ref.get(str(reference).casefold())
            placed = placed_components.get(str(reference).casefold())
            return {
                "reference": reference,
                "pin": pin,
                "point": self._point_xy(point),
                "virtual_power": bool(
                    component is not None
                    and self._component_is_power(component)
                    and getattr(placed, "_actuator_virtual_point", None) is not None
                ),
            }

        # Unnamed edges retain pairwise semantics because there is no explicit
        # net identity in the IR that would justify merging separate edges.
        for connection in unnamed:
            source = connection["from"]
            dest = connection["to"]
            sp = self._placed_pin_position(
                schematic, placed_components, unit_pin_maps,
                str(source["reference"]), str(source["pin"]),
            )
            dp = self._placed_pin_position(
                schematic, placed_components, unit_pin_maps,
                str(dest["reference"]), str(dest["pin"]),
            )
            allowed={(str(source['reference']).casefold(),str(source['pin'])),(str(dest['reference']).casefold(),str(dest['pin']))}
            self._add_orthogonal_wire(schematic, sp, dp, segments=emitted,forbidden_points=forbidden(allowed))

        for net_name, connections in named_groups.items():
            endpoints_by_key: dict[tuple[str, str], dict] = {}
            for connection in connections:
                for side in ("from", "to"):
                    item = connection[side]
                    ref = str(item["reference"])
                    pin = str(item["pin"])
                    key = (ref.casefold(), pin)
                    endpoints_by_key.setdefault(key, endpoint(ref, pin))
            endpoints = list(endpoints_by_key.values())
            points = [item["point"] for item in endpoints]

            virtual_power_endpoints = [
                item for item in endpoints if item.get("virtual_power")
            ]
            flag_endpoints = [
                item for item in endpoints
                if str(
                    (components_by_ref.get(str(item["reference"]).casefold()) or {}).get(
                        "component_type", ""
                    )
                ).casefold() == "power_flag"
            ]
            normal_endpoints = [
                item for item in endpoints
                if item not in virtual_power_endpoints and item not in flag_endpoints
            ]

            # v3.3 power-net style: one horizontal main line from a textual
            # +5V/GND endpoint to the normal pin, plus one short vertical PWR_FLAG
            # branch at a real junction.
            if (
                len(virtual_power_endpoints) == 1
                and len(flag_endpoints) == 1
                and len(normal_endpoints) == 1
            ):
                power_ep = virtual_power_endpoints[0]
                flag_ep = flag_endpoints[0]
                normal_ep = normal_endpoints[0]
                normal_point = normal_ep["point"]
                power_component = components_by_ref[
                    str(power_ep["reference"]).casefold()
                ]
                label_point = (
                    self._snap_route(
                        float(power_component["position"]["x"]),
                        grid=1.27,
                    ),
                    float(normal_point[1]),
                )
                junction = (
                    float(flag_ep["point"][0]),
                    float(normal_point[1]),
                )

                self._emit_wire_segment(
                    schematic, label_point, normal_point, emitted
                )
                self._emit_wire_segment(
                    schematic, flag_ep["point"], junction, emitted
                )
                schematic.junctions.add(position=junction)
                junction_count += 1
                schematic.add_label(
                    net_name,
                    position=label_point,
                    rotation=180.0 if label_point[0] > normal_point[0] else 0.0,
                )
                labels_added.append(net_name)
                continue

            if len(points) < 2:
                continue
            if len(points) == 2:
                class _Point:
                    pass
                left = _Point()
                left.x, left.y = points[0]
                right = _Point()
                right.x, right.y = points[1]
                _path, label_point = self._add_orthogonal_wire(
                    schematic, left, right, segments=emitted,forbidden_points=forbidden(set(endpoints_by_key))
                )
            else:
                label_point, junctions = self._route_endpoint_tree(
                    schematic, points, segments=emitted
                )
                junction_count += len(junctions)

            # Power symbols already name +5V/GND/etc. Exact two-pin internal
            # nets do not need a visible label: connectivity is unambiguous and
            # semantic verification accepts KiCad's generated local net name.
            endpoint_components = [
                components_by_ref.get(str(item["reference"]).casefold())
                for item in endpoints
            ]
            exact_two_pin_internal = (
                len(endpoints) == 2
                and all(
                    component is not None
                    and str(component.get("component_type", "component")).casefold()
                    not in {"power", "power_flag"}
                    and not str(component.get("lib_id", "")).casefold().startswith("power:")
                    for component in endpoint_components
                )
            )
            if (
                not exact_two_pin_internal
                and not self._net_has_named_power_symbol(
                    net_name, endpoints, components_by_ref
                )
            ):
                schematic.add_label(net_name, position=label_point)
                labels_added.append(net_name)

        return {
            "named_nets": len(named_groups),
            "unnamed_connections": len(unnamed),
            "segments": len(emitted),
            "junctions": junction_count,
            "labels_added": labels_added,
            "labels_suppressed": max(0, len(named_groups) - len(labels_added)),
        }

    def _install_multi_unit_symbol_normalizer(
        self,
        schematic,
        data: dict,
    ) -> None:
        """Normalize embedded multi-unit symbols to KiCad's native representation.

        kicad-sch-api 0.5.6 resolves inherited symbols, but for KiCad 10 a
        multi-unit derived symbol can differ from the library copy in two small
        metadata details: missing ki_locked and duplicated embedded_fonts.
        Normalizing only IR components explicitly marked multi_unit keeps basic
        symbols untouched.
        """
        import types
        import sexpdata

        multi_lib_ids = {
            str(component["lib_id"])
            for component in data.get("components", [])
            if bool(component.get("multi_unit", False))
        }
        if not multi_lib_ids:
            return

        original = schematic._convert_symbol_to_kicad_format

        def atom(value) -> str:
            getter = getattr(value, "value", None)
            if callable(getter):
                try:
                    return str(getter())
                except Exception:
                    pass
            return str(value)

        def normalize_symbol(converted, lib_id: str):
            if lib_id not in multi_lib_ids or not isinstance(converted, list):
                return converted

            # Keep exactly one top-level embedded_fonts entry.
            seen_embedded_fonts = False
            filtered = []
            for child in converted:
                if (
                    isinstance(child, list)
                    and child
                    and atom(child[0]) == "embedded_fonts"
                ):
                    if seen_embedded_fonts:
                        continue
                    seen_embedded_fonts = True
                filtered.append(child)
            converted = filtered

            # KiCad's native embedded copy of derived multi-unit symbols carries
            # the legacy empty ki_locked property after inheritance is flattened.
            has_ki_locked = any(
                isinstance(child, list)
                and len(child) >= 2
                and atom(child[0]) == "property"
                and str(child[1]) == "ki_locked"
                for child in converted
            )
            if not has_ki_locked:
                locked_property = [
                    sexpdata.Symbol("property"),
                    "ki_locked",
                    "",
                    [sexpdata.Symbol("at"), 0, 0, 0],
                    [
                        sexpdata.Symbol("show_name"),
                        sexpdata.Symbol("no"),
                    ],
                    [
                        sexpdata.Symbol("do_not_autoplace"),
                        sexpdata.Symbol("no"),
                    ],
                    [
                        sexpdata.Symbol("effects"),
                        [
                            sexpdata.Symbol("font"),
                            [
                                sexpdata.Symbol("size"),
                                1.27,
                                1.27,
                            ],
                        ],
                    ],
                ]
                insert_at = len(converted)
                for index, child in enumerate(converted):
                    if (
                        isinstance(child, list)
                        and len(child) >= 2
                        and atom(child[0]) == "property"
                        and str(child[1]) == "ki_keywords"
                    ):
                        insert_at = index
                        break
                converted.insert(insert_at, locked_property)

            return converted

        def wrapped(instance, symbol_def, lib_id: str):
            converted = original(symbol_def, lib_id)
            return normalize_symbol(converted, lib_id)

        schematic._convert_symbol_to_kicad_format = types.MethodType(
            wrapped,
            schematic,
        )

    def materialize_kicad(
        self,
        ir_path: str,
        schematic_path: str,
        kicad_cli: str,
        *,
        overwrite: bool = False,
        force: bool = False,
    ) -> dict:
        """Generate a KiCad schematic from Circuit IR and validate it before commit.

        This never drives the KiCad GUI. A staged .kicad_sch is generated through
        kicad-sch-api, round-tripped, exported to a KiCad netlist, and compared
        semantically with the IR before the destination is replaced.
        """
        self._require_edit()
        ir_file, data = self._load_ir(ir_path)
        validation = self._validate_data(data)
        if not validation["ok"]:
            raise ValueError(
                "Circuit IR contains validation errors and cannot be materialized: "
                + ", ".join(item["code"] for item in validation["errors"])
            )

        cli = Path(kicad_cli).expanduser().resolve(strict=True)
        if not cli.is_file():
            raise FileNotFoundError(str(cli))
        install_root = cli.parent.parent
        symbol_dir = install_root / "share" / "kicad" / "symbols"
        footprint_dir = install_root / "share" / "kicad" / "footprints"
        if not symbol_dir.is_dir():
            raise FileNotFoundError(f"KiCad symbol library directory not found: {symbol_dir}")
        if not footprint_dir.is_dir():
            raise FileNotFoundError(f"KiCad footprint library directory not found: {footprint_dir}")

        target = self.guard.resolve_target(schematic_path)
        if target.suffix.casefold() != ".kicad_sch":
            raise ValueError("schematic_path must end with .kicad_sch")
        target_parent = self.guard.resolve_target(target.parent)
        target_parent.mkdir(parents=True, exist_ok=True)
        target_parent = self.guard.resolve_existing(target_parent)

        previous = None
        if target.exists():
            existing = self.guard.resolve_existing(target)
            if not existing.is_file():
                raise IsADirectoryError(str(existing))
            current_hash = hashlib.sha256(existing.read_bytes()).hexdigest()
            previous = {"path": str(existing), "sha256": current_hash}
            recorded = data.get("materialized_schematic")
            recorded_path = str((recorded or {}).get("path", ""))
            recorded_hash = str((recorded or {}).get("sha256", ""))
            same_recorded_path = False
            if recorded_path:
                try:
                    same_recorded_path = (
                        Path(recorded_path).expanduser().resolve(strict=False)
                        == existing.resolve(strict=False)
                    )
                except OSError:
                    same_recorded_path = False
            if same_recorded_path and recorded_hash and recorded_hash != current_hash and not force:
                raise PermissionError(
                    "The materialized schematic changed outside Actuator. "
                    "Refusing to overwrite external edits without force=true."
                )
            if not same_recorded_path and not overwrite and not force:
                raise FileExistsError(
                    f"Schematic already exists: {existing}. "
                    "Set overwrite=true only when replacement is intended."
                )

        symbol_libs = sorted(
            {
                str(component["lib_id"]).split(":", 1)[0]
                for component in data["components"]
                if ":" in str(component.get("lib_id", ""))
            }
        )
        footprint_libs = sorted(
            {
                str(component["footprint"]).split(":", 1)[0]
                for component in data["components"]
                if ":" in str(component.get("footprint", ""))
            }
        )
        missing_symbols = [
            name for name in symbol_libs
            if not (symbol_dir / f"{name}.kicad_sym").is_file()
        ]
        missing_footprints = [
            name for name in footprint_libs
            if not (footprint_dir / f"{name}.pretty").is_dir()
        ]
        if missing_symbols or missing_footprints:
            raise FileNotFoundError(
                "Required KiCad libraries are missing. "
                f"symbols={missing_symbols}, footprints={missing_footprints}"
            )

        stage_root = self.guard.resolve_target(
            target_parent / ".actuator" / "materialize-staging" / uuid.uuid4().hex
        )
        stage_root.mkdir(parents=True, exist_ok=False)
        stage_root = self.guard.resolve_existing(stage_root)
        staged_schematic = stage_root / target.name
        stage_netlist = stage_root / f"{target.stem}.net"

        try:
            self._write_project_tables(
                stage_root,
                symbol_libs,
                footprint_libs,
                symbol_dir,
                footprint_dir,
                fail_if_existing_missing=False,
            )
            (stage_root / f"{target.stem}.kicad_pro").write_text(
                "{}\n", encoding="utf-8"
            )

            old_symbol_dir = os.environ.get("KICAD_SYMBOL_DIR")
            os.environ["KICAD_SYMBOL_DIR"] = str(symbol_dir)
            try:
                import kicad_sch_api as ksa

                schematic = ksa.create_schematic(
                    str(data.get("title") or data.get("name") or target.stem)
                )
                self._install_multi_unit_symbol_normalizer(schematic, data)
                placed_components: dict[str, object] = {}
                unit_pin_maps: dict[str, dict[int, set[str]]] = {}
                virtualized_power_refs: set[str] = set()
                render_power_labels = str(
                    (data.get("layout") or {}).get("strategy", "")
                ).casefold().startswith("connectivity-v3.3")

                for component in data["components"]:
                    ref_key = str(component["reference"]).casefold()
                    if render_power_labels and self._component_is_power(component):
                        point = SimpleNamespace(
                            x=float(component["position"]["x"]),
                            y=float(component["position"]["y"]),
                        )
                        placed_components[ref_key] = SimpleNamespace(
                            _actuator_virtual_point=point,
                            rotation=0.0,
                        )
                        virtualized_power_refs.add(ref_key)
                        continue

                    placed = schematic.components.add(
                        lib_id=str(component["lib_id"]),
                        reference=str(component["reference"]),
                        value=str(component.get("value", "")),
                        position=(
                            float(component["position"]["x"]),
                            float(component["position"]["y"]),
                        ),
                        footprint=str(component.get("footprint", "")) or None,
                        rotation=float(component.get("rotation", 0.0)),
                        add_all_units=bool(component.get("multi_unit", False)),
                        unit_spacing=float(component.get("unit_spacing", 25.4)),
                    )
                    placed_components[ref_key] = placed
                    if bool(component.get("multi_unit", False)):
                        unit_pin_maps[ref_key] = self._symbol_unit_pin_map(
                            str(component["lib_id"])
                        )

                normal_y_values = [
                    float(component["position"]["y"])
                    for component in data["components"]
                    if str(component.get("component_type", "component")).casefold()
                    not in {"power", "power_flag"}
                    and not str(component.get("lib_id", "")).casefold().startswith("power:")
                ]
                normal_mid_y = (
                    sorted(normal_y_values)[len(normal_y_values) // 2]
                    if normal_y_values else None
                )
                property_occupied = [
                    self._layout_symbol_box(component)
                    for component in data["components"]
                    if str(component["reference"]).casefold()
                    not in virtualized_power_refs
                ]
                property_layout_stats = []
                for component in data["components"]:
                    ref_key = str(component["reference"]).casefold()
                    placed = placed_components[ref_key]
                    if ref_key in virtualized_power_refs:
                        property_layout_stats.append(
                            {
                                "component_reference": str(component["reference"]),
                                "rendered_as_label": True,
                                "reference": False,
                                "value": False,
                                "footprint_hidden": False,
                                "collision_candidates_rejected": 0,
                                "property_rotation": 0.0,
                            }
                        )
                        continue
                    property_layout_stats.append(
                        {
                            "reference": str(component["reference"]),
                            **self._apply_component_property_layout(
                                placed,
                                component,
                                property_occupied,
                                normal_mid_y=normal_mid_y,
                            ),
                        }
                    )

                routing = self._route_connections_by_net(
                    schematic,
                    data,
                    placed_components,
                    unit_pin_maps,
                )

                for marker in data["markers"]:
                    kind = str(marker.get("kind", "")).casefold()
                    if kind == "label":
                        position = marker["position"]
                        label_kind = str(marker.get("label_kind", "local")).casefold()
                        pos = (float(position["x"]), float(position["y"]))
                        name = str(marker["name"])
                        if label_kind == "local":
                            schematic.add_label(name, position=pos)
                        elif label_kind == "global":
                            schematic.add_global_label(name, position=pos)
                        elif label_kind == "hierarchical":
                            schematic.add_hierarchical_label(name, position=pos)
                        else:
                            raise ValueError(f"Unsupported label_kind: {label_kind}")
                    elif kind == "no_connect":
                        point = self._placed_pin_position(
                            schematic,
                            placed_components,
                            unit_pin_maps,
                            str(marker["reference"]),
                            str(marker["pin"]),
                        )
                        schematic.no_connects.add(
                            (float(point.x), float(point.y))
                        )

                schematic.save(str(staged_schematic))
                hidden_footprints = self._hide_saved_property(
                    staged_schematic, "Footprint"
                )
                reloaded = ksa.load_schematic(str(staged_schematic))
                reloaded_refs = {
                    str(component.reference)
                    for component in reloaded.components
                }
                expected_refs = {
                    str(component["reference"])
                    for component in data["components"]
                    if str(component["reference"]).casefold()
                    not in virtualized_power_refs
                }
                if reloaded_refs != expected_refs:
                    raise RuntimeError(
                        "Round-trip component mismatch after materialization. "
                        f"expected={sorted(expected_refs)}, actual={sorted(reloaded_refs)}"
                    )
            finally:
                if old_symbol_dir is None:
                    os.environ.pop("KICAD_SYMBOL_DIR", None)
                else:
                    os.environ["KICAD_SYMBOL_DIR"] = old_symbol_dir

            format_upgrade = self._run_cli(
                cli,
                [
                    "sch",
                    "upgrade",
                    "--force",
                    str(staged_schematic),
                ],
                cwd=stage_root,
            )
            if format_upgrade["exit_code"] != 0:
                raise RuntimeError(
                    "KiCad could not upgrade the staged schematic to the latest "
                    "native file format. "
                    + format_upgrade["stderr"]
                )

            netlist_check = self._run_cli(
                cli,
                [
                    "sch", "export", "netlist",
                    "--format", "kicadsexpr",
                    "--output", str(stage_netlist),
                    str(staged_schematic),
                ],
                cwd=stage_root,
            )
            if netlist_check["exit_code"] != 0 or not stage_netlist.is_file():
                raise RuntimeError(
                    "KiCad rejected the staged schematic during netlist export. "
                    + netlist_check["stderr"]
                )
            semantic = self._compare_ir_to_netlist_data(
                data,
                stage_netlist.read_text(encoding="utf-8-sig"),
            )
            if not semantic["ok"]:
                raise RuntimeError(
                    "Staged KiCad netlist does not match Circuit IR: "
                    + "; ".join(item["message"] for item in semantic["errors"])
                )

            self._write_project_tables(
                target_parent,
                symbol_libs,
                footprint_libs,
                symbol_dir,
                footprint_dir,
                fail_if_existing_missing=True,
            )
            project_file = target_parent / f"{target.stem}.kicad_pro"
            existing_projects = list(target_parent.glob("*.kicad_pro"))
            if not project_file.exists() and not existing_projects:
                project_file.write_text("{}\n", encoding="utf-8")

            backup_path = None
            if target.exists():
                backup_dir = self.guard.resolve_target(
                    target_parent / ".actuator" / "schematic-backup"
                )
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_dir = self.guard.resolve_existing(backup_dir)
                current = self.guard.resolve_existing(target)
                current_hash = hashlib.sha256(current.read_bytes()).hexdigest()
                backup_path = self.guard.resolve_target(
                    backup_dir
                    / f"{target.stem}.{current_hash[:12]}.kicad_sch"
                )
                if not backup_path.exists():
                    shutil.copy2(current, backup_path)
                    backup_path = self.guard.resolve_existing(backup_path)

            pending = self.guard.resolve_target(
                target_parent / f".{target.name}.pending.{os.getpid()}.{uuid.uuid4().hex}"
            )
            shutil.copy2(staged_schematic, pending)
            os.replace(pending, target)
            committed = self.guard.resolve_existing(target)
            sha256 = hashlib.sha256(committed.read_bytes()).hexdigest()

            data["materialized_schematic"] = {
                "path": str(committed),
                "sha256": sha256,
                "ir_revision": int(data["revision"]),
                "materialized_at": self._now(),
                "generator": "kicad-sch-api",
                "semantic_check": semantic,
            }
            data["updated_at"] = self._now()
            self._atomic_write(ir_file, data)
            self.audit.record(
                "eda_sch_materialize",
                ir_path=str(ir_file),
                schematic_path=str(committed),
                revision=data["revision"],
                sha256=sha256,
                semantic_ok=semantic["ok"],
            )
            return {
                "path": str(committed),
                "ir_path": str(ir_file),
                "revision": data["revision"],
                "sha256": sha256,
                "backup_path": str(backup_path) if backup_path else None,
                "semantic_check": semantic,
                "libraries": {
                    "symbols": symbol_libs,
                    "footprints": footprint_libs,
                },
                "format_upgrade": format_upgrade,
                "staged_netlist_export": netlist_check,
                "routing": routing,
                "text_layout": {
                    "footprint_properties_hidden": hidden_footprints,
                    "components": property_layout_stats,
                    "collision_candidates_rejected": sum(
                        int(item.get("collision_candidates_rejected", 0))
                        for item in property_layout_stats
                    ),
                },
                "previous": previous,
            }
        finally:
            try:
                shutil.rmtree(stage_root)
            except OSError:
                pass

    def compare_ir_netlist(self, ir_path: str, netlist_path: str) -> dict:
        self._require_read()
        _, data = self._load_ir(ir_path)
        netlist = self.guard.resolve_existing(netlist_path)
        if not netlist.is_file():
            raise IsADirectoryError(str(netlist))
        result = self._compare_ir_to_netlist_data(
            data,
            netlist.read_text(encoding="utf-8-sig"),
        )
        self.audit.record(
            "eda_sch_compare_netlist",
            ir_path=str(ir_path),
            netlist_path=str(netlist),
            ok=result["ok"],
        )
        return {"netlist_path": str(netlist), **result}

    def freeze(
        self,
        ir_path: str,
        schematic_path: str,
        verification: dict,
        semantic: dict,
    ) -> dict:
        self._require_edit()
        ir_file, data = self._load_ir(ir_path)
        validation = self._validate_data(data)
        blockers = []
        blockers.extend(item["code"] for item in validation["errors"])
        blockers.extend(item["code"] for item in validation["warnings"])
        if not verification.get("ok", False):
            blockers.append("KICAD_VERIFY_FAILED")
        if not semantic.get("ok", False):
            blockers.append("NETLIST_SEMANTIC_MISMATCH")

        schematic = self.guard.resolve_existing(schematic_path)
        if not schematic.is_file() or schematic.suffix.casefold() != ".kicad_sch":
            blockers.append("SCHEMATIC_NOT_FOUND")
            actual_hash = None
        else:
            actual_hash = hashlib.sha256(schematic.read_bytes()).hexdigest()
            recorded = data.get("materialized_schematic") or {}
            if str(recorded.get("sha256", "")) != actual_hash:
                blockers.append("MATERIALIZED_HASH_MISMATCH")
            if int(recorded.get("ir_revision", -1)) != int(data["revision"]):
                blockers.append("MATERIALIZED_REVISION_STALE")

        if blockers:
            raise ValueError(
                "Schematic cannot be frozen: " + ", ".join(sorted(set(blockers)))
            )

        data["status"] = "frozen"
        data["verification"] = {
            "verified_at": self._now(),
            "schematic_path": str(schematic),
            "sha256": actual_hash,
            "erc": verification.get("erc"),
            "netlist": verification.get("netlist"),
            "semantic": semantic,
        }
        data["updated_at"] = self._now()
        self._atomic_write(ir_file, data)
        self.audit.record(
            "eda_sch_freeze",
            ir_path=str(ir_file),
            schematic_path=str(schematic),
            revision=data["revision"],
        )
        return {
            "path": str(ir_file),
            "status": "frozen",
            "revision": data["revision"],
            "schematic_path": str(schematic),
            "sha256": actual_hash,
        }

    def _write_project_tables(
        self,
        directory: Path,
        symbol_libs: list[str],
        footprint_libs: list[str],
        symbol_dir: Path,
        footprint_dir: Path,
        *,
        fail_if_existing_missing: bool,
    ) -> dict:
        directory = self.guard.resolve_existing(directory)
        sym_table = directory / "sym-lib-table"
        fp_table = directory / "fp-lib-table"

        required_sym_entries = [
            (
                name,
                symbol_dir / f"{name}.kicad_sym",
            )
            for name in symbol_libs
        ]
        required_fp_entries = [
            (
                name,
                footprint_dir / f"{name}.pretty",
            )
            for name in footprint_libs
        ]

        def ensure_table(
            path: Path,
            root_token: str,
            entries: list[tuple[str, Path]],
        ) -> bool:
            if path.exists():
                content = path.read_text(encoding="utf-8-sig")
                missing = [
                    name
                    for name, _ in entries
                    if f'(name "{name}")' not in content
                ]
                if missing and fail_if_existing_missing:
                    raise PermissionError(
                        f"Existing {path.name} is missing required libraries: {missing}. "
                        "Actuator will not rewrite an existing user library table."
                    )
                if not missing:
                    return False
            lines = [f"({root_token}", "  (version 7)"]
            for name, location in entries:
                uri = location.as_posix()
                lines.append(
                    f'  (lib (name "{name}") (type "KiCad") '
                    f'(uri "{uri}") (options "") (descr ""))'
                )
            lines.append(")")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True

        return {
            "sym_created": ensure_table(sym_table, "sym_lib_table", required_sym_entries),
            "fp_created": ensure_table(fp_table, "fp_lib_table", required_fp_entries),
        }

    def _run_cli(
        self,
        executable: Path,
        args: list[str],
        *,
        cwd: Path,
    ) -> dict:
        timeout = min(
            max(1.0, float(self.config.default_timeout_seconds)),
            float(self.config.max_timeout_seconds),
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [str(executable), *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
            creationflags=creationflags,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        limit = max(1024, int(self.config.max_output_bytes))
        if len(stdout.encode("utf-8", errors="replace")) > limit:
            stdout = stdout.encode("utf-8", errors="replace")[:limit].decode(
                "utf-8", errors="replace"
            ) + "\n...[truncated]"
        if len(stderr.encode("utf-8", errors="replace")) > limit:
            stderr = stderr.encode("utf-8", errors="replace")[:limit].decode(
                "utf-8", errors="replace"
            ) + "\n...[truncated]"
        return {
            "exit_code": completed.returncode,
            "ok": completed.returncode == 0,
            "stdout": stdout,
            "stderr": stderr,
        }

    def _compare_ir_to_netlist_data(self, data: dict, netlist_text: str) -> dict:
        root = self._parse_sexpr(netlist_text)
        if not root or root[0] != "export":
            raise SExpressionError("Netlist root token is not export.")

        components_node = next(
            (
                child
                for child in root[1:]
                if isinstance(child, list) and child and child[0] == "components"
            ),
            None,
        )
        nets_node = next(
            (
                child
                for child in root[1:]
                if isinstance(child, list) and child and child[0] == "nets"
            ),
            None,
        )
        if components_node is None or nets_node is None:
            raise SExpressionError("Netlist is missing components or nets section.")

        actual_components = {}
        for comp in components_node[1:]:
            if not isinstance(comp, list) or not comp or comp[0] != "comp":
                continue
            ref = self._child_atom(comp, "ref") or ""
            actual_components[ref.casefold()] = {
                "reference": ref,
                "value": self._child_atom(comp, "value") or "",
                "footprint": self._child_atom(comp, "footprint") or "",
            }

        actual_nets = []
        endpoint_map = {}
        for net in nets_node[1:]:
            if not isinstance(net, list) or not net or net[0] != "net":
                continue
            name = self._child_atom(net, "name") or ""
            nodes = set()
            node_types = {}
            for node in net[1:]:
                if not isinstance(node, list) or not node or node[0] != "node":
                    continue
                ref = self._child_atom(node, "ref") or ""
                pin = self._child_atom(node, "pin") or ""
                endpoint = (ref.casefold(), pin)
                nodes.add(endpoint)
                node_types[endpoint] = self._child_atom(node, "pintype") or ""
            index = len(actual_nets)
            actual_nets.append(
                {"name": name, "nodes": nodes, "node_types": node_types}
            )
            for endpoint in nodes:
                endpoint_map[endpoint] = index

        ir_components = {
            str(component["reference"]).casefold(): component
            for component in data["components"]
        }

        def virtual_kind(component: dict | None) -> str | None:
            if not component:
                return None
            explicit = str(component.get("component_type", "")).casefold()
            if explicit in {"power", "power_flag"}:
                return explicit
            lib_id = str(component.get("lib_id", "")).casefold()
            if lib_id == "power:pwr_flag":
                return "power_flag"
            if lib_id.startswith("power:") or str(
                component.get("reference", "")
            ).casefold().startswith("#pwr"):
                return "power"
            return None

        errors = []
        for component in data["components"]:
            if virtual_kind(component):
                # KiCad virtual power symbols and PWR_FLAG are not emitted as
                # normal BOM/netlist components.
                continue
            ref = str(component["reference"])
            actual = actual_components.get(ref.casefold())
            if actual is None:
                errors.append(
                    {
                        "code": "COMPONENT_MISSING",
                        "message": f"Component {ref} is missing from KiCad netlist.",
                    }
                )
                continue
            if str(component.get("value", "")) != actual["value"]:
                errors.append(
                    {
                        "code": "VALUE_MISMATCH",
                        "message": (
                            f"{ref} value differs: "
                            f"IR={component.get('value','')!r}, netlist={actual['value']!r}."
                        ),
                    }
                )
            if str(component.get("footprint", "")) != actual["footprint"]:
                errors.append(
                    {
                        "code": "FOOTPRINT_MISMATCH",
                        "message": (
                            f"{ref} footprint differs: "
                            f"IR={component.get('footprint','')!r}, "
                            f"netlist={actual['footprint']!r}."
                        ),
                    }
                )

        for connection in data["connections"]:
            source = (
                str(connection["from"]["reference"]).casefold(),
                str(connection["from"]["pin"]),
            )
            dest = (
                str(connection["to"]["reference"]).casefold(),
                str(connection["to"]["pin"]),
            )
            source_component = ir_components.get(source[0])
            dest_component = ir_components.get(dest[0])
            source_kind = virtual_kind(source_component)
            dest_kind = virtual_kind(dest_component)

            # Connections between two virtual power symbols cannot be recovered
            # from the exported netlist because neither endpoint is emitted as a
            # node. KiCad ERC remains authoritative for that geometry.
            if source_kind and dest_kind:
                continue

            if source_kind or dest_kind:
                virtual_component = source_component if source_kind else dest_component
                virtual_type = source_kind or dest_kind
                normal_endpoint = dest if source_kind else source
                normal_net = endpoint_map.get(normal_endpoint)
                if normal_net is None:
                    errors.append(
                        {
                            "code": "POWER_CONNECTION_MISMATCH",
                            "message": (
                                f"Connected endpoint is missing from KiCad netlist: "
                                f"{normal_endpoint[0]}.{normal_endpoint[1]}."
                            ),
                        }
                    )
                    continue

                actual_name = str(actual_nets[normal_net]["name"]).strip().lstrip("/")
                explicit_name = str(connection.get("net_name", "")).strip().lstrip("/")

                if virtual_type == "power":
                    expected_power_name = str(
                        virtual_component.get("value", "")
                    ).strip().lstrip("/")
                    if actual_name != expected_power_name:
                        errors.append(
                            {
                                "code": "POWER_NET_NAME_MISMATCH",
                                "message": (
                                    f"Power connection {connection.get('id')} differs: "
                                    f"IR={expected_power_name!r}, netlist={actual_name!r}."
                                ),
                            }
                        )
                        continue

                # PWR_FLAG drives whichever named rail it is wired to; its own
                # Value is not the rail name.
                if explicit_name and explicit_name != actual_name:
                    errors.append(
                        {
                            "code": "NET_NAME_MISMATCH",
                            "message": (
                                f"Explicit net name differs for virtual power connection "
                                f"{connection.get('id')}: IR={explicit_name!r}, "
                                f"netlist={actual_name!r}."
                            ),
                        }
                    )
                continue

            source_net = endpoint_map.get(source)
            dest_net = endpoint_map.get(dest)
            if source_net is None or dest_net is None or source_net != dest_net:
                errors.append(
                    {
                        "code": "CONNECTION_MISMATCH",
                        "message": (
                            f"Connection {connection.get('id')} is not represented "
                            f"by one KiCad net: "
                            f"{connection['from']['reference']}.{connection['from']['pin']} -> "
                            f"{connection['to']['reference']}.{connection['to']['pin']}."
                        ),
                    }
                )
                continue

            expected_name = str(connection.get("net_name", "")).strip().lstrip("/")
            if expected_name:
                actual_name = str(actual_nets[source_net]["name"]).strip().lstrip("/")
                exact_pair = actual_nets[source_net]["nodes"] == {source, dest}
                if actual_name != expected_name and not exact_pair:
                    errors.append(
                        {
                            "code": "NET_NAME_MISMATCH",
                            "message": (
                                f"Net name differs for connection {connection.get('id')}: "
                                f"IR={expected_name!r}, netlist={actual_name!r}."
                            ),
                        }
                    )

        for marker in data["markers"]:
            if marker.get("kind") != "no_connect":
                continue
            endpoint = (
                str(marker.get("reference", "")).casefold(),
                str(marker.get("pin", "")),
            )
            net_index = endpoint_map.get(endpoint)
            if net_index is None:
                errors.append(
                    {
                        "code": "NO_CONNECT_PIN_MISSING",
                        "message": (
                            f"No-connect endpoint is missing from netlist: "
                            f"{marker.get('reference')}.{marker.get('pin')}."
                        ),
                    }
                )
                continue
            pin_type = actual_nets[net_index]["node_types"].get(endpoint, "")
            if "no_connect" not in pin_type:
                errors.append(
                    {
                        "code": "NO_CONNECT_MISMATCH",
                        "message": (
                            f"KiCad did not mark {marker.get('reference')}."
                            f"{marker.get('pin')} as no-connect."
                        ),
                    }
                )

        return {
            "ok": not errors,
            "errors": errors,
            "counts": {
                "ir_components": len(data["components"]),
                "ir_power_symbols": sum(
                    1
                    for component in data["components"]
                    if virtual_kind(component) == "power"
                ),
                "ir_power_flags": sum(
                    1
                    for component in data["components"]
                    if virtual_kind(component) == "power_flag"
                ),
                "ir_multi_unit_components": sum(
                    1
                    for component in data["components"]
                    if bool(component.get("multi_unit", False))
                ),
                "netlist_components": len(actual_components),
                "ir_connections": len(data["connections"]),
                "netlist_nets": len(actual_nets),
            },
        }

    def _load_ir(self, ir_path: str) -> tuple[Path, dict]:



        path = self.guard.resolve_existing(ir_path)



        if not path.is_file():



            raise IsADirectoryError(str(path))



        data = json.loads(path.read_text(encoding="utf-8"))



        self._validate_ir_shape(data)



        return path, data







    def _validate_ir_shape(self, data: dict) -> None:



        if not isinstance(data, dict) or data.get("schema") != self.SCHEMA:



            raise ValueError(f"Not a supported schematic IR ({self.SCHEMA}).")



        for key in ("revision", "components", "connections", "markers"):



            if key not in data:



                raise ValueError(f"Schematic IR is missing required field: {key}")



        if not isinstance(data["components"], list) or not isinstance(data["connections"], list) or not isinstance(data["markers"], list):



            raise ValueError("Schematic IR collections must be arrays.")







    def _validate_data(self, data: dict) -> dict:
        errors = []
        warnings = []
        refs: dict[str, dict] = {}

        def component_kind(component: dict) -> str:
            explicit = str(component.get("component_type", "")).casefold()
            if explicit in {"component", "power", "power_flag"}:
                return explicit
            lib_id = str(component.get("lib_id", "")).casefold()
            if lib_id == "power:pwr_flag":
                return "power_flag"
            if lib_id.startswith("power:") or str(
                component.get("reference", "")
            ).casefold().startswith("#pwr"):
                return "power"
            return "component"

        for component in data.get("components", []):
            ref = str(component.get("reference", "")).strip()
            key = ref.casefold()
            if not ref or not _REF_RE.match(ref):
                errors.append({"code": "INVALID_REFERENCE", "reference": ref})
            elif key in refs:
                errors.append({"code": "DUPLICATE_REFERENCE", "reference": ref})
            else:
                refs[key] = component

            lib_id = str(component.get("lib_id", "")).strip()
            if ":" not in lib_id:
                errors.append(
                    {"code": "INVALID_LIB_ID", "reference": ref, "lib_id": lib_id}
                )

            kind = component_kind(component)
            explicit = str(component.get("component_type", "")).casefold()
            if explicit and explicit not in {"component", "power", "power_flag"}:
                errors.append(
                    {
                        "code": "INVALID_COMPONENT_TYPE",
                        "reference": ref,
                        "component_type": explicit,
                    }
                )

            if kind in {"power", "power_flag"}:
                if bool(component.get("multi_unit", False)):
                    errors.append(
                        {"code": "POWER_MULTI_UNIT_INVALID", "reference": ref}
                    )
                if str(component.get("footprint", "")).strip():
                    warnings.append(
                        {"code": "POWER_HAS_FOOTPRINT", "reference": ref}
                    )
            elif not str(component.get("footprint", "")).strip():
                warnings.append({"code": "MISSING_FOOTPRINT", "reference": ref})

            try:
                spacing = float(component.get("unit_spacing", 25.4))
            except (TypeError, ValueError):
                spacing = float("nan")
            if not math.isfinite(spacing) or spacing <= 0:
                errors.append(
                    {
                        "code": "INVALID_UNIT_SPACING",
                        "reference": ref,
                        "unit_spacing": component.get("unit_spacing"),
                    }
                )

        connected_pins = set()
        for connection in data.get("connections", []):
            for side in ("from", "to"):
                endpoint = connection.get(side, {})
                ref = str(endpoint.get("reference", "")).strip()
                pin = str(endpoint.get("pin", "")).strip()
                if ref.casefold() not in refs:
                    errors.append(
                        {
                            "code": "UNKNOWN_CONNECTION_REFERENCE",
                            "reference": ref,
                            "connection_id": connection.get("id"),
                        }
                    )
                if not pin:
                    errors.append(
                        {
                            "code": "EMPTY_PIN",
                            "reference": ref,
                            "connection_id": connection.get("id"),
                        }
                    )
                connected_pins.add((ref.casefold(), pin))

        no_connect_pins = set()
        for marker in data.get("markers", []):
            if marker.get("kind") != "no_connect":
                continue
            ref = str(marker.get("reference", "")).strip()
            pin = str(marker.get("pin", "")).strip()
            component = refs.get(ref.casefold())
            if component is None:
                errors.append(
                    {
                        "code": "UNKNOWN_NO_CONNECT_REFERENCE",
                        "reference": ref,
                        "marker_id": marker.get("id"),
                    }
                )
            elif component_kind(component) in {"power", "power_flag"}:
                errors.append(
                    {
                        "code": "POWER_NO_CONNECT_INVALID",
                        "reference": ref,
                        "pin": pin,
                    }
                )
            key = (ref.casefold(), pin)
            if key in no_connect_pins:
                warnings.append(
                    {"code": "DUPLICATE_NO_CONNECT", "reference": ref, "pin": pin}
                )
            no_connect_pins.add(key)

        for ref_key, pin in sorted(connected_pins & no_connect_pins):
            component = refs.get(ref_key)
            errors.append(
                {
                    "code": "PIN_CONNECTED_AND_NO_CONNECT",
                    "reference": component["reference"] if component else ref_key,
                    "pin": pin,
                }
            )

        return {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "limitations": [
                "Pin existence/type is authoritatively checked during KiCad materialization/ERC.",
                "Complex bus and hierarchical-sheet semantics are not yet modeled in Circuit IR.",
            ],
            "counts": {
                "components": len(data.get("components", [])),
                "power_symbols": sum(
                    1
                    for component in data.get("components", [])
                    if component_kind(component) == "power"
                ),
                "power_flags": sum(
                    1
                    for component in data.get("components", [])
                    if component_kind(component) == "power_flag"
                ),
                "multi_unit_components": sum(
                    1
                    for component in data.get("components", [])
                    if bool(component.get("multi_unit", False))
                ),
                "connections": len(data.get("connections", [])),
                "markers": len(data.get("markers", [])),
            },
        }

    @staticmethod
    def _connection_endpoint_key(connection: dict) -> tuple:



        a = (



            str(connection["from"]["reference"]).casefold(),



            str(connection["from"]["pin"]),



        )



        b = (



            str(connection["to"]["reference"]).casefold(),



            str(connection["to"]["pin"]),



        )



        return tuple(sorted((a, b)))







    def _commit_mutation(self, path: Path, data: dict, reason: str) -> None:
        current = json.loads(path.read_text(encoding="utf-8"))
        self._validate_ir_shape(current)
        self._snapshot(path, current)
        data["revision"] = int(current["revision"]) + 1



        data["updated_at"] = self._now()



        data["status"] = "draft"



        data["verification"] = None



        self._atomic_write(path, data)



        self.audit.record("eda_sch_ir_commit", path=str(path), revision=data["revision"], reason=reason)







    def _snapshot(self, path: Path, data: dict) -> None:



        history = self.guard.resolve_target(path.parent / ".history")



        history.mkdir(parents=True, exist_ok=True)



        history = self.guard.resolve_existing(history)



        revision = int(data["revision"])



        target = self.guard.resolve_target(history / f"{path.stem}.rev{revision:06d}.json")



        if not target.exists():



            self._atomic_write(target, data)







    def _atomic_write(self, path: Path, data: dict) -> None:



        target = self.guard.resolve_target(path)



        parent = self.guard.resolve_target(target.parent)



        parent.mkdir(parents=True, exist_ok=True)



        parent = self.guard.resolve_existing(parent)



        pending = parent / f".{target.name}.pending.{os.getpid()}.{uuid.uuid4().hex}"



        pending.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")



        os.replace(pending, target)



        self.guard.resolve_existing(target)







    @staticmethod



    def _summary(path: Path, data: dict) -> dict:



        return {



            "path": str(path),



            "schema": data["schema"],



            "name": data["name"],



            "revision": data["revision"],



            "status": data["status"],



            "components": len(data["components"]),



            "connections": len(data["connections"]),



            "markers": len(data["markers"]),



        }







    @staticmethod



    def _tokenize(text: str):



        i = 0



        n = len(text)



        while i < n:



            ch = text[i]



            if ch.isspace():



                i += 1



                continue



            if ch == ";":



                while i < n and text[i] not in "\r\n":



                    i += 1



                continue



            if ch in "()":



                yield ch



                i += 1



                continue



            if ch == '"':



                i += 1



                value = []



                while i < n:



                    ch = text[i]



                    if ch == '"':



                        i += 1



                        break



                    if ch == "\\" and i + 1 < n:



                        nxt = text[i + 1]



                        escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}



                        value.append(escapes.get(nxt, nxt))



                        i += 2



                        continue



                    value.append(ch)



                    i += 1



                else:



                    raise SExpressionError("Unterminated quoted string.")



                yield "".join(value)



                continue



            start = i



            while i < n and not text[i].isspace() and text[i] not in "()":



                i += 1



            yield text[start:i]







    @classmethod



    def _parse_sexpr(cls, text: str) -> list:



        stack: list[list] = []



        root = None



        for token in cls._tokenize(text):



            if token == "(":



                node: list = []



                if stack:



                    stack[-1].append(node)



                stack.append(node)



            elif token == ")":



                if not stack:



                    raise SExpressionError("Unexpected closing parenthesis.")



                node = stack.pop()



                if not stack:



                    if root is not None:



                        raise SExpressionError("Multiple root expressions.")



                    root = node



            else:



                if not stack:



                    if str(token).strip():



                        raise SExpressionError("Atom outside root expression.")



                else:



                    stack[-1].append(token)



        if stack:



            raise SExpressionError("Unclosed parenthesis.")



        if not isinstance(root, list):



            raise SExpressionError("No root expression found.")



        return root







    @staticmethod



    def _child_atom(node: list, token: str) -> str | None:



        for child in node[1:]:



            if isinstance(child, list) and len(child) >= 2 and child[0] == token and not isinstance(child[1], list):



                return str(child[1])



        return None







    @staticmethod



    def _properties(node: list) -> dict[str, str]:



        result = {}



        for child in node[1:]:



            if (



                isinstance(child, list)



                and len(child) >= 3



                and child[0] == "property"



                and not isinstance(child[1], list)



                and not isinstance(child[2], list)



            ):



                result[str(child[1])] = str(child[2])



        return result
