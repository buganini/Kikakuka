"""Launch Kikakuka tools while using CADhoc only for registration and focus."""
import os
from cadhoc.mesh import local_node, owned_pid_exists, registration_lock


def activate_or_open_file(filepath, opener, activator):
    """Activate the existing file owner or spawn one without launch races."""
    filepath = os.path.normcase(os.path.realpath(os.path.abspath(filepath)))
    node = local_node()
    if node is None:
        return opener(filepath), True

    with registration_lock(filepath):
        pid = node.snapshot().get(filepath)
        if pid is not None and owned_pid_exists(pid):
            try:
                activator(pid)
            except Exception:
                pass
            return pid, False
        if pid is not None:
            node.publish(filepath, None)
        pid = opener(filepath)
        if pid is not None:
            node.publish(filepath, pid)
        return pid, True


