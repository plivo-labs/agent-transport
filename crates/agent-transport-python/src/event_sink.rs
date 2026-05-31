//! Centralized owner of the per-endpoint Python event sink.
//!
//! The inner `Mutex<Option<Py<PyAny>>>` is **private to this module**, so the
//! only way any code — the Rust dispatcher thread, `set_event_sink`,
//! `poll_event`, `wait_for_event` — can touch the sink is through the methods
//! below. Every one of them acquires the lock via pyo3's
//! [`MutexExt::lock_py_attached`], which **detaches from the Python runtime
//! (releases the GIL) while waiting for the mutex** and re-attaches before
//! returning.
//!
//! That makes the classic GIL ⇄ mutex AB-BA deadlock **structurally impossible
//! regardless of acquisition order**: no thread can ever sit blocked on this
//! lock while holding the GIL, so the dispatcher (Rust thread) and the
//! GIL-holding `#[pymethods]` can never wedge each other. This replaces the
//! previous hand-reasoned "always GIL-before-lock" ordering plus hand-rolled
//! poison recovery, which had to be kept consistent by hand at every call site
//! (and was the exact class that bit production).

use std::sync::{Mutex, MutexGuard};

use pyo3::prelude::*;
use pyo3::sync::MutexExt;

/// Process-shared holder of one endpoint's event-sink callback.
pub struct EventSink {
    slot: Mutex<Option<Py<PyAny>>>,
}

impl EventSink {
    pub fn new() -> Self {
        Self {
            slot: Mutex::new(None),
        }
    }

    /// Acquire the slot via `lock_py_attached` (detach-while-waiting), recovering
    /// from poisoning. The slot only holds a clonable `Py` handle, so a poisoned
    /// guard's contents are always consistent — recover rather than drop/propagate
    /// so a panic elsewhere can't silently stop event delivery. Private: callers
    /// can only use the higher-level methods below, never hold the guard.
    fn guard(&self, py: Python<'_>) -> MutexGuard<'_, Option<Py<PyAny>>> {
        self.slot
            .lock_py_attached(py)
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Install (or clear, with `None`) the sink callback.
    pub fn set(&self, py: Python<'_>, callback: Option<Py<PyAny>>) {
        *self.guard(py) = callback;
    }

    /// Clone the installed callback, if any. The guard is dropped before the
    /// returned handle is used, so the dispatcher never invokes the callback
    /// while holding the lock.
    pub fn snapshot(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.guard(py).as_ref().map(|cb| cb.clone_ref(py))
    }

    /// Whether a sink is currently installed.
    pub fn is_set(&self, py: Python<'_>) -> bool {
        self.guard(py).is_some()
    }
}
