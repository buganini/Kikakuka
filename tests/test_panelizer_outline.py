import unittest

from panelizer_outline import merge_collinear_segments


class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class Edge:
    SEGMENT = 0
    ARC = 1

    def __init__(self, start, end, shape=SEGMENT):
        self.start = Point(*start)
        self.end = Point(*end)
        self.shape = shape

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetShape(self):
        return self.shape

    def SetStart(self, point):
        self.start = point

    def SetEnd(self, point):
        self.end = point


class MergeCollinearSegmentsTests(unittest.TestCase):
    def test_merges_tiny_tab_seam_into_full_spacing_segment(self):
        edges = [
            Edge((130_212_536, 26_600_000),
                 (130_212_536, 26_598_000)),
            Edge((130_212_536, 26_598_000),
                 (130_212_536, 25_000_000)),
        ]

        result = merge_collinear_segments(edges, Edge.SEGMENT)

        self.assertEqual(len(result), 1)
        points = {
            (result[0].GetStart().x, result[0].GetStart().y),
            (result[0].GetEnd().x, result[0].GetEnd().y),
        }
        self.assertEqual(points, {
            (130_212_536, 26_600_000),
            (130_212_536, 25_000_000),
        })

    def test_merges_an_entire_straight_chain(self):
        # Put the middle segment first to ensure one pass does not reuse its
        # stale endpoints for merges at both ends.
        edges = [
            Edge((1, 0), (2, 0)),
            Edge((0, 0), (1, 0)),
            Edge((2, 0), (3, 0)),
        ]

        result = merge_collinear_segments(edges, Edge.SEGMENT)

        self.assertEqual(len(result), 1)
        points = {
            (result[0].GetStart().x, result[0].GetStart().y),
            (result[0].GetEnd().x, result[0].GetEnd().y),
        }
        self.assertEqual(points, {(0, 0), (3, 0)})

    def test_preserves_corners_arcs_and_branches(self):
        corner = [Edge((0, 0), (1, 0)), Edge((1, 0), (1, 1))]
        arc_join = [
            Edge((0, 0), (1, 0)),
            Edge((1, 0), (2, 0), shape=Edge.ARC),
        ]
        branch = [
            Edge((0, 0), (1, 0)),
            Edge((1, 0), (2, 0)),
            Edge((1, 0), (1, 1)),
        ]

        self.assertEqual(
            len(merge_collinear_segments(corner, Edge.SEGMENT)), 2)
        self.assertEqual(
            len(merge_collinear_segments(arc_join, Edge.SEGMENT)), 2)
        self.assertEqual(
            len(merge_collinear_segments(branch, Edge.SEGMENT)), 3)

    def test_preserves_collinear_overlap(self):
        overlapping = [
            Edge((0, 0), (2, 0)),
            Edge((0, 0), (1, 0)),
        ]

        self.assertEqual(
            len(merge_collinear_segments(overlapping, Edge.SEGMENT)), 2)


if __name__ == "__main__":
    unittest.main()
