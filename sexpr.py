from parsimonious.grammar import Grammar
from parsimonious.nodes import NodeVisitor
import json

class SNode():
    def __init__(self, tag, value, children):
        self.tag = tag
        self.value = value
        self.children = children

    def get(self, key, default=None):
        for child in self.children:
            if child.tag == key:
                return child
        if default is None:
            return SNode("Empty", None, [])
        return default

    def get_all(self, key):
        return [child for child in self.children if child.tag == key]

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        if self.value is None:
            return f"SNode({self.tag} list={self.children})"
        return f"SNode({self.tag} value={self.value})"

grammar = Grammar(r"""
Node = Whitespaces? "(" Whitespaces? Identifier Whitespaces Child Whitespaces? ")" Whitespaces?
Whitespaces = ~r"\s+"
Child = Value / Node+
Value = Identifier / Number / String
Identifier = ~r"[A-Z][A-Z0-9_-]*"i
Number = Float / Integer
Float = ~r"-?[0-9]+\.[0-9]*"
Integer = ~r"-?[0-9]+"
String = ~r'"(?:[^"\\]|\\.)*"' / ~r"'(?:[^'\\]|\\.)*'"
""")


class SExprVisitor(NodeVisitor):
    def visit_Node(self, node, visited_children):
        # print("visit_Node", visited_children)
        tag = visited_children[3]
        value = None
        children = visited_children[5]
        if isinstance(children, SNode):
            children = [children]
        elif len(children) == 1 and not isinstance(children[0], SNode):
            value = children[0]
            children = []
        # print("SNode", tag, value, children)
        return SNode(tag, value, children)

    def visit_Child(self, node, visited_children):
        # print("Child", visited_children)
        return visited_children[0]

    def visit_Identifier(self, node, visited_children):
        return node.text

    def visit_Number(self, node, visited_children):
        return visited_children[0]

    def visit_Float(self, node, visited_children):
        return float(node.text)

    def visit_Integer(self, node, visited_children):
        return int(node.text)

    def visit_String(self, node, visited_children):
        return json.loads(node.text)

    def visit_Whitespaces(self, node, visited_children):
        return ' '

    def generic_visit(self, node, visited_children):
        if len(visited_children) == 1 and node.children[0].expr_name == "Node":
            return visited_children[0]
        return visited_children

def parse(text):
    ast = grammar.parse(text)
    return SExprVisitor().visit(ast)

if __name__ == "__main__":
    import sys
    ast = grammar.parse(open(sys.argv[1]).read())
    # print(ast)
    tree = SExprVisitor().visit(ast)
    print(tree)
    print(tree.get("lib").get("name"))