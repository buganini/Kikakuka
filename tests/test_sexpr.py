import unittest

import sexpr


class SExprTest(unittest.TestCase):
    def test_unquoted_kicad_variable_path_is_a_bare_value(self):
        tree = sexpr.parse("""(fp_lib_table
  (version 7)
  (lib (name Local)(type KiCad)(uri ${KIPRJMOD}/libraries/Local.pretty)(options "")(descr ""))
)""")

        library = tree.get("lib")
        self.assertEqual(tree.get("version").value, 7)
        self.assertEqual(library.get("name").value, "Local")
        self.assertEqual(library.get("type").value, "KiCad")
        self.assertEqual(
            library.get("uri").value,
            "${KIPRJMOD}/libraries/Local.pretty",
        )

    def test_bare_numeric_prefix_does_not_mask_the_whole_value(self):
        tree = sexpr.parse("(root (value 10.0.pretty))")

        self.assertEqual(tree.get("value").value, "10.0.pretty")


if __name__ == "__main__":
    unittest.main()
