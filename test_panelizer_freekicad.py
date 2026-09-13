import unittest

import panelizer_freekicad as subject


class _Drawing:
    def __init__(self, layer):
        self.layer = layer

    def GetLayer(self):
        return self.layer

    def SetLayer(self, layer):
        self.layer = layer


class _Board:
    def __init__(self, names, drawing_layers=()):
        self.names = dict(names)
        self.drawings = [_Drawing(layer) for layer in drawing_layers]

    def GetLayerName(self, layer):
        return self.names.get(layer, f"User.{layer}")

    def SetLayerName(self, layer, name):
        self.names[layer] = name

    def GetDrawings(self):
        return self.drawings


class PanelFreekiCADLayerTests(unittest.TestCase):
    def test_uses_first_source_layer_when_other_boards_do_not_conflict(self):
        boards = [
            _Board({4: "FreekiCAD"}, [4]),
            _Board({5: "freekicad"}, [5]),
        ]

        canonical, sources = subject.choose_panel_freekicad_layer(
            boards, range(1, 8), range(4, 8))

        self.assertEqual(canonical, 4)
        self.assertEqual(sources, [4, 5])

    def test_avoids_source_layer_used_for_unrelated_drawings(self):
        boards = [
            _Board({4: "FreekiCAD"}, [4, 5]),
            _Board({5: "freekicad"}, [5, 4]),
        ]

        canonical, _sources = subject.choose_panel_freekicad_layer(
            boards, range(1, 8), [4, 5, 6, 7])

        self.assertEqual(canonical, 6)

    def test_avoids_panel_reserved_layer(self):
        boards = [_Board({4: "FreekiCAD"}, [4])]

        canonical, _sources = subject.choose_panel_freekicad_layer(
            boards, range(1, 8), [4, 5, 6, 7], reserved_layers=[4])

        self.assertEqual(canonical, 5)

    def test_remaps_named_non_user_layer_to_a_user_layer(self):
        boards = [_Board({2: "FreekiCAD"}, [2])]

        canonical, sources = subject.choose_panel_freekicad_layer(
            boards, range(1, 8), [4, 5, 6, 7])

        self.assertEqual(canonical, 4)
        self.assertEqual(sources, [2])

    def test_remaps_drawings_and_clears_old_custom_name(self):
        board = _Board({4: "freekicad", 6: "User.6"}, [4, 2, 4])

        moved = subject.remap_freekicad_drawings(
            board, 4, 6, lambda layer: f"User.{layer}")

        self.assertEqual(moved, 2)
        self.assertEqual([item.layer for item in board.drawings], [6, 2, 6])
        self.assertEqual(board.names[4], "User.4")
        self.assertEqual(board.names[6], "freekicad")

    def test_rejects_multiple_named_layers_on_one_board(self):
        board = _Board({4: "FreekiCAD", 5: "FREEKICAD"})

        with self.assertRaisesRegex(ValueError, "multiple layers"):
            subject.find_freekicad_layer(board, range(1, 8))


if __name__ == "__main__":
    unittest.main()
