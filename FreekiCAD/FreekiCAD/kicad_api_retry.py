import time


def is_kicad_retryable_error(exc, retry_connection_timeout=False):
    """Return True when *exc* indicates KiCad is not ready yet."""
    try:
        from kipy.errors import ApiError, ConnectionError
        from kipy.proto.common import ApiStatusCode
    except Exception:
        return False

    if isinstance(exc, ApiError):
        return exc.code in (
            ApiStatusCode.AS_NOT_READY,
            ApiStatusCode.AS_BUSY,
            ApiStatusCode.AS_TIMEOUT,
        )

    if retry_connection_timeout and isinstance(exc, ConnectionError):
        return "timed out" in str(exc).lower()

    return False


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
