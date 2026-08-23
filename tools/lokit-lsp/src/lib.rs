#![forbid(unsafe_code)]

mod analysis;
mod backend;
mod document;
mod semantic;

pub use backend::Backend;
