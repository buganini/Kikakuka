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


if __name__ == "__main__":
    unittest.main()
