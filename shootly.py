
from shapely.geometry import Point, MultiPoint, Polygon, MultiPolygon, LineString, GeometryCollection
from shapely import intersection
import itertools

def exterior(obj):
    """
    Returns the exterior points of an object
    """
    if isinstance(obj, Point):
        return [obj]
    elif isinstance(obj, LineString):
        return [Point(p) for p in obj.coords]
    elif isinstance(obj, Polygon):
        return [Point(p) for p in obj.exterior.coords]
    else:
        raise RuntimeError("Unhandled type for exterior", obj)

# Returns the longest distance between two objects
def longest_distance(a, b):
    ret = None
    for a,b in itertools.product(exterior(a), exterior(b)):
        d = LineString([a, b]).length
        if ret is None or d > ret:
            ret = d
    return ret


def shoot(origin, obj, direction, arrow_size=None):
    """
    Returns the points where the object (exterior) would hit
    """

    # Make an arrow long enough to penetrate the target object
    if arrow_size:
        l2 = arrow_size
    else:
        l2 = longest_distance(origin, obj) * 2
    dl = LineString([(0,0), direction]).length
    d = (direction[0]*l2/dl, direction[1]*l2/dl)

    if isinstance(obj, Polygon):
        obj = obj.exterior

    ps = intersection(obj, LineString([origin, (origin.x+d[0], origin.y+d[1])]))

    if ps.is_empty:
        return []
    elif isinstance(ps, MultiPoint):
        return sorted(ps.geoms, key=lambda p: p.distance(origin))
    elif isinstance(ps, LineString):
        return [ps]
    elif isinstance(ps, Point):
        return [ps]
    elif isinstance(ps, GeometryCollection):
        ret = []
        for p in ps.geoms:
            ret.append(p)
        return sorted(ret, key=lambda p: p.distance(origin))
    else:
        raise RuntimeError("Unhandled intersection result", ps)


def interpolate(ps, n):
    """
    Returns with interpolated points between each pair of points in ps
    ps should be closed
    """
    ret = []
    l = len(ps) - 1
    for i in range(l):
        a = ps[i]
        b = ps[i+1]

        ret.append(a)
        dx = (b.x-a.x)/n
        dy = (b.y-a.y)/n
        for i in range(1, n):
            ret.append(Point(a.x+dx*i, a.y+dy*i))
    return ret


# BUG: return 0 when initially contacts on opposite side of direction, should only test on facade
def collision(origin, obj, direction, arrow_size=None):
    """
    Returns the coordinates on the exterior of the two objects where the collision would happen
    """

    d = None

    """
    skip line strings to avoid false positive on sliding objects,
    but this brings false negative to aligned objects,
    so interpolate mid-point on edges to detect collision between two aligned objects

    bug: cannot handle point-in-line-out for line-in-point-out
    """

    # forward tests with origin's vertices
    for a in interpolate(exterior(origin), 2):
        ps = shoot(a, obj, direction, arrow_size)
        if len(ps)==1 and isinstance(ps[0], Point): # contact on a point
            ps = []
        ps = [p for p in ps if not isinstance(p, LineString)]
        if ps:
            dist = a.distance(ps[0])
            if d is None or dist < d[0]:
                d = (dist, a, ps[0])

    # reversed tests with obj's vertices
    rdirection = (-direction[0], -direction[1])
    for b in interpolate(exterior(obj), 2):
        ps = shoot(b, origin, rdirection, arrow_size)
        if len(ps)==1 and isinstance(ps[0], Point): # contact on a point
            ps = []
        ps = [p for p in ps if not isinstance(p, LineString)]
        if ps:
            dist = b.distance(ps[0])
            if d is None or dist < d[0]:
                d = (dist, ps[0], b)

    if d:
        return d[1], d[2]
    else:
        return None


if __name__=="__main__":
    p0 = Point(0, 0)

    polygon = Polygon([(1,1), (1,2), (2,2), (2,1)])
    p = shoot(p0, polygon, (1,1))
    assert p==[Point(1, 1), Point(2, 2)], "Polygon"

    p = shoot(p0, polygon, (1, 0))
    assert p==[], "Polygon Missed"

    p = collision(
        Polygon([(1,1), (1,2), (2,2), (2,1)]),
        Polygon([(1,1+4), (1,2+4), (2,2+4), (2,1+4)]),
        (0, 1)
    )
    assert p==(Point(1,2),Point(1,5)), "Polygon Collision"
