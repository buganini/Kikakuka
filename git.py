import pygit2
import os
from datetime import datetime

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
        abs_file_path = os.path.abspath(file_path)

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

        rel_path = os.path.relpath(abs_file_path, repo_workdir)

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

def log(repo_path, file_path=None):
    """
    Get the commit history for a specific file in a Git repository.

    Args:
        repo_path (str): Path to the Git repository
        file_path (str): Path to the file relative to the repository root

    Returns:
        list: List of dictionaries containing commit information
    """

    # Open the repository
    repo = pygit2.Repository(repo_path)

    if file_path:
        file_path = os.path.relpath(os.path.abspath(file_path), repo_path)

    for commit in repo.walk(repo.head.target, pygit2.GIT_SORT_TIME):
        if commit.type != pygit2.GIT_OBJECT_COMMIT:
            continue
        if file_path and file_path not in commit.tree:
            continue
        dtg = datetime.fromtimestamp(commit.commit_time).strftime("%Y-%m-%d %H:%M")
        yield (commit.id, f"{dtg} {commit.message.strip()}")

def _extract_tree(repo, tree_id, destination):
    """Extract a tree object to the destination folder."""
    # Create the directory if it doesn't exist
    os.makedirs(destination, exist_ok=True)

    # Get the tree object
    tree = repo.get(tree_id)

    # Process all entries in the tree
    for entry in tree:
        path = os.path.join(destination, entry.name)

        if entry.type == pygit2.GIT_OBJ_TREE:
            # If it's a directory, recursively extract it
            _extract_tree(repo, entry.id, path)
        elif entry.type == pygit2.GIT_OBJ_BLOB:
            # If it's a file, extract it
            _extract_blob(repo, entry.id, path)

def _extract_blob(repo, blob_id, destination):
    """Extract a blob object to the destination file."""
    # Get the blob object
    blob = repo.get(blob_id)

    # Ensure the parent directory exists
    os.makedirs(os.path.dirname(destination), exist_ok=True)

    # Write the blob content to the file
    with open(destination, 'wb') as f:
        f.write(blob.data)

def checkout(repo_path, commit_id, outdir, file_paths):
    repo = pygit2.Repository(repo_path)
    commit = repo.get(commit_id)
    tree = commit.tree

    os.makedirs(outdir, exist_ok=True)

    for file_path in file_paths:
        file_path = os.path.relpath(os.path.abspath(file_path), repo_path)

        entry = tree[file_path]
        if entry.type == pygit2.GIT_OBJECT_TREE:
            # If it's a directory, extract all contents
            _extract_tree(repo, entry.id, os.path.join(outdir, os.path.basename(file_path)))
        else:
            # If it's a file, extract just that file
            _extract_blob(repo, entry.id, os.path.join(outdir, os.path.basename(file_path)))

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
