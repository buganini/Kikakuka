import pygit2
import os

def repo(file_path):
    """
    Check if a file is tracked in a Git repository.

    Args:
        file_path (str): Absolute or relative path to the file

    Returns:
        bool: True if the file is tracked in Git, False otherwise
    """
    try:
        # Convert to absolute path and normalize
        abs_file_path = os.path.abspath(file_path).replace("\\", "/")

        # Try to find the repository that contains this file
        repo_path = pygit2.discover_repository(os.path.dirname(abs_file_path))
        if not repo_path:
            return False

        # Open the repository
        repo = pygit2.Repository(repo_path)

        # Get the relative path within the repository
        repo_workdir = repo.workdir
        if not abs_file_path.startswith(repo_workdir):
            return False

        rel_path = os.path.relpath(abs_file_path, repo_workdir).replace("\\", "/")

        # Check if the file is tracked in the index
        try:
            # If this doesn't raise an exception, the file is in the index
            repo.index[rel_path]
            return repo_workdir
        except KeyError:
            # File is not in the index
            pass

        # Alternatively, check if the file exists in HEAD
        try:
            head_tree = repo.head.peel(pygit2.Tree)
            head_tree[rel_path]
            return repo_workdir
        except (KeyError, pygit2.GitError):
            # File is not in HEAD or HEAD doesn't exist (new repo)
            return False

    except (pygit2.GitError, ValueError, TypeError) as e:
        print(f"Error checking file: {e}")
        return False

def _tree_file_version(tree, file_path):
    """Return a file's blob and mode, or None when it is absent."""
    try:
        entry = tree[file_path]
    except KeyError:
        return None
    return entry.id, entry.filemode


def log(repo_path, file_path=None):
    """Yield commits that changed *file_path*, or every commit if omitted.

    File history uses the selected path at each commit. Renames from older
    paths are not followed.
    """

    # Open the repository
    repo = pygit2.Repository(repo_path)

    if file_path:
        if not os.path.isabs(file_path):
            file_path = os.path.join(repo.workdir or repo_path, file_path)
        file_path = os.path.relpath(
            os.path.realpath(file_path),
            os.path.realpath(repo.workdir or repo_path),
        ).replace("\\", "/")

    for commit in repo.walk(repo.head.target, pygit2.GIT_SORT_TIME):
        if commit.type != pygit2.GIT_OBJECT_COMMIT:
            continue
        if file_path:
            version = _tree_file_version(commit.tree, file_path)
            previous = (
                _tree_file_version(commit.parents[0].tree, file_path)
                if commit.parents else None
            )
            if version == previous:
                continue
        yield (
            commit.id,
            f"{commit.short_id} {commit.message.strip()}",
        )


def revision_at_or_before(revision, available, history):
    """Keep *revision*, or choose the next older available commit in history.

    Both histories are ordered newest first. An empty or unknown revision
    falls back to the working tree.
    """
    if not revision:
        return ""

    available_by_id = {str(commit_id): commit_id for commit_id, _ in available}
    selected_id = str(revision)
    if selected_id in available_by_id:
        return available_by_id[selected_id]

    found_selected = False
    for commit_id, _ in history:
        if found_selected and str(commit_id) in available_by_id:
            return available_by_id[str(commit_id)]
        if str(commit_id) == selected_id:
            found_selected = True
    return ""

def _extract_tree_recursive(repo, tree, destination_path):
    """
    Recursively extract all files and directories from a git tree.

    Args:
        repo: The pygit2.Repository object
        tree: The tree object to extract
        destination_path: The path where files should be extracted
    """
    # Create the destination directory if it doesn't exist
    os.makedirs(destination_path, exist_ok=True)

    # Process all entries in the tree
    for entry in tree:
        entry_path = os.path.join(destination_path, entry.name)

        if entry.type == pygit2.GIT_OBJECT_TREE:
            # It's a directory, recurse into it
            subtree = repo.get(entry.id)
            _extract_tree_recursive(repo, subtree, entry_path)
        elif entry.type == pygit2.GIT_OBJECT_BLOB:
            # It's a file, write it to disk
            blob = repo.get(entry.id)

            # Create parent directories if they don't exist
            os.makedirs(os.path.dirname(entry_path), exist_ok=True)

            # Write the file content
            with open(entry_path, 'wb') as f:
                f.write(blob.data)

def checkout(repo_path, commit_id, outdir):
    repo = pygit2.Repository(repo_path)
    commit = repo.get(commit_id)
    tree = commit.tree

    os.makedirs(outdir, exist_ok=True)

    # Get the commit tree
    tree = commit.tree

    # Extract all files from the tree
    _extract_tree_recursive(repo, tree, outdir)

if __name__ == "__main__":
    import sys
    repo_path = repo(sys.argv[1])
    if repo_path:
        if len(sys.argv) > 3:
            checkout(repo_path, sys.argv[2], sys.argv[3], [sys.argv[1]])
        else:
            for hex, msg in log(repo_path, sys.argv[1]):
                print(hex, msg)
    else:
        print("Not in a Git repository")
