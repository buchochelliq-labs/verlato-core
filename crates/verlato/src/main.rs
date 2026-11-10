//! The native `verlato` executable.

use std::process::ExitCode;

fn main() -> ExitCode {
    let code = verlato::cli::run(std::env::args().skip(1).collect());
    ExitCode::from(code.clamp(0, 255) as u8)
}
