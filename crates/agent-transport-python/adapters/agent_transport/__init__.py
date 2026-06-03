# Native (pyo3-built) extension. Optional at import time so the
# Python-only adapters (FfiQueue, audio sources, server, transports)
# can be imported in test / lint / docs environments that don't have
# the compiled wheel installed. Production builds always have it.
try:
    from .agent_transport import *  # type: ignore[no-redef]
    from . import agent_transport as _ext
    __doc__ = _ext.__doc__
    if hasattr(_ext, "__all__"):
        __all__ = _ext.__all__
except ImportError as _ext_err:  # pragma: no cover — defensive only
    import warnings as _w
    _w.warn(
        f"agent_transport native extension not loaded ({_ext_err}); "
        "only the pure-Python adapters are available",
        ImportWarning,
        stacklevel=2,
    )
