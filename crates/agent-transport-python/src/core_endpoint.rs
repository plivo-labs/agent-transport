//! Centralized, GIL-release-enforcing wrapper around a Rust core endpoint.
//!
//! The wrapped value is **private to this module**, so a `#[pymethods]` body
//! cannot call a core method while holding the GIL. The only accessor,
//! [`Core::with`], runs the closure inside `Python::detach` (GIL released), and
//! pyo3's `Ungil` bound on the closure (piggy-backing on `Send`) prevents
//! smuggling `Py`/GIL references into that region.
//!
//! This turns "hold the GIL across a core Mutex" from a hand-maintained
//! discipline — ~40 `#[pymethods]` each having to remember `detach` — into a
//! **compile error**: there is no way to reach the core without releasing the
//! GIL first. (The two production GIL stalls we found, `mute` and
//! `is_registered`, were exactly a forgotten `detach`; under this wrapper they
//! could not have compiled.)
//!
//! Note: the core crate has no pyo3 dependency and its mutexes are taken from
//! both pyo3 threads and pure tokio threads — so the event_sink layer uses
//! `lock_py_attached` (see [`crate::event_sink`]) while the core layer uses this
//! detach-before-call wrapper. Different layers, both centralized.

use pyo3::prelude::*;

/// GIL-release gate around a Rust core endpoint of type `T`.
pub struct Core<T>(T);

impl<T: Send + Sync> Core<T> {
    pub fn new(inner: T) -> Self {
        Self(inner)
    }

    /// Run `f` against the core endpoint with the GIL RELEASED. This is the
    /// ONLY way to reach the core, so every core-lock acquisition provably
    /// happens off the GIL — no thread can hold the GIL while blocked on a
    /// core Mutex.
    #[inline]
    pub fn with<R: Send>(&self, py: Python<'_>, f: impl FnOnce(&T) -> R + Send) -> R {
        py.detach(|| f(&self.0))
    }
}
