"""Cleanse module for the transformation.
all module has MoosasConatiner as an entrance/return
"""
from __future__ import annotations
import copy
import numpy as np
import shapely

from .element import *
from ...utils import TopologyError
from ...utils.tools import mixItemListToList, searchBy
from ...utils.constant import geom
from .geos import equals, overlapEdge, Vector
from ...model.container import MoosasContainer
from ...model.boundary import MoosasEdge


def _groupByNormal(listToGroup: list, listOfNormal: list[shapely.Geometry | np.ndarray]) -> list[list]:
    """
    Group items in a list based on their corresponding normal vectors.
    
    Parameters
    ----------
    listToGroup : list
        A list of elements to be grouped. The elements can be of any type.
    listOfNormal : list of shapely.Geometry or numpy.ndarray
        A list of normal vectors (geometric objects or arrays) used as grouping criteria.
        Must have the same length as `listToGroup`.
    
    Returns
    -------
    list of list
        A 2-dimensional list where each sublist contains elements from `listToGroup`
        that correspond to the same (or parallel) normal vector. The grouping considers
        both positive and negative parallels.
    """
    """group the items by the list of normal. This method can accelerate the calculation of the cleanse.
        The items with same factor (both positive and negative) will be pushed in the same group

        ---------------------------------
        listToGroup: anything that need to be grouped
        listOfNormal: the guidance vector, with the same lengh as listToGroup

        return: 2-dimensional list with the same type as listToGroup
    """
    if len(listOfNormal) != len(listToGroup):
        raise Exception('items and normals should have same number.')
    listToGroup = np.array(listToGroup)
    normalGroup,uniqueNormal = [[len(listOfNormal)-1]],[listOfNormal[-1]]
    for i,nor in enumerate(listOfNormal[:-1]):
        matched = False
        for g,un in enumerate(uniqueNormal):
            if Vector.parallel(nor,un):
                normalGroup[g].append(i)
                matched = True
                break
        if not matched:
            normalGroup.append([i])
            uniqueNormal.append(nor)

    itemsGroup = [list(listToGroup[groupIdx]) for groupIdx in normalGroup]
    return itemsGroup

    normal_ = [Vector(normal).string for normal in listOfNormal]
    sortedGroup = {nor: [] for nor in np.unique(normal_)}
    for nor, item in zip(normal_, listToGroup):
        sortedGroup[nor].append(item)
    return list(sortedGroup.values())


def _groupRelateArray(sequences: list) -> list:
    """
    Join arrays that have intersecting elements into unified groups.
    
    Parameters
    ----------
    sequences : list of list
        A list of sequences (lists) containing elements. Sequences that share common elements 
        will be merged into a single group.
    
    Returns
    -------
    list of list
        A list of lists where each sublist contains the union of originally connected sequences 
        (i.e., sequences that had overlapping elements are combined).
    """
    """join array together if they have intersections.
    e.g. [[1, 2], [2, 3, 4], [5, 6], [4, 7, 8], [6, 9, 10], [11, 12]]
    => [[1, 2, 3, 4, 7, 8], [5, 6, 9, 10], [11, 12]]
    """
    sequenceSet = [set(s) for s in sequences]
    length = len(sequenceSet)
    for _ in range(length):
        s = sequenceSet.pop()
        flag = True
        for i in np.arange(len(sequenceSet) - 1, -1, -1):
            if set(s) & set(sequenceSet[i]):
                sequenceSet[i] = s | sequenceSet[i]
                flag = False
                break
        if flag:
            sequenceSet = [s] + sequenceSet
    return [list(s) for s in sequenceSet]


def _groupByCollinear(listToGroup: list, listOfNormal: list[shapely.Geometry | np.ndarray],
                      listOfGeometry: list[shapely.Geometry]) -> list[list]:
    """
    Group elements of a list based on collinearity of corresponding geometries.
    
    Parameters
    ----------
    listToGroup : list
        A list of elements to be grouped. Can be any type, but typically corresponds to geometric objects or identifiers.
    listOfNormal : list of shapely.Geometry or numpy.ndarray
        A list of normal vectors (as geometries or coordinate arrays) associated with each element in `listToGroup`.
        Used to determine directional alignment (collinearity). Must have the same length as `listToGroup`.
    listOfGeometry : list of shapely.Geometry
        A list of LineString geometries used to test spatial relationships (e.g., intersections and point alignments).
        Must have the same length as `listToGroup`.
    
    Returns
    -------
    list of list
        A 2-dimensional list where each sublist contains elements from `listToGroup` that are determined to be collinear
        based on their normal vectors and spatial alignment (proximity along the direction of the normal).
        The grouping is stricter than `_groupByNormal`, resulting in more refined groups.
    """
    """push linestring in a group if they are collinear. (based on _groupByNormal function)
        it will produce more detailed groups than groupByNormal; and therefore more acceleration on the calculation.

         ---------------------------------
        listToGroup: anything that need to be grouped
        listOfNormal: the guidance vector, with the same lengh as listToGroup
        listOfGeometry: the guidance geometry (to test intersection), with the same lengh as listToGroup

        return: 2-dimensional list with the same type as listToGroup
    """
    if len(listOfNormal) != len(listToGroup) or len(listOfNormal) != len(listOfGeometry):
        raise Exception('items and normals should have same number.')
    if len(listOfNormal)==0:
        return listToGroup
    groupIdx = _groupByNormal(list(np.arange(len(listToGroup))), listOfNormal)
    listToGroup = [np.array(listToGroup)[group] for group in groupIdx]
    listOfNormal = [np.array(listOfNormal)[group] for group in groupIdx]
    listOfGeometry = [np.array(listOfGeometry)[group] for group in groupIdx]
    groups = []
    for itemGroup, norGroup, geometryGroup in zip(listToGroup, listOfNormal, listOfGeometry):
        extremePoint = np.min(shapely.get_coordinates(geometryGroup), axis=0)
        pointOnLines = [shapely.get_coordinates(geo)[0] for geo in geometryGroup]
        distance = [Vector.dot(nor, poi - extremePoint) for nor, poi in zip(norGroup, pointOnLines)]
        thisGroups = {dist: [] for dist in np.unique(distance)}
        for item, dist in zip(itemGroup, distance):
            thisGroups[dist].append(item)
        for gp in list(thisGroups.values()):
            groups.append(gp)
    return groups


def partitionWall(walls: list[MoosasWall], model: MoosasContainer, bottom=None, top=None) -> list[MoosasWall]:
    """
    Partition a list of walls by sorting their coordinates and creating new polygonal walls using specified top and bottom boundaries.
    
    Parameters
    ----------
    walls : list[MoosasWall]
        List of MoosasWall objects to be partitioned. The function processes their 2D coordinates and glazing elements.
    model : MoosasContainer
        The container model to which the new walls will be associated.
    bottom : float, optional
        The bottom elevation level for the new walls. If not provided, it is calculated as the minimum of (wall.level + wall.offset) across all walls.
    top : float, optional
        The top elevation level for the new walls. If not provided, it is calculated as the maximum of (wall.toplevel + wall.topoffset) across all walls.
    
    Returns
    -------
    list[MoosasWall]
        A list of new MoosasWall objects created from sorted unique coordinates and assigned glazing elements, bounded by the specified or inferred top and bottom levels.
    
    """
    """partition the walls by sorting their coordinates and making polygon using the top and bottom boundaries
    the glazing of all walls will be collected and try to attach to the new wall again.

    ***Warning: A legacy method. It should be replaced by MoosasWall._break() in the future!
    """
    coor = shapely.points(shapely.get_coordinates([w.force_2d() for w in walls]))

    coor = list(shapely.get_coordinates(list(set(coor))))

    coor.sort(key=lambda x: (x[0], x[1]))
    top = np.max([wall.toplevel + wall.topoffset for wall in walls]) if top is None else top
    bottom = np.min([wall.level + wall.offset for wall in walls]) if bottom is None else bottom
    gls: list[MoosasGlazing] = []
    for wall in walls:
        gls = list(np.append(gls, np.array(wall.glazingElement)))

    # print(coor, bottom, top, gls)
    wallNew: list[MoosasWall] = MoosasWall.fromSeriesPoint(shapely.points(coor), bottom, top, gls, model)
    return wallNew


def _fastOverlap(wall1: shapely.Geometry, wall2: shapely.Geometry) -> bool:
    """
    Very fast check whether two walls overlap based on coordinate sequence.
    
    Parameters
    ----------
    wall1 : shapely.Geometry
        First wall geometry to compare.
    wall2 : shapely.Geometry
        Second wall geometry to compare.
    
    Returns
    -------
    bool
        True if the walls overlap based on coordinate ordering and spatial proximity, False otherwise.
    """
    """very fast calculate weather two walls are containBy one another
    according the sequence of their coordinates.
    """
    coor = list(np.append(shapely.get_coordinates([wall1, wall2]), [[0], [0], [1], [1]], axis=1))
    coor.sort(key=lambda x: (x[0], x[1]))
    if not coor[0][2] == coor[1][2]:
        if Vector(coor[1][:2] - coor[2][:2]).length() > geom.POINT_PRECISION:
            return True
    return False


def cleanseDuplicatedLevel(model: MoosasContainer) -> MoosasContainer:
    """Keep the largest horizontal-area candidates subject to storey spacing.

    Areas are measured before reassignment, so traversal order and merged area
    cannot change the selected elevations. Inclined roofs do not vote for levels.
    """
    candidates = sorted(set(model.levelList))
    if not candidates:
        return model
    areas = {}
    for level in candidates:
        footprints = [face.force_2d() for face in model.faceList
                      if face.level == level and abs(float(face.normal[2])) >= 0.99]
        areas[level] = float(shapely.area(shapely.union_all(footprints))) if footprints else 0.0
    spacing = getattr(geom, "LEVEL_MIN_HEIGHT", geom.LEVEL_MAX_OFFSET)
    retained = []
    for level in sorted(candidates, key=lambda z: (-areas[z], z)):
        if areas[level] < geom.LEVEL_MIN_AREA:
            continue
        if all(abs(level - kept) >= spacing - 1e-9 for kept in retained):
            retained.append(level)
    # Preserve a usable reference for small models with no area-qualified level.
    if not retained:
        retained = [min(candidates, key=lambda z: (-areas[z], z))]
    retained.sort()
    for face in list(model.faceList) + list(model.skylightList):
        if face.level not in retained:
            reference_height = face.level + face.offset
            face.level = min(retained, key=lambda z: (abs(z - reference_height), z))
            face.offset = reference_height - face.level
    model.levelList = retained
    return model


def cleanseOverlapFace(model: MoosasContainer) -> MoosasContainer:
    """
    Identify and remove duplicated faces in the model using geometric overlap analysis.
    
    Parameters
    ----------
    model : MoosasContainer
        The input model container containing levels, faces, and walls. The function modifies 
        `model.faceList` in place by removing or splitting overlapping faces. One of each pair 
        of duplicated faces is removed, and differences are added as new faces.
    
    Returns
    -------
    MoosasContainer
        The modified model with duplicated faces removed or split to resolve overlaps. 
        The operation is performed level by level, and face containment is addressed 
        during the process.
    """
    """
        Identify the duplicated faces with shapely
        you must solve duplication before solving containment

        *** One of the duplicated walls would be removed from MoosasContainer.wallList.
    """

    for bld_level in model.levelList:
        print(f'\rCLEANSE: Duplicated face checking on {bld_level}', end='')
        completed = False
        while not completed:
            completed = True
            faceId = searchBy('face', bld_level, model.faceList)
            faces = np.array(model.faceList)[faceId]

            # sort the faces based on the face area, to solve the containment at the same time.
            area = [ - shapely.area(face) for face in faces] # sort from bigger faces to smaller faces
            argIdx = np.argsort(area)
            faces = faces[argIdx]
            faceId = np.array(faceId)[argIdx]
            faces = [f.force_2d() for f in faces]

            delId = []
            for fid,face in enumerate(faces[:-1]):
                for otherId in range(fid+1,len(faces)):
                    other = faces[otherId]
                    overArea = overlapArea(face,other)
                    if overArea>geom.AREA_PRECISION:
                        # check if face is redundant
                        if abs(overArea - shapely.area(face))<geom.AREA_PRECISION:
                            delId.append(faceId[fid])
                            break

                        # boolean difference
                        else:
                            delId.append(faceId[fid])
                            face = shapely.force_3d(shapely.difference(face, other),z=bld_level)
                            moFace = MoosasFace(model,faceId=model.includeGeo(face, Vector([0, 0, 1]).geometry, cat=0),level=bld_level)
                            model.faceList = np.append(model.faceList, moFace)

            # recursively divided
            if len(delId)>0:
                model.faceList = np.delete(model.faceList, delId)
                completed = False

    return model

def cleanseDuplicatedWall(model: MoosasContainer) -> MoosasContainer:
    """
    Identify and remove duplicated walls based on geometric duplication in 2D.
    
    Parameters
    ----------
    model : MoosasContainer
        The input model container containing wall and level lists. Walls are checked for duplication
        within each level, and duplicated walls are removed from the wallList.
    
    Returns
    -------
    MoosasContainer
        The updated model container with duplicated walls removed. One of each pair of duplicated walls
        is dissolved into another and then removed from the wallList.
    """
    """
        Identify the duplicated walls that 2 points of them are placed nearby
        you must solve duplication before solving containment
        this func is based on _groupByCollinear. if _groupByCollinear do not perform well, serious error will occur here

    *** One of the duplicated walls would be removed from MoosasContainer.wallList.

    """
    """build up the duplication check list"""
    duplicateCheckList: list[tuple[int, int]] = []
    for bld_level in model.levelList:
        wall_indices = searchBy('level', bld_level, model.wallList)
        wall_groups = {}
        for wall_index in wall_indices:
            wall = model.wallList[wall_index]
            geometry = shapely.normalize(wall.force_2d())
            group_key = (
                bool(wall.is_air_boundary),
                shapely.to_wkb(geometry),
            )
            wall_groups.setdefault(group_key, []).append(wall_index)
        for wall_group in wall_groups.values():
            for first_index, first_wall in enumerate(wall_group):
                for second_wall in wall_group[first_index + 1:]:
                    if (
                        model.wallList[first_wall].is_air_boundary
                        == model.wallList[second_wall].is_air_boundary
                        and equals(
                            model.wallList[first_wall].force_2d(),
                            model.wallList[second_wall].force_2d(),
                        )
                    ):
                        duplicateCheckList.append((first_wall, second_wall))

    """check if the walls are duplicated, and dissolve that wall into the others"""
    duplicatedWall: set[int] = set()
    for wl, (first_wall, second_wall) in enumerate(duplicateCheckList):
        print(f'\rCLEANSE: Duplicated wall checking: {wl}/{len(duplicateCheckList)}', end='')
        if first_wall in duplicatedWall or second_wall in duplicatedWall:
            continue
        model.wallList[first_wall].dissolve(model.wallList[second_wall])
        duplicatedWall.add(second_wall)

    print()
    model.wallList = np.delete(model.wallList, list(duplicatedWall))
    return model


def cleanseOverlapWall(model: MoosasContainer) -> MoosasContainer:
    """
    Solve overlapping walls by identifying and partitioning intersecting wall segments.
    
    Parameters
    ----------
    model : MoosasContainer
        The container object holding the wall and level data. The `wallList` attribute 
        contains the walls to be processed, and `levelList` is used to group walls by level.
    
    Returns
    -------
    MoosasContainer
        The modified model with overlapping walls removed and replaced by partitioned 
        non-overlapping wall segments. The `wallList` is updated in place to reflect 
        the changes.
    """
    """ Solve the overlapped of walls.

    Identify the big walls which overlaps with a small walls or other walls,
    and break the big walls according to the walls' intersections.

    To ensure the efficiency, this func do a lot of simplifications on geometries including:
    1. represent all walls in 2d.
    2. 'containBy' means the end points of two (or more) walls are not in sequences.
    3. all walls will be reconstructed using only 4 points to get simplified representations.
    4. the height of walls will be regarded as the same, but will be extended or trimmed to the top of the level.

    P.S.
    you must solve duplication before solving containment
    this func is based on _groupByCollinear. if _groupByCollinear do not perform well, serious error will occur here

    *** The overlap walls would be removed from MoosasContainer.wallList; And a partition by their intersections would be added.

    """
    """build the containment check list"""
    containCheckList: list[list[int]] = []
    mergeGroup: list[set[int]] = []
    for bld_level in model.levelList:
        wall_list = searchBy('level', bld_level, model.wallList)
        wall_group = _groupByCollinear(listToGroup=wall_list,
                                       listOfNormal=[model.wallList[w].normal for w in wall_list],
                                       listOfGeometry=[model.wallList[w].force_2d() for w in wall_list])
        for wall_list in wall_group:
            for i in range(len(wall_list)):
                containCheckList.append(wall_list[i:])

    """check containment and build the mergeGroup"""
    for i, task in enumerate(containCheckList):
        print(f'\rCLEANSE: Overlapped checking: {i+1}/{len(containCheckList)}', end='')
        solveWall = model.wallList[task[0]]
        for others in task[1:]:
            if _fastOverlap(solveWall.force_2d(), model.wallList[others].force_2d()):
                mergeGroup.append({task[0], others})
    # print('finish.')
    """merge the sets if a & b != None"""
    mergeGroup = _groupRelateArray(mergeGroup)

    """partition the walls into new walls"""
    wallNew = []
    for i,group in enumerate(mergeGroup):
        wallNew += partitionWall(np.array(model.wallList)[list(group)], model)
        print(f'\rCLEANSE: Overlapped checking merging:{i+1}/{len(mergeGroup)}', end='')

    """delete the old walls"""
    delList = [item for group in mergeGroup for item in list(group)]
    model.wallList = np.append(np.delete(model.wallList, delList), wallNew)

    print()
    return model


def cleanseInvalidWall(model: MoosasContainer) -> MoosasContainer:
    """
    Cleanse invalid walls from the model by removing walls with invalid geometry or zero dimensions and dissolving them into adjacent valid walls.
    
    Parameters
    ----------
    model : MoosasContainer
        The container object holding the wall list to be cleansed. Walls that are invalid due to zero height, zero length, or invalid shapely.Geometry 
        will be removed. Invalid walls that are geometrically coincident with valid walls below them will be dissolved into those walls.
    
    Returns
    -------
    MoosasContainer
        The updated MoosasContainer object with invalid walls removed and appropriate walls merged.
    """
    """check if the walls are valid including:
    1.zone length or zero height wall
    2.invalid shapely.Geometry
    3.then dissolve those walls to others valid walls,
    which have coincident edge with the invalid walls and lay below the them.

    *** The invalid walls would be removed from MoosasContainer.wallList.

    """

    def _isValid(_wall: MoosasWall) -> int:
        """
        Check and validate walls in the model, removing invalid ones and dissolving them into adjacent valid walls.
        
        Parameters
        ----------
        model : object
            The model object containing the wallList, levelList, and associated methods such as searchBy and overlapEdge.
            Must have attributes `wallList` (list of MoosasWall objects), `levelList` (list of levels), and methods
            `searchBy` (for querying walls by property) and `overlapEdge` (for checking geometric edge overlap).
        
        Returns
        -------
        model : object
            The modified model object with invalid walls removed or dissolved into neighboring walls.
            Walls that fail validation are either deleted or merged, and the updated wallList is returned within the model.
        """
        # for face in np.array(wall.face).flatten():
        # if not shapely.is_valid(face):
        #    print(face)
        #    return -1
        if shapely.get_dimensions(wall.force_2d()) <= 0:
            return 1
        if Vector(wall.force_2d()).length() < geom.POINT_PRECISION:
            return 2
        if wall.area3d() < geom.POINT_PRECISION*geom.POINT_PRECISION:
            return 2
        if (wall.level + wall.offset) == (wall.toplevel + wall.topoffset):
            return -1
        return 0

    """build the check list for walls' validation"""
    del_face = []
    check_list = list(np.arange(len(model.wallList)))
    total = len(check_list)
    while len(check_list) > 0:
        i = check_list.pop()
        wall = model.wallList[i]
        print(f'\rCLEANSE: Invalid checking: {total - len(check_list)}/{total}', end='')
        if _isValid(wall) != 0:
            del_face.append(i)
            if _isValid(wall) > 0:
                searchLevel = model.levelList.index(wall.level) - 1
                searchLevel = [model.levelList[searchLevel] + wall.level]

                """find a wall to dissolve this invalid wall"""
                checkWall = [index for index in searchBy('level', wall.level, model.wallList) if
                             not (index in del_face)]
                for j in checkWall:
                    if overlapEdge(wall.face, model.wallList[j].face):
                        # if model.wallList[j].height <= wall.height:
                            model.wallList[j].dissolve(wall)
                            check_list.append(j)
                            break


    print(f"\t\tdel walls {len(del_face)}")
    model.wallList = list(np.delete(model.wallList, del_face))
    return model


def cleanseInvalidFace(model: MoosasContainer) -> MoosasContainer:
    """
    Check and remove invalid 2D faces from a MoosasContainer.
    
    Parameters
    ----------
    model : MoosasContainer
        The input model containing a list of faces to be validated. Faces are tested for valid 2D geometry
        after triangulation and conversion via force_2d().
    
    Returns
    -------
    MoosasContainer
        The updated model with invalid faces removed from the faceList.
    """
    """check if the faces are valid including:
    face.force2d() was valid 2d geometry.
    all faces would be triangulated before testing.

    *** The invalid faces would be removed from MoosasContainer.wallList.

    """
    delface = []
    for i, face in enumerate(model.faceList):
        if not shapely.is_valid(face.force_2d()):
            print(f"***Warning: invalid horizontal face detected:{face.face}")
            delface.append(i)
    model.faceList = np.delete(model.faceList, delface)
    return model


def cleanseCoplannerLine(model: MoosasContainer) -> MoosasContainer:
    """
    Check and remove co-planar faces, merging them into single faces within a MoosasContainer.
    
    Parameters
    ----------
    model : MoosasContainer
        The input model container containing wall and level data. The `wallList` attribute holds the list of wall faces,
        and `levelList` contains the different building levels to process. Co-planar walls in each level are identified
        and merged; original walls are removed and merged versions are added.
    
    Returns
    -------
    MoosasContainer
        The modified model with co-planar walls merged. The `wallList` is updated by removing redundant co-planar faces
        and retaining the merged faces.
    """
    """check and remove the co-planner faces, then merge them into one face.
    the process has been redirect to the general method _conPlannerCleanse (for MoosasContainer or obj file)

    *** The original faces would be removed from MoosasContainer.wallList; and the new faces would be added.

    """
    total_a = len(model.wallList)
    for bld_level in model.levelList:
        total = len(model.wallList)
        face_list = np.array(model.wallList)[searchBy('level', bld_level, model.wallList)]
        _,redundant = _coPlannerCleanse(face_list)
        if len(redundant) >0:
            redundant = [list(model.wallList).index(w) for w in redundant]
            model.wallList = list(np.delete(model.wallList, redundant))
        print(f'\rCLEANSE: Merge walls: {total - len(model.wallList)} in Level: {bld_level}', end='')
    print(f"\t\ttotal merge wall: {total_a - len(model.wallList)}")
    return model

def _coplanar_elements(first, second):
    """Require every source vertex to lie in one physical face plane.

    The general vector parallel test permits several degrees of rotation.
    Using it to transitively merge adjacent faces turns a curved facade into
    a multi-plane wall with a fictitious diagonal bottom projection.
    """
    faces = list(np.asarray(first.face, dtype=object).reshape(-1))
    faces += list(np.asarray(second.face, dtype=object).reshape(-1))
    reference = shapely.get_coordinates(faces[0], include_z=True)[:-1]
    normal = np.cross(reference, np.roll(reference, -1, axis=0)).sum(axis=0)
    magnitude = np.linalg.norm(normal)
    if magnitude <= 1e-12:
        return False
    normal /= magnitude
    points = shapely.get_coordinates(faces, include_z=True)
    return bool(np.max(np.abs((points-reference[0]) @ normal)) <= geom.POINT_PRECISION)


def _coPlannerCleanse(elements: np.ndarray[MoosasElement]) -> (np.ndarray[MoosasElement],np.ndarray[MoosasElement]):
    """
    Delete coplanar lines by merging adjacent faces that are coplanar.
    
    Parameters
    ----------
    elements : np.ndarray[MoosasElement]
        Array of MoosasElement objects representing 3D geometric faces. Each element must provide
        methods `getEdgeStr()` to retrieve edge strings and `dissolve()` to merge with other faces.
        The normal vector of each face is accessed via the `normal` attribute.
    
    Returns
    -------
    tuple of (np.ndarray[MoosasElement], np.ndarray[MoosasElement])
        A tuple containing two arrays:
        - The first array contains the merged, non-redundant MoosasElement objects after coplanar faces have been dissolved.
        - The second array contains the redundant MoosasElement objects that were removed during the merging process.
    """
    """Delete Coplanar Lines: Lines with and only two adjacent faces are coplanar lines.

    This function works within 3d spaces, and operates directly on the geometries of the walls.
    It provides a clean set of walls for glazing matching and close contour calculation,
    as well as significantly improves the performance for other cleanse functions.

    This function needs to ensure the cleanse performance.
    Therefore, it will not do any simplifications on the geometries,
    which means a very high calculation cost.

    In the future, a multi processes should be added to this func.

    The process of the func can be described as:
    1. Traverse the horizontal & vertical planes of the same floor and create a dict of all lines (initialized with set())
    2. Iterate over dict to get set() of length 2
    3. Check whether the walls A and B in the set have a common line corresponding to the set length of 2
    4. Iterate through all sets and sets with common elements
    5. Iterate through all sets after the cleanup and call the dissolve method
    ---------------------------------
    elements: MoosasElement or MoosasGeometry as input

    Return: merged elements,redundant elements (np.ndarray[MoosasElement],np.ndarray[MoosasElement])
    """
    faceNum = len(elements)
    currentFaceNum = 0
    redundant = []

    while currentFaceNum != faceNum:
        currentFaceNum = faceNum
        edgeDict = {}

        """Get the topology of all faces"""
        for faceIdx, moface in enumerate(elements):
            edges = moface.getEdgeStr()
            for edge_str in edges:
                if edge_str not in edgeDict.keys():
                    edgeDict[edge_str] = [faceIdx]
                else:
                    edgeDict[edge_str] += [faceIdx]

        """Find coPlane faces"""
        _dissolveFaces = []
        for faces in edgeDict.values():
            if len(faces) == 2:
                if _coplanar_elements(elements[faces[0]], elements[faces[1]]):
                    coedges = set(elements[faces[0]].getEdgeStr()) & set(elements[faces[1]].getEdgeStr())
                    if len([edge_str for edge_str in list(coedges) if len(edgeDict[edge_str]) > 2]) == 0:
                        _dissolveFaces.append({faces[0],faces[1]})

        """Merge dissolve groups"""
        to_dissolveFaces = _groupRelateArray(_dissolveFaces)

        """Dissolve each group"""
        delfaces = set()
        for faces in to_dissolveFaces:
            faces = list(faces)
            parentFace = elements[faces[0]]
            # Coplanarity is not transitive under a numerical tolerance.
            child_indices = [i for i in faces[1:] if _coplanar_elements(parentFace, elements[i])]
            childFaces = [elements[i] for i in child_indices]
            delfaces = delfaces | set(child_indices)
            redundant += childFaces
            parentFace.dissolve(childFaces)

        elements = list(np.delete(elements, list(delfaces)))
        faceNum = len(elements)
    return elements,redundant

def solveIntersectionVertical(model: MoosasContainer) -> MoosasContainer:
    """
    Calculate the intersection of wall projections in 2D for each floor and split walls accordingly.
    
    This function computes pairwise intersections between vertical wall faces projected onto 2D space,
    then subdivides the walls into smaller segments based on these intersections. It operates only
    on vertical faces (walls) and ignores 3D spatial relationships such as multi-level overlaps.
    The calculation is optimized by grouping walls by their normal directions using `_groupByNormal`.
    Intersections use the same precision grid as wall projections and topology
    nodes so splitting does not shift shared junctions onto a different grid.
    
    Parameters
    ----------
    model : MoosasContainer
        A container object holding wall data structured per floor. Walls are assumed to be vertical
        and represented in 2D projection. The container will be modified in place as walls are split.
    
    Returns
    -------
    MoosasContainer
        A new MoosasContainer instance containing the original walls broken into minimal segments
        resulting from intersection calculations. The segmentation is performed recursively and
        may result in a significantly increased number of wall elements.
    """
    """Calculate the intersection of walls projection in 2d for each floor
    then break those walls into parts.

    this method cannot use to solve the intersection on vertical and horizontal faces,
    but only solve the intersection between vertical faces (walls).
    Besides, since we implement the function in 2d space, any 3d relations will be ignored.
    in this case, this function do not care about any walls cross multi-level.

    the _groupByNormal method has been applied to accelerate the calculation.

    *** The walls would be recursively divided. THIS SHOULD BE CAREFULLY MAINTENANCE!
    """


    # recursively break the wall into minimal parts
    def checkBreakIntersection(walls, otherWall2d):
        """Recursively checks and resolves intersections between walls by breaking them at intersection points.
        
            Parameters
            ----------
            walls : list or object
                A single wall object or a list of wall objects. If not a list, it will be converted to a list.
            otherWall2d : list of geometric objects
                A list of 2D geometric representations of walls (typically linestrings) used for intersection testing.
        
            Returns
            -------
            list
                A list of wall objects resulting from breaking the input walls at detected intersection points.
                If no intersections are found, returns the original walls. If breaks occur, recursively processes
                the new set of walls until no further intersections remain.
        """
        if not isinstance(walls, list):
            walls = [walls]
        newWalls = []
        for wall in walls:
            newWalls.append(wall)
            w2d = wall.force_2d()
            for w2dOther in otherWall2d:
                intersection = shapely.intersection(w2d, w2dOther, grid_size=geom.POINT_PRECISION)
                # print(w2d, w2dOther,Vector.parallel(Vector(w2d), Vector(w2dOther)),intersection)
                if (not shapely.is_empty(intersection)) and shapely.get_dimensions(intersection) == 0:
                    twins = shapely.points(shapely.get_coordinates(w2d))
                    if not (shapely.dwithin(twins[0], intersection, geom.POINT_PRECISION) or shapely.dwithin(twins[1],
                                                                                                           intersection,
                                                                                                           geom.POINT_PRECISION)):
                        brkResult = MoosasWall.break_(wall, intersection)
                        if brkResult is not None:
                            newWalls.pop()
                            newWalls += brkResult
                            break

        if len(newWalls) != len(walls):
            return checkBreakIntersection(newWalls, otherWall2d)
        else:
            return newWalls

    delWalls, newWalls = [], []
    removedGlazings = set()
    removedGeoIds = set()
    prs = 0
    model.wallList = list(model.wallList)
    for bld_level in model.levelList:
        wall_list = searchBy('level', bld_level, model.wallList)
        if len(wall_list) == 0:
            continue
        wallElement = np.array(model.wallList)[wall_list]
        wallNormal = [w.normal for w in wallElement]
        wallElementGroup = _groupByNormal(wallElement,wallNormal)
        wallListGroup = _groupByNormal(wall_list, wallNormal)

        for gidx,_ in enumerate(wallElementGroup):
            otherWallGroup = wallElementGroup[:gidx]+wallElementGroup[gidx+1:]
            _t = []
            for ggg in otherWallGroup:
                _t += ggg
            otherWallGroup = _t
            testSet2d = np.array([w.force_2d() for w in otherWallGroup])
            for wid, wall in zip(wallListGroup[gidx], wallElementGroup[gidx]):
                prs += 1
                print(f"\rCLEANSE: solve vertical faces intersection {prs}/{len(model.wallList)}", end='')
                brkResult = checkBreakIntersection(wall, testSet2d)
                if len(brkResult) > 1:
                    newWalls += brkResult
                    delWalls.append(wid)
                    removedGeoIds.update(mixItemListToList(wall.faceId))
                    for glazing in mixItemListToList(wall.glazingElement):
                        removedGlazings.add(glazing)
                        removedGeoIds.update(mixItemListToList(glazing.faceId))


        # for i, wall, w2d in zip(wall_list, wallElement, wall2d):
        #     prs += 1
        #
        #     print(f"\rCLEANSE: solve vertical faces intersection {prs}/{len(model.wallList)}", end='')
        #     parallel = [not(Vector.parallel(Vector(wall.normal), Vector(w.normal))) for w in wallElement]
        #     testSet2d = wall2d[parallel]
        #     for w2dOther in testSet2d:
        #         intersection = shapely.intersection(w2d, w2dOther,grid_size=geom.POINT_PRECISION)
        #         # print(w2d, w2dOther,Vector.parallel(Vector(w2d), Vector(w2dOther)),intersection)
        #         if (not shapely.is_empty(intersection)) and shapely.get_dimensions(intersection)==0:
        #             twins =shapely.points(shapely.get_coordinates(w2d))
        #             if not (shapely.dwithin(twins[0], intersection, geom.POINT_PRECISION) or shapely.dwithin(twins[1], intersection,geom.POINT_PRECISION)):
        #                 print(intersection)
        #                 brkResult = MoosasWall.break_(wall, intersection)
        #                 if brkResult is not None:
        #                     newWalls += brkResult
        #                     delWalls.append(i)
                # if shapely.contains(w2d, poi):
                #     twins = shapely.points(shapely.get_coordinates(w2d))
                #     if not (shapely.dwithin(twins[0], poi, geom.POINT_PRECISION) or shapely.dwithin(twins[1], poi,
                #                                                                                   geom.POINT_PRECISION)):
                #         wall1, wall2 = MoosasWall.break_(wall, poi)
                #         newWalls += [wall1, wall2]
                #         delWalls.append(i)

    print(f'\tbreak walls:{len(delWalls)} add:{len(newWalls)}',end='')

    model.wallList = list(np.delete(model.wallList, delWalls))
    model.wallList += newWalls

    referencedGeoIds = set()
    referencedGlazingUids = set()
    for element in model.getAllFaces():
        referencedGeoIds.update(mixItemListToList(getattr(element, "faceId", [])))
        for glazing in mixItemListToList(getattr(element, "glazingElement", [])):
            referencedGlazingUids.add(str(getattr(glazing, "Uid", "")))
            referencedGeoIds.update(mixItemListToList(getattr(glazing, "faceId", [])))

    if removedGlazings:
        model.glazingList = [
            glazing for glazing in model.glazingList
            if str(getattr(glazing, "Uid", "")) not in {str(getattr(item, "Uid", "")) for item in removedGlazings}
            or str(getattr(glazing, "Uid", "")) in referencedGlazingUids
        ]

    for face_id in removedGeoIds:
        if face_id in referencedGeoIds:
            continue
        if face_id in model.geoId:
            model.removeGeo(face_id)
    print()
    return model


def solveIntersectionHorizontal(model: MoosasContainer) -> MoosasContainer:
    """
    Calculate the intersection between faces and edges on each level by recursively dividing overlapping faces.
    
    Parameters
    ----------
    model : MoosasContainer
        The container object containing levels, faces, and edges. It holds the geometric data to be processed,
        including `levelList`, `faceList`, and `edgeList`. Faces are divided based on their overlap with edges
        from the same or adjacent levels.
    
    Returns
    -------
    MoosasContainer
        The input model with updated face divisions where overlapping faces have been split into minimal faces
        to resolve horizontal intersections. The modification is done in place, and the same model object is returned.
    """
    """Calculating the overlap on a level between faces and edges.
    The faces would be recursively divided until all faces are minimal faces.
    """
    dividedCount = 0
    for bldLevelIndex in range(len(model.levelList)):
        faces = list(np.array(model.faceList)[searchBy('level', model.levelList[bldLevelIndex], model.faceList)])
        edges = []
        if bldLevelIndex > 0:
            edges += list(np.array(model.edgeList)[searchBy('level', model.levelList[bldLevelIndex-1], model.edgeList)])
        if bldLevelIndex < len(model.levelList) - 1:
            edges += list(np.array(model.edgeList)[searchBy('level', model.levelList[bldLevelIndex], model.edgeList)])

        # from ...visual.geometry import plot_object
        # plot_object(edges,faces,colors=['blue','black'])
        dividedFaces = []

        while len(faces) > 0:
            f = faces.pop()
            dividedFaces.append(f)
            try:
                for e in edges:
                    intersectArea = overlapArea(e.force_2d(), f.force_2d())
                    if intersectArea > geom.POINT_PRECISION ** 2:
                        if intersectArea < f.area - geom.POINT_PRECISION ** 2:
                            # it means the MoosasFace need to be split
                            splitF = splitFaces(f, e)
                            if splitF is not None:
                                if len(splitF[1])>0 and splitF[0] is not None:
                                    faces += [splitF[0]] + splitF[1]
                                    dividedFaces.pop()
                                    dividedCount += 1
                                    break
            except GeometryError:
                pass
            print(f'\rPACKING: containBy checking for horizontal Faces-Level{model.levelList[bldLevelIndex]} remain:{len(faces)}', end='')
    print(f'\tdivided horizontal faces: {dividedCount}')
    return model


def splitFaces(face: MoosasFace, edge: MoosasEdge) -> (MoosasFace, list[MoosasFace]):
    """
    Split a face into inner and outer parts based on intersection with an edge.
    
    Parameters
    ----------
    face : MoosasFace
        The input face to be split. Must be planar and aligned with the XY plane.
    edge : MoosasEdge
        The edge used as a splitter; defines the boundary for splitting the face.
    
    Returns
    -------
    tuple
        A tuple containing:
        - innerFace (MoosasFace): The part of the face intersecting with the edge.
        - outerFaces (list[MoosasFace]): List of remaining face parts after subtraction of the intersection.
        Returns None if no valid split is possible due to area precision or geometric validity issues.
    """
    """split the face into the intersection with edge's boundary and the remained part
    if the face is not a planar face or the face is incline,
    it will not be changed since it is seldom connects to other spaces

    ***you should check if the face overlaps with the edge first by overlapArea method!!

    ***intersection will only create one face, but shapely.difference can create multi faces!!

        ---------------------------------
    face: MoosasFace as input to be split
    edge: MoosasEdge as input as a spliter

    Return: inner face and outer faces (MoosasFace, list[MoosasFace])
    """
    model: MoosasContainer = face.parent
    if not Vector.parallel(face.normal, [0, 0, 1]):
        return [face,[]]
    if face not in model.faceList:
        return [face,[]]

    f2d = makeValid(face.force_2d())[0]
    if shapely.covers(
        shapely.buffer(edge.force_2d(), geom.POINT_PRECISION), f2d,
    ):
        return None
    # print(shapely.is_valid_reason(f2d), edge.force_2d())
    """split opaque part"""
    innerFace = shapely.force_3d(shapely.intersection(f2d, edge.force_2d(),grid_size=geom.POINT_PRECISION), z=face.elevation)
    outerFace = shapely.force_3d(shapely.difference(f2d, edge.force_2d(),grid_size=geom.POINT_PRECISION), z=face.elevation)

    innerFace = makeValid(innerFace)[0]
    outerFace = [makeValid(outf)[0] for outf in shapely.get_parts(outerFace) if not shapely.is_empty(outf)]
    # A snapped overlay must reduce every child, not reproduce its parent indefinitely.
    area_tolerance = geom.POINT_PRECISION ** 2
    if (
        shapely.area(innerFace) >= f2d.area - area_tolerance
        or any(shapely.area(part) >= f2d.area - area_tolerance for part in outerFace)
    ):
        return None
    # outerFace = [makeValid(f)[0] for ff in outerFace for f in ff]
    if shapely.area(innerFace) > geom.POINT_PRECISION ** 2:
        innerFace = MoosasFace(model=model,
                               faceId=model.includeGeo(innerFace, Vector([0, 0, 1]).geometry, face.category))
    else:
        return None
    for i, outf in enumerate(outerFace):
        if shapely.area(outerFace[i]) > geom.POINT_PRECISION ** 2:
            outerFace[i] = MoosasFace(model=model,
                                      faceId=model.includeGeo(outf, Vector([0, 0, 1]).geometry, face.category))
        else:
            outerFace[i] = None
    outerFace = [outf for outf in outerFace if outf is not None]
    if len(outerFace)==0:
        return None
    model.faceList = list(np.append(np.append(model.faceList, [innerFace]), outerFace))
    model.faceList.remove(face)

    """split aperture part"""
    innerGlazings, outerGlazings = [], []  # record for inner and outer skylights
    for gls in face.glazingElement:
        g2d = makeValid(gls.force_2d())[0]
        overArea = overlapArea(g2d, edge.force_2d())

        # aperture lay outside the edge
        if overArea == 0:
            outerGlazings = np.append(outerGlazings, [gls])

        # aperture lay inside the edge
        elif overArea >= gls.area - area_tolerance:
            innerGlazings = np.append(innerGlazings, [gls])

        # aperture need to be split
        else:
            innerGls = shapely.force_3d(shapely.intersection(g2d, edge.force_2d(),grid_size=geom.POINT_PRECISION), z=face.elevation)
            outerGls = shapely.force_3d(shapely.difference(g2d, edge.force_2d(),grid_size=geom.POINT_PRECISION), z=face.elevation)
            outerGls = shapely.get_parts(outerGls)

            innerGls = MoosasSkylight(model=model,
                                      faceId=model.includeGeo(makeValid(innerGls)[0], Vector([0, 0, 1]).geometry,
                                                              gls.category))
            for i, outf in enumerate(outerGls):
                outerGls[i] = MoosasSkylight(model=model,
                                             faceId=model.includeGeo(makeValid(outf)[0], Vector([0, 0, 1]).geometry,
                                                                     gls.category))

            model.skylightList = list(np.append(np.append(model.skylightList, [innerGls]), outerGls))
            model.skylightList.remove(gls)
            innerGlazings = np.append(innerGlazings, [innerGls])
            outerGlazings = np.append(outerGlazings, outerGls)
    """attach the inner and outer skylight to the MoosasFace"""
    for gls in innerGlazings:
        innerFace.add_glazing(gls)
    for gls in outerGlazings:
        for outf in outerFace:
            if shapely.covers(
                shapely.buffer(outf.force_2d(), geom.POINT_PRECISION), gls.force_2d(),
            ):
                outf.add_glazing(gls)
                break
        else:
            raise TopologyError(splitFaces, "Split skylight has no covering parent face")

    return innerFace, outerFace
