import os
import tempfile
import types
import unittest
from unittest import mock

import githelper


class GitLogTests(unittest.TestCase):
    def test_log_label_omits_commit_time(self):
        commit = types.SimpleNamespace(
            type=githelper.pygit2.GIT_OBJECT_COMMIT,
            id="full-commit-id",
            short_id="abc1234",
            commit_time=1_788_588_000,
            message="Fix the board outline\n",
        )
        repo = types.SimpleNamespace(
            head=types.SimpleNamespace(target="head-id"),
            walk=mock.Mock(return_value=[commit]),
        )

        with mock.patch.object(githelper.pygit2, "Repository", return_value=repo):
            entries = list(githelper.log("/repo"))

        self.assertEqual(
            entries,
            [("full-commit-id", "abc1234 Fix the board outline")],
        )

    def test_file_history_excludes_commits_to_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = githelper.pygit2.init_repository(directory, False)
            author = githelper.pygit2.Signature("Test", "test@example.com")
            parent = None

            def commit(message, changes):
                nonlocal parent
                for relative_path, content in changes.items():
                    path = os.path.join(directory, relative_path)
                    if content is None:
                        os.remove(path)
                        repository.index.remove(relative_path)
                    else:
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(path, "w") as file:
                            file.write(content)
                        repository.index.add(relative_path)
                repository.index.write()
                tree = repository.index.write_tree()
                parent = repository.create_commit(
                    "HEAD", author, author, message, tree,
                    [parent] if parent is not None else [],
                )

            board = os.path.join("boards", "main.kicad_pcb")
            other = os.path.join("boards", "notes.txt")
            commit("Add files", {board: "board 1", other: "notes 1"})
            commit("Update notes", {other: "notes 2"})
            commit("Update board", {board: "board 2"})
            commit("Delete board", {board: None})

            board_messages = {
                message.split(" ", 1)[1]
                for _oid, message in githelper.log(
                    directory, os.path.join(directory, board)
                )
            }
            all_messages = {
                message.split(" ", 1)[1]
                for _oid, message in githelper.log(directory)
            }

            self.assertEqual(board_messages, {
                "Add files", "Update board", "Delete board",
            })
            self.assertEqual(all_messages, {
                "Add files", "Update notes", "Update board", "Delete board",
            })


if __name__ == "__main__":
    unittest.main()
