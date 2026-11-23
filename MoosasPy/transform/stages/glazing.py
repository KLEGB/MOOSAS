"""Glazing-to-parent element matching for transformed models."""
from __future__ import annotations

from ...model import MoosasModel
from ...utils import mixItemListToList, np, shapely
from ...utils.constant import geom
from ...utils.tools import searchBy
from ..geometry.element import (
    MoosasFace,
    MoosasGlazing,
    MoosasSkylight,
    MoosasWall,
    attach_glazing_to_parent,
)


def match_face_glazing(face: MoosasFace | MoosasWall, glazing: MoosasSkylight | MoosasGlazing) -> bool:
    """Attach glazing to a containing or coincident opaque parent face."""
    return attach_glazing_to_parent(face, glazing, tolerance=2 * geom.POINT_PRECISION)


def _split_at_wall_ends(window: MoosasGlazing, wall_projections) -> list[MoosasGlazing]:
    """Cut a window at the ends of collinear walls lying inside its span."""
    line = window.force_2d()
    if shapely.get_dimensions(line) != 1:
        return [window]
    start, end = shapely.get_coordinates(line)[[0, -1]]
    length = float(np.linalg.norm(end - start))
    axis = (end - start) / length
    tolerance = 2 * geom.POINT_PRECISION
    breaks = []
    for projection in wall_projections:
        if shapely.get_dimensions(projection) != 1:
            continue
        offsets = shapely.get_coordinates(projection) - start
        if np.max(np.abs(offsets[:, 0] * axis[1] - offsets[:, 1] * axis[0])) > tolerance:
            continue
        breaks += [start + axis * s for s in offsets @ axis if tolerance < s < length - tolerance]
    return MoosasGlazing.break_(window, shapely.points(breaks)) if breaks else [window]


def attach_glazing_to_faces(model: MoosasModel) -> MoosasModel:
    """Attach glazing and skylights, creating curtain parents where necessary."""
    glazing_count = 0
    matched_glazing_count = 0
    for level in model.levelList:
        glazing = np.array(model.glazingList)[searchBy("level", level, model.glazingList)]
        walls = np.array(model.wallList)[searchBy("level", level, model.wallList)]
        wall_projections = [wall.force_2d() for wall in walls]
        for window in glazing:
            glazing_count += 1
            print(f"\rLOADING: Matching glazing {glazing_count}/{len(model.glazingList)}", end="")
            pieces = _split_at_wall_ends(window, wall_projections)
            if len(pieces) > 1:
                model.glazingList = [item for item in model.glazingList if item is not window] + pieces
                for face_id in mixItemListToList(window.faceId):
                    model.removeGeo(face_id)
            for piece in pieces:
                distances = np.argsort([shapely.distance(piece.force_2d(), projection) for projection in wall_projections])
                for wall in walls[distances][:min(5, len(walls))]:
                    if match_face_glazing(wall, piece):
                        matched_glazing_count += 1
                        break
                else:
                    curtain = MoosasWall(model, faceId=piece.faceId)
                    curtain.add_glazing(piece)
                    model.wallList = list(np.append(model.wallList, [curtain]))

    print("\tmatched glazings: ", matched_glazing_count)
    print("\rLOADING: Matching skylight", end="")
    matched_skylight_count = 0
    for index, skylight in enumerate(model.skylightList):
        print(f"\rLOADING: Matching skylight {index}/{len(model.skylightList)}", end="")
        floors = np.array(model.faceList)[searchBy("level", skylight.level, model.faceList)]
        for floor in floors:
            if match_face_glazing(floor, skylight):
                matched_skylight_count += 1
                break
        else:
            roof = MoosasFace(model=model, faceId=skylight.faceId)
            roof.add_glazing(skylight)
            model.faceList = list(np.append(model.faceList, [roof]))
    print("\t\t\tmatched skylight: ", matched_skylight_count)
    return model
