"""Topology and optional finalization stage for transformations."""
from __future__ import annotations

from ...model import MoosasModel
from ...utils import np, shapely
from ...utils.tools import searchBy
def normalize_air_boundaries(model: MoosasModel) -> MoosasModel:
    """Convert air boundaries without two distinct spaces to opaque walls."""
    for wall in model.wallList:
        if wall.is_air_boundary and len({str(space_id) for space_id in wall.space}) != 2:
            wall.setCategory(0)
    return model


def attach_shading_content(model: MoosasModel) -> MoosasModel:
    """Attach leftover geometry as internal mass or nearby glazing shading."""
    shading = np.array(model.wall_remain + model.face_remain)
    keep_mask = [True] * len(shading)
    for index, element in enumerate(shading):
        print(f"\rCONTENT: attach internal thermal mass:{index}/{len(shading)}", end="")
        space_indices = searchBy("level", element.level, model.spaceList)
        for space_index in space_indices:
            if shapely.contains(model.spaceList[space_index].force_2d(), element.force_2d()):
                model.spaceList[space_index].addInternalMass(element)
                keep_mask[index] = False
                break

    shading = shading[keep_mask]
    existing_shading = set(model.shadingList)
    model.shadingList = np.append(
        model.shadingList,
        [element for element in shading if element not in existing_shading],
    )
    print()
    for index, face in enumerate(shading):
        print(f"\rCONTENT: attach shading element:{index}/{len(shading)}", end="")
        centroid = face.getWeightCenter()
        levels = [level for level in model.levelList if level < centroid[2]]
        if not levels:
            continue
        glazing = list(np.array(model.glazingList)[searchBy("level", levels[-1], model.glazingList)])
        glazing += list(np.array(model.skylightList)[searchBy("level", levels[-1], model.skylightList)])
        if not glazing:
            continue
        target_glazing = min(glazing, key=lambda item: shapely.distance(face.force_2d(), item.force_2d()))
        if shapely.distance(face.force_2d(), target_glazing.force_2d()) < 1.5:
            target_glazing.shading.append(face)
    print()
    return model
