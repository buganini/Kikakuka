def _point_key(point):
    return (int(point.x), int(point.y))


def merge_collinear_segments(edges, segment_shape):
    """Merge exactly connected, collinear PCB line segments.

    Shapely boolean operations can leave a very short segment where two tab
    polygons overlap.  The segment is topologically valid, but after KiCad
    integer-unit serialization it is just an unnecessary split in an
    otherwise straight Edge.Cuts line.  Work on the serialized coordinates so
    that the result reflects the geometry KiCad will actually save.
    """
    edges = list(edges)

    while True:
        incidents = {}
        endpoints = {}
        for index, edge in enumerate(edges):
            start = edge.GetStart()
            end = edge.GetEnd()
            start_key = _point_key(start)
            end_key = _point_key(end)
            endpoints[index] = ((start_key, start), (end_key, end))
            incidents.setdefault(start_key, []).append(index)
            incidents.setdefault(end_key, []).append(index)

        removed = set()
        consumed = set()
        changed = False
        for joint, indices in incidents.items():
            if len(indices) != 2:
                continue

            first_index, second_index = indices
            if (first_index == second_index or first_index in consumed
                    or second_index in consumed):
                continue

            first = edges[first_index]
            second = edges[second_index]
            if (first.GetShape() != segment_shape
                    or second.GetShape() != segment_shape):
                continue

            first_ends = endpoints[first_index]
            second_ends = endpoints[second_index]
            first_other = first_ends[1] if first_ends[0][0] == joint \
                else first_ends[0]
            second_other = second_ends[1] if second_ends[0][0] == joint \
                else second_ends[0]

            ax = first_other[0][0] - joint[0]
            ay = first_other[0][1] - joint[1]
            bx = second_other[0][0] - joint[0]
            by = second_other[0][1] - joint[1]

            # Only merge a straight continuation.  Collinear segments that
            # leave the joint in the same direction overlap and must remain
            # separate so malformed geometry is not hidden.
            if ax * by - ay * bx != 0 or ax * bx + ay * by >= 0:
                continue

            first.SetStart(first_other[1])
            first.SetEnd(second_other[1])
            consumed.update((first_index, second_index))
            removed.add(second_index)
            changed = True

        if not changed:
            return edges
        edges = [edge for index, edge in enumerate(edges)
                 if index not in removed]
