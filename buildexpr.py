from parsimonious.grammar import Grammar
from parsimonious.nodes import NodeVisitor
import json

buildexpr_grammar = Grammar(r"""
Expr = OrExpr
OrExpr = AndExpr OrOperand*
OrOperand = "|" AndExpr
AndExpr = NotExpr AndOperand*
AndOperand = "&" NotExpr
NotExpr = ( ws "~" ws NotExpr ) / PrimaryExpr
PrimaryExpr = ( ws "(" wsExpr ")" ws ) / wsIdentifier

wsExpr = ws Expr ws
wsIdentifier = ws Identifier ws
Identifier = ~r"[A-Z][A-Z0-9._-]*"i

ws = ~r"\s*"
""")


class BuildExprVisitor(NodeVisitor):
    def __init__(self, vmap):
        self.vmap = vmap

    def visit_Identifier(self, node, visited_children):
        # print("Identifier", node.text)
        return self.vmap.get(node.text, False)

    def visit_NotExpr(self, node, visited_children):
        # print("NotExpr", repr(node.text), visited_children, [f"{c.expr_name}:{c.text}" for c in node.children])
        if node.children[0].expr_name == "PrimaryExpr":
            return visited_children[0]
        return not visited_children[0][3]

    def visit_AndExpr(self, node, visited_children):
        # print("AndExpr", repr(node.text), visited_children, [f"{c.expr_name}:{c.text}" for c in node.children])
        operands = [visited_children[0]]
        if type(visited_children[1]) is bool:
            operands.append(visited_children[1])
        else:
            operands.extend(visited_children[1])
        ret = all(operands)
        # print("  =>", operands, "=>", ret)
        return ret
    
    def visit_AndOperand(self, node, visited_children):
        # print("AndOperand", repr(node.text), visited_children, [f"{c.expr_name}:{c.text}" for c in node.children])
        return visited_children[1]

    def visit_OrExpr(self, node, visited_children):
        # print("OrExpr", repr(node.text), visited_children, [f"{c.expr_name}:{c.text}" for c in node.children])
        operands = [visited_children[0]]
        if type(visited_children[1]) is bool:
            operands.append(visited_children[1])
        else:
            operands.extend(visited_children[1])
        ret = any(operands)
        # print("  =>", operands, "=>", ret)
        return ret

    def visit_OrOperand(self, node, visited_children):
        # print("OrOperand", repr(node.text), visited_children, [f"{c.expr_name}:{c.text}" for c in node.children])
        return visited_children[1]

    def visit_PrimaryExpr(self, node, visited_children):
        # print("PrimaryExpr", repr(node.text), visited_children)
        if len(visited_children) == 1:
            return visited_children[0]

        return visited_children[2]
    
    def visit_wsIdentifier(self, node, visited_children):
        # print("wsIdentifier", repr(node.text), visited_children)
        return visited_children[1]

    def visit_wsExpr(self, node, visited_children):
        # print("wsExpr", repr(node.text), visited_children)
        return visited_children[1]

    def generic_visit(self, node, visited_children):
        # print("generic_visit", repr(node.text), node, visited_children)
        if isinstance(visited_children, list) and len(visited_children) == 1:
            return visited_children[0]
        return visited_children

def buildexpr(text, flags):
    ast = buildexpr_grammar.parse(text)
    return BuildExprVisitor({k:True for k in flags}).visit(ast)

if __name__ == "__main__":
    import sys
    tests = [
        ("t", True),
        ("~t", False),
        ("t | f", True),
        ("~t | f", False),
        ("t & f", False),
        ("t & ~f", True),
        ("t & t & t & t", True),
        ("t & t & f & t", False),
        ("flag", True)
    ]
    for test, expected in tests:
        print("\n\n# Test ", repr(test), "is")
        ast = buildexpr_grammar.parse(test)
        # print(ast)
        res = BuildExprVisitor({
            "t": True,
            "f": False,
            "flag": True,
        }).visit(ast)
        print(" # Result =>", res)
        assert res == expected
