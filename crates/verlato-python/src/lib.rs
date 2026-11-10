//! `verlato._native`: the Python bindings for Verlato. The `verlato` Python package is a thin
//! wrapper over these functions; all behaviour lives in the `verlato` crate.

use pyo3::prelude::*;

/// Run the `verlato` command line with `argv` (without the program name) in this process and
/// return its exit code. The GIL is released while the command runs.
#[pyfunction]
fn run(py: Python<'_>, argv: Vec<String>) -> i32 {
    py.detach(move || verlato::cli::run(argv))
}

/// Answer one service request (the `verlato service` protocol) in-process. `params_json` is the
/// request's params object as JSON; `repo` is the repository directory (default: the current
/// directory). Returns the response envelope as JSON.
#[pyfunction]
#[pyo3(signature = (method, params_json = String::new(), repo = None))]
fn call(
    py: Python<'_>,
    method: String,
    params_json: String,
    repo: Option<std::path::PathBuf>,
) -> PyResult<String> {
    let cwd = match repo {
        Some(path) => path,
        None => std::env::current_dir()?,
    };
    Ok(py.detach(move || verlato::service::call(&cwd, &method, &params_json)))
}

/// The Verlato version these bindings were built from.
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// The IntentumDiff engine build linked into these bindings.
#[pyfunction]
fn engine_version() -> String {
    verlato::engine::engine_version()
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(run, module)?)?;
    module.add_function(wrap_pyfunction!(call, module)?)?;
    module.add_function(wrap_pyfunction!(version, module)?)?;
    module.add_function(wrap_pyfunction!(engine_version, module)?)?;
    Ok(())
}
