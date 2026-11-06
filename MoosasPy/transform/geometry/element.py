"""Element definition in moosas+

we split the MoosasModel definition from geometry.element to avoid circular import.
however, we still need some method in MoosasModel,
so actually all objects named model or attributes named parent are MoosasModel object as its abstract class MoosasContainer
"""
from __future__ import annotations

import os
import re

from .geos import Projection, Vector, faceNormal, is_ccw, simplify, overlapArea, equals, selfIntersect, makeValid, bBox
from .triangulation import triangulate2dFace
from ...utils import generate_code, searchBy, mixItemListToObject, mixItemListToList, encodeParams, GeometryError
from ...utils import shapely, np, ET
from ...utils.tools import path
from ...utils.constant import geom

# 不做inch meter转换
INCH_METER_MULTIPLIER = 1
INCH_METER_MULTIPLIER_SQR = 1


def glazing_matches_parent(face, glazing, tolerance: float | None = None) -> bool:
    """Return True when a face projection can host the glazing projection."""
    tolerance = 2 * geom.POINT_PRECISION if tolerance is None else tolerance
    try:
        face_projection = face.force_2d(region=True)
        glazing_projection = glazing.force_2d(region=True)
    except GeometryError:
        # A nearly zero-width SketchUp face can collapse to fewer than three
        # unique vertices after precision reduction. It cannot host a
        # glazing element, but it must not abort the whole model transform.
        return False
    if shapely.get_dimensions(face_projection) == shapely.get_dimensions(glazing_projection) == 1:
        face_points = shapely.get_coordinates(face_projection)
        glazing_points = shapely.get_coordinates(glazing_projection)
        if len(face_points) < 2 or len(glazing_points) < 2:
            return False
        face_axis = face_points[-1] - face_points[0]
        axis_length = np.linalg.norm(face_axis)
        if axis_length <= geom.POINT_PRECISION:
            return False
        for point in shapely.points(glazing_points):
            if shapely.distance(face_projection, point) > tolerance:
                return False

        unit_axis = face_axis / axis_length
        face_min = 0.0
        face_max = axis_length
        glazing_scalars = [
            float(np.dot(point - face_points[0], unit_axis))
            for point in glazing_points
        ]
        glazing_min = min(glazing_scalars)
        glazing_max = max(glazing_scalars)
        return glazing_min >= face_min - tolerance and glazing_max <= face_max + tolerance

    return bool(shapely.contains(face_projection, glazing_projection) or shapely.covers(face_projection, glazing_projection))


def attach_glazing_to_parent(face, glazing, tolerance: float | None = None) -> bool:
    """Attach glazing to a face when their projections satisfy the shared predicate."""
    if not glazing_matches_parent(face, glazing, tolerance=tolerance):
        return False
    face.add_glazing(glazing)
    return True


def attach_glazing_to_candidates(glazing, candidates, tolerance: float | None = None):
    """Attach glazing to the first matching candidate and return that parent."""
    for candidate in candidates:
        if attach_glazing_to_parent(candidate, glazing, tolerance=tolerance):
            return candidate
    return None


def _getElement(*key: str, dictionary: dict, strict=True) -> np.ndarray:
    """
    Get values from a dictionary corresponding to given keys and return as a numpy array.
    
    Parameters
    ----------
    *key : str
        Variable length argument list of keys to look up in the dictionary.
    dictionary : dict
        The dictionary from which to retrieve values using the provided keys.
    strict : bool, optional
        If True, raises an error when a key is not found. If False, returns an array with None if any key is missing. Default is True.
    
    Returns
    -------
    np.ndarray
        A numpy array containing the values from the dictionary corresponding to the input keys. If `strict` is False and a key is missing, returns an array with a single None value.
    """
    l = []
    for k in key:
        if k not in dictionary.keys():
            if strict:
                raise NameError(f'{k} not in the construct dictionary {dictionary}')
            else:
                return np.array([None])
        l = np.append(l, dictionary[k])
    return l


class MoosasGeometry(object):
    """protection for original geometry.
    the class object can only be created and never be changed.
    the is valid method is used to test whether this object can be used in moosas+.
    """
    __slots__ = ['__face', '__normal', '__faceId', '__category', '__holes', 'delete', 'flip']

    def __init__(self, face: shapely.Geometry | np.ndarray, faceId, normal: shapely.Geometry | Vector | np.ndarray = None,
                 category=0,
                 holes: list[shapely.Geometry | np.ndarray] = None, errors='ignore'):
        """
        Initialize a polygon object with face geometry, identifier, normal vector, and optional holes.
        
        Parameters
        ----------
        face : shapely.Geometry or np.ndarray
            The outer boundary of the polygon as a geometry object or coordinate array.
        faceId : hashable
            Identifier for the face, converted to string internally.
        normal : shapely.Geometry or Vector or np.ndarray, optional
            Normal vector of the face; if None, computed automatically using faceNormal.
        category : int, default 0
            Category label associated with the face.
        holes : list of shapely.Geometry or np.ndarray, optional
            List of inner boundaries (holes) within the face; defaults to empty list.
        errors : {'ignore', 'raise'}, default 'ignore'
            Specifies behavior when invalid geometry is detected: 'ignore' prints a warning,
            'raise' throws a GeometryError.
        
        Returns
        -------
        None
        """
        if normal is None:
            normal = faceNormal(face)
        if not holes:
            holes = []

        # test if input is valid
        if Vector(normal).length() == 0:
            raise GeometryError(face, "zero-length normal")
        self.__face: np.ndarray = self._treatFace(face)
        self.__holes: list[np.ndarray] = [self._treatFace(hole) for hole in holes]
        self.__normal: Vector = Vector(normal)
        self.flip = False

        self.__faceId: str = str(faceId)
        self.__category: int = category
        self.delete: bool = False

        # validation
        if self.invalid() is not None:
            if errors == 'ignore':
                print(f"******Warning: GeometryError, invalid polygon received:{self.invalid()}")
            else:
                raise GeometryError(face, f"invalid polygon received:{self.invalid()}")

    @staticmethod
    def _treatFace(face) -> np.ndarray:
        """
        Preprocess a face or hole by converting and validating its coordinates.
        
        Parameters
        ----------
        face : array-like or shapely.Geometry
            The input face or hole, either as a Shapely geometry object or a sequence of coordinate points.
            If it is a Shapely geometry, coordinates are extracted using `shapely.get_coordinates` with Z included.
        
        Returns
        -------
        numpy.ndarray
            A 2D NumPy array of shape (N, 3) containing the processed (x, y, z) coordinates of the face,
            with duplicate consecutive points removed and missing Z-coordinates filled with 0.
            Raises `GeometryError` if the resulting coordinate list has fewer than 3 unique non-collinear points.
        """
        """preprocess the face or holes."""

        # force planner by project and reproject
        face = shapely.polygons(face) if not isinstance(face, shapely.Geometry) else face
        coordinates=shapely.get_coordinates(face,include_z=True)
        normal=faceNormal(face)
        # Keep already planar source doubles exactly. A redundant inverse
        # transform can turn a shared .8 vertex into .7999999999999998,
        # destabilizing GEOS unions of otherwise exact tessellations.
        distance=(coordinates-coordinates[0])@normal.array
        if np.max(np.abs(distance))>1e-9:
            proj=Projection(origin=coordinates[0],unitZ=normal)
            face=proj.toUV(face)
            face=shapely.force_3d(shapely.force_2d(face),z=0)
            face=proj.toWorld(face)

        face = shapely.get_coordinates(face, include_z=True)
        _coordinates = []
        for point in face:
            if len(point) == 2:
                point = np.append(point, 0)
            if len(_coordinates) == 0:
                _coordinates.append(point)
            elif np.sum(np.abs(_coordinates[-1] - point)) != 0:
                _coordinates.append(point)
        if len(_coordinates) == 3:
            if np.sum(np.abs(_coordinates[-1] - _coordinates[0])) == 0:
                raise GeometryError(face, "too few points")
        if len(_coordinates) < 3:
            raise GeometryError(face, "too few points")

        return np.array(_coordinates)

    def invalid(self) -> str | None:
        """
        Check for invalid polygon geometries such as self-intersections.
        
        Parameters
        ----------
        self : object
            The instance containing the geometry data. Must have `__face` and `__holes` 
            attributes, where `__face` is the outer boundary and `__holes` is a list 
            of inner hole coordinates.
        
        Returns
        -------
        str or None
            Returns a string describing the validation error if an invalid condition 
            (e.g., self-intersection) is found; otherwise, returns None if the geometry is valid.
        """
        geos = [self.__face] + self.__holes
        for geo in geos:
            # if not shapely.points(geo[-1]) == shapely.points(geo[0]):
            #             #     return f"not a closed polygon"
            if selfIntersect(shapely.polygons(geo)):
                return f"self-intersect geo"
        # for hole in self.holes:
        #     if not shapely.contains(shapely.polygons(self.boundary), shapely.polygons(hole)):
        #         return "holes outside"
        return None

    def move_points(self, points, displacement, tolerance=geom.POINT_PRECISION) -> bool:
        """Move coordinates matching ``points`` and refresh the face normal."""
        displacement = np.asarray(displacement, dtype=float)
        changed = False
        for coordinates in [self.__face] + self.__holes:
            for index, coordinate in enumerate(coordinates):
                if any(np.all(np.abs(coordinate - point) <= tolerance) for point in points):
                    coordinates[index] = coordinate + displacement
                    changed = True
        if changed:
            self.__normal = Vector(faceNormal(self.face))
        return changed

    @property
    def face(self) -> shapely.Geometry:
        """
        Return the face geometry of the object as a polygon.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the boundary and holes attributes.
        
        Returns
        -------
        shapely.Geometry
            A Shapely geometry representing the polygon formed by the boundary and optional holes.
        """
        holes = self.holes if len(self.__holes) > 0 else None
        return shapely.polygons(geometries=self.boundary, holes=holes)

    @property
    def boundary(self):
        """
        Boundary of the face as a LinearRing.
        
        Returns a LinearRing geometry representing the boundary of the face
        using the shapely.linearrings function applied to the internal face data.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `__face` attribute.
        
        Returns
        -------
        shapely.Geometry
            A LinearRing geometry representing the boundary of the face.
        """
        return shapely.linearrings(self.__face)

    @property
    def normal(self) -> shapely.Geometry:
        """
        Geometry of the normal vector, optionally flipped.
        
        Parameters
        ----------
        self : object
            The instance of the class containing this property. It is expected to have
            attributes `__normal` (with a `geometry` attribute) and `flip` (boolean).
        
        Returns
        -------
        shapely.Geometry
            The geometry of the normal vector. If `self.flip` is True, returns the negated
            normal's geometry; otherwise, returns the original normal's geometry.
        """
        if self.flip:
            return (-self.__normal).geometry
        else:
            return self.__normal.geometry

    @property
    def faceId(self) -> str:
        """
        Get the face ID as a string.
        
        Returns
        -------
        str
            The face ID.
        """
        return self.__faceId

    @property
    def category(self) -> int:
        """
        Category identifier for the surface element.
        
        Returns the category of the surface as an integer, where each value represents 
        a specific type of surface or element (e.g., opaque, translucent, shading, etc.).
        
        Returns
        -------
        int
            The category code:
            - -2: Ignore faces (excluded from calculations)
            - -1: Shading faces (included as shading elements)
            -  0: Opaque surface
            -  1: Translucent surface
            -  2: Air wall
            -  3: Wall element (MoosasWall)
            -  4: Plane element (MoosasFace)
            -  5: Glazing element (MoosasGlazing)
            -  6: Skylight element (MoosasSkylight)
        """
        return self.__category

    def setCategory(self, cat=None) -> None:
        """
        Set the category attribute based on input or predefined conditions.
        
        Parameters
        ----------
        cat : int, optional
            The category value to set. If not provided, the category is determined
            based on the current value of `self.category` using internal rules:
            - If `self.category` is 3, 4, or -1, sets `self.__category` to 0.
            - If `self.category` is greater than or equal to 5, sets `self.__category` to 1.
            Default is None.
        
        Returns
        -------
        None
        """
        if cat is not None:
            self.__category = cat
        else:
            if self.category == 3 or self.category == 4 or self.category == -1:
                self.__category = 0
            if self.category >= 5:
                self.__category = 1

    @property
    def holes(self) -> list[shapely.Geometry]:
        """
        List of hole geometries as linearrings.
        
        Returns a list of hole geometries in the object, converted to linearrings using shapely.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the private attribute __holes.
        
        Returns
        -------
        list of shapely.Geometry
            A list of shapely Geometry objects representing the holes as linearrings.
        """
        return [shapely.linearrings(hole) for hole in self.__holes]

    def getEdgeStr(self) -> list[str]:
        """get a unique edge string of the boundary, ignore the direction of the edge."""
        faces = [self.boundary] + self.holes
        edge_str_s = {}
        for face in faces:
            coors = shapely.get_coordinates(face, include_z=True)

            for i in range(1, len(coors)):
                str1, str2 = '', ''
                for corDim in coors[i]:
                    str1 += f'{int(corDim * 100)}_'
                for corDim in coors[i - 1]:
                    str2 += f'{int(corDim * 100)}_'
                if str1 != str2:
                    if coors[i - 1][0] + coors[i - 1][1] + coors[i - 1][2] > coors[i][0] + coors[i][1] + coors[i][2]:
                        edge_str = f'{str1}{str2}'
                    else:
                        edge_str = f'{str2}{str1}'
                    if edge_str in edge_str_s:
                        edge_str_s[edge_str] = 1
                    else:
                        edge_str_s[edge_str] = 0

        return [edgeStr for edgeStr in edge_str_s if edge_str_s[edgeStr] == 0]


class MoosasElement(object):
    """
    Base class, which expresses all geometry, loads basic methods & basic members
    new feature:
    now the geometry will be created based on _Geometry class to ensure the consistency of id and geometry
    in this case, the model is required to input.
    in the init method we will check the consistency of id and geometry.
    besides, face, faceId and normal object will be properties and get from __geometries

    -------------------------------------------
    init params:
    faceId: geometry faceId in MoosasModel, should be in the list MoosasModel.geoId
    model: MoosasModel

    optional params:
    'level': the building floor level of the geometry
    'offset': the offset from the building level
    'glazingId': a list of glazing faceId
    'space': a list of space id this geometry belongs to

    attribute:
    'parent': The model to which the face belongs
    'isOuter': whether this face is an external faces
    'Uid': unique Id of the element
    'shading': a list of shading elements.
    'U_Value' : U-value of this element
    level,offset,space: those in optional params.

    properties:
    'face': Faces (list) of the loaded geometry
    'normal': a unique face normal
    'faceId': The identification of the face(s), defined by the id of the read geo data
    'category': category of the Geometry, 0==opaque element, 1==transparent element, 2==aperture element
    'area': total face area of the element
    'elevation': level + offset
    'wwr': Window-to-Wall ratio of this element calculated based on UV method
    'firstFaceId': the first faceId of all geometries in this element, to avoid error in some method.
    'glazingId': a list of glazing faceId
    'glazingElement': return MoosasGlazing object instead of glazingId


    -------------------------------------------

    method:
    'area3d': calculate area in 3d projection, for itself or other faces.
    'glazingUV': get UV faces of all glazing elements
    'faceUV': get UV faces of itself
    'getEdgeStr': get unique descriptions in string of all edges in this element
    'getWeightCenter': Gets the weighted center point
    'add_glazing': add glazing Element to the glazingId.

    Conceptual method:
    'force_2d': A conceptual approach to obtaining a two-dimensional representation of geometry on a floor plan
    'representation': get the simplified representation of the geometry
    'dissolve': conceptual method to merge other element
    'to_xml': Conceptual method to get the xml of the geo attribute
    fromDict: construct an element from a dictionary which may be given by toDictionary method from a xmlTree

    """
    __slots__ = ['__geometries', 'level', 'offset', 'Uid', '_u_value', 'settings', 'explicit_settings', '__glazingElement', 'parent', 'neighbor', 'isOuter', 'space',
                 'shading','description']

    def __init__(self, model: MoosasContainer,
                 faceId: str | list[str] | np.ndarray[str] | MoosasGeometry | list[MoosasGeometry] | np.ndarray[
                     MoosasGeometry], level: float = None,
                 offset: float = None,
                 glazingId: str | list[str] | np.ndarray[str] = None,
                 glazingElement: MoosasElement | list[MoosasElement] | np.ndarray[MoosasElement] = None, space=None,
                 uid=None):
        """
        Initialize a new instance with model, geometry, and optional parameters for shading and glazing.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model that holds the geometry and other elements.
        faceId : str or list of str or numpy.ndarray of str or MoosasGeometry or list of MoosasGeometry or numpy.ndarray of MoosasGeometry
            Identifier(s) or geometry object(s) representing faces; if string, must exist in model's geoId.
        level : float, optional
            Elevation level of the face(s), by default None.
        offset : float, optional
            Offset distance from the face(s), by default None.
        glazingId : str or list of str or numpy.ndarray of str, optional
            Identifier(s) for glazing elements to be associated, by default None.
        glazingElement : MoosasElement or list of MoosasElement or numpy.ndarray of MoosasElement, optional
            Predefined glazing element(s) to be associated, by default None.
        space : list of str, optional
            List of space identifiers this object belongs to, by default None.
        uid : str, optional
            Unique identifier for the instance; if not provided, a 6-character code is generated.
        
        Returns
        -------
        None
        """
        self.parent: MoosasContainer = model  # this is MoosasModel !!!
        self.Uid: str = generate_code(6) if uid is None else uid
        self.level: float = level
        self.offset: float = offset
        self.shading = []
        # Element properties are intentionally kept separate from zone settings.
        # The legacy U_Value attribute remains a property backed by this mapping.
        self.settings = {}
        self.explicit_settings = set()
        self._u_value: float = 1.8
        self.U_Value = 1.8
        self.__glazingElement: list[MoosasElement] = mixItemListToList(
            glazingElement) if glazingElement is not None else []
        if glazingId is not None:
            self.__glazingElement += self.glazingElementFromId(glazingId)
        self.space: list[str] = mixItemListToList(space) if space is not None else []
        self.isOuter: bool = True
        self.neighbor = {}
        self.description = ""

        # get the geometry(s)
        faceId = mixItemListToList(faceId)
        self.__geometries: np.ndarray[MoosasGeometry] = np.array([])
        for idd in faceId:
            if isinstance(idd, str):
                try:
                    idd = model.geoId.index(idd)
                except ValueError as ve:
                    raise ValueError(f"index {idd} is not in the library.")
                self.__geometries: np.ndarray[MoosasGeometry] = np.append(self.__geometries, model.geometryList[idd])
            elif isinstance(idd, MoosasGeometry):
                self.__geometries: np.ndarray[MoosasGeometry] = np.append(self.__geometries, idd)
            else:
                raise TypeError("idd must be either a string or a MoosasGeometry")

    @property
    def geometry(self):
        return self.__geometries

    @property
    def U_Value(self):
        return self.settings.get('u_value', self._u_value)

    @U_Value.setter
    def U_Value(self, value):
        self._u_value = value
        if hasattr(self, 'settings'):
            self.settings['u_value'] = value

    @property
    def glazingElement(self) -> list[MoosasGlazing | MoosasSkylight]:
        """protect the __glazingElement attribute"""
        return mixItemListToList(self.__glazingElement)

    @property
    def glazingId(self):
        """get glazingId from glazingElement"""
        glsId = [ids for gls in self.glazingElement for ids in mixItemListToList(gls.Uid)]
        return glsId

    @property
    def face(self) -> shapely.Geometry | np.ndarray[shapely.Geometry]:
        """if the element only contains one face, a pygoes.Geometry will be return
        if you want to get a list anyway,
        you can call mixItemListToList() func in utils.tools.
        """
        return mixItemListToObject([geo.face for geo in self.__geometries])

    @property
    def mergedFace(self) -> shapely.Geometry:
        """return a single face merging all faces contained in this element"""
        if len(self.__geometries) == 1:
            return self.__geometries[0].face
        proj = Projection(origin=np.mean(shapely.get_coordinates(self.face, include_z=True), axis=0), unitZ=self.normal)
        UVFaces = [proj.toUV(g) for g in self.face]
        mergedUVFace = shapely.force_3d(shapely.union_all(shapely.force_2d(UVFaces)), z=0)
        if shapely.is_empty(mergedUVFace):
            return shapely.GeometryCollection()
        return proj.toWorld(mergedUVFace)

    @property
    def holes(self) -> shapely.Geometry | np.ndarray[shapely.Geometry]:
        """
        List of hole geometries from all polygons in the collection.
        
        Returns a flattened list of hole geometries extracted from each polygon 
        in the internal geometries array. Each hole is represented as a shapely Geometry object.
        
        Returns
        -------
        shapely.Geometry or numpy.ndarray of shapely.Geometry
            A list or array containing the hole geometries from all polygons.
        """
        return [h for geo in self.__geometries for h in geo.holes]

    @property
    def normal(self) -> np.ndarray:
        """if the element contains multi faces,
        the normal has the best description of the faces will be returned"""
        if len(self.__geometries) == 1:
            return Vector(self.__geometries[0].normal).unit().array

        # PCA1: get covariance matrix
        coordinates = shapely.get_coordinates([geo.face for geo in self.__geometries], include_z=True) - np.array(
            self.getWeightCenter())
        C = np.zeros((3, 3))
        for coor in coordinates:
            C += np.matmul(coor.reshape(3, 1), coor.reshape(1, 3))
        C /= len(coordinates)

        # PCA2: the covariance matrix is real and symmetric.  ``eig`` may
        # return complex-valued vectors for a nearly singular matrix (notably
        # with newer NumPy versions), which Shapely cannot consume downstream.
        # ``eigh`` guarantees real eigenpairs for this matrix type and stores
        # eigenvectors by *column*.
        eig_values, eig_vectors = np.linalg.eigh(C)
        normal = np.real_if_close(eig_vectors[:, np.argmin(eig_values)])
        if np.iscomplexobj(normal):
            raise GeometryError(self.face, "complex multi-face normal")
        normal = np.asarray(normal, dtype=float)
        length = np.linalg.norm(normal)
        if not np.isfinite(normal).all() or length < geom.POINT_PRECISION:
            raise GeometryError(self.face, "invalid multi-face normal")

        # Eigenvector signs are arbitrary.  Preserve the orientation of the
        # first constituent face so callers get a stable outward normal.
        reference = Vector(self.__geometries[0].normal).unit().array
        if np.dot(normal, reference) < 0:
            normal = -normal
        return normal / length

    @property
    def faceId(self) -> str | np.ndarray[str]:
        """if the element only contains one face, a str will be return
            if you want to get a list anyway,
            you can call mixItemListToList() func in utils.tools.
        """
        return mixItemListToObject([geo.faceId for geo in self.__geometries])

    @property
    def category(self) -> int | np.ndarray[int]:
        """if the element only contains one face, a int will be return
            if you want to get a list anyway,
            you can call mixItemListToList() func in utils.tools.
        """
        return mixItemListToList([geo.category for geo in self.__geometries])[0]

    def setCategory(self, cat=None):
        for idx, geometry in enumerate(self.__geometries):
            self.__geometries[idx].setCategory(cat)

    @property
    def area(self) -> float:
        """quick link to self.area3d"""
        return self.area3d()

    @property
    def elevation(self) -> float:
        """correct elevation of the object"""
        return self.offset + self.level

    @property
    def wwr(self) -> float:
        """
        Calculate the Window-to-Wall Ratio (WWR) based on projected areas.
        
        The WWR is computed as the ratio of glazing area to the total wall surface area,
        using 3D projections onto wall surfaces. The calculation does not use the exact
        surface area of window objects, but rather their projection on the wall.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the method. Expected to have:
            - `glazingElement`: iterable of objects with a `face` attribute representing glazing geometry.
            - `face`: geometric representation of wall faces, possibly nested.
            - `area3d(faces)` method: computes the 3D area of given faces.
        
        Returns
        -------
        float
            The Window-to-Wall Ratio (WWR), defined as the ratio of projected glazing area 
            to the total projected wall surface area.
        """
        """calculate Window Wall Ratio based on self.area3d()
        the wwr is not based on exact surface area of the window object, but based on the projection on the wall surface.
        """
        gFace = []
        for glsFace in self.glazingElement:
            gFace = np.append(gFace, glsFace.face)
        areaGlazing = self.area3d(faces=gFace)
        surface = [shapely.polygons(shapely.get_exterior_ring(f)) for f in mixItemListToList(self.face)]
        areaSurface = self.area3d(faces=surface)
        if areaSurface <= 0:
            return 0.0
        return areaGlazing / areaSurface

    @property
    def firstFaceId(self):
        """
        First face ID from the object.
        
        Returns the first face ID from the object, which can accelerate calculations 
        and prevent errors in contexts where only a single face ID is expected. 
        This is particularly useful for objects containing only one face, such as 
        MoosasGlazing, MoosasSkylight, or MoosasFace, and ensures compatibility 
        with functions like `searchBy` in `utils.tools` that expect a single attribute.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `faceId` attribute. 
            It is expected to have a `faceId` attribute which is either 
            a scalar value or a numpy ndarray.
        
        Returns
        -------
        scalar or int or any
            The first face ID. If `faceId` is not a numpy array, returns `self.faceId`. 
            Otherwise, returns the first element of `self.faceId` (i.e., `self.faceId[0]`).
        """
        """give a single faceid,
        which can accelerate the calculation and avoid error sometimes.
        for example, the MoosasGlazing/MoosasSkylight/MoosasFace object are all contain only one face.
        and the searchBy() func in utils.tools only supports singe attr in searching.
        ***actually searching by list are valid in python 3.10
        """
        if type(self.faceId) != np.ndarray:
            return self.faceId
        else:
            return self.faceId[0]

    def glazingElementFromId(self, glazingIds) -> list[MoosasGlazing | MoosasSkylight]:
        """
        Get glazing elements by their IDs.
        
        Parameters
        ----------
        glazingIds : list or array-like
            List of glazing element IDs (Uid) to search for. Can be a mix of types that is converted to a flat list.
        
        Returns
        -------
        list of MoosasGlazing or MoosasSkylight
            A list of glazing objects (either MoosasGlazing or MoosasSkylight instances) matching the provided IDs.
        """
        """get the glazing object in a ndarray
        the glazing are searchBy firstFaceId, since all glazing only contains one face.
        """
        glsid = mixItemListToList(glazingIds)
        if len(glsid) == 0:
            return []
        gls = []
        gls = np.append(gls,
                        np.array(self.parent.glazingList)[searchBy('Uid', glsid, self.parent.glazingList)])
        gls = np.append(gls,
                        np.array(self.parent.skylightList)[searchBy('Uid', glsid, self.parent.skylightList)])
        return list(gls)

    def replaceGeo(self, geoId):
        """
        Replace the current geometries with new ones based on provided geometry IDs.
        
        Parameters
        ----------
        geoId : int or list of int
            Geometry ID(s) to be used for replacing the current geometries. 
            If a single integer is provided, it will be treated as a list with one element.
        
        Returns
        -------
        None
            This function does not return any value. It modifies the internal `__geometries` attribute in place.
        """
        # get the geometry(s)
        faceId = mixItemListToList(geoId)
        self.__geometries: np.ndarray[MoosasGeometry] = np.array([])
        for idd in faceId:
            try:
                idd = self.parent.geoId.index(idd)
            except:
                raise ValueError(f"index {idd} is not in the library.")
            self.__geometries: np.ndarray[MoosasGeometry] = np.append(self.__geometries, self.parent.geometryList[idd])

    def delete(self):
        """
        Delete all geometries in the object by setting their delete flag to True.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the geometries to be marked for deletion.
            It is expected to have a private attribute `__geometries` which is an iterable
            of geometry objects that each support assignment to a `delete` attribute.
        
        Returns
        -------
        None
            This function does not return any value.
        """
        for geo in self.__geometries:
            geo.delete = True

    def area3d(self, faces=None, project=None) -> float:
        """Measure the projected union so merged coincident faces count once."""
        if project is None:
            trans = Projection(self.getWeightCenter(), self.normal)
        else:
            trans = project
        if faces is None:
            faces = np.array(self.face).flatten()
        # print(self.face)
        # [print(faceNormal(face), self.normal) for face in faces]
        return float(shapely.area(shapely.union_all([trans.toUV(face) for face in faces])))

    def faceUV(self, uniform=False) -> list[shapely.Geometry]:
        """
        Get the UV-projected faces of a surface, optionally normalized to a unit square.
        
        Parameters
        ----------
        uniform : bool, optional
            If True, the UV coordinates are normalized to fit within the unit square [0, 1] 
            based on the bounding box of all faces. Default is False.
        
        Returns
        -------
        list of shapely.Geometry
            A list of 2D geometries representing the UV-projected faces. If `uniform` is True,
            the coordinates are scaled to the unit square; otherwise, they are in raw UV space.
        """
        """Ver1.3 The projection class is added to perform UV expression on the surface and the glass surface
        get the UV faces
        """
        trans = Projection(self.getWeightCenter(), self.normal)
        faces = np.array(self.face).flatten()
        faces = [shapely.force_2d(trans.toUV(face)) for face in faces]
        if uniform:
            boundaryBox = shapely.get_coordinates(faces)
            boundaryBox = [[np.min(boundaryBox.T[0]), np.min(boundaryBox.T[1])],
                           [np.max(boundaryBox.T[0]), np.max(boundaryBox.T[1])]]
            uniformFaces = []
            for face in faces:
                face = shapely.get_coordinates(face)
                for i in range(len(face)):
                    face[i][0] = (face[i][0] - boundaryBox[0][0]) / (boundaryBox[1][0] - boundaryBox[0][0])
                    face[i][1] = (face[i][1] - boundaryBox[0][1]) / (boundaryBox[1][1] - boundaryBox[0][1])
                uniformFaces.append(shapely.polygons(face))

            return uniformFaces
        else:
            return faces

    def glazingUV(self, uniform=False) -> list[shapely.Geometry]:
        """
        Get UV-projected glazing faces from the surface.
        
        Parameters
        ----------
        self : object
            The instance of the class containing glazing elements and geometric data.
        uniform : bool, optional
            If True, normalizes the UV coordinates to the unit square [0, 1] based on the bounding box 
            of the input faces. Default is False.
        
        Returns
        -------
        list of shapely.Geometry
            A list of geometries representing the UV-projected glazing faces. If `uniform` is True, 
            the coordinates are normalized; otherwise, they are in raw UV space.
        """
        """Ver1.3 The projection class is added to perform UV expression on the surface and the glass surface
            get the UV glazing faces
        """
        faces = []
        gidList = []
        for gElement in self.glazingElement:
            gidList += mixItemListToList(gElement.faceId)
        for gid in gidList:
            gface = self.parent.geoFaceList[self.parent.geoId.index(gid)]
            trans = Projection(self.getWeightCenter(), self.normal)
            faces.append(trans.toUV(gface))
        if uniform:
            boundaryBox = shapely.get_coordinates(np.array(self.face).flatten())
            boundaryBox = [[np.min(boundaryBox.T[0]), np.min(boundaryBox.T[1])],
                           [np.max(boundaryBox.T[0]), np.max(boundaryBox.T[1])]]
            uniformFaces = []
            for face in faces:
                face = shapely.get_coordinates(face)
                for i in range(len(face)):
                    face[i][0] = (face[i][0] - boundaryBox[0][0]) / (boundaryBox[1][0] - boundaryBox[0][0])
                    face[i][1] = (face[i][1] - boundaryBox[0][1]) / (boundaryBox[1][1] - boundaryBox[0][1])
                uniformFaces.append(shapely.polygons(face))
            return uniformFaces
        else:
            return faces

    def getEdgeStr(self) -> list[str]:
        """get a unique edge string of the boundary, ignore the direction of the edge."""
        edge_str_s = set()
        for geo in self.__geometries:
            edge_str_s = edge_str_s | set(geo.getEdgeStr())
        return list(edge_str_s)

    def getWeightCenter(self) -> np.ndarray[np.ndarray]:
        """
        Compute the weight center (centroid) of a 3D face.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `face` attribute, which is a geometric object 
            supported by shapely representing a 2D or 3D polygonal face.
        
        Returns
        -------
        np.ndarray
            A 1D numpy array of shape (3,) containing the x, y, and z coordinates of the centroid, 
            computed as the mean of the face's vertex coordinates.
        """
        point_list = shapely.get_coordinates(self.face, include_z=True)[:-1]
        return np.array([np.mean(point_list.T[0]), np.mean(point_list.T[1]), np.mean(point_list.T[2])])

    def add_glazing(self, glazingObject: MoosasGlazing | MoosasSkylight):
        """
        Add a glazing object to the current element.
        
        Parameters
        ----------
        glazingObject : MoosasGlazing or MoosasSkylight
            The glazing object to be added. This object will be appended to the 
            internal list of glazing elements and its parentFace attribute will 
            be set to the current instance.
        
        Returns
        -------
        None
            This function does not return any value.
        """
        self.__glazingElement = list(np.append(self.__glazingElement, glazingObject))
        glazingObject.parentFace = self

    def dissolve(self, others):
        """method to merge multiple elements"""
        if not (isinstance(others, list) or isinstance(others, np.ndarray)):
            others = [others]
        others = list(others)
        target = None
        for o in others:
            # find face with coEdge and merge it
            edgeStr = set(self.getEdgeStr()) & set(o.getEdgeStr())
            if len(edgeStr) > 0:
                target = o
                break
        if target:
            self._merge(target)
            # continue to merge others
            others.remove(target)
            self.dissolve(others)
        else:
            return

    def _merge(self, other):
        """
        Merge another object into this one by aligning face normals and combining geometries.
        
        Parameters
        ----------
        other : object
            Another object to merge with this one. Must have methods `getEdgeStr`, 
            `getWeightCenter`, and attributes `__geometries`, `normal`, `offset`, 
            `level`, and `glazingElement`. The `getEdgeStr` method should return a 
            list of edge strings, and `getWeightCenter` should return the center point.
        
        Returns
        -------
        None
            This function modifies the current instance in place by merging geometries, 
            adjusting offset and level, flipping normals if necessary, and adding 
            glazing elements from the other object.
        """
        """flip all face to the same side normal"""
        edgeStr = list(set(self.getEdgeStr()) & set(other.getEdgeStr()))[0]
        edgeStr = np.array([int(dim) / 100 for dim in edgeStr.split('_')[:-1]])

        poi1, poi2 = edgeStr[:3], edgeStr[3:]
        trans = Projection(poi1, poi2 - poi1)
        vectors = [
            self.getWeightCenter(),
            other.getWeightCenter(),
            self.normal,
            other.normal
        ]
        vectors = [trans.toUV(Vector(v).geometry) for v in vectors]
        unitxSelf, unitxOther, unitySelf, unityOther = vectors
        if Vector.dot(Vector.cross(unitxSelf, unitySelf), Vector.cross(unitxOther, unityOther)) > 0:
            for g in range(len(other.__geometries)):
                other.__geometries[g].flip = True

        """method to merge one other elements"""
        self.__geometries = np.append(self.__geometries, other.__geometries)
        self.offset = min(self.offset + self.level, other.offset + other.level) - min(self.level, other.level)
        self.level = min(self.level, other.level)
        for gls in other.glazingElement:
            self.add_glazing(gls)

    def force_2d(self) -> shapely.Geometry:
        """return a linestring formatted in shapely,or an array vector object"""
        raise NotImplementedError("force_2d method should be implemented in child class")

    def representation(self) -> shapely.Geometry:
        """return a simplified representation for the geometry"""
        raise NotImplementedError("representation method should be implemented in child class")

    @classmethod
    def fromDict(cls, elementDict, model: MoosasContainer):
        """construct an element from a dictionary
        if the faceId record in the dictionary is already occurred in the model,
        the MoosasElement contains that faceId will be returned directly.
        """
        faceId = _getElement('faceId', dictionary=elementDict)[0]
        if not hasattr(model.builtData, 'elements') or not hasattr(model.builtData, 'glazing'):
            model.update()
        if faceId in model.builtData.elements:
            return model.builtData.elements[faceId]
        else:
            element = cls(
                faceId=_getElement('faceId', dictionary=elementDict)[0].split(" "),
                model=model,
                level=_getElement('level', dictionary=elementDict, strict=False)[0],
                offset=_getElement('offset', dictionary=elementDict, strict=False)[0],
                space=_getElement('space', dictionary=elementDict, strict=False)[0],
            )
            for ids in _getElement('faceId', dictionary=elementDict)[0].split(" "):
                model.builtData.elements[ids] = element
            glazingId: str = _getElement('glazingId', dictionary=elementDict, strict=False)[0]
            if glazingId is not None:
                for ids in glazingId.split(" "):
                    if ids not in model.builtData.glazing:
                        glsGeometry: MoosasGeometry = model.findFace(ids)[0]
                        if np.abs(Vector.dot(glsGeometry.normal, Vector([0, 0, 1]))) >= geom.HORIZONTAL_ANGLE_THRESHOLD:
                            glsElement = MoosasSkylight(model, glsGeometry.faceId)
                            model.skylightList.append(glsElement)
                        else:
                            glsElement = MoosasGlazing(model, glsGeometry.faceId)
                            model.glazingList.append(glsElement)
                        element.add_glazing(glsElement)
                        model.builtData.glazing[ids] = glsElement
                    else:
                        element.add_glazing(model.builtData.glazing[ids])

    def to_xml(self, model: MoosasContainer, element_tag='geometry', writeGeometry=False) -> ET.Element:
        """
        Convert the MoosasFace object to an XML element representation.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model associated with the geometry.
        element_tag : str, optional
            The tag name for the root XML element (default is 'geometry').
        writeGeometry : bool, optional
            If True, includes the detailed geometric coordinates in the XML output (default is False).
        
        Returns
        -------
        xml.etree.ElementTree.Element
            An XML Element representing the MoosasFace object with attributes such as Uid, faceId,
            level, offset, area, glazingId, height, normal, external, space, and neighbor edges.
            Optionally includes geometric point data if writeGeometry is True.
        """
        """get a dictionary of all information we get for this object.
        it can be translated to json by toDictionary() func in the uitls package
        """
        geometry = ET.Element(element_tag)
        idList = np.array(self.faceId).flatten()
        glsIdList = np.array(self.glazingId).flatten()
        spcList = np.array(self.space).flatten()
        '''
            face, faceId, normal, level=None, offset=None, area=None, model = None, glazingId = None
            'face', 'level', 'offset', 'area', 'faceId', 'Uid', 'normal','glazingId','parent','isOuter','space'
        '''
        ET.SubElement(geometry, "Uid").text = str(self.Uid)
        ET.SubElement(geometry, "faceId").text = ' '.join(idList.astype(str))
        ET.SubElement(geometry, "level").text = str(self.level)
        ET.SubElement(geometry, "offset").text = str(self.offset)
        ET.SubElement(geometry, "area").text = str((self.area) / INCH_METER_MULTIPLIER_SQR)
        ET.SubElement(geometry, "glazingId").text = ' '.join(glsIdList.astype(str))
        ET.SubElement(geometry, "height").text = str((self.level + self.offset) / INCH_METER_MULTIPLIER)
        ET.SubElement(geometry, "normal").text = ' '.join(Vector(self.normal).array.astype(str))
        ET.SubElement(geometry, "external").text = str(self.isOuter)
        ET.SubElement(geometry, "U_Value").text = str(self.U_Value)
        ET.SubElement(geometry, "parentSpace").text = str(spcList.astype(str))
        neighbor = ET.SubElement(geometry, "neighbor")
        for key in self.neighbor:
            obj = ET.SubElement(neighbor, "edge")
            obj.set("key", key)
            obj.text = str(' '.join(self.neighbor[key]))
        if writeGeometry:
            geo = ET.SubElement(geometry, "geometries")
            for pts in shapely.get_coordinates(self.face, include_z=True).astype(str):
                ET.SubElement(geo, "pt").text = ' '.join(pts)

        return geometry


class MoosasFace(MoosasElement):
    """
    The base class, which records the horizontal face
    since we also have MoosasFloor to record multi-surface,
    we strictly require the face attribute only contain one geometry
    """
    __slots__ = ['parentFloors']

    def __init__(self, model: MoosasContainer, faceId: str | MoosasGeometry, level: float = None,
                 offset: float = None, glazingId=None,
                 glazingElement: MoosasElement | list[MoosasElement] | np.ndarray[MoosasElement] = None, space=None,
                 uid=None):
        """
        Initialize a MoosasFace object with geometric and structural properties.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model to which the face belongs.
        faceId : str or MoosasGeometry
            Identifier or geometry object representing the face. Must not be a list or array.
        level : float, optional
            The level (elevation) of the face. If not provided, inferred from geometry.
        offset : float, optional
            Vertical offset of the face relative to its level. Calculated if not provided.
        glazingId : Any, optional
            Identifier for glazing associated with the face. Default is None.
        glazingElement : MoosasElement or list[MoosasElement] or np.ndarray[MoosasElement], optional
            Glazing element(s) attached to the face. Default is None.
        space : Any, optional
            Spatial context or zone to which the face belongs. Default is None.
        uid : str, optional
            Unique identifier for the face. Generated if not provided.
        
        Returns
        -------
        None
            This constructor does not return a value.
        """
        if isinstance(faceId, list) or isinstance(faceId, np.ndarray):
            raise ValueError("MoosasFace should only contain one geometry")
        if isinstance(faceId, MoosasGeometry):
            uid = f"face_{faceId.faceId}" if uid is None else uid
        else:
            uid = f"face_{mixItemListToList(faceId)[0]}" if uid is None else uid
        super(MoosasFace, self).__init__(model, faceId, level=level, offset=offset, glazingElement=glazingElement,
                                         space=space, glazingId=glazingId, uid=uid)
        self.parentFloors: list[MoosasFloor] = []
        coordinates_z = shapely.get_coordinates(self.face, include_z=True)[:, 2]
        inclined = abs(float(self.normal[2])) < 0.99
        reference_height = float(np.round(
            np.min(coordinates_z) if inclined else np.mean(coordinates_z), 3
        ))
        if not np.isfinite(reference_height):
            raise GeometryError(self.face, "invalid geometry")

        if self.level is None:
            nearest = min(model.levelList, key=lambda z: (abs(z - reference_height), z)) if model.levelList else None
            tolerance = geom.LEVEL_MAX_OFFSET if inclined else geom.POINT_PRECISION
            frozen = getattr(model, "_levels_frozen", False)
            if nearest is not None and (abs(nearest - reference_height) <= tolerance + 1e-9 or (frozen and not inclined)):
                self.level = nearest
            elif frozen:
                raise GeometryError(self.face, "inclined face has no storey within LEVEL_MAX_OFFSET")
            else:
                self.level = reference_height
                model.levelList.append(self.level)
                model.levelList.sort()
        if offset is None:
            self.offset = reference_height - self.level

    @classmethod
    def fromDict(cls, elementDict, model: MoosasContainer):
        """
        Create a MoosasFace instance from a dictionary representation.
        
        Parameters
        ----------
        elementDict : dict
            Dictionary containing the element data.
        model : MoosasContainer
            Model container that holds the built data and manages elements.
        
        Returns
        -------
        MoosasFace
            An instance of MoosasFace initialized with data from elementDict and associated with the given model.
        """
        element = super(MoosasFace, cls).fromDict(elementDict, model)
        faceElement = cls(model, element.faceId, element.glazingId)
        for fid in mixItemListToList(element.faceId):
            model.builtData.elements[fid] = faceElement
        return faceElement

    def force_2d(self, region=True) -> shapely.Geometry:
        """
        Force the geometry into 2 dimensions.
        
        Parameters
        ----------
        region : bool, optional
            Placeholder argument for consistency; has no effect on the operation.
        
        Returns
        -------
        shapely.Geometry
            The input geometry converted to 2D.
        """
        # region is an useless arg to ensure consistency

        footprint = shapely.force_2d(self.face)
        if not shapely.is_valid(footprint):
            # Coordinate snapping can leave a backtracking edge or split a
            # ring at a touching vertex. Keep every repaired polygonal part;
            # zero-area lines must not enter floor-area overlay operations.
            repaired = shapely.make_valid(footprint)
            parts = [part for part in shapely.get_parts(repaired)
                     if shapely.get_dimensions(part) == 2]
            footprint = shapely.union_all(parts)
        return footprint

    def to_xml(self, model: MoosasContainer, Element_tag='face', writeGeometry=False):
        """
        Convert the MoosasFace object to an XML representation.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model containing the face data to be converted.
        Element_tag : str, optional
            The XML tag name for the element, default is 'face'.
        writeGeometry : bool, optional
            If True, includes geometry information in the XML output; default is False.
        
        Returns
        -------
        face_xml : Element
            The XML element representing the face, possibly including geometry based on writeGeometry flag.
        """
        face_xml = super(MoosasFace, self).to_xml(model, Element_tag, writeGeometry=writeGeometry)

        return face_xml

    def dissolve(self, wall):
        """
        Dissolves the specified wall from the structure.
        
        Parameters
        ----------
        self : object
            The instance of the class calling this method.
        wall : str or int
            Identifier for the wall to be dissolved. Can be a name (string) or index (integer).
        
        Returns
        -------
        None
            This function does not return any value but raises an exception when called.
        """
        raise Exception("MoosasFace cannot used to dissolve")

    def representation(self) -> shapely.Geometry:
        """
        Return a 3D geometric representation of the object with specified elevation.
        
        Parameters
        ----------
        self : object
            The instance of the class containing `force_2d` and `elevation` attributes.
            Must have methods `force_2d()` and attribute `elevation` defined.
        
        Returns
        -------
        shapely.Geometry
            A 3D geometry created by converting the 2D forced geometry to 3D using the given elevation.
        """
        return shapely.force_3d(self.force_2d(), z=self.elevation)


class MoosasSkylight(MoosasFace):
    '''
        一个特别简单的glazing类，只为与Moosasface区分开
        '''
    __slots__ = ['parentFace']

    def __init__(self, model: MoosasContainer, faceId: str | MoosasGeometry, level: float = None,
                 offset: float = None, glazingId=None,
                 glazingElement: MoosasElement | list[MoosasElement] | np.ndarray[MoosasElement] = None, space=None,
                 uid=None, operable: float = 0.5):
        """
        Initialize a MoosasSkylight object.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model to which the skylight belongs.
        faceId : str or MoosasGeometry
            Identifier or geometry object representing the skylight's face. Must not be a list.
        level : float, optional
            Elevation level of the skylight. Default is None.
        offset : float, optional
            Vertical offset from the base level. Default is None.
        glazingId : object, optional
            Identifier for the glazing material or component. Default is None.
        glazingElement : MoosasElement or list of MoosasElement or np.ndarray of MoosasElement, optional
            Glazing element(s) associated with the skylight. Default is None.
        space : object, optional
            Space to which the skylight belongs. Default is None.
        uid : str, optional
            Unique identifier for the skylight. If not provided, it is generated based on `faceId`.
        
        Returns
        -------
        None
        """
        if isinstance(faceId, list):
            raise ValueError("MoosasFace should only contain one geometry")
        if isinstance(faceId, MoosasGeometry):
            uid = f"sky_{faceId.faceId}" if uid is None else uid
        else:
            uid = f"sky_{mixItemListToList(faceId)[0]}" if uid is None else uid
        super(MoosasSkylight, self).__init__(model, faceId, level=level, offset=offset, glazingElement=glazingElement,
                                             space=space, glazingId=glazingId, uid=uid)
        self.parentFace: MoosasFace | None = None
        self.SHGC = None
        self.operable = operable

    @property
    def SHGC(self):
        return self.settings['shgc']

    @SHGC.setter
    def SHGC(self, value):
        self.settings['shgc'] = value

    @property
    def operable(self):
        return self.settings['operable']

    @operable.setter
    def operable(self, value):
        self.settings['operable'] = value

    @property
    def orientation(self):
        """
        Return the orientation vector based on the normal.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `normal` attribute.
        
        Returns
        -------
        Vector
            A Vector object created from the `normal` attribute of the instance.
        """
        return Vector(self.normal)

    def apply_to_face(self, face: MoosasFace):
        """
        Apply the glazing to a given face.
        
        Parameters
        ----------
        face : MoosasFace
            The face object to which the glazing will be added.
        
        Returns
        -------
        None
            This function does not return any value.
        """
        face.add_glazing(self)
        # self.parentFace = face
        # face.glazingId.append(self.Uid)

    def to_xml(self, model: MoosasContainer, Element_tag='skylight', writeGeometry=False):
        """Convert the MoosasSkylight object to an XML element representation.
        
                Parameters
                ----------
                model : MoosasContainer
                    The container model to which the skylight belongs.
                Element_tag : str, optional
                    The tag name for the XML element, default is 'skylight'.
                writeGeometry : bool, optional
                    If True, geometry information will be included in the XML output, default is False.
        
                Returns
                -------
                xml_element : xml.etree.ElementTree.Element
                    The XML element representing the skylight, with parent face UID and shading ID as sub-elements.
                """
        skylightXml = super(MoosasSkylight, self).to_xml(model, Element_tag, writeGeometry=writeGeometry)
        ET.SubElement(skylightXml, "parentFace").text = str(self.parentFace.Uid)
        ET.SubElement(skylightXml, "shadingId").text = ' '.join(
            np.array([shade.Uid for shade in self.shading]).astype(str)
        )
        ET.SubElement(skylightXml, "SHGC").text = "" if self.SHGC is None else str(self.SHGC)
        return skylightXml


class MoosasWall(MoosasElement):
    """
    The basic class, which expresses the read vertical face, has the following new members:
        Bottom Data (Unique):
        '__botProjection': The bottom projection line of the wall, which will be used to identify the closed area, represented by a sequence of dots, is automatically generated
        Top Data (Unique):
        '__topProjection': The projection line on the top surface of the wall, represented by a sequence of dots, is automatically generated
        'toplevel': The top elevation of the wall, defined by the floor, with an elevation difference greater than 1.5 meters, is automatically generated
        'topoffset': The elevation offset of the top surface of the wall, which is less than plus or minus 1.5 meters, is automatically generated
    """
    __slots__ = ['__botProjection', '__topProjection', 'toplevel', 'topoffset', 'orientation']

    def __init__(self, model: MoosasContainer, faceId: str | list[str] | np.ndarray[str], level: float = None,
                 offset: float = None, glazingId=None,
                 glazingElement: MoosasElement | list[MoosasElement] | np.ndarray[MoosasElement] = None, space=None,
                 uid=None):
        """
        Initialize a MoosasWall instance with geometric and spatial properties.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model that holds the wall and associated level information.
        faceId : str or list of str or numpy.ndarray of str
            Identifier(s) for the face(s) representing the wall geometry.
        level : float, optional
            The base level (elevation) of the wall. If not provided, inferred from geometry and model levels.
        offset : float, optional
            Vertical offset from the base level. If not provided, calculated from geometry.
        glazingId : object, optional
            Identifier for glazing elements associated with the wall. Default is None.
        glazingElement : MoosasElement or list of MoosasElement or numpy.ndarray of MoosasElement, optional
            Glazing element(s) attached to the wall. Default is None.
        space : object, optional
            Spatial context or enclosure to which the wall belongs. Default is None.
        uid : str, optional
            Unique identifier for the wall. If not provided, generated based on faceId.
        
        Returns
        -------
        None
        """
        if isinstance(faceId, MoosasGeometry):
            uid = f"wall_{faceId.faceId}" if uid is None else uid
        else:
            uid = f"wall_{mixItemListToList(faceId)[0]}" if uid is None else uid
        super(MoosasWall, self).__init__(model, faceId, level=level, offset=offset, glazingElement=glazingElement,
                                         space=space, glazingId=glazingId, uid=uid)
        pointlist = shapely.get_coordinates(self.face, include_z=True)
        self.toplevel = None
        self.isOuter = True
        self.orientation: Vector = Vector(self.normal)

        # find the bottom height of the wall
        coordinates_z = pointlist[:, 2]
        botheight = np.round(np.min(coordinates_z), 3)
        topheight = np.round(np.max(coordinates_z), 3)

        for i in range(len(model.levelList) - 1):
            # best match: the wall located between two level
            if model.levelList[i] <= botheight and model.levelList[i + 1] >= topheight:
                self.level = model.levelList[i]
                self.toplevel = model.levelList[i + 1]
                break
            # second match: the wall cross a level
            if botheight <= model.levelList[i] <= topheight:
                if model.levelList[i] - botheight < topheight - model.levelList[i]:
                    self.level = model.levelList[i]
                    self.toplevel = model.levelList[i + 1]
                else:
                    self.toplevel = model.levelList[i]
                    if i == 0:
                        self.level = model.levelList[i]
                    else:
                        self.level = model.levelList[i - 1]
                break

        # Worst match: the wall lies below/above the known levels. A level may
        # already have been supplied by RDF while its matching top level was
        # not, so resolve the two values independently before computing offsets.
        if self.level is None:
            if topheight <= model.levelList[0]:
                self.level = model.levelList[0]
            else:
                self.level = model.levelList[-1]
        if self.toplevel is None:
            # Preserve the real upper geometry when no building level brackets
            # the wall. This allows partially specified RDF to be exported to
            # IDF instead of failing with ``topheight - None``.
            self.toplevel = topheight

        self.offset = botheight - self.level
        self.topoffset = topheight - self.toplevel

        # prepare the projection for the force_2d method
        self.prepareProjection()

    @classmethod
    def fromDict(cls, elementDict, model: MoosasContainer):
        """
        Create a MoosasWall instance from a dictionary representation.
        
        Parameters
        ----------
        elementDict : dict
            Dictionary containing the element's data.
        model : MoosasContainer
            Model container to which the element belongs.
        
        Returns
        -------
        MoosasWall
            A new MoosasWall instance initialized from the provided dictionary and model.
        """
        element = super(MoosasWall, cls).fromDict(elementDict, model)
        faceElement = cls(model, element.faceId, element.glazingId)
        for fid in mixItemListToList(element.faceId):
            model.builtData.elements[fid] = faceElement
        return faceElement

    @classmethod
    def fromProjection(cls, prjLine: shapely.Geometry, bottom: float, top: float, model: MoosasContainer,
                       airBoundary=False):
        """
        Create a wall or glazing object from a 2D projection line and elevation bounds.
        
        Parameters
        ----------
        prjLine : shapely.Geometry
            A 2D line geometry representing the projection of the wall.
        bottom : float
            The bottom elevation (z-coordinate) of the wall.
        top : float
            The top elevation (z-coordinate) of the wall.
        model : MoosasContainer
            The model container to which the geometry will be added.
        airBoundary : bool, optional
            If True, creates a category-2 air-boundary wall; otherwise creates an opaque wall.
        
        Returns
        -------
        wall : cls
            An instance of the class (e.g., Wall) created from the projected geometry and added to the model.
        """
        stPoint, edPoint = shapely.get_coordinates(prjLine)
        airBound = [
            np.append(stPoint, bottom),
            np.append(edPoint, bottom),
            np.append(edPoint, top),
            np.append(stPoint, top),
            np.append(stPoint, bottom),
        ]

        idx = model.includeGeo(shapely.polygons(airBound), cat=2 if airBoundary else 0)
        return cls(model, idx)

    @property
    def is_air_boundary(self) -> bool:
        """Whether this wall is a virtual air boundary instead of an opaque wall."""
        return self.category == 2

    @classmethod
    def break_(cls, wall: MoosasWall, breakPoints: list[shapely.Geometry] | shapely.Geometry):
        """
        Break a wall into multiple segments at specified break points.
        
        Parameters
        ----------
        cls : type
            The class instance (used as part of a classmethod).
        wall : MoosasWall
            The wall object to be broken into segments. Must have 2D geometry and level/top-level attributes.
        breakPoints : list[shapely.Geometry] or shapely.Geometry
            A single point or a list of shapely geometry points where the wall should be broken.
        
        Returns
        -------
        list
            A list of new wall objects (type determined by `cls`) created by breaking the original wall at the specified points.
            If insufficient break points are provided, returns a list containing the original unbroken wall.
        """
        twins = shapely.get_coordinates(wall.force_2d())
        if len(twins) < 2:
            return [wall]
        bottom = wall.level + wall.offset
        top = wall.toplevel + wall.topoffset
        unit = Vector(twins[1] - twins[0]).unit()
        breakPoints = np.array([breakPoints]).flatten()
        breakPoints = np.append(shapely.points(twins), breakPoints)
        for i, bp in enumerate(breakPoints):
            vecBp = Vector(shapely.get_coordinates(bp)[0] - twins[0])
            breakPoints[i] = Vector(unit * Vector.dot(vecBp, unit) + Vector(twins[0])).geometry
        # breakPoints = [breakP for breakP in breakPoints if shapely.contains(wall.force_2d(), breakP)]
        breakPoints = shapely.force_2d(breakPoints)

        coor = shapely.get_coordinates(breakPoints)
        argIdx = np.lexsort((coor[:, 0], coor[:, 1]))
        st, ed = list(argIdx).index(0), list(argIdx).index(1)
        argIdx = argIdx[min(st, ed):max(st, ed) + 1]
        breakPoints = breakPoints[argIdx]

        if len(breakPoints) < 3:
            # dont need to break
            return [wall]
        # print("\nbreakFunction",cls)
        newWalls = cls.fromSeriesPoint(breakPoints, bottom, top, wall.glazingElement, wall.parent)
        for descendant in newWalls:
            descendant.settings = dict(wall.settings)
            descendant.explicit_settings = set(wall.explicit_settings)
            descendant.setCategory(wall.category)
            descendant.space = list(wall.space)
            descendant.isOuter = wall.isOuter
            descendant.shading = list(wall.shading)
            descendant.description = wall.description
        return newWalls

    @classmethod
    def fromSeriesPoint(cls, breakPoints: list[shapely.Geometry] | shapely.Geometry, bottom: float, top: float,
                        gls: list[MoosasGlazing], model: MoosasContainer) -> list[MoosasWall]:
        """
        Partition walls based on break points and reassign glazing elements.
        
        Parameters
        ----------
        breakPoints : list[shapely.Geometry] or shapely.Geometry
            A geometry or list of geometries representing the points where walls are to be split.
        bottom : float
            The bottom elevation for the generated wall segments.
        top : float
            The top elevation for the generated wall segments.
        gls : list[MoosasGlazing]
            List of glazing elements to be reassigned to the new wall segments after partitioning.
        model : MoosasContainer
            The model container that holds the glazing list and other contextual data.
        
        Returns
        -------
        list[MoosasWall]
            A list of newly created wall segments formed by partitioning at the given break points,
            with glazing elements appropriately reassigned based on spatial containment.
        """
        """partition the walls by sorting their coordinates and making polygon using the top and bottom boundaries
        the glazing of all walls will be collected and try to attach to the new wall again.
        """
        coor = list(shapely.get_coordinates(breakPoints))
        coor.sort(key=lambda x: (x[0], x[1]))

        wallNew: list[MoosasWall] = []
        for thisPoi, nextPoi in zip(coor[:-1], coor[1:]):
            if Vector(thisPoi - nextPoi).length() > geom.POINT_PRECISION:
                edges = shapely.linestrings([thisPoi, nextPoi])
                wallNew.append(cls.fromProjection(edges, bottom, top, model))

        inherited_glazing = []
        for glazing in gls:
            split_glazing = MoosasGlazing.break_(glazing, breakPoints)
            inherited_glazing.extend(split_glazing)

        for glazing in inherited_glazing:
            if glazing is not None:
                if not glazing in model.glazingList:
                    model.glazingList = list(np.append(model.glazingList, glazing))

                # Prefer the split descendants of the original parent wall first.
                parent = attach_glazing_to_candidates(glazing, wallNew)
                if parent is not None:
                    continue

                # Fall back to the rest of the model only if the inherited descendants fail.
                other_walls = [wall for wall in mixItemListToList(model.wallList) if wall not in wallNew]
                attach_glazing_to_candidates(glazing, other_walls)

        return wallNew

    @property
    def height(self):
        """
        Height of the element including glazing elements.
        
        Calculates the total height by finding the difference between the maximum top level 
        (including top offset and toplevel) and the minimum bottom level (including offset and level) 
        across the main element and all associated glazing elements.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the height property. Must have attributes
            `toplevel`, `topoffset`, `level`, `offset`, and `glazingElement`. The `glazingElement`
            attribute should be an iterable of objects with `toplevel`, `topoffset`, `level`, and `offset` attributes.
        
        Returns
        -------
        float or int
            The calculated height as the difference between the highest top and lowest bottom position.
        """
        top = [self.toplevel + self.topoffset] + [g.toplevel + g.topoffset for g in self.glazingElement]
        bot = [self.level + self.offset] + [g.level + g.offset for g in self.glazingElement]
        return np.max(top) - np.min(bot)

    def prepareProjection(self):
        """
        Prepare top and bottom projections of the face geometry.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the face attribute and methods.
            Must have a `face` attribute accessible via `self.face` that represents
            a geometry object compatible with shapely, and a `geom.POINT_PRECISION`
            constant for precision control.
        
        Returns
        -------
        None
            This function does not return any value. It modifies the instance attributes
            `__botProjection` and `__topProjection` by setting them to lists of projected
            2D points (with Z-coordinate filtered) at specified precision.
        """
        pointlist = shapely.get_coordinates(self.face, include_z=True)
        bottom = np.min(pointlist[:, 2])
        above = np.max(pointlist[:, 2])
        self.__botProjection = []
        for _point in pointlist:
            if np.abs(_point[2] - bottom) < geom.POINT_PRECISION:
                self.__botProjection.append(shapely.set_precision(shapely.points(_point), geom.POINT_PRECISION))
        self.__topProjection = []
        for _point in pointlist:
            if np.abs(_point[2] - above) < geom.POINT_PRECISION:
                self.__topProjection.append(shapely.set_precision(shapely.points(_point), geom.POINT_PRECISION))

    # conceptual method in the based class
    def force_2d(self, top=False, region=False) -> shapely.Geometry | None:
        """
        Project the 3D geometry into a 2D representation based on top or bottom projections.
        
        Parameters
        ----------
        top : bool
            If True, use the top projection of the geometry; otherwise, use the bottom projection.
        region : bool
            If True, combine top and bottom projections to form a 2D region (e.g., polygon or closed line);
            if False, return the 2D representation of the specified projection (top or bottom).
        
        Returns
        -------
        shapely.Geometry or None
            A 2D geometric object representing the projected line, point, or polygon;
            returns None if the projection cannot be computed.
        """
        if region:
            lBot, lTop = self.force_2d(False, False), self.force_2d(True, False)
            if not shapely.disjoint(lBot, lTop):
                return lBot
            lBot = shapely.get_coordinates(lBot).tolist()
            lTop = shapely.get_coordinates(lTop).tolist()
            if len(lTop) == 1 and len(lBot) == 1:
                return shapely.linestrings(list(lBot) + list(lTop))
            lTop.reverse()
            lbound = list(lBot) + list(lTop)
            for i in range(2, len(lbound)):
                # detect the co-linear projections
                if not Vector.parallel(Vector(Vector(lbound[1]) - Vector(lbound[0])),
                                       Vector(Vector(lbound[i]) - Vector(lbound[0]))):
                    return simplify(shapely.polygons(lbound + [lbound[0]]))
            return shapely.linestrings([lbound[0], lbound[-1]])

        else:
            if top:
                target = self.__topProjection
            else:
                target = self.__botProjection

            # invalid projection, try to use the bottom projection
            if len(target) < 2:
                if top:
                    return self.force_2d(False, False)
                else:
                    return target[0]
            botx = np.array([shapely.get_x(poi) for poi in target])
            boty = np.array([shapely.get_y(poi) for poi in target])
            if np.max(botx) == np.min(botx):
                p1 = np.array([botx[np.argmin(boty)], boty[np.argmin(boty)]])
                p2 = np.array([botx[np.argmax(boty)], boty[np.argmax(boty)]])
            else:
                p1 = np.array([botx[np.argmin(botx)], boty[np.argmin(botx)]])
                p2 = np.array([botx[np.argmax(botx)], boty[np.argmax(botx)]])
            if np.linalg.norm(p1 - p2) >= geom.POINT_PRECISION:
                return shapely.linestrings([p1, p2])
            else:
                return shapely.points(p1)

    def to_xml(self, model: MoosasContainer, Element_tag='wall', writeGeometry=False):
        """
        Convert the MoosasWall object to an XML element representation.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model to which the XML element will be added.
        Element_tag : str, optional
            The tag name for the XML element (default is 'wall').
        writeGeometry : bool, optional
            If True, geometry information is included in the XML (default is False).
        
        Returns
        -------
        xml.etree.ElementTree.Element
            The XML element representing the wall, with attributes such as length, 
            force2d coordinates, toplevel, and topoffset converted to inches.
        """
        wall = super(MoosasWall, self).to_xml(model, Element_tag, writeGeometry=writeGeometry)
        'faces, faceId, normal, glazingId=None, _area=None'
        ET.SubElement(wall, 'length').text = str(shapely.length(self.force_2d()) / INCH_METER_MULTIPLIER)
        ET.SubElement(wall, 'force2d').text = str(
            shapely.get_coordinates(self.force_2d(), include_z=False) / INCH_METER_MULTIPLIER)
        ET.SubElement(wall, 'toplevel').text = str(self.toplevel)
        ET.SubElement(wall, 'topoffset').text = str(self.topoffset)

        return wall

    def dissolve(self, wall: MoosasWall | list[MoosasWall]):
        """
        Merge the current MoosasWall with one or more other MoosasWall objects into a single entity.
        
        Parameters
        ----------
        wall : MoosasWall or list of MoosasWall
            The wall or walls to be merged with the current instance. If a single MoosasWall is provided,
            it is converted into a list for uniform processing.
        
        Returns
        -------
        None
            This function does not return any value.
        """
        if isinstance(wall, MoosasWall):
            wall = [wall]
        if len(wall) == 0:
            return
        """merge two faces into one MoosasWall object"""
        super().dissolve(wall)
        toplevel = max(self.toplevel, np.max([w.toplevel for w in wall]))
        self.topoffset = max(self.topoffset + self.toplevel,
                             np.max([w.topoffset for w in wall]) + np.max([w.toplevel for w in wall])) - toplevel
        self.toplevel = toplevel
        self.prepareProjection()

    def representation(self) -> shapely.Geometry:
        """
        Return a 3D polygon representation of the glazing element.
        
        Parameters
        ----------
        self : MoosasGlazing
            The instance of MoosasGlazing containing geometric and level data for generating the 3D polygon.
            Must have methods `force_2d`, `level`, `offset`, `toplevel`, and `topoffset`.
        
        Returns
        -------
        shapely.Geometry
            A 3D shapely polygon representing the vertical extrusion of the glazing element,
            constructed from bottom and top boundary coordinates.
        """
        lBot = shapely.force_3d(self.force_2d(False, False), z=self.level + self.offset)
        lTop = shapely.force_3d(self.force_2d(False, False), z=self.toplevel + self.topoffset)
        # lTop = shapely.force_3d(self.force_2d(True, False), z=self.toplevel + self.topoffset)

        lBot = shapely.get_coordinates(lBot, include_z=True).tolist()
        lTop = shapely.get_coordinates(lTop, include_z=True).tolist()
        lTop.reverse()

        return shapely.polygons(list(lBot) + list(lTop) + [lBot[0]])


class MoosasGlazing(MoosasWall):
    """
    glazing element based on MoosasWall.
    this element should only contain one geometry.

    attribute:
    parentFace: the Uid of parent MoosasWall element
    orientation: normal facing outside.
    """
    __slots__ = ['parentFace', '_shgc', '_operable']

    def __init__(self, model: MoosasContainer, faceId: str | list[str] | np.ndarray[str], level: float = None,
                 offset: float = None, glazingId=None,
                 glazingElement: MoosasElement | list[MoosasElement] | np.ndarray[MoosasElement] = None, space=None,
                 operable=0.5,
                 uid=None):
        """
        Initialize a MoosasGlazing object with geometric and structural properties.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model to which the glazing belongs.
        faceId : str or list of str or numpy.ndarray of str
            Identifier(s) for the associated face(s). If a MoosasGeometry object is passed,
            its faceId is used.
        level : float, optional
            Elevation level of the bottom of the glazing. If not provided, defaults to None.
        offset : float, optional
            Vertical offset from the base level. If not provided, defaults to None.
        glazingId : any, optional
            User-defined identifier for the glazing element. Defaults to None.
        glazingElement : MoosasElement or list of MoosasElement or numpy.ndarray of MoosasElement, optional
            The glazing element(s) defining the geometry and properties. Defaults to None.
        space : any, optional
            Associated space for the glazing. Defaults to None.
        uid : str, optional
            Unique identifier for the glazing. If not provided, it is generated based on faceId.
        
        Returns
        -------
        None
            This constructor does not return a value.
        """
        if isinstance(faceId, MoosasGeometry):
            uid = f"gls_{faceId.faceId}" if uid is None else uid
        else:
            uid = f"gls_{mixItemListToList(faceId)[0]}" if uid is None else uid
        super(MoosasGlazing, self).__init__(model, faceId, level=level, offset=offset, glazingElement=glazingElement,
                                            space=space, glazingId=glazingId, uid=uid)
        self.parentFace: MoosasWall | None = None
        self._shgc: float | None = None
        self._operable: float = operable
        self.SHGC = None
        self.operable = operable
        if self.offset < -0.2:
            new_level = model.levelList[model.levelList.index(self.level) - 1]
            # print('\nMOVE!!!!!!!!!!!!!!!!!!!!!!!!!!!',new_level)
            self.offset = self.level + self.offset - new_level
            self.level = new_level
        if self.topoffset < -0.1:
            new_level = model.levelList[model.levelList.index(self.toplevel) - 1]
            self.topoffset = self.toplevel + self.topoffset - new_level
            self.toplevel = new_level

    @property
    def SHGC(self):
        return self.settings.get('shgc', self._shgc)

    @SHGC.setter
    def SHGC(self, value):
        self._shgc = value
        self.settings['shgc'] = value

    @property
    def operable(self):
        return self.settings.get('operable', self._operable)

    @operable.setter
    def operable(self, value):
        self._operable = value
        self.settings['operable'] = value

    @classmethod
    def fromProjection(cls, prjLine: shapely.Geometry, bottom: float, top: float, model: MoosasContainer,
                       airBoundary=False):
        """
        Create an instance from a projection line with defined bottom and top elevations.
        
        Parameters
        ----------
        prjLine : shapely.Geometry
            A linestring geometry representing the projection; must have sufficient length.
        bottom : float
            The bottom elevation (z-coordinate) of the generated geometry.
        top : float
            The top elevation (z-coordinate) of the generated geometry.
        model : MoosasContainer
            The model container used to include the generated geometry.
        airBoundary : bool, optional
            If True, includes an air boundary; defaults to False.
        
        Returns
        -------
        gls : cls or None
            An instance of the class created from the projection, or None if the input line is too short.
        """
        if shapely.length(prjLine) < geom.POINT_PRECISION:
            return None
        stPoint, edPoint = shapely.get_coordinates(prjLine)
        airBound = [
            np.append(stPoint, bottom),
            np.append(edPoint, bottom),
            np.append(edPoint, top),
            np.append(stPoint, top),
            np.append(stPoint, bottom),
        ]
        idx = model.includeGeo(shapely.polygons(airBound), cat=0)
        gls = cls(model, idx)
        return gls

    def force_2d(self, top=False, region=False):
        """
        Force the geometry into a 2D representation.
        
        Parameters
        ----------
        top : bool, optional
            If True, project to the top view. Default is False.
        region : bool, optional
            If True, return as a 2D region. Default is False.
        
        Returns
        -------
        object
            A 2D representation of the geometry, type depends on implementation in parent class.
        """
        return super(MoosasGlazing, self).force_2d(top, region)

    def to_xml(self, model, Element_tag='glazing', writeGeometry=False):
        """
        Convert the MoosasGlazing object to an XML representation.
        
        Parameters
        ----------
        model : object
            The model context in which the XML is generated; passed to the parent class method.
        Element_tag : str, optional
            The tag name for the XML element representing the glazing. Default is 'glazing'.
        writeGeometry : bool, optional
            If True, geometry information will be included in the XML output. Default is False.
        
        Returns
        -------
        xml_element : xml.etree.ElementTree.Element
            The XML element representing the glazing, with additional subelements for 'parentFace' and 'shadingid'.
        """
        glazingXml = super(MoosasGlazing, self).to_xml(model, Element_tag, writeGeometry=writeGeometry)
        ET.SubElement(glazingXml, "parentFace").text = self.parentFace.Uid
        ET.SubElement(glazingXml, "shadingId").text = ' '.join(
            np.array([shade.Uid for shade in self.shading]).astype(str)
        )
        ET.SubElement(glazingXml, "SHGC").text = "" if self.SHGC is None else str(self.SHGC)
        return glazingXml


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
    'to_xml': xmlTree object of this element, can also be used to create a some MoosasFloor


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

    def to_xml(self, model: MoosasContainer, Element_tag='floor', writeGeometry=False) -> ET.Element:
        """
        Construct an XML element representing the floor with face UIDs as subelements.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model containing the data to be represented in XML.
        Element_tag : str, optional
            The tag name for the root XML element (default is 'floor').
        writeGeometry : bool, optional
            If True, includes geometric data in the XML output (default is False).
        
        Returns
        -------
        ET.Element
            An XML element with the specified tag containing face UIDs as text subelements.
        """
        floor = ET.Element(Element_tag)
        for f in self.face:
            ET.SubElement(floor, "face").text = f.Uid
            # floor.append(f.to_xml(model,writeGeometry=writeGeometry))
        return floor


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
                points = np.unique(
                    shapely.get_coordinates(wall.face, include_z=True)[:, :2],
                    axis=0,
                )
                if len(points) < 2:
                    raise GeometryError(wall.face, "degenerate explicit wall")
                distances = np.linalg.norm(
                    points[:, None, :] - points[None, :, :], axis=2)
                ia, ib = np.unravel_index(np.argmax(distances), distances.shape)
                vector = np.asarray(points[ib] - points[ia], dtype=float)
                length = float(np.linalg.norm(vector))
                if length <= 0:
                    raise GeometryError(wall.face, "degenerate explicit wall")
                owner_normal = wall.settings.get('geometry_owner_normals', {}).get(
                    getattr(self, '_MoosasEdge__ownerId', None))
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
                              min(.001, length * .25)))
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

    def to_xml(self, model: MoosasContainer, Element_tag='edge', writeGeometry=False):
        """
        Convert the MoosasSpace object's edge and wall data into an XML element representation.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model providing context for the XML conversion.
        Element_tag : str, optional
            The tag name for the root XML element (default is 'edge').
        writeGeometry : bool, optional
            If True, includes geometric data in the XML output (default is False).
        
        Returns
        -------
        xml.etree.ElementTree.Element
            An XML element representing the edge and its walls with associated properties.
        """
        edge = ET.Element(Element_tag)
        for w, factor in zip(self.wall, self.FactorOfWall):
            factor = factor.array
            wall = ET.SubElement(edge, "wall")
            ET.SubElement(wall, "Uid").text = w.Uid
            ET.SubElement(wall, "normal").text = str(factor[0]) + ' ' + str(factor[1]) + ' ' + '0'
            # w_xml = w.to_xml(model,writeGeometry=writeGeometry)
            # normal = w_xml.findall("normal")
            # for i in range(len(normal)):
            #     normal[i].text = str(factor[0]) + ' ' + str(factor[1]) + ' ' + '0'
            # edge.append(w_xml)
        return edge


class MoosasSpace(object):
    """define a space with topology and related data.
    it can be a void if floor or ceiling is None or area of floor/ceiling < area of edge

    """
    __slots__ = ['floor', 'edge', 'ceiling', '__void', '__id','__uniqueId', '__neighbor', 'internalMass', 'settings',
                 'description', 'space_type', 'conditioned', 'explicit_settings']

    def __init__(self, _floor: MoosasFloor | None, _edge: MoosasEdge, _ceiling: MoosasFloor | None,
                 void: list[MoosasSpace] = None, Uid: str = None, space_type: str = "room",
                 conditioned: bool = True):
        """
        Initialize a new instance with floor, edge, ceiling, and optional void spaces.
        
        Parameters
        ----------
        _floor : MoosasFloor or None
            The floor object associated with the space, or None if not present.
        _edge : MoosasEdge
            The edge object defining the boundary and internal mass of the space.
        _ceiling : MoosasFloor or None
            The ceiling object associated with the space, or None if not present.
        void : list of MoosasSpace, optional
            A list of void spaces within the zone. Defaults to an empty list if None.
        
        Returns
        -------
        None
        """
        self.floor: MoosasFloor | None = _floor
        self.edge: MoosasEdge = _edge
        self.ceiling: MoosasFloor | None = _ceiling
        self.description = ""
        self.space_type = space_type
        self.conditioned = bool(conditioned)
        self.explicit_settings = []

        self.__neighbor = {}
        self.internalMass: list[MoosasElement] = _edge.internalMass
        self.__void: list[MoosasSpace] = [] if void is None else void
        self.__id: str = '' if Uid is None else Uid
        self.__uniqueId: bool = False if Uid is None else True
        self.regenerateId()

        # Thermal Settings
        self.settings = {
            "zone_name": self.id,

            "zone_summerrad": None,  # summer radiant heat units:kwh
            "zone_winterrad": None,  # winter radiant heat units:kwh

            "zone_template": None
        }


    @classmethod
    def fromDict(cls, spaceDict, model: MoosasContainer):
        """
        Construct a Space object from a dictionary representation.
        
        Parameters
        ----------
        spaceDict : dict
            Dictionary containing space elements such as 'edge', 'ceiling', 'floor', 
            'internalMass', and 'void'.
        model : MoosasContainer
            Model container providing context for constructing associated objects.
        
        Returns
        -------
        Space
            A new Space instance constructed from the provided dictionary and model.
        """
        edge = _getElement('edge', dictionary=spaceDict)[0]
        ceiling = _getElement('ceiling', dictionary=spaceDict, strict=False)[0]
        floor = _getElement('floor', dictionary=spaceDict, strict=False)[0]
        Uid = _getElement('id', dictionary=spaceDict, strict=False)[0]
        space_type = _getElement('space_type', dictionary=spaceDict, strict=False)[0]
        conditioned_value = _getElement('conditioned', dictionary=spaceDict, strict=False)[0]
        conditioned = True if conditioned_value is None else str(conditioned_value).strip().lower() == "true"

        internalMass = _getElement('internalMass', dictionary=spaceDict, strict=False)
        void = _getElement('void', dictionary=spaceDict, strict=False)

        if ceiling:
            ceiling = MoosasFloor.fromDict(floor, model)
        if edge:
            edge = MoosasEdge.fromDict(edge, model)
        if floor:
            floor = MoosasFloor.fromDict(ceiling, model)
        
        if Uid:
            space = cls(
                floor, edge, ceiling, Uid=Uid, space_type=space_type or "room", conditioned=conditioned
            )
        else:
            space = cls(floor, edge, ceiling, space_type=space_type or "room", conditioned=conditioned)

        if internalMass[0]:
            for _intWall in internalMass:
                space.addInternalMass(MoosasWall.fromDict(_intWall, model))

        if void[0]:
            for _void in void:
                space.add_void(cls.fromDict(_void, model))
        return space

    @property
    def neighbor(self) -> dict:
        """
        Dictionary of neighbors associated with the object.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the neighbor property.
        
        Returns
        -------
        dict
            A dictionary representing the neighbors.
        """
        return self.__neighbor

    @property
    def void(self) -> list[MoosasSpace]:
        """
        List of MoosasSpace objects representing the void.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the void property.
        
        Returns
        -------
        list[MoosasSpace]
            A list of MoosasSpace objects stored in the private __void attribute.
        """
        return self.__void

    @property
    def parent(self):
        """
        Parent property of the edge.
        
        Returns the parent node associated with the edge of this instance.
        
        Returns
        -------
        object
            The parent node of the edge.
        """
        return self.edge.parent

    @property
    def id(self) -> str:
        """
        Return the ID of the object as a string.
        
        Returns
        -------
        str
            The private attribute `__id` representing the object's ID.
        """
        return self.__id

    @property
    def area(self) -> float:
        """
        Compute the effective area of the object, accounting for any voids.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `edge` and `void` attributes.
            It is expected to have an `edge` attribute with an `area` property,
            and a `void` attribute which is a collection of objects each having an `area` property.
        
        Returns
        -------
        float
            The effective area, calculated as the area of the edge minus the sum of the areas of all voids.
        """
        area = self.edge.area
        if len(self.void) > 0:
            for _void in self.void:
                area -= _void.area
        return area

    @property
    def level(self) -> float:
        """
        Level of the edge.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `edge` attribute.
        
        Returns
        -------
        float
            The level value from the associated edge.
        """
        return self.edge.level

    @property
    def topLevel(self) -> float:
        """
        Top-level elevation of the structure, depending on whether it is void or not.
        
        Returns the top-level elevation based on the object's state: if the object is void,
        returns the toplevel from the edge; otherwise, returns the ceiling level.
        
        Parameters
        ----------
        self : object
            The instance of the class containing this property. Assumes the presence
            of `is_void()`, `edge.toplevel`, and `ceiling.level` attributes/methods.
        
        Returns
        -------
        float
            The top-level elevation value, either from `edge.toplevel` (if void) or `ceiling.level`.
        """
        explicit=getattr(self,'settings',{}).get('geometry_top_elevation_m')
        if explicit is not None:return float(explicit)
        if self.space_type=='attic' and self.ceiling:
            return max(float(np.max(shapely.get_coordinates(f.face,include_z=True)[:,2])) for f in self.ceiling.face)
        if self.is_void():
            return self.edge.toplevel
        else:
            return self.ceiling.level

    @property
    def height(self) -> float:
        """
        Height of the object calculated based on edge, ceiling, and floor levels and offsets.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the height property. It is expected to have
            methods `is_void()` and attributes `edge`, `ceiling`, and `floor`. The `edge`
            attribute should have `toplevel` and `level` properties. The `ceiling` and `floor`
            attributes should each have `level` and `offset` properties.
        
        Returns
        -------
        float
            The computed height. If the instance is void (determined by `is_void()`), returns
            the difference between `toplevel` and `level` of the edge. Otherwise, returns the
            difference between the adjusted ceiling level and the adjusted floor level.
        """
        volume=getattr(self,'settings',{}).get('geometry_volume_m3')
        if volume is not None:
            volume=float(volume)
            if not np.isfinite(volume) or volume<=0 or self.area<=0:raise ValueError('Invalid explicit physical volume')
            return volume/self.area
        if self.is_void():
            return self.edge.toplevel - self.edge.level
        else:
            if self.space_type == "attic":
                # Roof facets can carry different reference levels. Use their
                # actual elevations so RDF interface order cannot change the
                # reported attic height during a save/reload cycle.
                ceiling_z = np.mean([face.level + face.offset for face in self.ceiling.face])
                floor_z = np.mean([face.level + face.offset for face in self.floor.face])
                return float(ceiling_z - floor_z)
            return self.ceiling.level + self.ceiling.offset - self.floor.level - self.floor.offset

    @property
    def spaceType(self) -> str:
        """
        Determine the type of space based on geometric properties of 2D faces.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `force_2d` method and geometric data.
            It is assumed that `self` has a method `force_2d()` which returns a 2D geometric representation,
            and that `shapely` and `bBox` utilities are available for area and bounding box computations.
        
        Returns
        -------
        str
            The classified space type, one of 'Corridor', 'MainSpace', or 'privateSpace',
            based on area, aspect ratio, and dimensional thresholds of decomposed convex faces.
        """
        """Select one of the following types of the space:
        Corridor: most of the pieces are narrow and long
        MainSpace: the main space in the building, like the living room or the hall
        privateSpace: the small space in the building, provided for a few amount of persons
        """
        convexFaces, _ = triangulate2dFace(self.force_2d())
        narrowPart, mainPart = [], []
        for face in convexFaces:
            if shapely.area(face) > 9.0:
                boxDict = bBox(face)
                xSize = boxDict['x-domain'][1] - boxDict['x-domain'][0]
                ySize = boxDict['y-domain'][1] - boxDict['y-domain'][0]
                if min(xSize, ySize) < 2.5:
                    narrowPart.append(face)
                elif max(xSize, ySize) / min(xSize, ySize) > 3 and min(xSize, ySize) < 5:
                    narrowPart.append(face)
                else:
                    mainPart.append(face)
        if len(mainPart) == 0:
            return 'Corridor'
        else:
            if np.sum([shapely.area(p) for p in mainPart]) / 18 < 3.0:
                """space less for 3 person"""
                return 'privateSpace'
            else:
                return 'MainSpace'

    def regenerateId(self) -> str:
        """calculate the id for the space
        the id comes from 7 params,each params space two indent('0'to'9' & 'a'to'j')
        so the id will be encoded like this:
            0x 1a 2b 3c 4d 5e 6f 7g

        Returns:
            str: self.id
        """
        originalId = self.id
        if not self.__uniqueId:
            walls = self.getAllFaces(to_dict=True)['MoosasWall']
            params = [self.area, self.height * 10, self.level * 10, len(walls)]
            params += list([np.sum([w.wwr * 100 for w in walls])])
            params += list(self.edge.getWeightCenter() * 10)
            self.__id = encodeParams(*params)
        # Record self.id to all MoosasGeometries
        is_void = self.is_void()
        for moface in self.getAllFaces(to_dict=False):
            if originalId != "" and originalId in moface.space:
                moface.space.remove(originalId)
            if not is_void:
                if not self.id in moface.space:
                    moface.space.append(self.id)
        return self.__id

    def add_void(self, void: MoosasSpace) -> None:
        """
        Add a void space to the collection of voids and update space attributes in all faces.
        
        Parameters
        ----------
        void : MoosasSpace
            The void space object to be added to the internal void list.
        
        Returns
        -------
        None
        """
        """add void to self.__void, and change the space attribute in self.getAllFaces()
        """
        self.__void.append(void)
        # Record self.id to all MoosasGeometries
        self.regenerateId()

    def force_2d(self, top=False) -> shapely.Geometry:
        """
        Project the geometry to 2D and return a 2D polygon.
        
        Parameters
        ----------
        top : bool, optional
            If True, project to the top plane; otherwise, use default 2D projection.
            Default is False.
        
        Returns
        -------
        shapely.Geometry
            A 2D polygon geometry. If the object has voids, a polygon with holes is constructed;
            otherwise, the 2D version of the edge is returned directly.
        """
        if len(self.void) > 0:
            outerRing = shapely.linearrings(shapely.get_coordinates(self.edge.force_2d(top)))
            innerRing = [shapely.linearrings(shapely.get_coordinates(v.edge.force_2d(top))) for v in self.void]
            polygon = shapely.polygons(outerRing, holes=innerRing)
        else:
            polygon = self.edge.force_2d(top)
        return polygon

    def getWeightCenter(self) -> np.ndarray:
        """Return the geometric center of the space boundary and vertical extent."""
        centroid = shapely.centroid(self.force_2d())
        if self.is_void():
            bottom = self.edge.level
            top = self.edge.toplevel
        else:
            bottom = self.floor.level + self.floor.offset
            top = self.ceiling.level + self.ceiling.offset
        return np.array([centroid.x, centroid.y, (bottom + top) / 2.0])

    def is_void(self):
        """
        Check if the space is considered void based on floor and ceiling area conditions.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the attributes `floor`, `ceiling`, `area`, 
            where `floor` and `ceiling` are objects with an `area` attribute, and `area` 
            represents the reference area of the space. It is assumed that `geom.AREA_PRECISION` 
            is a predefined constant used for numerical precision tolerance.
        
        Returns
        -------
        bool
            True if the space is considered void (i.e., either floor or ceiling is missing, 
            or their area is less than the space's area within the given precision), 
            False otherwise.
        """
        if not self.floor or not self.ceiling:
            return True
        # Labelled solid shells already carry a validated closed boundary.
        # A multi-level hall can have floors and ceilings at several levels;
        # comparing one modal elevation or net opaque ceiling area to the
        # footprint incorrectly classifies its rooflights as an open void.
        if getattr(self, 'settings', {}).get('geometry_shell_closed') is True:
            volume = float(self.settings.get('geometry_volume_m3', 0))
            if not np.isfinite(volume) or volume <= 0:
                raise ValueError('Closed shell requires a positive physical volume')
            return False
        # Inclined roof faces do not cover the footprint in XY projection. An attic
        # is validated by its existing floor, eave walls and roof topology instead.
        if self.space_type == "attic":
            return False
        if (
            self.ceiling.level + self.ceiling.offset
            - self.floor.level - self.floor.offset
            < geom.LEVEL_MAX_OFFSET
        ):
            return True
        if self.floor.area < self.area - geom.AREA_PRECISION:
            return True
        if self.ceiling.area < self.area - geom.AREA_PRECISION:
            # print(self.ceiling.face[0].face if len(self.ceiling.face)>0 else None)
            return True
        return False

    def is_open(self):
        """
            Check if the space is considered exposed to the outdoor air based on floor and ceiling area conditions.

            Parameters
            ----------
            self : object
                The instance of the class containing the attributes `floor`, `ceiling`, `area`,
                where `floor` and `ceiling` are objects with an `area` attribute, and `area`
                represents the reference area of the space.

            Returns
            -------
            bool
                True if the space is considered open (i.e., either floor or ceiling is missing,
                or their area is less than the space's area within the given precision),
                False otherwise.
        """
        for moElement in self.getAllFaces(to_dict=False):
            # moElement:MoosasElement = moElement
            for glazing in moElement.glazingElement:
                if glazing.category == 2 and moElement.isOuter:
                        return True
        return False

    def boundBox(self) -> np.ndarray:
        """
        Compute the axis-aligned bounding box of all faces in the object.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `getAllFaces` method, which returns a list of face objects.
            Each face object must have a `face` attribute compatible with `shapely.get_coordinates`.
        
        Returns
        -------
        numpy.ndarray
            A 2x3 array containing the minimum and maximum coordinates of the bounding box.
            The first row is the minimum (x, y, z) corner, and the second row is the maximum (x, y, z) corner.
        """
        facesCoor = [shapely.get_coordinates(moface.face, include_z=True) for moface in self.getAllFaces(to_dict=False)]
        facesCoorMin = np.min([np.min(coor, axis=0) for coor in facesCoor], axis=0)
        facesCoorMax = np.max([np.max(coor, axis=0) for coor in facesCoor], axis=0)
        return np.array([facesCoorMin, facesCoorMax])

    def applySettings(self, template_id):
        """Select a registered BuildingTemplate without copying its parameters."""
        from ...model.building_templates import space_template
        if not isinstance(template_id, str):
            raise ValueError('Select a BuildingTemplate using its complete namespaced id')
        space_template(self.parent, self, template_id)
        self.settings['zone_template'] = template_id


    def add_neighbor(self, neighbor_id, element: MoosasElement):
        """
        Add a neighbor element to the specified neighbor ID.
        
        Parameters
        ----------
        neighbor_id : hashable
            The identifier for the neighbor group to which the element will be added.
        element : MoosasElement
            The element to be added to the neighbor list associated with neighbor_id.
        
        Returns
        -------
        None
            This function does not return any value.
        """
        if neighbor_id not in self.neighbor:
            self.neighbor[neighbor_id] = [element]
        else:
            self.neighbor[neighbor_id] += [element]

    def clear_neighbors(self):
        """Clear derived space adjacency before rebuilding it."""
        self.neighbor.clear()

    def addInternalMass(self, wall: MoosasWall):
        """
        Add an internal mass wall to the current object.
        
        Parameters
        ----------
        wall : MoosasWall
            The wall object representing internal mass to be added.
        
        Returns
        -------
        None
            This function does not return any value.
        """
        self.internalMass.append(wall)

    def getAllFaces(self, to_dict=False) -> list[MoosasElement] | dict:
        """get all faces in the space.

        Args:
            to_dict (bool, optional): whether to return a dictionary or a list. Defaults to False.

        Returns:
            list[MoosasElement]: all faces in the space.
            dict:
            {
                MoosasFloor: list[MoosasFloor],
                MoosasCeiling:list[MoosasCeiling],
                MoosasWall:list[MoosasWall],
                MoosasSkylight:list[MoosasSkylight],
                MoosasGlazing:list[MoosasGlazing],
                Shading:list[MoosasElement],
                InternalMass:list[MoosasElement],
            }

        """

        faces = {}
        faces['MoosasFloor']: list[MoosasFace] = [f for f in self.floor.face] if self.floor else []
        faces['MoosasCeiling']: list[MoosasFace] = [f for f in self.ceiling.face] if self.ceiling else []
        secondary=set(getattr(self,'settings',{}).get('secondary_ceiling_element_ids',[]))
        if secondary and self.edge and len(self.edge.wall):
            existing={f.Uid for f in faces['MoosasCeiling']}
            actual={w.Uid:w for w in self.edge.wall[0].parent.wallList if w.Uid in secondary}
            if secondary-set(actual):raise ValueError('Secondary ceiling refers to missing physical wall')
            faces['MoosasCeiling'] += [actual[uid] for uid in sorted(secondary-existing)]
        faces['MoosasWall']: list[MoosasWall] = [w for w in self.edge.wall]
        for void in self.void:
            faces['MoosasWall'] += void.edge.wall

        faces['MoosasSkylight']: list[MoosasSkylight] = []
        faces['MoosasGlazing']: list[MoosasGlazing] = []
        faces['Shading']: list[MoosasElement] = []

        for moface in faces['MoosasFloor'] + faces['MoosasCeiling']:
            faces['MoosasSkylight'] += moface.glazingElement
        for moface in faces['MoosasWall']:
            faces['MoosasGlazing'] += moface.glazingElement
        for moface in faces['MoosasGlazing']:
            faces['Shading'] += moface.shading

        for moface in faces['MoosasSkylight']:
            faces['Shading'] += moface.shading

        faces['InternalMass'] = self.internalMass

        if to_dict:
            return faces
        else:
            return [item for subList in list(faces.values()) for item in subList]

    def open_edges(self):
        """
        Return a dictionary of open edges from the geometry faces.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the method. It is expected to have
            methods `getAllFaces` and access to face geometry objects with `getEdgeStr`.
        
        Returns
        -------
        dict
            A dictionary where keys are edge strings and values are the corresponding
            geometry objects (`moGeometry`) that are not shared (i.e., open edges).
        """
        edges = {}
        for moGeometry in self.getAllFaces(False):
            for edge_str in moGeometry.getEdgeStr():
                if edge_str in edges.keys():
                    edges[edge_str] = None
                else:
                    edges[edge_str] = moGeometry
        openEdges = {key: edges[key] for key in edges.keys() if edges[key] is not None}
        return openEdges

    def to_string(self, model: MoosasContainer):
        """lagacy method to print the space info"""
        string_out = 'Space' + ' ' + self.id + '\n'
        string_out += '-Area' + ' ' + str((self.area) / INCH_METER_MULTIPLIER_SQR) + '\n'
        string_out += '-Height' + ' ' + str((self.height) / INCH_METER_MULTIPLIER) + '\n'
        string_out += '-Boundary\n'
        corrdiantes = shapely.get_coordinates(shapely.force_3d(self.edge.force_2d(), z=self.floor.level), include_z=True)
        for poi in corrdiantes:
            string_out += str(poi[0] / INCH_METER_MULTIPLIER) + ' ' \
                          + str(poi[1] / INCH_METER_MULTIPLIER) + ' ' \
                          + str(poi[2] / INCH_METER_MULTIPLIER) + '\n'

        string_out += '-Floor' + ' '

        for Moosasface in self.floor.face:
            string_out += str(Moosasface.faceId) + ' '
        string_out += '\n--Area' + ' ' + str((self.area) / INCH_METER_MULTIPLIER_SQR) + '\n'
        floor_normal = shapely.get_coordinates(model.geoNormal[model.geoId.index(self.floor.face[0].faceId)],
                                              include_z=True).flatten()
        string_out += '--Normal ' + str(floor_normal[0]) + ' ' + str(floor_normal[1]) + ' ' + str(
            floor_normal[2]) + '\n'
        string_out += '--Height' + ' ' + str((self.floor.level) / INCH_METER_MULTIPLIER) + '\n'

        string_out += '-Ceiling' + ' '

        for Moosasface in self.ceiling.face:
            string_out += str(Moosasface.faceId) + ' '
        string_out += '\n--Area' + ' ' + str((self.area) / INCH_METER_MULTIPLIER_SQR) + '\n'
        floor_normal = shapely.get_coordinates(model.geoNormal[model.geoId.index(self.ceiling.face[0].faceId)],
                                              include_z=True).flatten()
        string_out += '--Normal ' + str(floor_normal[0]) + ' ' + str(floor_normal[1]) + ' ' + str(
            floor_normal[2]) + '\n'
        string_out += '--Height' + ' ' + str((self.ceiling.level) / INCH_METER_MULTIPLIER) + '\n'

        for wall, factor in zip(self.edge.wall, self.edge.FactorOfWall):
            factor = factor.array
            string_out += '-Wall' + ' '
            if type(wall.faceId) == np.ndarray:
                for indd in wall.faceId:
                    string_out += str(indd) + ' '
            else:
                string_out += str(wall.faceId) + ' '
            string_out += '\n--Area' + ' ' + str((wall.area) / INCH_METER_MULTIPLIER_SQR) + '\n'
            string_out += '--Internal' + ' ' + str(wall.isOuter) + '\n'
            string_out += '--Height' + ' ' + str((wall.level) / INCH_METER_MULTIPLIER) + '\n'
            string_out += '--Normal' + ' ' + str(factor[0]) + ' ' + str(factor[1]) + ' ' + str(0) + '\n'
            string_out += '--Glazing' + ' '
            if type(wall.glazingId) == np.ndarray:
                for indd in wall.glazingId:
                    string_out += str(indd) + ' '
            else:
                string_out += str(wall.glazingId) + ' '
            string_out += '\n--Edge\n'
            twins = shapely.get_coordinates(shapely.force_3d(wall.force_2d()), include_z=True)
            string_out += str(twins[0][0]) + ' ' + str(twins[0][1]) + ' ' + str(twins[0][2]) + '\n'
            string_out += str(twins[1][0]) + ' ' + str(twins[1][1]) + ' ' + str(twins[1][2]) + '\n'

        string_out += 'End\n'
        return string_out

    def __repr__(self):
        """Return a string representation of the object using its 'id' attribute.
        
                Parameters
                ----------
                self : object
                    The instance of the class containing the 'id' attribute.
        
                Returns
                -------
                str
                    The string value of the instance's 'id' attribute.
                """
        return self.id

    def to_xml(self, model: MoosasContainer = None, xml_tag="space", writeGeometry=False):
        """
        Convert the object to an XML element representation.
        
        Parameters
        ----------
        model : MoosasContainer, optional
            The container model holding global variables and geometry lists. If not provided, uses the parent attribute of the object.
        xml_tag : str, default="space"
            The tag name for the root XML element.
        writeGeometry : bool, default=False
            If True, includes geometric data in the XML output.
        
        Returns
        -------
        xml.etree.ElementTree.Element
            The XML element representing the object, containing attributes such as id, area, height, boundary coordinates,
            settings, topology (floor, ceiling, edge), neighbors, and internal mass elements.
        """
        if not model:
            model = self.parent
        root = ET.Element(xml_tag)
        ET.SubElement(root, "id").text = self.id
        ET.SubElement(root, "space_type").text = self.space_type
        ET.SubElement(root, "conditioned").text = str(self.conditioned)
        ET.SubElement(root, "area").text = str(self.area)
        if self.ceiling and self.floor:
            height = (self.edge.toplevel - self.edge.level) / INCH_METER_MULTIPLIER
        else:
            height = None
        ET.SubElement(root, "height").text = str(height)
        ET.SubElement(root, "is_void").text = str(self.is_void())
        ET.SubElement(root, "void").text = " ".join([str(v) for v in self.void])
        bound = ET.SubElement(root, "boundary")

        corrdiantes = shapely.get_coordinates(
            shapely.force_3d(self.edge.force_2d(), z=self.edge.elevation), include_z=True)
        for poi in corrdiantes:
            ET.SubElement(bound, "pt").text = ' '.join([str(p / INCH_METER_MULTIPLIER) for p in poi])
        settingXml = ET.SubElement(root, "setting")
        for key in self.settings.keys():
            ET.SubElement(settingXml, key).text = str(self.settings[key])

        topology = ET.SubElement(root, "topology")
        if self.floor:
            topology.append(self.floor.to_xml(model, writeGeometry=writeGeometry))
        if self.ceiling:
            topology.append(self.ceiling.to_xml(model, 'ceiling', writeGeometry=writeGeometry))
        topology.append(self.edge.to_xml(model, writeGeometry=writeGeometry))

        for _nei in self.neighbor:
            _neiElement = ET.SubElement(root, "neighbor")
            ET.SubElement(_neiElement, "Uid").text = str([w.Uid for w in self.neighbor[_nei]])
            ET.SubElement(_neiElement, "id").text = str(_nei)

        for _intWall in self.internalMass:
            root.append(_intWall.to_xml(model, 'internalMass', writeGeometry=writeGeometry))

        return root


class MoosasContainer(object):
    """Define all the global variables needed for Moosas+.

    This class does not have slots for the sake of flexible attributes.

    Attributes:
        geoId (str): Geometries' identification.
        geometryList (List): List of all geometries.
        faceList (List): List of MoosasFace objects.
        wallList (List): List of MoosasWall objects.
        skylightList (List): List of MoosasSkylight objects.
        glazingList (List): List of MoosasGlazing objects.
        levelList (List): List of all levels in the model.
        boundaryList (List): List of recognized boundaries.
        floorList (List): List of floors as MoosasFloor objects.
        edgeList (List): List of MoosasEdge objects.
        ceilingList (List): List of ceilings as MoosasFloor objects.
        spaceList (List): List of valid spaces as MoosasSpace objects.
        voidList (List): List of void spaces as MoosasSpace objects.
        weather (WeatherData): Hourly weather series and location metadata, default is None.
        builtData (Object): Data used to construct space manually.

    Properties:
        spaceIdDict (dict): A dictionary recording spaceId: MoosasSpace.

    Methods:
        fromDict(cls, spaceDict: dict) -> MoosasSpace: Create MoosasSpace from a dictionary.
        update(self) -> None: update self.builtData, which is used to record current elements and glazing when creating space manually.
        getAllFaces(self) -> List: Get all elements in the model.
        includeGeo(self, geo: shapely.Geometry, normal: shapely.Geometry | Vector | np.ndarray = None, cat: int = 0,
                   holes=None) -> str: Include a shapely.Geometry to the library.
        findFace(self, faceId) -> list[MoosasGeometry]: Find a geometry by its geoId.
    """

    def __init__(self):
        """
        Initialize the MoosasModel with default lists and assign appropriate types to these lists.
        
        Parameters
        ----------
        self : object
            The instance of the MoosasModel class being initialized. This method sets up all the 
            internal list attributes used to store geometric and structural components of the model.
        
        Returns
        -------
        None
            This method does not return any value.
        """
        """initialize the MoosasModel with default list, and apply type to these list"""
        self.geoId = []
        self.geometryList: list[MoosasGeometry] = []
        self.newIndex = 0

        # horizontalVerticalPlaneSet
        self.faceList: list[MoosasFace] = []
        self.wallList: list[MoosasWall] = []
        self.shadingList: list[MoosasElement] = []
        self.wall_remain: list[MoosasWall] = []
        self.face_remain: list[MoosasFace] = []

        # Identify the result set
        self.levelList: list[float] = []
        self.boundaryList = []

        # envelope floor space set
        self.floorList: list[MoosasFloor] = []
        self.ceilingList: list[MoosasFloor] = []
        self.glazingList: list[MoosasGlazing] = []
        self.skylightList: list[MoosasSkylight] = []
        self.edgeList: list[MoosasEdge] = []
        self.spaceList: list[MoosasSpace] = []
        self.voidList: list[MoosasSpace] = []

        # object used to construct a space
        self.builtData = object()

    @property
    def spaceIdDict(self) -> dict:
        """space id dictionary for all spaces in self.spaceList

        Returns:
            dict: {spaceId:MoosasSpace}
        """
        return {space.id: space for space in self.spaceList}

    def fromDict(self, spaceDict: dict) -> MoosasSpace:
        """construct a space from a dictionary
        the space will be added to self.spaceList automatically,
        and the space topology will be automatically recalculate.
        for more information please refer to MoosasSpace.fromDict()

        Args:
            spaceDict (dict): Dictionary to construct space from.

        Returns:
            MoosasSpace: created MoosasSpace object.
        """
        if not hasattr(self.builtData, 'elements') or not hasattr(self.builtData, 'glazing'):
            self.update()
        space = MoosasSpace.fromDict(spaceDict, self)
        for void in self.voidList:
            if shapely.contains(space.force_2d(), void.force_2d()):
                space.voidList.append(void)
        self.spaceList.append(space)
        return space

    def update(self) -> None:
        """
        Update the builtData attribute to reflect current elements and glazing.
        
        This method initializes the element and glazing dictionaries in builtData if they do not exist,
        then populates them with face and glazing data from the instance's glazingList, skylightList,
        and all faces obtained via getAllFaces.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the method. It is expected to have the following attributes:
            - builtData: an object that will be updated with 'element' and 'glazing' dictionaries.
            - glazingList: a list of objects, each having a 'glazingId' attribute.
            - skylightList: a list of objects, each having a 'glazingId' attribute.
            - getAllFaces(): a method returning a collection of face objects, each with a 'faceId' attribute.
            - mixItemListToList: a function used to convert faceId items into a flat list.
        
        Returns
        -------
        None
        """
        """update self.builtData, which is used to record current elements and glazing when creating space manually.
        builtData.element is a dictionary: {MoosasElement.faceId:MoosasElement}
        builtData.glazing is a dictionary: {MoosasGlazing.faceId:MoosasElement}
        """
        if not hasattr(self.builtData, 'elements') or not hasattr(self.builtData, 'glazing'):
            self.builtData.element = {}
            self.builtData.glazing = {}
        for gls in self.glazingList:
            for glsId in gls.glazingId:
                self.builtData.glazing[glsId] = gls
        for gls in self.skylightList:
            for glsId in gls.glazingId:
                self.builtData.glazing[glsId] = gls
        for element in self.getAllFaces():
            for eleId in mixItemListToList(element.faceId):
                self.builtData.element[eleId] = element

    def getAllFaces(self, dumpUseless=False) -> list[MoosasElement] | dict:
        """get all MoosasElement in the model as a list
        the elements in the list will not change their type hence you can test which element it is by isinstance()

        Returns:
            list[MoosasElement]: all MoosasElement in the model
        """
        if not dumpUseless:
            faces = []
            for elementList in [self.wallList, self.faceList, self.glazingList, self.skylightList, self.shadingList]:
                faces = np.append(faces, elementList)
            return list(faces)
        else:
            mElements = {'MoosasFace': set(), 'MoosasSkylight': set(), 'MoosasWall': set(), 'MoosasGlazing': set()}
            for space in self.spaceList + self.voidList:
                elementDict = space.getAllFaces(to_dict=True)

                mElements['MoosasFace'] = mElements['MoosasFace'] | set(
                    elementDict['MoosasFloor'] + elementDict['MoosasCeiling'])
                mElements['MoosasWall'] = mElements['MoosasWall'] | set(
                    elementDict['MoosasWall'] + elementDict['InternalMass'])
                mElements['MoosasSkylight'] = mElements['MoosasSkylight'] | set(elementDict['MoosasSkylight'])
                mElements['MoosasGlazing'] = mElements['MoosasGlazing'] | set(elementDict['MoosasGlazing'])
            return mElements

    def includeGeo(self, geo: shapely.Geometry, normal: shapely.Geometry | Vector | np.ndarray = None, cat: int = 0,
                   holes=None) -> str:
        """Include a geometry into the geometry library.

        Args:
            geo (shapely.Geometry): The polygon to include.
            normal (shapely.Geometry, optional): The normal vector of the polygon. Defaults to None.
            cat (int, optional): Category of the geometry (opaque == 0, transparent == 1, aperture == 2). Defaults to 0.
            holes (List[shapely.Geometry], optional): The inner holes of the geometry. Defaults to None.

        Returns:
            str: GeoId of the geometry, can be used to construct faces.
        """
        if holes is None:
            holes = []
        if normal is None:
            normal = faceNormal(geo)
        rings = shapely.get_rings(geo)
        if len(rings) > 1:
            geo = shapely.polygons(rings[0])
            holes += [shapely.polygons(r) for r in rings[1:]]
        faceId = f"n{self.newIndex}"
        self.newIndex += 1

        self.geometryList.append(MoosasGeometry(geo, faceId, normal, cat, holes))
        self.geoId = list(np.append(self.geoId, [faceId]))
        return self.geoId[-1]

    def removeGeo(self, geo: MoosasGeometry | shapely.Geometry | str):
        """
        Remove a geometry from the internal geometry list.
        
        Parameters
        ----------
        geo : MoosasGeometry or shapely.Geometry or str
            The geometry to be removed. Can be a MoosasGeometry object, a shapely.Geometry object, 
            or a string representing the face ID of the geometry.
        
        Returns
        -------
        None
        """
        if isinstance(geo, shapely.Geometry):
            for geoItems in self.geometryList:
                if geoItems.face == geo:
                    geo = geoItems
        if isinstance(geo, str):
            geo = self.geometryList[self.geoId.index(geo)]
        if isinstance(geo, MoosasGeometry):
            self.geometryList.remove(geo)
            self.geoId.remove(geo.faceId)

    def findFace(self, faceId: str | list[str]) -> list[MoosasGeometry]:
        """find a geometry in the library
        it will test the validation of the identification automatically, and skip invalid geometry

        Args:
            faceId (str|list[str]): The id of the face in the geo file or library

        Returns:
            list[MoosasGeometry]: a list of MoosasGeometry object of the face
        """
        if isinstance(faceId, str):
            faceId = [faceId]
        _faceId = []
        for idd in faceId:
            try:
                _faceId.append(self.geoId.index(idd))
            except:
                print(f"the geo: {idd} not in the geometry library.")
        return [self.geometryList[idd] for idd in _faceId]

    def setCategory(self, reset=False):
        """
        Returns
        -------
        int
            The category code:
            - -2: Ignore faces (excluded from calculations)
            - -1: Shading faces (included as shading elements)
            -  0: Opaque surface
            -  1: Translucent surface
            -  2: Air wall
            -  3: Wall element (MoosasWall)
            -  4: Plane element (MoosasFace)
            -  5: Glazing element (MoosasGlazing)
            -  6: Skylight element (MoosasSkylight)
        """
        res = 0
        if reset:
            for geo in self.getAllFaces():
                geo.setCategory()
        else:
            for i in range(len(self.geoId)):
                if self.geometryList[i].category != 2:
                    self.geometryList[i].setCategory(-1)
            almoface = self.getAllFaces(dumpUseless=True)
            refs = {'MoosasFace': 4, 'MoosasSkylight': 6, 'MoosasWall': 3, 'MoosasGlazing': 5}
            for key in almoface.keys():
                for item in almoface[key]:
                    if mixItemListToList(item.category)[0] != 2:
                        item.setCategory(refs[key])
                    elif key == 'MoosasWall':
                        res += 1

    def removeSpace(self, space: str | MoosasSpace):
        """
        safely delete a space and change all the boundary conditions of reset of the faces.

        Parameters
        -------
        spaceId : str
        space id or space object to remove
        """
        if isinstance(space, str):
            space = self.spaceIdDict[space]
        for moElement in space.getAllFaces(False):
            try:
                moElement.isOuter = True
                moElement.space.remove(space.id)
            except:
                pass
        try:
            self.spaceList = list(self.spaceList)
            self.spaceList.remove(space)
        except:
            pass
        try:
            self.voidList = list(self.voidList)
            self.voidList.remove(space)
        except:
            pass
