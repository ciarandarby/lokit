use std::str::FromStr;

use lokit_format::{
    canonicalize_placeholders as canonicalize, detect_placeholders as detect,
    literalize_data_placeholders as literalize_data, project_data_placeholders as project_data,
    project_placeholders as project, reform_placeholders as reform,
    resolve_data_placeholders as resolve_data, DetectionOptions, PlaceholderLimits,
    PlaceholderOccurrence, PlaceholderProjectionOptions, PlaceholderSyntax,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyModule, PyTuple};

use crate::lokit::{data_from_python, PythonClasses};

pub(crate) fn projection_options(
    runtime_placeholders: bool,
    inline_placeholders: bool,
    syntaxes: Option<Vec<String>>,
) -> PyResult<PlaceholderProjectionOptions> {
    let detection = python_options(
        syntaxes,
        None,
        true,
        PythonLimits {
            max_input_bytes: None,
            max_occurrences: None,
            max_placeholder_bytes: None,
            max_nesting: None,
        },
    )?;
    Ok(PlaceholderProjectionOptions {
        detection,
        runtime_placeholders,
        inline_placeholders,
        project_targets: true,
    })
}

pub(crate) fn project_native(
    data: lokit_format::Data,
    options: &PlaceholderProjectionOptions,
) -> PyResult<lokit_format::Data> {
    if !options.runtime_placeholders && !options.inline_placeholders {
        return Ok(data);
    }
    let mut options = options.clone();
    if let Some((_, flags)) = data.extensions.iter().find(|(key, _)| key == "flags") {
        options.detection = DetectionOptions::from_hints(
            options.detection.syntaxes.clone(),
            std::slice::from_ref(flags),
            true,
        );
    }
    project_data(data, &options).map_err(placeholder_error)
}

type PythonOccurrence = (
    String,
    String,
    String,
    String,
    usize,
    usize,
    Option<usize>,
    Option<usize>,
    String,
);

#[derive(Clone, Copy)]
struct PythonLimits {
    max_input_bytes: Option<usize>,
    max_occurrences: Option<usize>,
    max_placeholder_bytes: Option<usize>,
    max_nesting: Option<usize>,
}

#[pyfunction(name = "detect_placeholders")]
#[pyo3(signature = (
    text,
    syntaxes=None,
    gettext_flags=None,
    auto_detect=true,
    max_input_bytes=None,
    max_occurrences=None,
    max_placeholder_bytes=None,
    max_nesting=None,
))]
#[allow(clippy::too_many_arguments)]
fn detect_placeholders_py(
    py: Python<'_>,
    text: String,
    syntaxes: Option<Vec<String>>,
    gettext_flags: Option<Vec<String>>,
    auto_detect: bool,
    max_input_bytes: Option<usize>,
    max_occurrences: Option<usize>,
    max_placeholder_bytes: Option<usize>,
    max_nesting: Option<usize>,
) -> PyResult<Vec<PythonOccurrence>> {
    let options = python_options(
        syntaxes,
        gettext_flags,
        auto_detect,
        PythonLimits {
            max_input_bytes,
            max_occurrences,
            max_placeholder_bytes,
            max_nesting,
        },
    )?;
    let analysis = py
        .detach(move || detect(&text, &options))
        .map_err(placeholder_error)?;
    Ok(analysis
        .occurrences
        .into_iter()
        .map(python_occurrence)
        .collect())
}

#[pyfunction(name = "project_placeholders")]
#[pyo3(signature = (
    text,
    syntaxes=None,
    gettext_flags=None,
    auto_detect=true,
    max_input_bytes=None,
    max_occurrences=None,
    max_placeholder_bytes=None,
    max_nesting=None,
))]
#[allow(clippy::too_many_arguments)]
fn project_placeholders_py(
    py: Python<'_>,
    text: String,
    syntaxes: Option<Vec<String>>,
    gettext_flags: Option<Vec<String>>,
    auto_detect: bool,
    max_input_bytes: Option<usize>,
    max_occurrences: Option<usize>,
    max_placeholder_bytes: Option<usize>,
    max_nesting: Option<usize>,
) -> PyResult<Py<PyTuple>> {
    let options = python_options(
        syntaxes,
        gettext_flags,
        auto_detect,
        PythonLimits {
            max_input_bytes,
            max_occurrences,
            max_placeholder_bytes,
            max_nesting,
        },
    )?;
    let projection = py
        .detach(move || project(&text, &options))
        .map_err(placeholder_error)?;
    let classes = PythonClasses::import(py)?;
    let rendered = projection.text.into_pyobject(py)?.into_any();
    let tag_map = classes.tie_map(py, projection.tag_map)?.into_any();
    let parts = classes.parts(py, projection.parts)?.into_any();
    let token_prefix = projection.token_prefix.into_pyobject(py)?.into_any();
    Ok(PyTuple::new(py, [rendered, tag_map, parts, token_prefix])?.unbind())
}

#[pyfunction(name = "project_data_placeholders")]
#[pyo3(signature = (
    data,
    runtime_placeholders=true,
    inline_placeholders=true,
    project_targets=true,
    syntaxes=None,
    gettext_flags=None,
    auto_detect=true,
    max_input_bytes=None,
    max_occurrences=None,
    max_placeholder_bytes=None,
    max_nesting=None,
))]
#[allow(clippy::too_many_arguments)]
fn project_data_placeholders_py(
    py: Python<'_>,
    data: &Bound<'_, PyAny>,
    runtime_placeholders: bool,
    inline_placeholders: bool,
    project_targets: bool,
    syntaxes: Option<Vec<String>>,
    gettext_flags: Option<Vec<String>>,
    auto_detect: bool,
    max_input_bytes: Option<usize>,
    max_occurrences: Option<usize>,
    max_placeholder_bytes: Option<usize>,
    max_nesting: Option<usize>,
) -> PyResult<Py<PyAny>> {
    let data = data_from_python(data)?;
    let detection = python_options(
        syntaxes,
        gettext_flags,
        auto_detect,
        PythonLimits {
            max_input_bytes,
            max_occurrences,
            max_placeholder_bytes,
            max_nesting,
        },
    )?;
    let options = PlaceholderProjectionOptions {
        detection,
        runtime_placeholders,
        inline_placeholders,
        project_targets,
    };
    let projected = py
        .detach(move || project_data(data, &options))
        .map_err(placeholder_error)?;
    Ok(PythonClasses::import(py)?.data(py, projected)?.unbind())
}

#[pyfunction(name = "resolve_data_placeholders")]
fn resolve_data_placeholders_py(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let data = data_from_python(data)?;
    let resolved = py
        .detach(move || resolve_data(data))
        .map_err(placeholder_error)?;
    Ok(PythonClasses::import(py)?.data(py, resolved)?.unbind())
}

#[pyfunction(name = "literalize_data_placeholders")]
fn literalize_data_placeholders_py(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let data = data_from_python(data)?;
    let literal = py
        .detach(move || literalize_data(data))
        .map_err(placeholder_error)?;
    Ok(PythonClasses::import(py)?.data(py, literal)?.unbind())
}

#[pyfunction(name = "canonicalize_placeholders")]
#[pyo3(signature = (
    text,
    syntaxes=None,
    gettext_flags=None,
    auto_detect=true,
    max_input_bytes=None,
    max_occurrences=None,
    max_placeholder_bytes=None,
    max_nesting=None,
))]
#[allow(clippy::too_many_arguments)]
fn canonicalize_placeholders_py(
    py: Python<'_>,
    text: String,
    syntaxes: Option<Vec<String>>,
    gettext_flags: Option<Vec<String>>,
    auto_detect: bool,
    max_input_bytes: Option<usize>,
    max_occurrences: Option<usize>,
    max_placeholder_bytes: Option<usize>,
    max_nesting: Option<usize>,
) -> PyResult<(String, String)> {
    let options = python_options(
        syntaxes,
        gettext_flags,
        auto_detect,
        PythonLimits {
            max_input_bytes,
            max_occurrences,
            max_placeholder_bytes,
            max_nesting,
        },
    )?;
    let result = py
        .detach(move || canonicalize(&text, &options))
        .map_err(placeholder_error)?;
    Ok((result.text, result.signature))
}

#[pyfunction(name = "reform_placeholders")]
#[pyo3(signature = (
    candidate_source,
    candidate_target,
    query_source,
    syntaxes=None,
    gettext_flags=None,
    auto_detect=true,
    max_input_bytes=None,
    max_occurrences=None,
    max_placeholder_bytes=None,
    max_nesting=None,
))]
#[allow(clippy::too_many_arguments)]
fn reform_placeholders_py(
    py: Python<'_>,
    candidate_source: String,
    candidate_target: String,
    query_source: String,
    syntaxes: Option<Vec<String>>,
    gettext_flags: Option<Vec<String>>,
    auto_detect: bool,
    max_input_bytes: Option<usize>,
    max_occurrences: Option<usize>,
    max_placeholder_bytes: Option<usize>,
    max_nesting: Option<usize>,
) -> PyResult<(String, bool, String)> {
    let options = python_options(
        syntaxes,
        gettext_flags,
        auto_detect,
        PythonLimits {
            max_input_bytes,
            max_occurrences,
            max_placeholder_bytes,
            max_nesting,
        },
    )?;
    let result = py
        .detach(move || {
            reform(
                &candidate_source,
                &candidate_target,
                &query_source,
                &options,
            )
        })
        .map_err(placeholder_error)?;
    Ok((result.text, result.changed, result.signature))
}

fn python_options(
    syntaxes: Option<Vec<String>>,
    gettext_flags: Option<Vec<String>>,
    auto_detect: bool,
    limits: PythonLimits,
) -> PyResult<DetectionOptions> {
    let syntaxes = syntaxes
        .unwrap_or_default()
        .into_iter()
        .map(|value| PlaceholderSyntax::from_str(&value).map_err(placeholder_error))
        .collect::<PyResult<Vec<_>>>()?;
    let gettext_flags = gettext_flags.unwrap_or_default();
    let mut options = DetectionOptions::from_hints(syntaxes, &gettext_flags, auto_detect);
    let mut configured = PlaceholderLimits::default();
    set_limit(
        &mut configured.max_input_bytes,
        limits.max_input_bytes,
        "max_input_bytes",
    )?;
    set_limit(
        &mut configured.max_occurrences,
        limits.max_occurrences,
        "max_occurrences",
    )?;
    set_limit(
        &mut configured.max_placeholder_bytes,
        limits.max_placeholder_bytes,
        "max_placeholder_bytes",
    )?;
    set_limit(
        &mut configured.max_nesting,
        limits.max_nesting,
        "max_nesting",
    )?;
    options.limits = configured;
    Ok(options)
}

fn set_limit(target: &mut usize, value: Option<usize>, name: &str) -> PyResult<()> {
    if let Some(value) = value {
        if value == 0 {
            return Err(PyValueError::new_err(format!(
                "{name} must be greater than zero"
            )));
        }
        *target = value;
    }
    Ok(())
}

fn python_occurrence(value: PlaceholderOccurrence) -> PythonOccurrence {
    let (key_start, key_end) = value
        .key_range
        .map_or((None, None), |range| (Some(range.start), Some(range.end)));
    (
        value.syntax.as_str().to_owned(),
        value.role.as_str().to_owned(),
        value.value_type.as_str().to_owned(),
        value.key,
        value.range.start,
        value.range.end,
        key_start,
        key_end,
        value.original_text,
    )
}

fn placeholder_error(error: impl ToString) -> PyErr {
    PyValueError::new_err(error.to_string())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(detect_placeholders_py, module)?)?;
    module.add_function(wrap_pyfunction!(project_placeholders_py, module)?)?;
    module.add_function(wrap_pyfunction!(project_data_placeholders_py, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_data_placeholders_py, module)?)?;
    module.add_function(wrap_pyfunction!(literalize_data_placeholders_py, module)?)?;
    module.add_function(wrap_pyfunction!(canonicalize_placeholders_py, module)?)?;
    module.add_function(wrap_pyfunction!(reform_placeholders_py, module)?)?;
    Ok(())
}
