"""
    This module defines the main function for geometric processing. This module is used as a foundation for the models,
    transforming and encoding module. It has no documented public API and should not be used directly.
    This module has been translated from Chinese into English by Microsoft translation. Some expressions may be
    inaccurate or unprofessional.
"""
from __future__ import annotations

from ...utils import shapely, np, GeometryError, Iterable
from ...utils.constant import geom


class Vector(object):
    """
        The geometric operations of points and vectors and related 2D and 3D are defined, and the data formats of shapely
        and numpy are fused with high fault tolerance x,y,z: vector three-dimensional components style: The format of the
        data used to create the vector, which will be returned according to the format
    """
    __slots__ = ['x', 'y', 'z', 'style']

    ANGLE_TOLERANCE = 0.01

    def __init__(self, *vec: Vector | Iterable | shapely.Geometry | float | int):
        """
        Initialize a Vector object from various input types.
        
        Parameters
        ----------
        vec : Vector or Iterable or shapely.Geometry or float or int
            Input representing a vector, which can be provided as:
            - A Vector instance
            - A shapely.Geometry (point, line, etc.)
            - An Iterable (list, tuple, numpy array) of coordinates
            - Individual float or int values (as variable arguments)
        
        Returns
        -------
        None
            This constructor initializes the instance attributes x, y, z, and style.
        """
        """
            accepts input in 5 formats: Vector, shapely.Geometry, np.ndarry, list, numbers
            default: ndarry
        """
        if len(vec) == 1:
            vec = vec[0]
        if isinstance(vec, Vector):
            vec = vec.array
            self.style = np.ndarray
        if isinstance(vec, shapely.Geometry):
            if shapely.is_empty(vec):
                vec = np.array([0, 0, 0])
                self.style = np.ndarray
            else:
                vec = shapely.force_3d(vec, z=0)
                vec = shapely.get_coordinates(vec, include_z=True)
                if len(vec) > 1:
                    vec = vec[-1] - vec[0]
                else:
                    vec = vec[0]
                self.style = shapely.Geometry
        else:
            if not (isinstance(vec, Iterable)):
                raise Exception(f'Expect Iterable, got{type(vec)}')
            self.style = np.ndarray
        if len(vec) <= 2:
            vec = np.append(vec, 0)
        vec = np.nan_to_num(vec, nan=0)
        self.x = vec[0]
        self.y = vec[1]
        self.z = vec[2]

    @property
    def dump(self):
        """
        Return the underlying geometry or array representation based on the current style.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `dump` property, with attributes `style`, `geometry`, and `array`.
        
        Returns
        -------
        numpy.ndarray or shapely.Geometry
            If `self.style` is `shapely.Geometry`, returns `self.geometry`; otherwise, returns `self.array`.
        """
        if self.style == shapely.Geometry:
            return self.geometry
        else:
            return self.array

    @property
    def geometry(self) -> shapely.Geometry:
        """get geometry representation of the vector"""
        return shapely.points([self.x, self.y, self.z])

    @property
    def array(self) -> np.ndarray:
        """get array representation of the vector"""
        return np.array([self.x, self.y, self.z])

    @property
    def string(self):
        """
        Return a string representation of the vector for comparing direction, where small values are zeroed and components are rounded.
        
        Parameters
        ----------
        self : Vector
            The Vector instance whose direction string is to be generated. The vector is normalized and its components are processed
            to allow comparison of direction, treating opposite vectors as equivalent in certain contexts.
        
        Returns
        -------
        str
            A string representation of the vector with components separated by underscores. Components smaller than 
            `geom.POINT_PRECISION` are replaced with '0.00', others are rounded to 2 decimal places. This format allows 
            forward and reverse vectors (e.g., [0,0,1] and [0,0,-1]) to have the same string representation when symmetry 
            in direction comparison is desired.
        """
        """
            The vector is expressed as a string describing the direction, and the forward and reverse vectors are
            expressed in the same way, which is used to quickly determine whether the normal vectors of the surface are
            parallel.
            e.g.
            vec = np.array([0,0,1])
            Vector(vec).string == Vector(-vec).string
        """
        vec = Vector(self).uniform.array

        for i in range(3):
            if abs(vec[i]) < geom.POINT_PRECISION:
                vec[i] = '0.00'
            else:
                vec[i] = round(vec[i], 2)
        return '_'.join(vec.astype(str))

    @property
    def uniform(self):
        """
        Get a normalized uniform representation of the vector.
        
        This property returns a unit vector in a consistent direction based on the
        lexicographic sign convention, ensuring that antipodal vectors (like `vec` and `-vec`)
        map to the same uniform vector. Specifically, the signs are flipped if the first
        non-zero component is negative.
        
        Parameters
        ----------
        self : Vector
            The vector instance for which the uniform representation is computed.
        
        Returns
        -------
        Vector
            A new Vector instance representing the uniform unit vector. The direction
            is adjusted so that the first non-zero component is non-negative, ensuring
            consistency across opposite vectors.
        """
        """get an uniform vector,
        in which Vector(vec).uniform == Vector(-vec).uniform == Vector(-vec * 10).uniform
        """
        vec = Vector(self).unit().array
        if vec[0] < 0:
            return Vector(np.array([-vec[0], -vec[1], -vec[2]]))
        else:
            if vec[0] == 0 and vec[1] < 0:
                return Vector(np.array([-vec[0], -vec[1], -vec[2]]))
            else:
                if vec[0] == 0 and vec[1] == 0 and vec[2] < 0:
                    return Vector(np.array([-vec[0], -vec[1], -vec[2]]))

        return Vector(vec)

    @classmethod
    def azimuthToVector(cls, azimuth):
        """
        Convert an azimuth angle to a unit direction vector.
        
        Parameters
        ----------
        azimuth : float
            The azimuth angle in degrees, measured clockwise from the positive y-axis.
            Negative angles are normalized to the range [0, 360).
        
        Returns
        -------
        Vector
            A unit vector in the xy-plane corresponding to the given azimuth, with z-component zero.
        """
        x, y = 1, 0
        if azimuth < 0:
            azimuth = azimuth + 360
        if azimuth == 270:
            x, y = -1, 0
        elif azimuth == 0 or azimuth == 360:
            x, y = 0, 1
        elif azimuth == 180:
            x, y = 0, -1
        else:
            y = np.tan(np.radians(azimuth)) * x
            if azimuth > 180:
                x, y = -x, -y

        return cls([x, y, 0]).unit()

    def altitude(self, to_degree=False):
        """get the angle to Vector([0,0,1])"""
        tan = np.power(self.z, 2) / (np.power(self.x, 2) + np.power(self.y, 2))
        radius = np.arctan(np.sqrt(tan))
        if to_degree:
            radius *= 180 / np.pi
        return radius

    def azimuth(self, to_degree=False):
        """get the angle to Vector(0,1,0) in clockwise"""
        if self.x == 0.0:
            return 0.0 if self.y >= 0 else np.pi
        radius = np.arctan(self.y / self.x)

        if self.x <= 0.0:
            radius = np.pi + radius
        if to_degree:
            radius *= 180 / np.pi
        return radius

    def length(self, power=False):
        """
        Compute the length (magnitude) of the vector.
        
        Parameters
        ----------
        power : bool, optional
            If True, return the squared length (sum of squares) without taking the square root,
            which can accelerate the calculation. Default is False.
        
        Returns
        -------
        float
            The length of the vector. If `power` is True, returns the squared length;
            otherwise, returns the Euclidean norm.
        """
        """get length of the vector, Set power to True to accelerate the calculation
        """
        if power:
            return np.sum([i * i for i in self.array])
        else:
            return np.sqrt(np.sum([i * i for i in self.array]))

    def unit(self):
        """
            Returns a normalized vector. The original vector will be modified and returned to itself
            If you don't want to change the vector, you can do like this:
            unitVec = Vector(vec).unit()
        """
        length = self.length()
        if length == 0: raise GeometryError(self, 'zero vector')
        self.x /= length
        self.y /= length
        self.z /= length
        return self

    def quickAngle(self):
        """
            a quick calculation for angle to Vector(1,0,0)
            if the self.y>=0: get Vector.dot([1,0],vec) in [-1,1]
            if the self.y<0: get -vector.dot([1,0],vec)-2 in [-3,-1]
            the return result is in [-3,1] and is positive correlation to the angle.
            for example:
            [1,0]==1,[0,1]==0,[-1,0]==-1
            [.99,-.01]==-3,[0,-1]==-2,[-.99,-.01]==-1
        """
        if self.length() == 0:
            print('zero length vector')
            return None
        vec = self / self.length()
        dot = Vector.dot(np.array([1, 0]), vec)
        if vec[1] < 0:
            dot = -dot - 2
        return dot

    @staticmethod
    def dot(vec1, vec2):
        """call np.dot"""
        vec1 = Vector(vec1).array
        vec2 = Vector(vec2).array
        return np.sum([vec1[i] * vec2[i] for i in range(len(vec1))])

    @staticmethod
    def cross(vec1, vec2, style=np.ndarray):
        """call np.cross"""
        vec1 = Vector(vec1).array
        vec2 = Vector(vec2).array
        if style == np.ndarray:
            return np.cross(vec1, vec2)
        elif style == shapely.Geometry:
            return shapely.points(np.cross(vec1, vec2))
        else:
            return Vector(np.cross(vec1, vec2))

    @staticmethod
    def parallel(vec1, vec2, tolerance=None):
        """test if two vector is parallel, based on their dot value"""
        if tolerance is None:
            tolerance = Vector.ANGLE_TOLERANCE
        vec1 = Vector(vec1)
        vec2 = Vector(vec2)
        if vec1.length() == 0 or vec2.length() == 0:
            return True
        dot = pow(vec1 * vec2, 2) / vec2.length(True) / vec1.length(True)
        if 1.0 + tolerance > dot > 1.0 - tolerance:
            return True
        if -1.0 + tolerance > dot > -1.0 - tolerance:
            return True
        return False

    @staticmethod
    def equal(vec1, vec2):
        """
        Check if two vectors are approximately equal within a given precision.
        
        Parameters
        ----------
        vec1 : array-like
            First vector to compare. Can be a list, tuple, or array.
        vec2 : array-like
            Second vector to compare. Can be a list, tuple, or array.
        
        Returns
        -------
        bool
            True if the vectors are approximately equal within POINT_PRECISION, False otherwise.
        """
        vec1 = Vector(vec1)
        vec2 = Vector(vec2)
        if Vector(vec1.array - vec2.array).length(True) < geom.POINT_PRECISION:
            return True
        return False

    def __add__(self, other):
        """Add two Vector objects element-wise.
        
                Parameters
                ----------
                other : Vector
                    Another Vector object whose array elements will be added to this vector's array.
        
                Returns
                -------
                Vector
                    A new Vector object containing the element-wise sum of the two vectors.
                """
        return Vector(self.array + other.array)

    def __sub__(self, other):
        """
        Subtracts another vector from this vector element-wise.
        
        Parameters
        ----------
        other : Vector
            The vector to be subtracted from this vector. Must have an `array` attribute 
            compatible with numpy-style subtraction.
        
        Returns
        -------
        Vector
            A new Vector instance containing the result of element-wise subtraction 
            of `other.array` from `self.array`.
        """
        return Vector(self.array - other.array)

    def __abs__(self):
        """Return the absolute value of each component in the vector.
        
                Returns
                -------
                Vector
                    A new Vector instance with the absolute values of the original components.
                """
        return Vector(np.abs(self.array))

    def __neg__(self):
        """Return the negation of the object by multiplying by -1.
        
                Returns
                -------
                Any
                    A new object that is the negation of `self`, obtained by multiplying by -1.
        """
        return self.__mul__(-1)

    def __getitem__(self, item):
        """
        Get item from the array using indexing.
        
        Parameters
        ----------
        item : int or slice
            Index or slice object specifying the position(s) to retrieve from the array.
        
        Returns
        -------
        Any
            The element or subarray at the specified index or slice.
        """
        return self.array.__getitem__(item)

    def __mul__(self, other):
        """
        Scalar multiplication or dot product of two vectors.
        
        Parameters
        ----------
        other : Vector or float or int
            The other vector or scalar to multiply with. If `other` is a Vector, 
            computes the dot product. If `other` is a scalar (int or float), 
            performs scalar multiplication and returns a new Vector.
        
        Returns
        -------
        float or Vector
            If `other` is a Vector, returns the dot product as a float. 
            If `other` is a scalar, returns a new Vector with components scaled by the scalar.
        """
        if isinstance(other, Vector):
            return self.x * other.x + self.y * other.y + self.z * other.z
        else:
            return Vector(other * self.array)

    def __truediv__(self, other):
        """
        Divides the vector by a scalar or array and returns a new Vector instance.
        
        Parameters
        ----------
        other : scalar or array-like
            The value(s) to divide the vector's components by. Can be a scalar or an array-like 
            object compatible with numpy broadcasting rules.
        
        Returns
        -------
        Vector
            A new Vector instance containing the result of element-wise division.
        """
        return Vector(self.array / other)

    def __xor__(self, other):
        """
        Element-wise XOR or cross product operation between two Vectors or a Vector and a scalar.
        
        Parameters
        ----------
        other : Vector or array_like
            The second operand for the operation. If `other` is a Vector, computes the cross 
            product of the two vectors using their underlying arrays. If `other` is a scalar 
            or array_like, performs element-wise power (np.pow) of the vector's array with `other`.
        
        Returns
        -------
        Vector
            A new Vector instance containing the result of the cross product (if `other` is a Vector) 
            or element-wise power operation (if `other` is a scalar or array_like).
        """
        if isinstance(other, Vector):
            return Vector(np.cross(self.array, other.array))
        else:
            return Vector(np.pow(self.array, other))

    def __key(self):
        """A tuple based on the object properties, useful for hashing."""
        return (self.x, self.y, self.z)

    def __hash__(self):
        """
        Compute the hash value of the object based on its key.
        
        Returns
        -------
        int
            The hash value of the object, computed from its key.
        """
        return hash(self.__key())

    def __eq__(self, other):
        """
        Check equality between this Vector and another object.
        
        Parameters
        ----------
        self : Vector
            The first vector operand.
        other : object
            The second operand to compare against, typically a Vector or compatible object.
        
        Returns
        -------
        bool
            True if the two vectors are equal, False otherwise.
        """
        return Vector.equal(self, other)

    def __ne__(self, other):
        """Check inequality between this object and another.
        
                Parameters
                ----------
                other : object
                    The object to compare with this instance.
        
                Returns
                -------
                bool
                    True if the objects are not equal, False otherwise.
                """
        return not self.__eq__(other)

    def __repr__(self):
        """
        Return a string representation of the Vector instance with formatted coordinates.
        
        Returns
        -------
        str
            A string in the format "Vector(x, y, z)" where x, y, and z are formatted to two decimal places.
        """
        return f"Vector({'%.2f' % self.x},{'%.2f' % self.y},{'%.2f' % self.z})"


class Ray(Vector):
    """
    Defines a ray with a direction and can also be used to express an infinite plane
    origin: The origin of the ray, of the Vector type
    direction: The direction of the ray, Vector type
    value: Used to store related data, which can be in any data format
    """
    __slots__ = ['origin', 'direction', 'value']

    def __init__(self, origin, direction, value=0):
        """
        Initialize a Ray object with an origin, direction, and optional value.
        
        Parameters
        ----------
        origin : array-like or Vector
            The starting point of the ray. If not a Vector, it will be converted to one.
        direction : array-like or Vector
            The direction vector of the ray. If not a Vector, it will be converted to one.
            It will be normalized to a unit vector.
        value : float, optional
            An associated scalar value with the ray (default is 0).
        
        Returns
        -------
        None
        """
        if not isinstance(origin, Vector):
            origin = Vector(origin)
        if not isinstance(direction, Vector):
            direction = Vector(direction)

        super(Ray, self).__init__(direction.unit())
        self.origin: Vector = origin
        self.value = value
        self.direction: Vector = direction.unit()

    def reverse(self):
        """
        Reverse the direction of the ray.
        
        Parameters
        ----------
        self : Ray
            The Ray instance whose direction is to be reversed.
        
        Returns
        -------
        Ray
            A new Ray instance with the same origin and value, but with the direction reversed.
        """
        return Ray(self.origin, Vector(-self.direction.array), self.value)

    def mirror(self, mir):
        """
        Compute a mirror image of the ray based on a given normal vector.
        
        Parameters
        ----------
        mir : Vector
            The normal vector defining the plane of reflection. The direction of this vector
            is used to compute the reflection; its head and tail positions are ignored.
        
        Returns
        -------
        Ray
            A new Ray instance representing the mirrored (reflected) ray. The returned ray
            is reversed such that it represents the correct propagation direction after reflection.
        """
        """
        Compute a mirror image of itself based on the normal vector, ignoring the heads and tails of the input normal
        vector. The obtained mirror rays are equivalent to the reflection of their own relative infinite planes
        """
        if Vector.dot(self.direction, mir.direction) <= 0:
            mir = mir.reverse()
        transfrom = 2 * (self.direction.array - Vector.dot(self.direction, mir.direction) * mir.direction.array)
        return Ray(self.origin, Vector(self.direction.array + transfrom), self.value).reverse()

    def dump(self):
        """
        Get the standard ray export string for MoosasRad.exe.
        
        Parameters
        ----------
        self : object
            The instance of the class containing `origin` and `direction` attributes,
            each having an `array` property with numeric values.
        
        Returns
        -------
        str
            A comma-separated string representation of the ray's origin and direction coordinates.
        """
        """
        get the standard ray export to MoosasRad.exe
        """
        rayStr = list(self.origin.array) + list(self.direction.array)
        rayStr = [str(ray) for ray in rayStr]
        return ','.join(rayStr)

    def __repr__(self):
        """Return a string representation of the Ray object with origin and direction.
        
        Parameters
        ----------
        self : Ray
            The instance of the Ray object to represent as a string.
        
        Returns
        -------
        str
            A formatted string showing the origin and direction of the Ray.
        """
        return f"Ray( ori {self.origin.__repr__()} dir {self.direction.__repr__()} )"


class Projection(Ray):
    """
    Establish a three-dimensional coordinate system based on the infinite plane input and realize the conversion with
    the world coordinate system Since shapely does not provide 3D processing, it is necessary to process 3D collection
    processing on UVs after projection through the coordinate system
    """
    __slots__ = ['axisX']

    def __init__(self, origin, unitZ, unitX=None):
        """
        Initialize a Projection object with an origin and coordinate axes.
        
        Parameters
        ----------
        origin : array-like
            The origin point of the projection, converted to a Vector.
        unitZ : array-like
            The unit vector defining the Z-axis direction of the projection.
        unitX : array-like, optional
            The unit vector defining the X-axis direction. If not provided, it is 
            computed automatically based on the Z-axis and a default reference direction.
        
        Returns
        -------
        None
            This method initializes the object and does not return a value.
        """
        origin = Vector(origin)
        unitZ = Vector(unitZ).unit().array
        super(Projection, self).__init__(origin, unitZ)
        if unitX is not None:
            self.axisX = Vector(unitX).unit().array
        else:
            if not Vector.parallel(self.axisZ, np.array([0, 0, 1])):
                self.axisX = Vector(np.cross(np.array([0, 0, 1]), self.axisZ)).unit().array
            else:
                self.axisX = np.array([1, 0, 0])

    @classmethod
    def fromRay(cls, plane: Ray):
        """
        Create a new instance from a Ray object.
        
        Parameters
        ----------
        plane : Ray
            The Ray object containing origin and direction used to create the new instance.
        
        Returns
        -------
        Projection
            A new instance of the class initialized with the origin and direction from the Ray.
        """
        return cls(plane.origin, plane.direction)

    @classmethod
    def fromPolygon(cls, polygon: shapely.Geometry):
        """
        Construct a coordinate system from a given polygon.
        
        Parameters
        ----------
        polygon : shapely.Geometry
            A polygon geometry used to define the coordinate system. Must be a valid 3D polygon.
        
        Returns
        -------
        Projection
            An instance of the class representing the coordinate system defined by the polygon's 
            normal vector, center point, and an orthogonal basis vector derived from a cross-section.
        """
        unitz = faceNormal(polygon)
        if Vector.parallel(unitz, np.array([0, 0, 1])):
            return cls.findOrthogonalBasis([polygon])
        center = np.mean(shapely.get_coordinates(polygon, include_z=True), axis=0)
        return cls(center, unitz)

    @classmethod
    def findOrthogonalBasis(cls, polygons):
        """
        Find an orthogonal basis from the most frequent edge directions in given polygons.
        
        Parameters
        ----------
        polygons : shapely.Geometry or list of shapely.Geometry
            A single polygon or a list of polygons from which edge vectors are extracted 
            to determine the dominant orthogonal axes.
        
        Returns
        -------
        proj : object
            An instance of the class (likely a coordinate system or projection object) 
            representing the orthogonal basis, with `unitX` aligned to the most frequent 
            edge direction and `unitZ` set to [0, 0, 1]. The basis is centered at the 
            mean coordinates of all input polygons.
        """
        """
        Find a twin of orthogonal axis from given polygons by counting the most popular vectors in the polygons' edges
        """
        if isinstance(polygons, shapely.Geometry):
            polygons = [polygons]
        # Organize the axis of the boundary, and find the most oriented direction to establish the coordinate system
        projAxisCount = {}  # statistics of the axis
        edgeVectors = []  # the orientation of all edges
        for boundary in polygons:
            coordinates = shapely.get_coordinates(shapely.force_3d(boundary, z=0), include_z=True)
            coordinates = np.nan_to_num(coordinates, nan=0)
            edgeVectors += [coordinates[i] - coordinates[i + 1] for i in range(len(coordinates) - 1)]
            for vec in edgeVectors:
                if Vector(vec).length() > geom.POINT_PRECISION:
                    vecStr = Vector(vec).unit().string
                    if vecStr not in projAxisCount.keys():
                        projAxisCount[vecStr] = 1
                    else:
                        projAxisCount[vecStr] += 1

        # Sort the axis to find the one facing the most, as unitX of the projection. (unitZ == [0,0,1])
        sortlist = [[vecStr, projAxisCount[vecStr]] for vecStr in projAxisCount.keys()]
        sortlist.sort(key=lambda x: (x[1]))
        unitX = sortlist[-1][0]
        unitX = [eval(dim) for dim in unitX.split('_')]
        center = np.mean(shapely.get_coordinates(polygons, include_z=True), axis=0)
        proj = cls(center, [0, 0, 1], unitX)  # The orthogonal coordinate system applied when orthogonalization
        return proj

    @property
    def rotateMatrix(self):
        """
        Rotation matrix representing the orientation axes.
        
        Returns
        -------
        numpy.ndarray
            A 3x3 array where each column is one of the local axes (axisX, axisY, axisZ),
            representing the rotation from the local coordinate system to the global coordinate system.
        """
        return np.array([self.axisX, self.axisY, self.axisZ]).T

    @property
    def axisZ(self):
        """
        Return the array attribute as a property.
        
        Returns
        -------
        numpy.ndarray
            The array associated with the instance.
        """
        return self.array

    @property
    def axisY(self):
        """
        Return the Y-axis vector of the right-handed local coordinate system.
        
        Parameters
        ----------
        self : object
            The instance of the class containing axisX and axisZ attributes, which are 3D vectors.
        
        Returns
        -------
        numpy.ndarray
            A 3D vector representing the Y-axis, computed as the cross product of axisZ and axisX.
        """
        return np.cross(self.axisZ, self.axisX)

    def toUV(self, worldGeometry: shapely.Geometry):
        """
        Converts a geometry from world coordinates to UV coordinates on a specified plane.
        
        Parameters
        ----------
        worldGeometry : shapely.Geometry
            The input geometry in world coordinates to be transformed. Can be a point, 
            line, or polygon. Must be a valid 2D or 3D geometry.
        
        Returns
        -------
        shapely.Geometry
            The geometry transformed into UV coordinates on the specified plane. The 
            output retains the type (point, linestring, polygon, etc.) of the input 
            geometry but is represented in the 2D UV coordinate system of the plane.
        """
        """
        Converts geometry from world coordinates to a specified plane, obtaining UV coordinates on that plane
        """
        dims = shapely.get_dimensions(worldGeometry)
        if isinstance(dims, np.ndarray):
            flat_geometries = np.array(worldGeometry, dtype=object).flatten().tolist()
            valid_geometries = [geo for geo in flat_geometries if shapely.get_dimensions(geo) != -1]
            if len(valid_geometries) == 0:
                raise Exception(f'invalid geometry: {worldGeometry}')
            # Keep compatibility with callers expecting a single geometry in projection.
            worldGeometry = valid_geometries[0]
            dims = shapely.get_dimensions(worldGeometry)

        if dims == -1:
            raise Exception(f'invalid geometry: {worldGeometry}')
        if dims == 2:
            rings = [[self.toUV(ring) for ring in shapely.get_rings(part)] for part in shapely.get_parts(worldGeometry)]
            parts = []
            for ring in rings:
                if len(ring) > 1:
                    try:
                        parts.append(shapely.polygons(rings[0], rings[1:]))
                    except Exception as e:
                        # failed to create hole
                        parts.append(shapely.polygons(rings[0]))
                else:
                    parts.append(shapely.polygons(rings[0]))
            return shapely.union_all(parts)
        worldGeometry = shapely.force_3d(worldGeometry, z=0)
        coors = shapely.get_coordinates(worldGeometry, include_z=True)
        coors = np.nan_to_num(coors, nan=0)
        coor_new = [coor - self.origin.array for coor in coors]
        coor_new = [coor @ self.rotateMatrix for coor in coor_new]
        coor_new = np.array([coor.flatten() for coor in coor_new])
        if dims == 0:
            return shapely.points(coor_new[0])
        if dims == 1:
            if shapely.points(coor_new[0]) == shapely.points(coor_new[-1]):
                return shapely.linearrings(coor_new)
            else:
                return shapely.linestrings(coor_new)
        # if shapely.get_dimensions(worldGeometry) == 2:
        #     return shapely.polygons(coor_new)

    def toWorld(self, UVGeometry: shapely.Geometry):
        """
        Converts a geometry from UV coordinate system to world coordinate system.
        
        Parameters
        ----------
        UVGeometry : shapely.Geometry
            The input geometry in UV coordinates to be transformed. Must be a valid 2D or 3D geometry.
        
        Returns
        -------
        shapely.Geometry
            The transformed geometry in the world coordinate system. The type of geometry (point, linestring, polygon) 
            is preserved after transformation.
        """
        """
        Converts the geometry represented by UV coordinates to the world coordinate system
        """
        if shapely.get_dimensions(UVGeometry) == -1:
            raise Exception(f'invalid geometry: {UVGeometry}')
        if shapely.get_dimensions(UVGeometry) == 2:
            rings = [self.toWorld(ring) for ring in shapely.get_rings(UVGeometry)]
            if len(rings) == 0:
                return shapely.GeometryCollection()
            if len(rings) > 1:
                return shapely.polygons(
                    shapely.get_coordinates(rings[0], include_z=True),
                    [shapely.get_coordinates(ring, include_z=True) for ring in rings[1:]]
                )
            else:
                return shapely.polygons(shapely.get_coordinates(rings[0], include_z=True))
        UVGeometry = shapely.force_3d(UVGeometry, z=0)
        coors = shapely.get_coordinates(UVGeometry, include_z=True)
        coor_new = [coor @ np.linalg.inv(self.rotateMatrix) for coor in coors]
        coor_new = np.array([coor + self.origin.array for coor in coor_new])

        if shapely.get_dimensions(UVGeometry) == 0:
            return shapely.points(coor_new[0])
        if shapely.get_dimensions(UVGeometry) == 1:
            return shapely.linestrings(coor_new)


class Transformation2d:
    """
    Realize two-dimensional transformation, including movement and rotation, and define the rotation angle in clockwise
    """
    __slots__ = ['moveVec', 'rotateRadius', 'rotateOrigin']

    def __init__(self, moveVec=np.array([0, 0]), rotateRadius: float = 0, rotateOrigin=None):
        """
        Initialize transformation parameters for movement and rotation.
        
        Parameters
        ----------
        moveVec : array-like or shapely.Geometry, optional
            Vector representing the translation to apply. If a shapely Geometry is provided,
            its coordinates are extracted. Default is numpy array [0, 0].
        rotateRadius : float, optional
            Angular distance (in radians) to rotate. Default is 0.
        rotateOrigin : array-like or shapely.Geometry, optional
            Point around which rotation occurs. If a shapely Geometry is provided,
            its coordinates are extracted. If None, no rotation origin is set. Default is None.
        
        Returns
        -------
        None
        """
        if isinstance(moveVec, shapely.Geometry):
            moveVec = shapely.get_coordinates(moveVec)
        if isinstance(rotateOrigin, shapely.Geometry):
            rotateOrigin = shapely.get_coordinates(rotateOrigin)
        self.rotateRadius = rotateRadius
        self.moveVec = np.array(moveVec)
        self.rotateOrigin = None
        if rotateOrigin is not None:
            self.rotateOrigin = np.array(rotateOrigin)

    @property
    def rotateMatrix(self):
        """
        Rotation matrix property based on the object's rotation angle.
        
        Returns the inverse of the 2D rotation matrix corresponding to the `rotateRadius` attribute,
        where the rotation angle is given in radians. The returned matrix can be used to reverse
        the rotation transformation.
        
        Parameters
        ----------
        self : object
            The instance having the `rotateRadius` attribute, which specifies the rotation
            angle in radians.
        
        Returns
        -------
        numpy.ndarray
            A 2x2 inverse rotation matrix represented as a NumPy array.
        """
        rotateMatrix = [np.cos(self.rotateRadius), -np.sin(self.rotateRadius)], [np.sin(self.rotateRadius),
                                                                                 np.cos(self.rotateRadius)]
        rotateMatrix = np.linalg.inv(np.array(rotateMatrix).T)
        return rotateMatrix

    @classmethod
    def opposite(cls, transformation):
        """
        Get the inverse of a given transformation.
        
        Parameters
        ----------
        transformation : object
            An object representing a transformation, which must have attributes `moveVec` (numpy.ndarray or similar),
            `rotateAngle` (float), and `rotateOrigin` (numpy.ndarray or None). The `moveVec` represents the translation 
            vector, `rotateAngle` the rotation angle in radians, and `rotateOrigin` the origin point for rotation, if any.
        
        Returns
        -------
        object
            A new instance of the class (same type as `cls`) representing the opposite transformation, with reversed 
            translation (`-moveVec`), reversed rotation angle (`-rotateAngle`), and adjusted rotation origin if applicable.
        """
        """
            get the opposite transformation.
        """
        if transformation.rotateOrigin is not None:
            rotateOrigin = transformation.rotateOrigin - transformation.moveVec
            return cls(- transformation.moveVec, - transformation.rotateAngle, rotateOrigin)
        else:
            return cls(- transformation.moveVec, - transformation.rotateAngle)

    def transfrom(self, geo: shapely.Geometry):
        """
        Transform a geometry by applying translation followed by rotation.
        
        Parameters
        ----------
        geo : shapely.Geometry
            The input geometry to be transformed. Can be a point, linestring, or polygon.
        
        Returns
        -------
        shapely.Geometry
            The transformed geometry after applying translation and optional rotation.
            The output type matches the input geometry type (point, linestring, or polygon).
        """
        """
        Move first then rotate next. If the rotate origin do not provide, it will rotate around its weight center:
        rotateOrigin = np.array([np.mean(coor) for coor in coordiantes.T])
        It can accept geometries in different dimensions
        """
        coordiantes = shapely.get_coordinates(geo)
        coordiantes = np.array([coor + self.moveVec for coor in coordiantes])
        if self.rotateRadius != 0:
            rotateOrigin = self.rotateOrigin
            if rotateOrigin is None:
                rotateOrigin = np.array([np.mean(coor) for coor in coordiantes.T])
            coordinatesRelative = np.array([coor - rotateOrigin for coor in coordiantes])
            coordinatesRelative = np.array([coor @ self.rotateMatrix for coor in coordinatesRelative])
            coordiantes = np.array([coor + rotateOrigin for coor in coordinatesRelative])
        if shapely.get_dimensions(geo) == 0:
            return shapely.points(coordiantes[0])
        if shapely.get_dimensions(geo) == 1:
            return shapely.linestrings(coordiantes)
        if shapely.get_dimensions(geo) == 2:
            return shapely.polygons(coordiantes)


def bBox(geo: shapely.Geometry):
    """calculate the bounding box of the geometry with direction(calculating by OrthogonalBasis):
    two projection will be done:
    1. project the geo to 2d faces geoProj as projection 1
    2.1 in the projection 1, find the Orthogonal Basis of geoProj as projection 2
    2.2 reversed project.axisX the projection 2 to the world
    3. construct bBoxProjection using the projection2World.axisX and projection1.axisZ

    ----------------------------------------
    geo (shapely.Geometry) : input 3d geometry

    returns: dict() include:
    Projection:(Projection) the OrthogonalBasis projection (3d)
    x-domain:(float,float) x min and max of the bBox
    y-domain:(float,float) y min and max of the bBox
    """
    proj1 = Projection.fromPolygon(geo)
    geoProj = proj1.toUV(geo)
    proj2 = Projection.findOrthogonalBasis(geoProj)
    minX, minY, maxX, maxY = shapely.bounds(geoProj)
    worldAxisX = proj1.toWorld(Vector(proj2.axisX).geometry)
    worldAxisZ = proj1.axisZ
    origin = shapely.centroid(geo)
    bBoxProjection = Projection(origin=origin, unitZ=worldAxisZ, unitX=worldAxisX)
    return {"Projection": bBoxProjection, "x-domain": (minX, maxX), "y-domain": (minY, maxY)}


def is_ccw(geo: shapely.Geometry) -> bool:
    """
    Determine if a polygon's ring is oriented counter-clockwise.
    
    Parameters
    ----------
    geo : shapely.Geometry
        A geometry object representing a polygon or line string. Must have at least 3 points to form a ring.
    
    Returns
    -------
    bool
        True if the ring is oriented counter-clockwise, False otherwise.
    """

    ring = geo.exterior if isinstance(geo, shapely.Polygon) else shapely.LinearRing(shapely.get_coordinates(geo))
    return bool(shapely.is_ccw(ring))


def selfIntersect(geo: shapely.Geometry) -> bool:
    """
    Test whether a geometry is self-intersecting.
    
    Parameters
    ----------
    geo : shapely.Geometry
        A Shapely geometry object to be tested for self-intersection.
    
    Returns
    -------
    bool
        True if the geometry is self-intersecting, False otherwise.
    """
    """
    test whether a geometry is self-intersect
    """

    pointList = shapely.points(shapely.get_coordinates(geo, include_z=True))
    if (len(pointList) - len(set(pointList))) > 1:
        return True
    # if str(shapely.is_valid_reason(geo)).startswith('Self-intersection'):
    #     return True
    return False


def overlapEdge(geo1: shapely.Geometry, geo2: shapely.Geometry) -> bool:
    """
    Determine if two geometries share at least two endpoints, indicating a common edge.
    
    Parameters
    ----------
    geo1 : shapely.Geometry
        First geometry object to compare.
    geo2 : shapely.Geometry
        Second geometry object to compare.
    
    Returns
    -------
    bool
        True if the two geometries share at least two endpoints (i.e., overlap on an edge), False otherwise.
    """
    """
    Determines whether two geometries containBy on at least 2 endpoints,
    which means that they share an exact same edge
    """
    try:
        geo1 = shapely.get_coordinates(geo1, include_z=True)
        geo1 = set(shapely.set_precision(
            shapely.points(geo1),
            geom.POINT_PRECISION))
        geo2 = shapely.get_coordinates(geo2, include_z=True)
        geo2 = set(shapely.set_precision(
            shapely.points(geo2),
            geom.POINT_PRECISION))
        if len(geo1.intersection(geo2)) >= 2:
            return True
        return False
    except:
        return False


def overlapArea(geo1: shapely.Geometry, geo2: shapely.Geometry) -> float:
    """
    Calculate the overlapping area between two geometries.
    
    Parameters
    ----------
    geo1 : shapely.Geometry
        The first input geometry.
    geo2 : shapely.Geometry
        The second input geometry.
    
    Returns
    -------
    float
        The area of the intersection between geo1 and geo2. Returns 0.0 if there is no overlap, 
        if either geometry is empty, or if an error occurs during computation.
    """
    """
    retrun the containBy area of two geometries
    """

    if shapely.is_empty(geo1) or shapely.is_empty(geo1):
        return 0.0
    geo1 = makeValid(geo1)[0]
    geo2 = makeValid(geo2)[0]
    try:
        if shapely.disjoint(geo1, geo2):
            return 0.0
        intersections = shapely.intersection(geo1, geo2, grid_size=geom.POINT_PRECISION)
        if shapely.get_dimensions(intersections) != 2:
            return 0.0
        area1 = shapely.area(intersections)
    except:
        return 0.0
    return area1


def area3d(geo: shapely.Geometry) -> float:
    """Calculate polygon area in 3D by projecting to its local UV plane.

    shapely only measures planar 2D area, so this helper projects each
    polygon part to its own UV coordinate system and sums UV areas.
    """
    if geo is None or shapely.is_empty(geo):
        return 0.0

    total = 0.0
    try:
        parts = [p for p in shapely.get_parts(geo) if shapely.get_dimensions(p) == 2]
    except Exception:
        parts = [geo] if shapely.get_dimensions(geo) == 2 else []

    for part in parts:
        try:
            proj = Projection.fromPolygon(part)
            uv = shapely.force_2d(proj.toUV(part))
            total += float(shapely.area(uv))
        except Exception:
            continue
    return total


def makeValid(geo: shapely.Geometry, error='raise') -> list[shapely.Geometry] | None:
    """revise method of shapely.make_valid()"""
    geos = shapely.make_valid(geo)
    geos = [g for g in shapely.get_parts(geos) if shapely.get_dimensions(g) == 2]
    if len(geos) == 0:
        if error == 'raise':
            raise GeometryError(geo, "No valid geometries")
        else:
            print('******Warning: GeometryError: no valid geometries')
            return None
    for i, g in enumerate(geos):
        rings = shapely.get_rings(g)
        if len(rings) > 1:
            innerRings = [shapely.intersection(r, rings[0], grid_size=geom.POINT_PRECISION) for r in rings[1:]]
            innerRings = [r for r in innerRings if shapely.get_dimensions(r) == 2]
            # print(rings[0],innerRings)
            if len(innerRings) > 0:
                geos[i] = shapely.polygons(shapely.get_coordinates(rings[0], include_z=True), holes=innerRings)
            else:
                geos[i] = shapely.linestrings(shapely.get_coordinates(rings[0], include_z=True))
    return geos


def contains(child: shapely.Geometry, parent: shapely.Geometry):
    """
    Check if all points of a child geometry are within a specified distance from a parent geometry.
    
    Parameters
    ----------
    child : shapely.Geometry
        The geometry whose points are to be checked for proximity to the parent.
    parent : shapely.Geometry
        The geometry used as reference for proximity checking.
    
    Returns
    -------
    bool
        True if all points of the child geometry are within twice the POINT_PRECISION distance 
        from the parent geometry, False otherwise. Returns False if an error occurs during processing.
    """
    # child = shapely.set_precision(child, geom.POINT_PRECISION)
    child = shapely.get_coordinates(child)
    # print(child)
    # parent = shapely.set_precision(parent, geom.POINT_PRECISION)
    # geo1=shapely.get_point(geo1,[0,1])
    try:
        for i in range(len(child)):
            # if shapely.dwithin(geo1[0],geo2,2*geom.POINT_PRECISION) and shapely.dwithin(geo1[1],geo2,2*geom.POINT_PRECISION):
            if not shapely.dwithin(shapely.points(child[i]), parent, 2 * geom.POINT_PRECISION):
                # if not shapely.contains(parent,shapely.points(child[i])):
                return False
    except:
        return False
    return True


def equals(geo1: shapely.Geometry, geo2: shapely.Geometry):
    """Check if two geometries are approximately equal by comparing their points within a tolerance.
    
    Parameters
    ----------
    geo1 : shapely.Geometry
        First geometry to compare.
    geo2 : shapely.Geometry
        Second geometry to compare.
    
    Returns
    -------
    bool
        True if the geometries have the same number of points and all corresponding points 
        (in forward or reverse order) are within 1.2 * geom.POINT_PRECISION distance; otherwise False.
    """
    geo1 = shapely.get_point(geo1, range(shapely.get_num_points(geo1)))
    geo2 = shapely.get_point(geo2, range(shapely.get_num_points(geo2)))
    if len(geo1) != len(geo2): return False
    valid = True
    for i in range(len(geo1)):
        if not shapely.dwithin(geo1[i], geo2[i], 1.2 * geom.POINT_PRECISION):
            valid = False
    if not valid:
        valid = True
        geo2 = geo2[::-1]
        for i in range(len(geo1)):
            if not shapely.dwithin(geo1[i], geo2[i], 1.2 * geom.POINT_PRECISION):
                valid = False
    if valid:
        return True
    else:
        return False


def projectTo(child: shapely.Geometry, parent: shapely.Geometry) -> shapely.Geometry:
    """
        project a child surface to the parent surface.

        Parameters
        ----------
        child : shapely.Geometry
            3D polygon geometry with z-coordinates (shapely.Polygon type) to be projected.
        parent : shapely.Geometry
            3D polygon geometry with z-coordinates (shapely.Polygon type) as the Projection aixs system.

        Returns
        -------
        shapely.Geometry

        Notes
        -----
        - Projection.toUV method
    """
    proj = Projection.fromPolygon(parent)
    childProj = proj.toUV(child)
    childProj = shapely.force_3d(shapely.force_2d(childProj), z=0)
    child = proj.toWorld(childProj)
    if Vector.dot(faceNormal(child), faceNormal(parent)) < 0:
        coordinates = shapely.get_coordinates(child,include_z=True)[::-1]
        child = shapely.polygons(coordinates)
    return child


def trim(child: shapely.Geometry, parent: shapely.Geometry) -> shapely.Geometry | None:
    """
    Trim a child surface with the parent surface.

    Parameters
    ----------
    child : shapely.Geometry
        3D polygon geometry with z-coordinates (shapely.Polygon type) to be trimmed.
    parent : shapely.Geometry
        3D polygon geometry with z-coordinates (shapely.Polygon type) as the splitter.

    Returns
    -------
    shapely.Geometry

    Notes
    -----
    - shapely.intersection method
    """
    proj = Projection.fromPolygon(parent)
    childProj = proj.toUV(child)
    parentProj = proj.toUV(parent)
    if overlapArea(childProj, parentProj) < shapely.area(childProj):
        childProjIntersection = shapely.intersection(childProj, parentProj)
        if shapely.get_dimensions(childProjIntersection) == 2:
            childProjIntersection = shapely.get_parts(childProjIntersection)[0]
            return proj.toWorld(childProjIntersection)
        else:
            return None
    return child


def offset(polygon: shapely.Geometry, offset: float) -> shapely.Geometry:
    """
    Offset a geometry by a specified offset. Positive for outer offset and negative for inner offset.

    Parameters
    ----------
    polygon : shapely.Geometry
        3D polygon geometry with z-coordinates (shapely.Polygon type) to be offset.
    offset : float
        offset distance

    Returns
    -------
    shapely.Geometry

    Notes
    -----
    - shapely.buffer method


    """
    proj = Projection.fromPolygon(polygon)
    polygonProj = proj.toUV(polygon)
    polygonProjOffset = shapely.buffer(polygonProj, offset)
    return proj.toWorld(polygonProjOffset)


def faceNormal(poly: shapely.Geometry, EPS: float = geom.POINT_PRECISION) -> Vector:
    """
    Compute a stable plane normal for a 3D polygon, independent of vertex winding.
    Use ``ccwNormal`` when the normal must follow the exterior vertex order.

    Parameters
    ----------
    poly : shapely.Geometry
        3D polygon geometry with z-coordinates (shapely.Polygon type)
    EPS : float, optional
        Floating point precision threshold (default: 1e-9)

    Returns
    -------
    Vector
        Unit normal vector (x, y, z) of shape (3,)
        Returns None if computation fails (invalid input/insufficient vertices)

    Notes
    -----
    - Uses PCA + SVD for non-coplanar vertices (most stable method)
    - Falls back to edge cross product for triangular faces (faster)
    - The plane-normal sign does not encode the polygon's vertex winding
    """
    # ------------------- Step 1: Validate input and extract 3D coordinates -------------------

    # Extract 3D coordinates (remove closing duplicate point)
    coords = shapely.get_coordinates(poly, include_z=True)
    if coords.shape[1] != 3:
        return Vector(0, 0, 1)

    # Remove duplicate closing point and filter valid vertices
    vertices = coords[:-1] if np.allclose(coords[0], coords[-1], atol=EPS) else coords
    vertices = np.unique(vertices, axis=0)  # Remove duplicate vertices
    n_vertices = len(vertices)

    if n_vertices < 3:
        return Vector(0, 0, 1)  # Need at least 3 unique vertices

    # ------------------- Step 2: Fast path for triangular faces (3 vertices) -------------------
    if n_vertices == 3:
        # Compute two edge vectors
        v1 = vertices[1] - vertices[0]
        v2 = vertices[2] - vertices[0]

        # Cross product for normal
        normal = np.cross(v1, v2)
        norm = np.linalg.norm(normal)

        if norm < EPS:
            print("******GeometryError: invalid face results in zero normal")
            return Vector(0, 0, 1)  # Collinear vertices
        return Vector(normal / norm)

    # ------------------- Step 3: PCA + SVD for non-coplanar polygons (stable method) -------------------
    # Center vertices (remove translation)
    centroid = np.mean(vertices, axis=0)
    centered = vertices - centroid

    # Compute covariance matrix (3x3, symmetric)
    cov_matrix = np.dot(centered.T, centered) / (n_vertices - 1)

    # SVD decomposition (stable for small matrices)
    _, _, vh = np.linalg.svd(cov_matrix)
    normal = vh[-1]  # Normal = eigenvector with smallest eigenvalue (best fit plane)

    # ------------------- Step 4: Ensure consistent orientation -------------------
    # Use right-hand rule with first two edges for orientation consistency
    edge1 = vertices[1] - vertices[0]
    edge2 = vertices[2] - vertices[0]
    reference_normal = np.cross(edge1, edge2)

    # Flip normal if it opposes reference orientation
    if np.dot(normal, reference_normal) < 0:
        normal = -normal

    # ------------------- Step 5: Normalize and return unit vector -------------------
    norm = np.linalg.norm(normal)
    if norm > EPS:
        norm = normal / norm
        return Vector(norm)
    else:
        raise GeometryError(poly, "******GeometryError: invalid face results in zero normal")

def ccwNormal(poly: shapely.Geometry, EPS: float = geom.POINT_PRECISION) -> Vector:
    """
    Return the right-hand normal determined by the polygon's exterior winding.

    Parameters
    ----------
    poly : shapely.Geometry
        3D polygon geometry with z-coordinates (shapely.Polygon type)
    EPS : float, optional
        Floating point precision threshold (default: 1e-9)

    Returns
    -------
    Vector
        Unit normal vector (x, y, z) consistent with counterclockwise vertices.

    Notes
    -----
    - Uses PCA + SVD for non-coplanar vertices (most stable method)
    - Falls back to edge cross product for triangular faces (faster)
    - Ensures consistent orientation via right-hand rule
    """
    norm = faceNormal(poly, EPS)
    proj = Projection(origin=shapely.get_coordinates(poly)[0],unitZ=norm)
    polyProj = proj.toUV(poly)
    polyProj = shapely.force_2d(polyProj)
    if is_ccw(polyProj):
        return norm
    else:
        return -norm
# def faceNormalLegacy(face: shapely.Geometry) -> Vector:
# #     """Calculate the normal vector of a face using cross product of non-parallel edges.
# #
# #         Parameters
# #         ----------
# #         face : shapely.Geometry
# #             A geometry object representing a face or linestring. Coordinates are extracted to compute edge vectors.
# #
# #         Returns
# #         -------
# #         Vector
# #             A unit vector representing the normal to the face, computed via the cross product of two non-parallel edges.
# #             If no such pair is found, returns a Vector constructed directly from the face.
# #         """
# #     """calculate the face normal by cross calculation.
# #     we only need to find two edges that do not parallel.
# #     in this case, this method is valid even if a linestring is provided
# #     """
# #     coordinates = shapely.get_coordinates(face, include_z=True)
# #     edges = [coordinates[i] - coordinates[i + 1] for i in range(len(coordinates) - 1)]
# #     for i in range(1, len(edges)):
# #         if not Vector.parallel(edges[i], edges[0]):
# #             return Vector(np.cross(edges[i], edges[0])).unit()
# #     return Vector(face)


"""constructive methods"""


def difference(geoBase: shapely.Geometry, geoDifference: shapely.Geometry) -> list[shapely.Geometry]:
    """
    3D difference operation between two polygons.
    
    Performs a 3D boolean difference between a base geometry and a differencing geometry by projecting 
    them into a 2D UV plane, computing the difference, and transforming the result back to 3D space.
    
    Parameters
    ----------
    geoBase : shapely.Geometry
        The base geometry from which parts will be subtracted. Must be a valid polygon.
    geoDifference : shapely.Geometry
        The geometry to subtract from the base. Must be a valid polygon.
    
    Returns
    -------
    list of shapely.Geometry
        A list of geometries representing the result of the 3D difference operation.
    """
    """
        3d difference for polygons
    """
    proj = Projection(
        origin=shapely.points(shapely.get_coordinates(geoBase)[0]),
        unitZ=faceNormal(geoBase)
    )
    geoBaseProj = shapely.set_precision(proj.toUV(geoBase), geom.POINT_PRECISION)
    geoDifferenceProj = shapely.set_precision(proj.toUV(geoDifference), geom.POINT_PRECISION)
    geoBaseProj = shapely.difference(geoBaseProj, geoDifferenceProj, grid_size=geom.POINT_PRECISION)
    return proj.toWorld(geoBaseProj)


def intersection(geoBase: shapely.Geometry, geoDifference: shapely.Geometry) -> list[shapely.Geometry]:
    """
    Compute the 3D intersection of two geometric polygons by projecting them into 2D, performing the intersection, and transforming back.
    
    Parameters
    ----------
    geoBase : shapely.Geometry
        The base geometry (polygon) involved in the intersection.
    geoDifference : shapely.Geometry
        The geometry to intersect with the base geometry.
    
    Returns
    -------
    list of shapely.Geometry
        A list containing the resulting geometry or geometries from the intersection operation in 3D space.
    """
    """
        3d difference for polygons
    """
    proj = Projection(
        origin=shapely.points(shapely.get_coordinates(geoBase)[0]),
        unitZ=faceNormal(geoBase)
    )
    geoBaseProj = shapely.set_precision(proj.toUV(geoBase), geom.POINT_PRECISION)
    geoDifferenceProj = shapely.set_precision(proj.toUV(geoDifference), geom.POINT_PRECISION)
    geoBaseProj = shapely.difference(geoBaseProj, geoDifferenceProj, grid_size=geom.POINT_PRECISION)
    return proj.toWorld(geoBaseProj)


def rayFaceIntersect(ray: Ray, face: shapely.Geometry,
                     normal: Vector = None, infinity_face=False, limit_distance=None) -> shapely.Geometry | None:
    """func to calculate the intersection for face and ray in many circumstances.

    ray: input ray as Ray object
    face: input face as shapely.Geometry
    normal: faceNormal(face), you can provide one to accelerate the calculation
    infinity_face: do not test the containment of the face and the intersection
    limit_distance: the "ray" is a line and have limit length

    return: point as shapely.points, None if no intersection

    --------------------------------------
        plan expression: (P - p0).n = 0
        ray expression: P(t) = p1 + tu
        cross them: (P(t) - p0).n = (p1 + tu - p0).n = 0
        as result: P(t) = p1 + t*u = p1 + ((p0 - p1).n/u.n) * u
    """
    if normal is None:
        normal = faceNormal(face)
    if Vector.dot(ray.direction, normal) == 0:
        return None

    vec = ray.direction.unit().array
    normal = Vector(normal).unit().array

    p0 = shapely.get_coordinates(face, include_z=True)[0]
    p1 = ray.origin.array
    t = np.dot((p0 - p1), normal) / np.dot(normal, vec)
    if t < 0:
        return None
    if limit_distance is not None:
        if t > limit_distance:
            return None
    pt = p1 + t * vec
    if infinity_face:
        return shapely.points(pt)
    else:
        coordinates = shapely.get_coordinates(face, include_z=True)
        if np.min(coordinates[:, 0]) <= pt[0] <= np.max(coordinates[:, 0]):
            if np.min(coordinates[:, 1]) <= pt[1] <= np.max(coordinates[:, 1]):
                if np.min(coordinates[:, 2]) <= pt[2] <= np.max(coordinates[:, 2]):
                    return shapely.points(pt)
        # proj = Projection(origin=p0, unitZ=normal)
        # face = proj.toUV(face)
        # pt = proj.toUV(pt)
        # if shapely.contains(shapely.force_2d(face), shapely.force_2d(pt)):
        #     return proj.toWorld(pt)

        else:
            return None


def simplify(geo: shapely.Geometry, include_z=False) -> shapely.Geometry:
    """simplified the geometry to remove redundant points where the last and next directions are parallel"""
    dimensions = shapely.get_dimensions(geo)
    coordinates = shapely.get_coordinates(geo, include_z=include_z)
    if len(coordinates) == 0:
        return geo

    is_polygon = dimensions == 2
    if is_polygon and len(coordinates) > 1 and np.allclose(
            coordinates[0], coordinates[-1], atol=geom.POINT_PRECISION):
        coordinates = coordinates[:-1]

    # First remove only adjacent duplicate points.  Keeping this as a Python
    # list avoids ``np.append`` flattening Shapely object arrays.
    simplified = []
    for point in coordinates:
        if not simplified or np.linalg.norm(point - simplified[-1]) >= geom.POINT_PRECISION:
            simplified.append(point)
    if is_polygon and len(simplified) > 1 and np.linalg.norm(simplified[0] - simplified[-1]) < geom.POINT_PRECISION:
        simplified.pop()

    minimum_vertices = 3 if is_polygon else 2
    if len(simplified) < minimum_vertices:
        raise GeometryError(geo, f"requires at least {minimum_vertices} unique vertices")

    def _is_redundant_middle(previous, current, following):
        incoming = current - previous
        outgoing = following - current
        incoming_length = np.linalg.norm(incoming)
        outgoing_length = np.linalg.norm(outgoing)
        if incoming_length < geom.POINT_PRECISION or outgoing_length < geom.POINT_PRECISION:
            return True
        # Delete a vertex only when it lies on the same directed line.  The
        # former angle-only test treated narrow, valid triangles as collinear.
        if len(incoming) == 2:
            cross_length = abs(incoming[0] * outgoing[1] - incoming[1] * outgoing[0])
        else:
            cross_length = np.linalg.norm(np.cross(incoming, outgoing))
        line_distance = cross_length / max(incoming_length, outgoing_length)
        return line_distance < geom.POINT_PRECISION and np.dot(incoming, outgoing) > 0

    changed = True
    while changed and len(simplified) > minimum_vertices:
        changed = False
        for index in range(len(simplified)):
            if not is_polygon and index in (0, len(simplified) - 1):
                continue
            previous = simplified[index - 1]
            following = simplified[(index + 1) % len(simplified)]
            if _is_redundant_middle(previous, simplified[index], following):
                simplified.pop(index)
                changed = True
                break

    coordinates = np.asarray(simplified, dtype=float)
    if dimensions == 1:
        return shapely.linestrings(coordinates)
    if is_polygon:
        try:
            # Shapely's LinearRing requires an explicit closing coordinate.
            return shapely.polygons(np.vstack((coordinates, coordinates[0])))
        except Exception as e:
            raise GeometryError(geo, str(e))
    return geo


def split(geo: shapely.Geometry, spliter: Ray | shapely.Geometry, normal=None) -> list[list[shapely.Geometry]]:
    """
    Split a polygon geometry using a curve or plane.
    
    Parameters
    ----------
    geo : shapely.Geometry
        The input polygon geometry to be split. Only polygon geometries are supported.
    spliter : Ray or shapely.Geometry
        The splitting element, which can be a Ray object representing a plane, or a shapely.Geometry
        (e.g., line or polygon) used to define the split. If a Ray is provided, it defines both the
        splitting plane and its normal direction.
    normal : array-like or None, optional
        The normal vector of the splitting plane. If not provided and `spliter` is a Ray, the normal
        is taken from the Ray's direction. If `spliter` is a geometry, the normal is computed using
        the face normal of the geometry.
    
    Returns
    -------
    list[list[shapely.Geometry]]
        A list containing one or more lists of geometric components resulting from the split.
        Each inner list represents a connected part of the split result, composed of shapely.Geometry objects.
    """
    """
    split the polygon by curve or plane
    In this version only polygon can be accepted as geo,
    line(pygoes.Geometry) polygon(shapely.Geometry) plane(Ray) can be accepted as spliter.
    the normal of the spliter can be automatically calculated.
    Besides, you can send a Ray object as a spliter to create both the normal and spliter plane
    """
    if isinstance(spliter, Ray):
        normal = spliter.direction
        proj = Projection(spliter.origin, spliter.direction)
        spliter = proj.toWorld(shapely.polygons(
            [[-9999, -9999, 0], [-9999, 9999, 0], [9999, 9999, 0], [9999, -9999, 0], [-9999, -9999, 0]]))
    elif not isinstance(spliter, shapely.Geometry):
        raise Exception(f'wrong type of spliter, except{shapely.Geometry} or {Ray} got {type(spliter)}')

    # if spliter is a linestring, call splitByCurve directly
    if shapely.get_dimensions(spliter) == 1:
        return splitByCurve(geo, spliter)

    coordinates = shapely.get_coordinates(geo, include_z=True)
    edges = [[shapely.points(coordinates[i]), shapely.points([coordinates[i + 1] - coordinates[i]])] for i in
             range(len(coordinates) - 1)]
    if normal is None:
        normal = faceNormal(spliter).array
    else:
        normal = Vector(normal).array

    # Calculates the intersection point, and the insertion sequence number of the intersection point
    intersectPoint = [
        rayFaceIntersect(Ray(edge[0], Vector(edge[1]).unit()), face=spliter, normal=Vector(normal),
                         limit_distance=Vector(edge[1]).length()) for edge in edges]
    intersectPoint = [poi for poi in intersectPoint if poi is not None]

    # Sort intersect points according to the order of x, y, and z
    sortlist = np.array([intersectPoint, shapely.get_x(intersectPoint), shapely.get_y(intersectPoint),
                         shapely.get_z(intersectPoint)]).T.tolist()
    sortlist.sort(key=lambda x: (x[3], x[2], x[1]))

    # construct 3d split lines
    spliter = shapely.linestrings(shapely.get_coordinates([sortlist[0][0], sortlist[-1][0]]))
    return splitByCurve(geo, spliter)


def section(geo: shapely.Geometry, elevation: float, segment=True) -> list[shapely.Geometry] | shapely.Geometry | None:
    """Calculate the section for a geometry on given elevation(z value), which can be used to do a section on z
    Return all parts of the section if segment==True
    Otherwise, only the biggest line will be return, it can be used to split the geometry by split() method
    """
    coordinates = shapely.get_coordinates(geo)
    points = shapely.points([np.append(coor, elevation) for coor in coordinates])

    points = [poi for poi in points if distance(poi, geo) < geom.POINT_PRECISION]

    if len(points) < 2:
        return []

    # sort the point by x and y coordinates, find the biggest line
    sort_list = [[poi, coor[0], coor[1]] for poi, coor in zip(points, coordinates)]
    sort_list.sort(key=lambda x: (x[1], x[2]))
    points = [item[0] for item in sort_list]
    if segment:
        edges = [shapely.linestrings(shapely.get_coordinates(points, include_z=True)[i:i + 2]) for i in
                 range(len(points) - 1)]
        secionedges = []
        for edge in edges:
            if shapely.contains(geo, edge):
                secionedges.append(edge)
        return secionedges
    else:
        if shapely.distance(points[0], points[-1]) > geom.POINT_PRECISION:
            return shapely.linestrings(shapely.get_coordinates([points[0], points[-1]], include_z=True))
        else:
            return None


def distance(point, polygon: shapely.Geometry, normal=None):
    """
    Get the distance from a point to a polygon or plane.
    
    Parameters
    ----------
    point : array-like
        The point for which the distance to the polygon or plane is calculated.
        It will be converted to a numpy array internally.
    polygon : shapely.Geometry
        A geometric object representing the polygon. Coordinates of the polygon
        are used to compute the distance.
    normal : array-like, optional
        The normal vector of the plane. If not provided, it is computed using
        the `faceNormal` function based on the polygon. If provided, it will be
        converted to a Vector and normalized.
    
    Returns
    -------
    float
        The absolute distance from the point to the polygon or plane, computed
        as the absolute dot product between the vector from a point on the polygon
        to the input point and the normal vector.
    """
    """
        Get the distance for a point to a polygon or plane.
        actually if you know the origin of the plane(as Ray) the distance should be
        abs(Vector.dot(Vector(point).array - plane.origin.array, plane.direction))
    """
    point = Vector(point).array
    if normal is None:
        normal = faceNormal(polygon).array
    else:
        normal = Vector(normal).array
    vec = shapely.get_coordinates(polygon, include_z=True)[0] - point
    return np.abs(Vector.dot(vec, normal))


def splitByCurveLagacy(geoBase: shapely.Geometry, curve: shapely.Geometry) -> list[list[shapely.Geometry]]:
    """
    Split a geometry into two parts based on intersection with a dividing curve using legacy projection-based method.
    
    This function is part of the split function. It should not be used directly.
    
    Parameters
    ----------
    geoBase : shapely.Geometry
        The base geometry to be split, typically a linestring or polygon.
    curve : shapely.Geometry
        The curve geometry used as the splitting divider; intersections with `geoBase` determine split locations.
    
    Returns
    -------
    list[list[shapely.Geometry]]
        A list containing two lists of geometries: the first sublist represents geometries on one side of the split curve,
        and the second sublist represents geometries on the other side. Each sublist contains reconstructed curve segments
        after splitting and re-projection back to world coordinates.
    """
    """
        This function is part of the split function. It should not be used directly.
    """
    proj = Projection(
        origin=shapely.points(shapely.get_coordinates(geoBase, include_z=True)[0]),
        unitZ=faceNormal(geoBase)
    )
    geoBaseProj = shapely.set_precision(proj.toUV(geoBase), geom.POINT_PRECISION)
    curveProj = shapely.set_precision(proj.toUV(curve), geom.POINT_PRECISION)
    points = shapely.points(shapely.get_coordinates(geoBaseProj, include_z=True))
    geoCollection = [[], []]
    breakPoint = 0
    pointOnCurve = None
    side = 1

    # Start by segmenting the curve according to both sides of the dividing line
    for i in range(len(points) - 1):
        # If the current point is on the split line, move the split point to that point and continue
        if shapely.covers(curveProj, points[i]):
            breakPoint = i
            pointOnCurve = None
            continue

        # If the current and back points are on opposite sides of z, or the back point is on the dividing line,
        # the segment crosses the dividing line (the current point will not be on the dividing line)
        edge = shapely.linestrings(shapely.get_coordinates([points[i], points[i + 1]]))
        if shapely.intersects(edge, curveProj):
            subCurve = points[breakPoint:i + 1]
            breakPoint = i + 1
            # Insert last point
            if pointOnCurve is not None:
                subCurve = np.append([pointOnCurve], subCurve)
            # Insert next point
            pointOnCurve = shapely.intersection(edge, curveProj, grid_size=geom.POINT_PRECISION)
            subCurve = np.append(subCurve, pointOnCurve)
            geoCollection[int((side + 1) / 2)].append(subCurve)
            side *= -1
    side = int((side + 1) / 2)
    subCurve = points[breakPoint:]
    # Insert last point
    if pointOnCurve is not None:
        subCurve = np.append(pointOnCurve, subCurve)
    geoCollection[side].append(subCurve)

    # If the start point and end point of the curve belong to two segments in the same group, join them.
    for i in range(len(geoCollection[side])):
        if points[0] in geoCollection[side][i] and i != len(geoCollection[side]) - 1:
            geoCollection[side][i] = np.append(geoCollection[side][-1][:-1], geoCollection[side][i])
            geoCollection[side].pop()
            break

    # The envelope times of the inner and outer collections were respectively used to determine
    # the positive and negative shapes, and hollowed out
    for group in [0, 1]:
        collection = []
        geoCollection[group] = [closeTheCurve(shapely.linestrings(shapely.get_coordinates(curve, include_z=True))) for
                                curve in geoCollection[group]]
        voidVolume = [1 for i in geoCollection[group]]
        diffDict = {i: [] for i in range(len(geoCollection[group]))}
        for i in range(len(geoCollection[group])):
            for j in range(i, len(geoCollection[group])):
                if shapely.contains(geoCollection[group][i], geoCollection[group][j]):
                    voidVolume[j] *= -1
                    diffDict[i].append(j)
                if shapely.contains(geoCollection[group][j], geoCollection[group][i]):
                    voidVolume[i] *= -1
                    diffDict[j].append(i)

        for i in diffDict.keys():
            if voidVolume[i] == -1: continue
            thisGeo = geoCollection[group][i]
            for j in diffDict[i]:
                if voidVolume[j] == 1: continue
                thisGeo = difference(thisGeo, geoCollection[group][j])
            collection.append(thisGeo)
        geoCollection[group] = collection

    # Reproject the curve to worldXY
    for group in [0, 1]:
        for i in range(len(geoCollection[group])):
            faceProj = geoCollection[group][i]
            faceWorld = proj.toWorld(faceProj)
            geoCollection[group][i] = faceWorld

    return geoCollection


def splitOnZ(geoBase: shapely.Geometry, level: float, EPS: float = 1e-9) -> list[list[shapely.Geometry]]:
    """
    Simple logic for 3D polygon cutting: insert intersection points 鈫?reorder path 鈫?split segments 鈫?close to rings 鈫?classify output

    Parameters
    ----------
    geoBase : shapely.Geometry
        3D simple polygon (no inner rings) with z-coordinates
    level : float
        Cutting height (z-coordinate value)
    EPS : float, optional
        Floating point precision threshold, default 1e-9

    Returns
    -------
    list[list[shapely.Geometry]]
        List containing two sublists:
        - First sublist: Upper polygons (z > level)
        - Second sublist: Lower polygons (z < level)
        Returns original polygon wrapped in GeometryCollection if cutting fails

    Notes
    -----
    The algorithm follows these key steps:
    1. Detect intersections between polygon edges and z=level plane
    2. Reorder polygon path to start from first intersection point
    3. Split path into segments separated by intersection points
    4. Close segments to form valid rings
    5. Classify rings into upper/lower groups based on z-coordinates
    """

    coordinates = shapely.get_coordinates(geoBase, include_z=True)
    if len(coordinates) == 0:
        return [[], []]

    z_values = coordinates[:, 2]
    if np.max(z_values) <= level + EPS:
        return [[], [geoBase]]
    if np.min(z_values) >= level - EPS:
        return [[geoBase], []]

    proj = Projection(
        origin=shapely.points(coordinates[0]),
        unitZ=faceNormal(geoBase),
    )
    world_rings = shapely.get_rings(geoBase)
    if len(world_rings) == 0:
        return [[geoBase], []]
    uv_rings = [shapely.force_2d(proj.toUV(ring)) for ring in world_rings]
    hole_coordinates = [shapely.get_coordinates(ring) for ring in uv_rings[1:]]
    if len(hole_coordinates) > 0:
        geo_proj = shapely.polygons(shapely.get_coordinates(uv_rings[0]), hole_coordinates)
    else:
        geo_proj = shapely.polygons(shapely.get_coordinates(uv_rings[0]))
    geo_proj = shapely.set_precision(geo_proj, geom.POINT_PRECISION)

    uv_coordinates = shapely.get_coordinates(geo_proj)
    if len(uv_coordinates) == 0:
        return [[geoBase], []]

    z_gradient = np.array([proj.axisX[2], proj.axisY[2]], dtype=float)
    gradient_norm = np.linalg.norm(z_gradient)
    if gradient_norm <= EPS:
        return [[geoBase], []]

    minx = float(np.min(uv_coordinates[:, 0]))
    maxx = float(np.max(uv_coordinates[:, 0]))
    miny = float(np.min(uv_coordinates[:, 1]))
    maxy = float(np.max(uv_coordinates[:, 1]))
    center = np.array([(minx + maxx) / 2.0, (miny + maxy) / 2.0], dtype=float)

    signed_distance = (np.dot(z_gradient, center) + proj.origin.array[2] - level) / gradient_norm
    plane_point = center - signed_distance * (z_gradient / gradient_norm)
    tangent = np.array([-z_gradient[1], z_gradient[0]], dtype=float) / gradient_norm
    normal = z_gradient / gradient_norm

    span = max(maxx - minx, maxy - miny, 1.0)
    extent = 4.0 * (span + abs(signed_distance) + 10.0)
    line_start = plane_point - tangent * extent
    line_end = plane_point + tangent * extent
    upper_mask = shapely.polygons([
        line_start,
        line_end,
        line_end + normal * extent,
        line_start + normal * extent,
        line_start,
    ])
    lower_mask = shapely.polygons([
        line_start,
        line_end,
        line_end - normal * extent,
        line_start - normal * extent,
        line_start,
    ])

    def _collect_split_parts(clipped_geometry: shapely.Geometry) -> list[shapely.Geometry]:
        if shapely.is_empty(clipped_geometry):
            return []
        valid_geometry = shapely.make_valid(clipped_geometry)
        parts = [part for part in shapely.get_parts(valid_geometry) if shapely.get_dimensions(part) == 2]
        world_parts = []
        for part in parts:
            if shapely.area(part) <= geom.AREA_PRECISION:
                continue
            try:
                world_parts.append(proj.toWorld(part))
            except Exception:
                continue
        return world_parts

    upper_parts = _collect_split_parts(
        shapely.intersection(geo_proj, upper_mask, grid_size=geom.POINT_PRECISION)
    )
    lower_parts = _collect_split_parts(
        shapely.intersection(geo_proj, lower_mask, grid_size=geom.POINT_PRECISION)
    )

    if len(upper_parts) + len(lower_parts) == 0:
        return [[geoBase], []]
    return [upper_parts, lower_parts]


def splitFace2d(geoBaseProj: shapely.Geometry, curveProj: shapely.Geometry) -> list[list[shapely.Geometry]]:
    points = shapely.points(shapely.get_coordinates(geoBaseProj, include_z=True))
    geoCollection = [[], []]
    pointOnCurve = []
    curveWithBreakPoint = list(np.array(points))

    # Start by adding breakPoints
    for i in range(len(points) - 1):
        # If the current point is on the split line, append to pointOnCurve and continue
        if shapely.covers(curveProj, points[i]):
            pointOnCurve.append(i)
        elif not shapely.covers(curveProj, points[i + 1]):
            # If the current and back points are on opposite sides of z, or the back point is on the dividing line,
            # the segment crosses the dividing line (the current point will not be on the dividing line)
            edge = shapely.linestrings(shapely.get_coordinates([points[i], points[i + 1]]))
            if shapely.intersects(edge, curveProj):
                breakPoint = shapely.intersection(edge, curveProj, grid_size=geom.POINT_PRECISION)
                shift = len(curveWithBreakPoint) - len(points)
                curveWithBreakPoint = curveWithBreakPoint[:i + 1 + shift] + [breakPoint] + curveWithBreakPoint[
                                                                                           i + 1 + shift:]
                pointOnCurve.append(i + 1 + shift)

    if len(pointOnCurve) < 2:
        print("******GeometryError: Failed to split: no break point")
        return None
    elif len(pointOnCurve) == 2:
        if np.abs(pointOnCurve[0] - pointOnCurve[1]) == 1:
            print("******GeometryError: Failed to split: overlap")
            return None

    if shapely.covers(curveProj, points[-1]):
        pointOnCurve.append(len(curveWithBreakPoint) - 1)

    # Translate the pointOnCurve into index
    # for i,p in enumerate(pointOnCurve):
    #     pointOnCurve[i] = curveWithBreakPoint.index(p)

    # start breaking the curve side by side
    side = 1
    for i in range(len(pointOnCurve) - 1):
        if pointOnCurve[i + 1] - pointOnCurve[i] > 1:
            subCurve = range(pointOnCurve[i], pointOnCurve[i + 1])
            subCurve = np.append(subCurve, [pointOnCurve[i + 1], pointOnCurve[i]])
            geoCollection[int((side + 1) / 2)].append(subCurve)
            side *= -1

    # add the first and last segments to the collection[0]
    if pointOnCurve[0] != 0:
        subCurve = list(range(pointOnCurve[0])) + [pointOnCurve[0], pointOnCurve[-1]] + list(
            range(pointOnCurve[-1] + 1, len(curveWithBreakPoint)))
        geoCollection[0].append(np.array(subCurve))

    # print("\nbase:",geoBase)
    # print("\nadd:",[proj.toWorld(x)for x in curveWithBreakPoint])
    # print()
    # print(pointOnCurve)
    # print(geoCollection)
    # The envelope times of the inner and outer collections were respectively used to determine
    # the positive and negative shapes, and hollowed out

    for group in [0, 1]:
        collection = []
        geoCollection[group] = [np.array(curveWithBreakPoint)[curve] for curve in geoCollection[group] if
                                len(curve) > 3]
        geoCollection[group] = [shapely.polygons(shapely.get_coordinates(curve, include_z=True)) for curve in
                                geoCollection[group]]
        # faceWorld=[proj.toWorld(faceProj) for faceProj in geoCollection[group]]
        # z = shapely.get_coordinates(faceWorld, include_z=True)[:,2]
        # print(z.min(),z.max())
        voidVolume = [1 for i in geoCollection[group]]
        diffDict = {i: [] for i in range(len(geoCollection[group]))}
        for i in range(len(geoCollection[group])):
            for j in range(i + 1, len(geoCollection[group])):
                if shapely.contains(geoCollection[group][i], geoCollection[group][j]):
                    voidVolume[j] *= -1
                    diffDict[i].append(j)
                if shapely.contains(geoCollection[group][j], geoCollection[group][i]):
                    voidVolume[i] *= -1
                    diffDict[j].append(i)

        for i in diffDict.keys():
            if voidVolume[i] == -1: continue
            thisGeo = geoCollection[group][i]
            for j in diffDict[i]:
                if voidVolume[j] == 1: continue
                thisGeo = difference(thisGeo, geoCollection[group][j])
            collection.append(thisGeo)
        geoCollection[group] = collection
    return geoCollection


def splitByCurve(geoBase: shapely.Geometry, curve: shapely.Geometry) -> list[list[shapely.Geometry]]:
    """
    Split a geometric object by a curve using projection and intersection analysis.
    
    Parameters
    ----------
    geoBase : shapely.Geometry
        The base geometry to be split, typically a polygon or linestring in 3D space.
        It serves as the input shape that will be divided based on its intersection with the curve.
    curve : shapely.Geometry
        A curve (linestring) used to split the geoBase. This curve is projected into the same
        plane as geoBase for intersection calculations.
    
    Returns
    -------
    list of list of shapely.Geometry
        A list containing two groups of geometries resulting from the split operation.
        Each group is a list of shapely.Geometry objects representing polygons.
        The first sublist typically represents one side of the split, and the second sublist
        the other side, with holes properly subtracted based on containment relationships.
    """
    """
        This function is part of the split function. It should not be used directly.
    """
    proj = Projection(
        origin=shapely.points(shapely.get_coordinates(geoBase, include_z=True)[0]),
        unitZ=faceNormal(geoBase)
    )
    # z=shapely.get_coordinates(geoBase, include_z=True)
    # print(z.min(),z.max())
    # print(curve)
    geoBaseProj = proj.toUV(geoBase)
    curveProj = proj.toUV(curve)

    geoCollection = splitFace2d(geoBaseProj, curveProj)

    if geoCollection is None:
        return None
    # Reproject the curve to worldXY
    for group in [0, 1]:
        for i in range(len(geoCollection[group])):
            faceProj = geoCollection[group][i]
            faceWorld = proj.toWorld(faceProj)
            geoCollection[group][i] = faceWorld

    return geoCollection


def lineIntersection(l1: shapely.Geometry, l2: shapely.Geometry):
    """
    Compute the intersection point of two line segments using vector mathematics.
    
    Parameters
    ----------
    l1 : shapely.Geometry
        A LineString geometry representing the first line segment.
    l2 : shapely.Geometry
        A LineString geometry representing the second line segment.
    
    Returns
    -------
    shapely.Geometry
        A Point geometry representing the intersection point of the two lines,
        or None if the lines are parallel or nearly collinear.
    """
    '''
        define the intersection point as P
        for any point P on line_1: p = o1 + t * v1
        p is on line_2: np.cross(p - o2, v2)==0
        which means: np.cross(o1-o2+t*v1,v2)==0
        which means: np.cross(o1-o2,v2) + np.cross(t*v1,v2)== np.cross(o1-o2,v2) + t * np.cross(v1,v2) ==0
        which means: t=-np.cross(o1-o2,v2)/np.cross(v1,v2)
    '''
    edge1 = shapely.get_coordinates(l1)
    edge2 = shapely.get_coordinates(l2)
    o1, v1 = edge1[0], edge1[1] - edge1[0]
    o2, v2 = edge2[0], edge2[1] - edge2[0]
    if np.abs(Vector.dot(Vector(v1).unit(), Vector(v2).unit())) > 0.999: return None
    t = -np.cross(o1 - o2, v2) / np.cross(v1, v2)
    p = o1 + t * v1
    return shapely.points(p)


def closeTheCurve(geo: shapely.Geometry):
    """
    Close an open geometric curve by adding the first coordinate to the end if not already closed.
    
    Parameters
    ----------
    geo : shapely.Geometry
        A geometric object (e.g., LineString) that may be open or closed. If the geometry is already closed,
        it is returned as-is.
    
    Returns
    -------
    shapely.Geometry
        A new geometry where the input curve is closed by connecting the last point to the first.
        If the input was already closed, the original geometry is returned.
    """
    # Ver 2.0：使曲线闭合。
    if shapely.is_closed(geo):
        return geo
    coordinates = shapely.get_coordinates(geo, include_z=True).tolist()
    coordinates.append(coordinates[0])
    return shapely.polygons(coordinates)


# 旧版本的 vector 计算。
# 向量计算 / 整合了shapely.Geometry类型，比np的泛用性广
# def vector.dot(vec1, vec2):
#    vec1 = vector(vec1).array
#    vec2 = vector(vec2).array
#    return np.sum([vec1[i] * vec2[i] for i in range(len(vec1))])


# def vector.to_array(vec, _3d=False):
#    vec = vector(vec).array
#    if not _3d:
#        vec = vec[:2]
#    return vec

# def vec_cross(vec1, vec2):
"""
Compute the cross product of two 3-dimensional vectors.

Parameters
----------
vec1 : array_like
    First input vector, must be a 3-dimensional array-like object.
vec2 : array_like
    Second input vector, must be a 3-dimensional array-like object.

Returns
-------
numpy.ndarray
    The cross product of `vec1` and `vec2`, returned as a 3-dimensional numpy array.
"""
#    vec1 = vector(vec1).array
#    vec2 = vector(vec2).array
#    return np.cross(vec1, vec2)


# def vec_length(vec, power=False):
"""
Calculate the length (magnitude) of a vector.

Parameters
----------
vec : array-like
    Input vector, represented as a sequence of numerical values.
power : bool, optional
    If True, return the squared magnitude (sum of squares) instead of the Euclidean length.
    Default is False.

Returns
-------
float
    The Euclidean length of the vector if `power` is False;
    otherwise, the squared magnitude (sum of squares).
"""
#    vec = vector(vec).array
#    if power:
#        return np.sum([i * i for i in vec])
#    else:
#        return np.power(np.sum([i * i for i in vec]), 0.5)


# def vec_angle(vec):
"""
Compute a normalized angular measure of a 2D vector relative to the positive x-axis, adjusted for quadrant.

Parameters
----------
vec : array-like or shapely.Geometry
    Input 2D vector. If a shapely geometry is provided, it will be converted to a numpy array.
    Must have at least two components (x, y).

Returns
-------
float or None
    A value in the range [-3, 1] that is monotonically related to the counter-clockwise angle from the positive x-axis.
    Returns None if the input vector has zero length.
"""
#    '''
#    vec 在正 y 轴：返回 vector.dot([1, 0], vec)，结果在 [-1, 1] 中。
#    vec 在负 y 轴：返回 -vector.dot([1, 0], vec) - 2，结果在 [-3, -1] 中。
#    返回值与沿逆时针方向[-3,1]的角度大小正相关
#    '''
#
#    if type(vec) == shapely.Geometry:
#        vec = vector.to_array(vec, False)
#    if vec_length(vec) == 0:
#        print('zero length vector')
#        return None
#    vec = vec / vec_length(vec)
#    dot = vector.dot(np.array([1, 0]), vec)
#    if vec[1] < 0:
#        dot = -dot - 2
#    return dot


# def vec_to_string(vec):
"""
Convert a 3D vector to a formatted string representation.

Parameters
----------
vec : array_like
    A 3-element array or list representing a 3D vector. Elements are rounded to one decimal place.

Returns
-------
str
    A string representation of the vector with components separated by underscores, where each component is rounded to one decimal place and zero values are represented as '0.0'.
"""
#    vec = vec_unit(vec)
#    if vec[0] < 0:
#        return vec_to_string(np.array([-vec[0], -vec[1], -vec[2]]))
#    else:
#        if vec[0] == 0 and vec[1] < 0:
#            return vec_to_string(np.array([-vec[0], -vec[1], -vec[2]]))
#        else:
#            if vec[0] == 0 and vec[1] == 0 and vec[2] < 0:
#                return vec_to_string(np.array([-vec[0], -vec[1], -vec[2]]))

#    vec = vec.tolist()
#    for i in range(3):
#        if vec[i] == 0:
#            vec[i] = '0.0'
#        else:
#            vec[i] = str(round(vec[i], 1))
#    return '_'.join(vec)


# Ver1.3 判断平行
# def vector.parallel(vec1, vec2):
#    vec1 = vec_unit(vec1)
#    vec2 = vec_unit(vec2)
#    dot = vector.dot(vec1, vec2)
#    if dot < 1.0 + 0.001 and dot > 1.0 - 0.001:
#        return True
#    if dot < -1.0 + 0.001 and dot > -1.0 - 0.001:
#        return True
#    return False

# def vec_equal(vec1, vec2):
"""
Check if two vectors are equal within a given precision.

Parameters
----------
vec1 : array_like
    First vector to compare.
vec2 : array_like
    Second vector to compare.

Returns
-------
bool
    True if the vectors are equal within the specified precision, False otherwise.
"""
#    vec1 = vector(vec1)
#    vec2 = vector(vec2)
#    if vec_length(vec1.array-vec2.array , True) < geom.POINT_PRECISION:
#        return True
#    return False
