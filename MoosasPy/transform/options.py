"""Configuration contract for the geometry transformation pipeline."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TransformOptions:
    """Options for orchestrating a geometry transformation."""

    solve_duplicated: bool = True
    solve_redundant: bool = True
    solve_overlap: bool = True
    triangulate_faces: bool = True
    break_wall_vertical: bool = True
    break_wall_horizontal: bool = True
    attach_shading: bool = False
    divided_zones: bool = False
    simplify_boundary: bool = False
    insert_core: bool = False  # Offset thermal zoning; no physical shaft insertion.
    standardize: bool = False
