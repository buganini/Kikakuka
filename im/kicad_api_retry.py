import time


def _message_says_kicad_is_not_ready(exc):
    message = str(exc).lower()
    return (
        "kicad is busy" in message
        or "cannot respond to api requests right now" in message
        or "kicad returned error: busy" in message
        or "kicad returned error: not ready" in message
    )


def is_kicad_retryable_error(exc, retry_connection_timeout=False):
    """Return True when *exc* indicates KiCad is not ready yet."""
    try:
        from kipy.errors import ApiError, ConnectionError
        from kipy.proto.common import ApiStatusCode
    except Exception:
        return False

    if isinstance(exc, ApiError):
        if getattr(exc, "code", None) in (
            ApiStatusCode.AS_NOT_READY,
            ApiStatusCode.AS_BUSY,
            ApiStatusCode.AS_TIMEOUT,
        ):
            return True
        return _message_says_kicad_is_not_ready(exc)

    if retry_connection_timeout and isinstance(exc, ConnectionError):
        return "timed out" in str(exc).lower()

    return False


def probe_kicad_board_ready(board):
    """Exercise a board API query that FreekiCAD needs during load."""
    board.get_shapes()


def get_ready_kicad_board(
    kicad,
    max_retries=15,
    delay_s=1.0,
    on_retry=None,
    retry_connection_timeout=False,
):
    """Return a board proxy only after KiCad's board API answers queries."""
    def _get_and_probe():
        board = kicad.get_board()
        probe_kicad_board_ready(board)
        return board

    return retry_kicad_call(
        _get_and_probe,
        max_retries=max_retries,
        delay_s=delay_s,
        on_retry=on_retry,
        retry_connection_timeout=retry_connection_timeout,
    )


def retry_kicad_call(
    func,
    max_retries=15,
    delay_s=1.0,
    on_retry=None,
    retry_connection_timeout=False,
):
    """Call *func* and retry when KiCad reports a transient not-ready state."""
    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:
            if (
                is_kicad_retryable_error(
                    e, retry_connection_timeout=retry_connection_timeout
                )
                and attempt < max_retries
            ):
                if on_retry is not None:
                    on_retry(attempt + 1, max_retries, e)
                time.sleep(delay_s)
                continue
            raise
