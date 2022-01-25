use prost::Message;
use pyo3::exceptions::{PyException, PyTypeError};
use pyo3::{create_exception, prelude::*, types::PyBytes, wrap_pyfunction};
use std::sync::Arc;
use temporal_sdk_core::Url;
use temporal_sdk_core::{
    init, CoreInitOptionsBuilder, CoreSDK, ServerGatewayOptionsBuilder, TelemetryOptionsBuilder,
    WorkerConfigBuilder,
};
use temporal_sdk_core_api::Core as CoreTrait;
use temporal_sdk_core_protos::coresdk::workflow_completion::WorkflowActivationCompletion;

create_exception!(temporal_sdk_core, UnexpectedError, PyException);

#[pyclass]
struct Worker {
    task_queue: String,
    core: Arc<CoreSDK>,
}

#[pyfunction]
fn poll_workflow_activation<'py>(py: Python<'py>, worker: &Worker) -> PyResult<&'py PyAny> {
    let core = worker.core.clone();
    let task_queue = worker.task_queue.clone();
    pyo3_asyncio::tokio::future_into_py(py, async move {
        match core.poll_workflow_activation(&task_queue).await {
            Ok(activation) => {
                let len = activation.encoded_len();
                Python::with_gil(|py| {
                    PyBytes::new_with(py, len, |mut bytes: &mut [u8]| {
                        activation
                            .encode(&mut bytes)
                            .map_err(|err| UnexpectedError::new_err(format!("{}", err)))
                    })
                    .map(|bytes| {
                        let py_bytes: Py<PyBytes> = bytes.into_py(py);
                        py_bytes
                    })
                })
            }
            Err(err) => Err(UnexpectedError::new_err(format!("{}", err))),
        }
    })
}

#[pyfunction]
fn complete_workflow_activation<'py>(
    py: Python<'py>,
    worker: &Worker,
    buf: &PyBytes,
) -> PyResult<&'py PyAny> {
    let core = worker.core.clone();
    let completion = WorkflowActivationCompletion::decode(buf.as_bytes()).unwrap();

    pyo3_asyncio::tokio::future_into_py(py, async move {
        core.complete_workflow_activation(completion)
            .await
            .map_err(|err| UnexpectedError::new_err(format!("{}", err)))
    })
}

#[pyclass]
struct Core {
    inner: Arc<CoreSDK>,
}

#[pyfunction]
fn core_new(py: Python) -> PyResult<&PyAny> {
    pyo3_asyncio::tokio::future_into_py(py, async {
        let gateway_opts = ServerGatewayOptionsBuilder::default()
            .target_url(Url::parse("http://localhost:7233").unwrap())
            .namespace("default".to_owned())
            .client_name("temporal-python".to_owned())
            .client_version("0.1.0".to_owned())
            .worker_binary_id("0000000000000000".to_owned())
            .build()
            .map_err(|err| PyTypeError::new_err(format!("{}", err)))?;
        let telemetry_opts = TelemetryOptionsBuilder::default()
            .build()
            .map_err(|err| PyTypeError::new_err(format!("{}", err)))?;
        let core_init_opts = CoreInitOptionsBuilder::default()
            .gateway_opts(gateway_opts)
            .telemetry_opts(telemetry_opts)
            .build()
            .map_err(|err| PyTypeError::new_err(format!("{}", err)))?;
        match init(core_init_opts).await {
            Ok(core) => Python::with_gil(|py| {
                Py::new(
                    py,
                    Core {
                        inner: Arc::new(core),
                    },
                )
            }),
            Err(err) => Err(UnexpectedError::new_err(format!("{}", err))),
        }
    })
}

#[pyfunction]
fn register_worker<'py>(py: Python<'py>, core: &Core, task_queue: String) -> PyResult<&'py PyAny> {
    let core = core.inner.clone();
    pyo3_asyncio::tokio::future_into_py(py, async move {
        let config = WorkerConfigBuilder::default()
            .task_queue(task_queue.clone())
            .build()
            .map_err(|err| PyTypeError::new_err(format!("{}", err)))?;
        match core.register_worker(config) {
            Ok(_) => Ok(Worker {
                core: core.clone(),
                task_queue,
            }),
            Err(err) => Err(PyTypeError::new_err(format!("{}", err))),
        }
    })
}

#[pymodule]
fn temporal_sdk_core_bridge(py: Python, m: &PyModule) -> PyResult<()> {
    m.add_class::<Core>()?;
    m.add_class::<Worker>()?;
    m.add_function(wrap_pyfunction!(core_new, m)?)?;
    m.add_function(wrap_pyfunction!(register_worker, m)?)?;
    m.add_function(wrap_pyfunction!(poll_workflow_activation, m)?)?;
    m.add_function(wrap_pyfunction!(complete_workflow_activation, m)?)?;
    m.add("UnexpectedError", py.get_type::<UnexpectedError>())?;

    Ok(())
}
