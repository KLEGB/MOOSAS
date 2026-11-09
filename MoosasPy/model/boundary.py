"""Aggregated upper/lower surfaces and lateral boundaries of building spaces."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..transform.geometry.element import (
    MoosasElement,
    MoosasFace,
    MoosasSkylight,
    MoosasWall,
    _getElement,
)
from ..transform.geometry.geos import Vector, is_ccw, makeValid, overlapArea
from ..utils import GeometryError, generate_code, mixItemListToList, np, shapely
from ..utils.constant import geom

if TYPE_CHECKING:
    from .container import MoosasContainer


class MoosasFloor:
    """
    this class define a floor contains multi horizontal/incline face elements.

    init:
    face: list of MoosasFace

    classmethod:
    fromDict: construct a floor from a dictionary, which may be given by toDictionary method from a xmlTree

    attribute:
    'face': The set of horizontal planes that make up the slab, class Moosasface
    ***Many-to-one!Since the slab and ceiling coincide, there may be two identical Moosasfloors in the same location
    'Uid': The id of the element

    properties:
    'area': total face area
    'level': floor level
    'offset':average offset of all faces
    'glazingId': all glazingId of this element
    'glazingElement': all skylight elements of this element

    method:
    'getWeightCenter': the weight center of all geometries
    'force_2d': 2d projection of this element


    'level': The elevation of the floor slab, defined by the floor, with an elevation difference
    greater than 1.5 meters
    'area': The total area of the quilt
    """
    __slots__ = ['face', 'Uid']

    def __init__(self, faces: list[MoosasFace]):
        """
        Initialize a new instance with a list of MoosasFace objects.
        
        Parameters
        ----------
        faces : list of MoosasFace
            A list of MoosasFace instances to be associated with this object. 
            The input may be a nested list, which will be flattened using mixItemListToList.
        
        Returns
        -------
        None
            This constructor does not return a value.
        """
        faces = mixItemListToList(faces)
        self.Uid = generate_code(4)
        self.face: list[MoosasFace] = faces
        for f in self.face:
            f.parentFloors.append(self)

    @classmethod
    def fromDict(cls, floorDict, model: MoosasContainer):
        """
        Create a new instance from a dictionary representation of a floor.
        
        Parameters
        ----------
        floorDict : dict
            Dictionary containing floor data, must include 'face' key with list of face data.
        model : MoosasContainer
            Model container used for creating MoosasFace instances.
        
        Returns
        -------
        cls
            A new instance of the class initialized with MoosasFace objects created from the input dictionary.
        """
        faces = _getElement('face', dictionary=floorDict)
        return cls([MoosasFace.fromDict(f, model) for f in faces])

    @property
    def area(self) -> float:
        """
        Compute the total area of all faces in the object.
        
        Returns
        -------
        float
            The sum of the areas of all faces in the object.
        """
        return np.sum([f.area for f in self.face]).item()

    @property
    def level(self) -> float:
        """
        Level of the first face.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the face attribute.
        
        Returns
        -------
        float
            The level value of the first face in the face list.
        """
        return self.face[0].level

    @property
    def offset(self) -> float:
        """
        Mean offset value across all faces in the object.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `face` attribute, which is expected to be 
            a collection of objects each having an `offset` property.
        
        Returns
        -------
        float
            The mean value of the `offset` attribute from all faces, converted to a Python float.
        """
        # Faces can use different Storey datums. Average their actual reference
        # elevations, then express the result against this Floor's datum.
        return np.mean([f.level + f.offset for f in self.face]).item() - self.level

    @property
    def glazingId(self) -> list[str]:
        """
        Get the list of glazing IDs from all faces.
        
        Returns a concatenated list of glazing IDs by iterating over each face in the object's `face` attribute and collecting their `glazingId` values.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `face` attribute, which is expected to be an iterable of objects each having a `glazingId` property.
        
        Returns
        -------
        list of str
            A list of glazing IDs extracted from each face.
        """
        glsId = []
        for f in self.face:
            glsId = np.append(glsId, f.glazingId)
        return glsId

    @property
    def glazingElement(self) -> list[MoosasSkylight]:
        """
        Get all glazing elements from the faces.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `face` attribute, which is expected to be an iterable 
            of objects each having a `glazingElement` property.
        
        Returns
        -------
        list of MoosasSkylight
            A list containing all glazing elements extracted from each face in `self.face`.
        """
        glsObj = []
        for f in self.face:
            glsObj = np.append(glsObj, f.glazingElement)
        return glsObj

    def getWeightCenter(self) -> np.ndarray:
        """
        Compute the weighted center of all faces in the object.
        
        Parameters
        ----------
        self : object
            The instance of the class containing a list of face objects, each with a `getWeightCenter` method.
        
        Returns
        -------
        np.ndarray
            A 1D numpy array representing the mean coordinates (center) across all face centers.
        """
        center = np.array([face.getWeightCenter() for face in self.face])
        return np.array([np.mean(x) for x in center.T])

    def force_2d(self) -> shapely.Geometry:
        """
        Force the geometry into a 2D representation and return the union of all faces.
        
        Parameters
        ----------
        self : object
            The object containing a `face` attribute, which is a collection of geometric faces.
            Each face must have a `force_2d` method that returns a 2D geometry.
        
        Returns
        -------
        shapely.Geometry
            A single 2D geometry representing the union of all faces. If the union fails,
            a multipolygon composed of the individual 2D faces is returned instead.
        """
        faces = [f.force_2d() for f in self.face]
        try:
            return shapely.union_all(faces, grid_size=geom.POINT_PRECISION)
        except:
            return shapely.multipolygons(faces)


class MoosasEdge:
    """
    This class specifies a closed envelope

    attributes:
    'wall': The set of areas that make up the envelope, class Moosaswall
    'Uid': Unique id for the edge
    '__botBound': The lower contour of the envelope, which is used to delineate the floor slab
    '__topBound': The upper contour of the envelope, used to deline the ceiling

    properties:
    'FactorOfWall': An outward-facing 2d vector set made by ccw calculation, which is not normal
    'level': The lower elevation of the envelope, which is used to reduce the scope of floor identification
    'toplevel': The upper elevation of the envelope, which is used to reduce the ceiling identification area
    """
    __slots__ = ('wall', 'Uid', '__botBound', '__topBound', '__explicitBoundary', '__ownerId', 'internalMass')

    def __init__(self, walls: list[MoosasWall], boundary: shapely.Geometry | None = None, owner_id=None):
        """
        Initialize a boundary object composed of walls.
        
        Parameters
        ----------
        walls : list of MoosasWall
            List of MoosasWall objects that form the boundary. Must contain at least 3 walls.
        
        Returns
        -------
        None
        """
        self.wall: list[MoosasWall] = walls
        self.__ownerId = str(owner_id) if owner_id is not None else None
        self.__botBound = []
        self.__topBound = []
        self.__explicitBoundary = boundary is not None
        self.Uid = generate_code(4)
        # 创建底面和顶面投影多边形。
        if len(self.wall) < 3:
            raise GeometryError(walls, "A boundary requires at least 3 walls.")
        if boundary is not None:
            # RDF supplies the authoritative ring. Reconstructing a provisional
            # polygon on the reader's centimetre grid can collapse legitimate
            # sub-grid wall segments before this boundary is installed.
            self.__botBound = shapely.force_2d(boundary)
            self.__topBound = self.__botBound
            for wall, factor in zip(self.wall, self.FactorOfWall):
                wall.orientation = factor
                for glazing in wall.glazingElement:
                    glazing.orientation = factor
        else:
            self.prepareBoundary()
        self.internalMass: list[MoosasElement] = []
        for w in walls:
            self.internalMass += w.shading

    def prepareBoundary(self):
        """
        Prepare boundary polygons for walls and glazings.
        
        This method processes wall elements to generate bottom and top boundary polygons
        using 2D force representations, and assigns orientation factors to walls and their glazing elements.
        If top boundary generation fails, it defaults to the bottom boundary.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the method. Expected attributes include:
            - wall (list): List of wall objects, each having `force_2d`, `glazingElement`, and `orientation` attributes.
            - FactorOfWall (list): List of orientation factors corresponding to each wall.
            - __botBound (list): Internal list to store bottom boundary points.
            - __topBound (list): Internal list to store top boundary points.
            - get_polygon (callable): Method to convert a list of points into a polygon.
        
        Returns
        -------
        None
            This function does not return any value. It modifies the instance's
            `__botBound` and `__topBound` attributes in place and updates the orientation
            of walls and their glazing elements.
        """
        self.__botBound = []
        self.__topBound = []
        for _wall in self.wall:
            self.__botBound.append(_wall.force_2d())
            self.__topBound.append(_wall.force_2d(top=True))
            # if _wall.level>self.level: self.level=_wall.level
            # if _wall.toplevel < self.toplevel: self.toplevel = _wall.toplevel
        # print(self.__botBound,self.__topBound)
        self.__botBound = self.get_polygon(self.__botBound)
        try:
            self.__topBound = self.get_polygon(self.__topBound)
        except:
            self.__topBound = self.__botBound

        # overwrite orientation for walls and glazings
        for _wall, _factor in zip(self.wall, self.FactorOfWall):
            _wall.orientation = _factor
            for gls in _wall.glazingElement:
                gls.orientation = _factor

    @classmethod
    def fromDict(cls, floorDict, model):
        """
        Create a class instance from a dictionary representation of a floor.
        
        Parameters
        ----------
        floorDict : dict
            Dictionary containing floor data, expected to include a 'face' key.
        model : object
            Model object passed to the creation of individual MoosasWall instances.
        
        Returns
        -------
        cls
            A new instance of the class, initialized with a list of MoosasWall objects created from the input dictionary.
        """
        faces = _getElement('face', dictionary=floorDict)
        return cls([MoosasWall.fromDict(f, model) for f in faces])

    @classmethod
    def difference(cls, mainEdge: MoosasEdge, subBoundary: shapely.Geometry):
        """
        Compute the geometric difference between a main edge and a sub-boundary.
        
        Parameters
        ----------
        mainEdge : MoosasEdge
            The primary edge geometry to be processed. Must be valid.
        subBoundary : shapely.Geometry
            The sub-boundary geometry to subtract from the main edge. Must fully overlap with the 2D projection of mainEdge.
        
        Returns
        -------
        shapely.Geometry
            The resulting geometry after subtracting subBoundary from mainEdge.
        """
        if not mainEdge.is_valid():
            raise GeometryError(mainEdge, "invalid boundary:{}")
        if overlapArea(mainEdge.force_2d(), subBoundary) != shapely.area(subBoundary):
            # must be the same or errors would occur when splitting the walls
            raise GeometryError(subBoundary, "invalid subBoundary in boundary divided:{}")

    @classmethod
    def matchWall(cls, segment: shapely.Geometry, walls: list[MoosasWall]):
        """Return the existing wall matching a boundary segment, or ``None``."""
        segment_coordinates = shapely.get_coordinates(segment)
        tolerance = 1.01 * geom.POINT_PRECISION
        for wall in np.array(walls).flatten():
            wall_coordinates = shapely.get_coordinates(wall.force_2d())
            if len(segment_coordinates) != len(wall_coordinates):
                continue
            if (
                np.all(np.abs(segment_coordinates - wall_coordinates) <= tolerance)
                or np.all(np.abs(segment_coordinates - wall_coordinates[::-1]) <= tolerance)
            ):
                return wall
        return None

    @classmethod
    def selectWall(cls, boundary: shapely.Geometry, walls: list[MoosasWall]):
        """
        Select existing walls that match every edge of a boundary.
        
        Parameters
        ----------
        boundary : shapely.Geometry
            A geometry object whose edges must match existing walls.
        walls : list of MoosasWall
            A list of wall objects to be matched against the boundary edges. Must not be empty.
        
        Returns
        -------
        cls
            An instance constructed from the walls matching every boundary edge.
        """
        walls = np.array(walls).flatten()
        boundary = shapely.get_coordinates(boundary)
        # from ...visual.geometry import plot_object
        # plot_object(walls,boundary,colors=['red','black'])
        edges = [shapely.linestrings([poi1, poi2]) for poi1, poi2 in zip(boundary[:-1], boundary[1:])]
        validWalls = []
        for edg in edges:
            matched_wall = cls.matchWall(edg, walls)
            if matched_wall is None:
                raise GeometryError(edg, "no existing wall matches boundary segment")
            validWalls.append(matched_wall)

        # print(edges,validWalls)
        # edge = cls(validWalls)
        # print([w.force_2d() for w in validWalls],"\n",edge.force_2d())
        return cls(validWalls)

    @property
    def parent(self):
        """
        The parent object of the wall associated with this instance.
        
        Parameters
        ----------
        None
        
        Returns
        -------
        object
            The parent object of the first element in the wall list.
        """
        return self.wall[0].parent

    @property
    def level(self) -> float:
        """
        Minimum level value among all walls.
        
        Returns the minimum level value computed across all wall objects 
        contained in the instance's `wall` attribute.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `wall` attribute, which 
            is expected to be an iterable of objects each having a `level` 
            attribute.
        
        Returns
        -------
        float
            The minimum level value from the `level` attributes of all walls.
        """
        return np.min([w.level for w in self.wall])

    @property
    def toplevel(self) -> float:
        """
        Maximum top level among all walls.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `wall` attribute, which is a collection 
            of wall objects each having a `toplevel` property.
        
        Returns
        -------
        float
            The maximum value of the `toplevel` property across all walls in `self.wall`.
        """
        return np.max([w.toplevel for w in self.wall])

    @property
    def elevation(self) -> float:
        """
        Mean elevation of all walls.
        
        Returns the average elevation value computed from the `elevation` 
        attribute of each wall in the `self.wall` collection.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `wall` attribute, which is 
            expected to be a collection of objects each having an `elevation` property.
        
        Returns
        -------
        float
            The mean elevation of all walls in the collection.
        """
        return np.mean([w.elevation for w in self.wall]).item()

    @property
    def FactorOfWall(self) -> np.ndarray[Vector]:
        """
        Compute the normal factor vectors for wall edges based on polygon orientation.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the boundary and orientation methods.
            Must have a `__botBound` attribute accessible via `self.__botBound` and an `is_ccw()` method.
        
        Returns
        -------
        np.ndarray[Vector]
            An array of Vector objects representing the cross product of the orientation 
            factor (determined by clockwise or counter-clockwise polygon winding) and 
            each edge vector of the polygon boundary.
        """
        if self.__explicitBoundary or (
                shapely.get_type_id(self.__botBound) == 3
                and len(self.__botBound.interiors)):
            # Flattening Polygon coordinates inserts fictitious exterior/hole
            # connectors and misaligns every following normal with its wall.
            factors = []
            for wall in self.wall:
                owner_normal = wall.settings.get('geometry_owner_normals', {}).get(
                    getattr(self, '_MoosasEdge__ownerId', None))
                # The room ring follows the wall's bottom trace. Using the
                # farthest XY vertices of the full 3D face creates a diagonal
                # through an inclined wall, or includes an overhanging upper
                # panel that never touches this ring.
                points = np.unique(shapely.get_coordinates(wall.force_2d()), axis=0)
                if len(points) < 2:
                    # Millimetre-grid projection can collapse a valid narrow
                    # wall segment. If the RDF carries the owning space's
                    # outward normal, preserve its direction with a unit
                    # orientation vector; the wall's sub-grid length is not
                    # used as physical boundary area.
                    if owner_normal is not None:
                        outward = np.asarray(owner_normal, dtype=float)
                        if outward.shape == (3,) and np.isfinite(outward).all() and np.linalg.norm(outward[:2]) > 0:
                            factors.append(Vector(outward / np.linalg.norm(outward[:2])))
                            continue
                    # If no owner normal was serialized, recover the actual
                    # sub-millimetre footprint edge from the 3D wall instead
                    # of the rounded force_2d projection.
                    xyz = shapely.get_coordinates(wall.face, include_z=True)
                    raw_points = np.unique(np.asarray(xyz[:, :2], dtype=float), axis=0)
                    if len(raw_points) >= 2:
                        distances = np.linalg.norm(
                            raw_points[:, None, :] - raw_points[None, :, :], axis=2)
                        ia, ib = np.unravel_index(np.argmax(distances), distances.shape)
                        if distances[ia, ib] > 1e-12:
                            points = raw_points
                    if len(points) < 2:
                        raise GeometryError(wall.face, "degenerate explicit wall")
                distances = np.linalg.norm(
                    points[:, None, :] - points[None, :, :], axis=2)
                ia, ib = np.unravel_index(np.argmax(distances), distances.shape)
                vector = np.asarray(points[ib] - points[ia], dtype=float)
                length = float(np.linalg.norm(vector))
                if length <= 0:
                    raise GeometryError(wall.face, "degenerate explicit wall")
                if owner_normal is not None:
                    outward = np.asarray(owner_normal, dtype=float)
                    if outward.shape != (3,) or not np.isfinite(outward).all() or np.linalg.norm(outward[:2]) <= 0:
                        raise GeometryError(wall.face, 'invalid explicit owner normal')
                    factors.append(Vector(outward / np.linalg.norm(outward[:2]) * length))
                    continue
                normal = np.array([vector[1], -vector[0]]) / length
                center = (points[ia] + points[ib]) / 2
                epsilon = min(.001, length * .001)
                # A fine-grid residual between the supplied ring and an actual
                # short wall can exceed length/1000. Shrinking that probe alone
                # leaves both points on the same side. Also try bounded local
                # probes up to a quarter of the segment under the same 1 mm
                # cap. This changes neither geometry nor review tolerances.
                steps = [epsilon * 10. ** (-power) for power in range(10)]
                steps.extend((min(.001, length * .01),
                              min(.001, length * .1),
                              min(.001, length * .25),
                              min(.01, length * .01),
                              min(.05, length * .1),
                              min(.1, length * .25)))
                sign = None
                for step in steps:
                    plus = shapely.contains(
                        self.__botBound, shapely.points(center + normal * step))
                    minus = shapely.contains(
                        self.__botBound, shapely.points(center - normal * step))
                    if plus != minus:
                        sign = -1 if plus else 1
                        break
                if sign is None:
                    # A probe can fall on both sides of a rounded boundary
                    # segment for very short/oblique walls. Infer its outward
                    # side from an interior point of the owning room instead
                    # of rejecting an otherwise importable RDF space.
                    interior = self.__botBound.representative_point()
                    toward_wall = center - np.asarray([interior.x, interior.y], dtype=float)
                    projection = float(np.dot(normal, toward_wall))
                    if abs(projection) > max(1e-10, length * 1e-8):
                        sign = 1 if projection > 0 else -1
                if sign is None:
                    raise GeometryError(
                        wall.face,
                        "cannot establish actual holed-room outward wall normal",
                    )
                factors.append(Vector([
                    float(normal[0] * length * sign),
                    float(normal[1] * length * sign),
                    0,
                ]))
            return np.asarray(factors)
        poly_coordinates = shapely.get_coordinates(self.__botBound)
        if self.is_ccw():
            factor = np.array([0, 0, -1])
        else:
            factor = np.array([0, 0, 1])
        poly_vector = [poly_coordinates[i] - poly_coordinates[i - 1] for i in range(1, len(poly_coordinates))]
        return np.array([Vector.cross(factor, vec, style=Vector) for vec in poly_vector])

    @property
    def area(self) -> float:
        """
        Area of the geometry.
        
        Returns the area of the 2D projection of the geometry.
        
        Parameters
        ----------
        self : object
            The geometry object on which the property is accessed. Must have `force_2d` method.
        
        Returns
        -------
        float
            The area of the geometry in 2D.
        """
        return shapely.area(self.force_2d())

    def getWeightCenter(self) -> np.ndarray:
        """
        Compute the weight center of wall force coordinates.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `wall` attribute, which is a collection 
            of wall objects that have a `force_2d` method returning 2D coordinate data.
        
        Returns
        -------
        np.ndarray
            A 1D NumPy array containing the mean (center) coordinates along axis 0 
            of the 2D force coordinates extracted from all walls.
        """
        return np.mean(shapely.get_coordinates([w.force_2d() for w in self.wall]), axis=0)

    def is_ccw(self) -> bool:
        """
        Determine if the polygon boundary is oriented counter-clockwise (CCW).
        
        Parameters
        ----------
        self : object
            The instance of the class containing the polygon boundary.
            It must have a private attribute `__botBound` that represents
            the boundary geometry compatible with shapely.
        
        Returns
        -------
        bool
            True if the polygon is oriented counter-clockwise, False otherwise.
        """
        return is_ccw(self.__botBound)

    def get_polygon(self, target) -> shapely.Geometry:
        """
        Get the polygon geometry for a given target.
        
        Parameters
        ----------
        target : str or int
            The identifier or name of the target polygon to retrieve.
        
        Returns
        -------
        shapely.Geometry
            A Shapely geometry object representing the requested polygon.
        """

        def reverseTwin(point_twin):
            """
            Reverse the elements of a two-element list in place.
            
            Parameters
            ----------
            point_twin : list
                A list with exactly two elements that will be swapped in place.
            
            Returns
            -------
            list
                The same list with its two elements swapped.
            """
            tmp = point_twin[0]
            point_twin[0] = point_twin[1]
            point_twin[1] = tmp
            return point_twin

        point_twin = [shapely.points(shapely.get_coordinates(shapely.set_precision(line, geom.POINT_PRECISION))) for line
                      in target]
        if not shapely.dwithin(point_twin[0][0], point_twin[-1][0], 1.2 * geom.POINT_PRECISION):
            if not shapely.dwithin(point_twin[0][0], point_twin[-1][1], 1.2 * geom.POINT_PRECISION):
                point_twin[0] = reverseTwin(point_twin[0])

        for i in range(1, len(point_twin)):
            if not shapely.dwithin(point_twin[i][0], point_twin[i - 1][1], 1.2 * geom.POINT_PRECISION):
                point_twin[i] = reverseTwin(point_twin[i])
        polyPoints = [twin[0] for twin in point_twin]
        # polyPoints = np.array([])
        # for twins in point_twin:
        #     connection = set(twins) & set(polyPoints)
        #     if len(connection) == 1 and len(polyPoints) >= 2:
        #         if list(connection)[0] == polyPoints[-2]:
        #             polyPoints = np.append(polyPoints[:-2], [polyPoints[-1], polyPoints[-2]])
        #     polyPoints = np.append(polyPoints, list(set(twins).difference(set(polyPoints))))
        # print(polyPoints)
        polyPoints = np.append(polyPoints, polyPoints[0])
        poly_coordinates = shapely.get_coordinates(polyPoints)

        polyg = shapely.polygons(poly_coordinates)
        if not shapely.is_valid(polyg):
            polyg_ori = polyg
            try:
                valid_parts = makeValid(polyg)
            except Exception:
                # Some exported gbXML boundaries contain repeated or crossed
                # wall segments.  ``make_valid`` can reduce those rings to
                # lines, so recover the largest polygon enclosed by the
                # original wall segments instead of aborting the whole model.
                polygonized = shapely.get_parts(shapely.polygonize(target))
                polygonized = [p for p in polygonized if not shapely.is_empty(p)]
                if not polygonized:
                    raise
                valid_parts = polygonized
            polyg = max(valid_parts, key=shapely.area)
            print(f"******Warning: GeometryError, invalid boundary:{polyg_ori.__repr__()}fix to {polyg.__repr__()}")
        return polyg

    def force_2d(self, top=False) -> shapely.Geometry:
        """
        Force the geometry into a 2D representation.
        
        Parameters
        ----------
        top : bool
            If True, returns the top boundary; otherwise, returns the bottom boundary.
        
        Returns
        -------
        shapely.Geometry
            The 2D geometry representing either the top or bottom boundary.
        """
        if top:
            target = self.__topBound
        else:
            target = self.__botBound

        # if selfIntersect(target):
        #     raise f"self-intersection, top:{top} {target}"
        return target

    def is_valid(self) -> bool:
        """
        Check if the geometry is valid based on area, dimensions, and self-intersection.
        
        Parameters
        ----------
        self : object
            The instance of the class containing geometric data. Must have attributes `area`, `level`, 
            and method `force_2d()`. The `force_2d()` method should return a geometry object compatible with shapely.
        
        Returns
        -------
        bool
            True if the geometry meets minimum area and dimension requirements and has no self-intersections; 
            False otherwise.
        """
        try:
            if self.area < geom.ROOM_MIN_AREA:
                print('******Warning: GeometryError, area invalid, floor:', self.level)
                return False
            boundary_box = shapely.get_coordinates(shapely.boundary(self.force_2d()))
            dimension = [np.max(boundary_box[:, 0]) - np.min(boundary_box[:, 0]),
                         np.max(boundary_box[:, 1]) - np.min(boundary_box[:, 1])]
            if dimension[0] <= geom.ROOM_MIN_DIMENSION or dimension[1] <= geom.ROOM_MIN_DIMENSION:
                print('******Warning: GeometryError, dimension invalid %.3f' % dimension[0], '%.3f' % dimension[1],
                      'floor:', self.level)
                # print(boundary_box)
                return False
            if str(shapely.is_valid_reason(self.force_2d())).find('Self-intersection') != -1:
                print("******Warning: GeometryError, self-intersection", self.force_2d())
                return False
        except:
            print('******Warning: GeometryError, Boundary validation failed, floor:', self.level)
            return False
        return True
