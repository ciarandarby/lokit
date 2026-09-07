use lokit_format::id_registry::BoundedIdRegistry;
use pyo3::prelude::*;
use pyo3::types::PySequence;
use std::io;

pub(crate) fn resolve_id(registry: &mut BoundedIdRegistry, preferred: &str) -> io::Result<String> {
    if registry.insert(preferred)? {
        return Ok(preferred.to_owned());
    }
    loop {
        let suffix = registry.next_suffix(preferred)?;
        let candidate = format!("{preferred}#{suffix}");
        if registry.insert(&candidate)? {
            return Ok(candidate);
        }
    }
}

pub(crate) fn resolve_tmx(
    registry: &mut BoundedIdRegistry,
    generated: &mut usize,
    raw: &str,
) -> io::Result<String> {
    if raw.is_empty() {
        let preferred = format!("auto_{generated}");
        *generated += 1;
        resolve_id(registry, &preferred)
    } else {
        resolve_id(registry, raw)
    }
}

pub(crate) fn resolve_xliff(
    registry: &mut BoundedIdRegistry,
    preferred: &str,
    resource: usize,
) -> io::Result<String> {
    if registry.insert(preferred)? {
        return Ok(preferred.to_owned());
    }
    resolve_id(registry, &format!("{resource}:{preferred}"))
}

pub(crate) fn xliff_preferred(raw: &str, parent: &str, resource: usize, v2: bool) -> String {
    match (v2, raw.is_empty(), parent.is_empty()) {
        (true, false, false) => format!("{parent}:{raw}"),
        (true, true, false) => parent.to_owned(),
        (_, true, _) => resource.to_string(),
        _ => raw.to_owned(),
    }
}

#[pyclass(module = "lokit._interchange_rust")]
#[derive(Default)]
pub(crate) struct IdentityRegistry {
    registry: BoundedIdRegistry,
    generated: usize,
}

#[pymethods]
impl IdentityRegistry {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    fn resolve(&mut self, preferred: &str) -> PyResult<String> {
        Ok(resolve_id(&mut self.registry, preferred)?)
    }

    fn resolve_path(&mut self, path: Vec<String>) -> PyResult<String> {
        self.resolve(&path.join("."))
    }

    fn tmx(&mut self, raw: &str) -> PyResult<String> {
        Ok(resolve_tmx(&mut self.registry, &mut self.generated, raw)?)
    }

    fn xliff(&mut self, raw: &str, parent: &str, resource: usize, v2: bool) -> PyResult<String> {
        Ok(resolve_xliff(
            &mut self.registry,
            &xliff_preferred(raw, parent, resource, v2),
            resource,
        )?)
    }

    fn resolve_tabular(
        &mut self,
        row: &Bound<'_, PySequence>,
        index: usize,
        id_column: isize,
        format_label: &str,
    ) -> PyResult<String> {
        if let Ok(column) = usize::try_from(id_column) {
            if column < row.len()? {
                let raw = row.get_item(column)?.extract::<String>()?;
                if !raw.is_empty() {
                    return self.resolve(&raw);
                }
            }
        }
        self.resolve(&format!("{format_label}:{index}"))
    }
}

#[pyfunction]
fn structural_json_path(path: Vec<String>) -> PyResult<String> {
    serde_json::to_string(&path)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<IdentityRegistry>()?;
    module.add_function(wrap_pyfunction!(structural_json_path, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn suffixes_do_not_overwrite_explicit_ids() {
        let mut ids = BoundedIdRegistry::default();
        let input = ["same", "same#2", "same", "same#2", "", ""];
        let expected = ["same", "same#2", "same#3", "same#2#2", "", "#2"];
        for (raw, resolved) in input.into_iter().zip(expected) {
            assert_eq!(resolve_id(&mut ids, raw).unwrap(), resolved);
        }
    }
}
