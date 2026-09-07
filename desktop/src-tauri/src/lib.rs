use serde::Serialize;
use std::ffi::OsString;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::process::Command;
use std::sync::{mpsc, Mutex};
use std::time::{Duration, Instant};
use tauri::ipc::Channel;
use tauri::webview::{NewWindowResponse, WebviewWindowBuilder};
use tauri::{AppHandle, Manager, RunEvent, State, WebviewUrl};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::{Update, UpdaterExt};
use url::Url;

const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 8765;
const DASHBOARD_WINDOW: &str = "dashboard";
const STABLE_UPDATE_ENDPOINT: &str =
    "https://github.com/SiriusNEO/StarAgent/releases/latest/download/latest.json";
const NIGHTLY_UPDATE_ENDPOINT: &str =
    "https://github.com/SiriusNEO/StarAgent/releases/download/nightly/latest.json";

#[derive(Default)]
struct RuntimeState {
    child: Mutex<Option<ManagedRuntime>>,
}

#[derive(Default)]
struct DesktopUpdateState {
    pending: Mutex<Option<Update>>,
}

#[derive(Clone, Copy)]
enum DesktopUpdateChannel {
    Stable,
    Nightly,
}

impl DesktopUpdateChannel {
    fn parse(value: &str) -> Result<Self, String> {
        match value.trim().to_ascii_lowercase().as_str() {
            "stable" => Ok(Self::Stable),
            "nightly" => Ok(Self::Nightly),
            _ => Err("Unsupported desktop update channel.".to_string()),
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::Stable => "stable",
            Self::Nightly => "nightly",
        }
    }

    fn endpoint(self) -> Result<Url, String> {
        let value = match self {
            Self::Stable => STABLE_UPDATE_ENDPOINT,
            Self::Nightly => NIGHTLY_UPDATE_ENDPOINT,
        };
        Url::parse(value).map_err(|error| format!("Invalid desktop update endpoint: {error}"))
    }
}

struct ManagedRuntime(tauri_plugin_shell::process::CommandChild);

impl ManagedRuntime {
    fn pid(&self) -> u32 {
        self.0.pid()
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct EnvironmentInfo {
    desktop_version: &'static str,
    build_commit: Option<String>,
    platform: &'static str,
    strategy: &'static str,
    runtime_available: bool,
    runtime_version: String,
    session_backend: &'static str,
    session_backend_available: bool,
    runtime_running: bool,
    default_endpoint: String,
    install_hint: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeStart {
    endpoint: String,
    reused: bool,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopUpdateInfo {
    current_version: String,
    channel: &'static str,
    available: bool,
    version: Option<String>,
    commit: Option<String>,
    notes: Option<String>,
    published_at: Option<String>,
}

#[derive(Clone, Serialize)]
#[serde(tag = "event", rename_all = "camelCase")]
enum DesktopUpdateEvent {
    Started,
    Progress {
        #[serde(rename = "chunkLength")]
        chunk_length: usize,
        #[serde(rename = "contentLength")]
        content_length: Option<u64>,
    },
    Downloaded,
}

#[tauri::command]
async fn environment_info() -> Result<EnvironmentInfo, String> {
    tauri::async_runtime::spawn_blocking(inspect_environment)
        .await
        .map_err(|error| format!("Runtime inspection failed: {error}"))?
}

#[tauri::command]
async fn start_local_runtime(app: AppHandle, port: u16) -> Result<RuntimeStart, String> {
    tauri::async_runtime::spawn_blocking(move || start_runtime(&app, port))
        .await
        .map_err(|error| format!("Runtime startup task failed: {error}"))?
}

#[tauri::command]
async fn open_dashboard(app: AppHandle, endpoint: String) -> Result<String, String> {
    let url = normalize_endpoint(&endpoint)?;
    let normalized = url.to_string();
    let (sender, receiver) = mpsc::sync_channel(1);
    let handle = app.clone();
    app.run_on_main_thread(move || {
        let result = show_dashboard_window(&handle, url);
        let _ = sender.send(result);
    })
    .map_err(|error| format!("Could not schedule the workspace window: {error}"))?;
    receiver
        .recv()
        .map_err(|_| "The workspace window did not respond.".to_string())??;
    Ok(normalized)
}

#[tauri::command]
async fn check_desktop_update(
    app: AppHandle,
    state: State<'_, DesktopUpdateState>,
    channel: String,
) -> Result<DesktopUpdateInfo, String> {
    *state
        .pending
        .lock()
        .map_err(|_| "The desktop update lock is unavailable.".to_string())? = None;

    let channel = DesktopUpdateChannel::parse(&channel)?;
    let exit_handle = app.clone();
    let updater = app
        .updater_builder()
        .endpoints(vec![channel.endpoint()?])
        .map_err(|error| format!("Could not select desktop update channel: {error}"))?
        .on_before_exit(move || {
            stop_managed_runtime(&exit_handle);
            exit_handle.cleanup_before_exit();
        })
        .build()
        .map_err(|error| format!("Could not initialize desktop updates: {error}"))?;
    let current_version = updater_current_version(&app);
    let update = updater
        .check()
        .await
        .map_err(|error| format!("Could not check for desktop updates: {error}"))?;

    let Some(update) = update else {
        return Ok(DesktopUpdateInfo {
            current_version,
            channel: channel.name(),
            available: false,
            version: None,
            commit: None,
            notes: None,
            published_at: None,
        });
    };
    let info = DesktopUpdateInfo {
        current_version,
        channel: channel.name(),
        available: true,
        version: Some(update.version.clone()),
        commit: normalized_commit(
            update
                .raw_json
                .get("commit")
                .and_then(|value| value.as_str()),
        ),
        notes: compact_update_notes(update.body.as_deref()),
        published_at: update.date.map(|date| date.to_string()),
    };
    *state
        .pending
        .lock()
        .map_err(|_| "The desktop update lock is unavailable.".to_string())? = Some(update);
    Ok(info)
}

#[tauri::command]
async fn install_desktop_update(
    app: AppHandle,
    state: State<'_, DesktopUpdateState>,
    on_event: Channel<DesktopUpdateEvent>,
) -> Result<(), String> {
    let update = state
        .pending
        .lock()
        .map_err(|_| "The desktop update lock is unavailable.".to_string())?
        .take()
        .ok_or_else(|| "Check for a desktop update before installing it.".to_string())?;

    let _ = on_event.send(DesktopUpdateEvent::Started);
    let progress_channel = on_event.clone();
    let downloaded_channel = on_event;
    update
        .download_and_install(
            move |chunk_length, content_length| {
                let _ = progress_channel.send(DesktopUpdateEvent::Progress {
                    chunk_length,
                    content_length,
                });
            },
            move || {
                let _ = downloaded_channel.send(DesktopUpdateEvent::Downloaded);
            },
        )
        .await
        .map_err(|error| format!("Could not install the desktop update: {error}"))?;

    // Windows exits from the updater after running the hook configured above.
    // macOS and Linux need an explicit restart after the verified package is installed.
    stop_managed_runtime(&app);
    app.restart();
}

fn updater_current_version(app: &AppHandle) -> String {
    app.package_info().version.to_string()
}

fn normalized_commit(value: Option<&str>) -> Option<String> {
    let value = value?.trim().to_ascii_lowercase();
    if matches!(value.len(), 40 | 64) && value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        Some(value)
    } else {
        None
    }
}

fn build_commit() -> Option<String> {
    normalized_commit(option_env!("STARAGENT_BUILD_COMMIT"))
}

fn compact_update_notes(notes: Option<&str>) -> Option<String> {
    const MAX_CHARS: usize = 4_000;
    let notes = notes?.trim();
    if notes.is_empty() {
        return None;
    }
    let mut compact = notes.chars().take(MAX_CHARS).collect::<String>();
    if notes.chars().count() > MAX_CHARS {
        compact.push('…');
    }
    Some(compact)
}

fn inspect_environment() -> Result<EnvironmentInfo, String> {
    let endpoint = local_endpoint(DEFAULT_PORT);
    let runtime_available = bundled_runtime_path().is_some_and(|path| path.is_file());
    let (platform, session_backend) = if cfg!(target_os = "windows") {
        ("windows", "Windows ConPTY")
    } else if cfg!(target_os = "macos") {
        ("macos", "Native PTY")
    } else {
        ("linux", "Native PTY")
    };
    Ok(EnvironmentInfo {
        desktop_version: env!("CARGO_PKG_VERSION"),
        build_commit: build_commit(),
        platform,
        strategy: "bundled",
        runtime_available,
        runtime_version: if runtime_available {
            format!("v{}", env!("CARGO_PKG_VERSION"))
        } else {
            String::new()
        },
        session_backend,
        session_backend_available: runtime_available,
        runtime_running: local_runtime_ready(DEFAULT_PORT),
        default_endpoint: endpoint,
        install_hint: String::new(),
    })
}

fn bundled_runtime_path() -> Option<PathBuf> {
    let directory = std::env::current_exe().ok()?.parent()?.to_path_buf();
    let name = if cfg!(target_os = "windows") {
        "staragent-runtime.exe"
    } else {
        "staragent-runtime"
    };
    Some(directory.join(name))
}

fn start_runtime(app: &AppHandle, port: u16) -> Result<RuntimeStart, String> {
    if !(1024..=65535).contains(&port) {
        return Err("The local Launcher port must be between 1024 and 65535.".to_string());
    }
    let endpoint = local_endpoint(port);
    if local_runtime_ready(port) {
        return Ok(RuntimeStart {
            endpoint,
            reused: true,
        });
    }
    if local_dashboard_ready(port) {
        return Err(format!(
            "Port {port} is occupied by a non-bundled or older StarAgent runtime. Stop it before starting the desktop Launcher."
        ));
    }
    start_bundled_runtime(app, port, endpoint)
}

fn start_bundled_runtime(
    app: &AppHandle,
    port: u16,
    endpoint: String,
) -> Result<RuntimeStart, String> {
    let state = app.state::<RuntimeState>();
    if state
        .child
        .lock()
        .map_err(|_| "The runtime process lock is unavailable.".to_string())?
        .is_some()
    {
        return wait_for_runtime(port, endpoint, true);
    }

    let mut command = app
        .shell()
        .sidecar("staragent-runtime")
        .map_err(|error| format!("Bundled StarAgent runtime is unavailable: {error}"))?
        .args([
            "--host",
            DEFAULT_HOST,
            "--port",
            &port.to_string(),
            "--mode",
            "launcher",
        ]);
    if let Some(path) = native_path() {
        command = command.env("PATH", path);
    }
    let (mut events, child) = command
        .spawn()
        .map_err(|error| format!("Could not start the bundled StarAgent runtime: {error}"))?;
    let pid = child.pid();
    *state
        .child
        .lock()
        .map_err(|_| "The runtime process lock is unavailable.".to_string())? =
        Some(ManagedRuntime(child));

    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        while events.recv().await.is_some() {}
        clear_runtime_if_pid(&handle, pid);
    });

    let result = wait_for_runtime(port, endpoint, false);
    if result.is_err() {
        stop_managed_runtime(app);
    }
    result
}

fn wait_for_runtime(port: u16, endpoint: String, reused: bool) -> Result<RuntimeStart, String> {
    let deadline = Instant::now() + Duration::from_secs(20);
    while Instant::now() < deadline {
        if local_runtime_ready(port) {
            return Ok(RuntimeStart { endpoint, reused });
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    let hint = "Check that the port is free. If this repeats, reinstall the desktop package.";
    Err(format!(
        "StarAgent did not become ready at {endpoint} within 20 seconds. {hint}"
    ))
}

fn native_path() -> Option<OsString> {
    let mut paths: Vec<PathBuf> = login_shell_output("printf %s \"$PATH\"")
        .map(OsString::from)
        .into_iter()
        .chain(std::env::var_os("PATH"))
        .flat_map(|value| std::env::split_paths(&value).collect::<Vec<_>>())
        .collect();

    if let Some(home) = std::env::var_os("HOME") {
        let home = PathBuf::from(home);
        paths.push(home.join(".local/bin"));
        paths.push(home.join(".cargo/bin"));
    }
    paths.extend(
        ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]
            .into_iter()
            .map(PathBuf::from),
    );
    paths.dedup();
    std::env::join_paths(paths).ok()
}

fn login_shell_output(script: &str) -> Option<String> {
    let shell = std::env::var_os("SHELL").unwrap_or_else(|| OsString::from("/bin/sh"));
    let mut command = Command::new(shell);
    command.args(["-lc", script]);
    suppress_console_window(&mut command);
    successful_output(&mut command)
}

fn successful_output(command: &mut Command) -> Option<String> {
    let output = command.output().ok()?;
    if !output.status.success() {
        return None;
    }
    let value = String::from_utf8_lossy(&output.stdout).trim().to_string();
    Some(value)
}

#[cfg(target_os = "windows")]
fn suppress_console_window(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(target_os = "windows"))]
fn suppress_console_window(_command: &mut Command) {}

fn local_endpoint(port: u16) -> String {
    format!("http://{DEFAULT_HOST}:{port}")
}

fn local_dashboard_ready(port: u16) -> bool {
    local_http_response_contains(port, "/login", "StarAgent")
}

fn local_runtime_ready(port: u16) -> bool {
    local_http_response_contains(port, "/api/runtime", "\"desktop_bundled\":true")
}

fn local_http_response_contains(port: u16, path: &str, marker: &str) -> bool {
    let address = (DEFAULT_HOST, port)
        .to_socket_addrs()
        .ok()
        .and_then(|mut addresses| addresses.next());
    let Some(address) = address else {
        return false;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(250)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(350)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(350)));
    let request =
        format!("GET {path} HTTP/1.1\r\nHost: {DEFAULT_HOST}:{port}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = Vec::with_capacity(4096);
    if stream.take(32 * 1024).read_to_end(&mut response).is_err() {
        return false;
    }
    String::from_utf8_lossy(&response).contains(marker)
}

fn stop_managed_runtime(app: &AppHandle) {
    let state = app.state::<RuntimeState>();
    let Ok(mut guard) = state.child.lock() else {
        return;
    };
    if let Some(child) = guard.take() {
        #[cfg(target_os = "windows")]
        terminate_windows_process_tree(child.pid());
        #[cfg(unix)]
        {
            // Let uvicorn run Python's atexit cleanup so native PTY children do
            // not survive an app update or a full desktop exit.
            if let Ok(pid) = i32::try_from(child.pid()) {
                unsafe {
                    let _ = libc::kill(pid, libc::SIGTERM);
                }
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        let _ = child.0.kill();
    }
}

fn clear_runtime_if_pid(app: &AppHandle, pid: u32) {
    let state = app.state::<RuntimeState>();
    let Ok(mut guard) = state.child.lock() else {
        return;
    };
    if guard.as_ref().is_some_and(|child| child.pid() == pid) {
        *guard = None;
    }
}

#[cfg(target_os = "windows")]
fn terminate_windows_process_tree(pid: u32) {
    let mut command = Command::new("taskkill.exe");
    command.args(["/PID", &pid.to_string(), "/T", "/F"]);
    suppress_console_window(&mut command);
    let _ = command.output();
}

fn normalize_endpoint(input: &str) -> Result<Url, String> {
    let value = input.trim();
    if value.is_empty() {
        return Err("Enter a Hub address.".to_string());
    }
    let candidate = if value.contains("://") {
        value.to_string()
    } else {
        format!("http://{value}")
    };
    let mut url = Url::parse(&candidate).map_err(|_| "Enter a valid Hub address.".to_string())?;
    if !matches!(url.scheme(), "http" | "https") {
        return Err("Hub addresses must use HTTP or HTTPS.".to_string());
    }
    if url.host_str().is_none() || !url.username().is_empty() || url.password().is_some() {
        return Err(
            "The Hub address must contain a host and must not contain credentials.".to_string(),
        );
    }
    url.set_path("/");
    url.set_query(None);
    url.set_fragment(None);
    Ok(url)
}

fn same_origin(candidate: &Url, expected: &Url) -> bool {
    candidate.scheme() == expected.scheme()
        && candidate.host_str() == expected.host_str()
        && candidate.port_or_known_default() == expected.port_or_known_default()
}

fn show_dashboard_window(app: &AppHandle, url: Url) -> Result<(), String> {
    if let Some(window) = app.get_webview_window(DASHBOARD_WINDOW) {
        window
            .navigate(url)
            .map_err(|error| format!("Could not navigate to the Hub: {error}"))?;
        window
            .show()
            .map_err(|error| format!("Could not show the workspace: {error}"))?;
        let _ = window.set_focus();
    } else {
        let allowed_origin = url.clone();
        let window = WebviewWindowBuilder::new(app, DASHBOARD_WINDOW, WebviewUrl::External(url))
            .title("StarAgent Workspace")
            .inner_size(1320.0, 860.0)
            .min_inner_size(760.0, 560.0)
            .center()
            .prevent_overflow()
            .enable_clipboard_access()
            .on_navigation(move |candidate| same_origin(candidate, &allowed_origin))
            .on_new_window(|target, _features| {
                if matches!(target.scheme(), "http" | "https" | "mailto") {
                    let _ = open::that_detached(target.as_str());
                }
                NewWindowResponse::Deny
            })
            .on_document_title_changed(|window, title| {
                let title = if title.trim().is_empty() {
                    "StarAgent Workspace".to_string()
                } else {
                    format!("{title} — StarAgent")
                };
                let _ = window.set_title(&title);
            })
            .build()
            .map_err(|error| format!("Could not create the workspace window: {error}"))?;
        install_dashboard_close_handler(app, &window);
    }
    if let Some(main) = app.get_webview_window("main") {
        let _ = main.hide();
    }
    Ok(())
}

fn install_dashboard_close_handler(app: &AppHandle, dashboard: &tauri::WebviewWindow) {
    let app = app.clone();
    dashboard.on_window_event(move |event| {
        if matches!(event, tauri::WindowEvent::CloseRequested { .. }) {
            if let Some(main) = app.get_webview_window("main") {
                let _ = main.show();
                let _ = main.set_focus();
            }
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(RuntimeState::default())
        .manage(DesktopUpdateState::default())
        .invoke_handler(tauri::generate_handler![
            environment_info,
            start_local_runtime,
            open_dashboard,
            check_desktop_update,
            install_desktop_update
        ])
        .on_window_event(|window, event| {
            if window.label() == "main" {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    stop_managed_runtime(window.app_handle());
                    window.app_handle().exit(0);
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building the StarAgent desktop application");

    app.run(|handle, event| {
        if matches!(event, RunEvent::Exit) {
            stop_managed_runtime(handle);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::{
        bundled_runtime_path, compact_update_notes, normalize_endpoint, normalized_commit,
        same_origin, DesktopUpdateChannel, DesktopUpdateEvent, NIGHTLY_UPDATE_ENDPOINT,
        STABLE_UPDATE_ENDPOINT,
    };
    use url::Url;

    #[test]
    fn endpoint_defaults_to_http_and_root() {
        let value = normalize_endpoint("staragent.internal:8080/nodes?ignored=1").unwrap();
        assert_eq!(value.as_str(), "http://staragent.internal:8080/");
    }

    #[test]
    fn bundled_runtime_is_resolved_beside_the_desktop_executable() {
        let path = bundled_runtime_path().unwrap();
        let expected = if cfg!(target_os = "windows") {
            "staragent-runtime.exe"
        } else {
            "staragent-runtime"
        };
        assert_eq!(path.file_name().unwrap(), expected);
    }

    #[test]
    fn endpoint_rejects_unsafe_schemes_and_credentials() {
        assert!(normalize_endpoint("file:///tmp/staragent").is_err());
        assert!(normalize_endpoint("https://user:secret@example.com").is_err());
    }

    #[test]
    fn navigation_is_limited_to_the_selected_origin() {
        let expected = normalize_endpoint("https://hub.example.com").unwrap();
        assert!(same_origin(
            &Url::parse("https://hub.example.com/nodes/local/agents").unwrap(),
            &expected,
        ));
        assert!(!same_origin(
            &Url::parse("https://other.example.com/").unwrap(),
            &expected,
        ));
        assert!(!same_origin(
            &Url::parse("http://hub.example.com/").unwrap(),
            &expected,
        ));
    }

    #[test]
    fn update_notes_are_trimmed_and_bounded() {
        assert_eq!(
            compact_update_notes(Some("  Fixes and polish.  ")).as_deref(),
            Some("Fixes and polish.")
        );
        assert_eq!(compact_update_notes(Some("  ")), None);
        let long = "a".repeat(4_001);
        let compact = compact_update_notes(Some(&long)).unwrap();
        assert_eq!(compact.chars().count(), 4_001);
        assert!(compact.ends_with('…'));
    }

    #[test]
    fn desktop_update_channels_are_fixed_and_allowlisted() {
        assert_eq!(
            DesktopUpdateChannel::parse("stable")
                .unwrap()
                .endpoint()
                .unwrap()
                .as_str(),
            STABLE_UPDATE_ENDPOINT
        );
        assert_eq!(
            DesktopUpdateChannel::parse("NIGHTLY")
                .unwrap()
                .endpoint()
                .unwrap()
                .as_str(),
            NIGHTLY_UPDATE_ENDPOINT
        );
        assert!(DesktopUpdateChannel::parse("https://updates.invalid").is_err());
    }

    #[test]
    fn update_commit_accepts_only_full_git_object_ids() {
        let sha = "0123456789abcdef0123456789abcdef01234567";
        assert_eq!(normalized_commit(Some(sha)).as_deref(), Some(sha));
        assert_eq!(normalized_commit(Some("ABCDEF")), None);
        assert_eq!(normalized_commit(Some("../../nightly.json")), None);
    }

    #[test]
    fn update_progress_event_matches_frontend_contract() {
        let event = serde_json::to_value(DesktopUpdateEvent::Progress {
            chunk_length: 512,
            content_length: Some(1_024),
        })
        .unwrap();
        assert_eq!(event["event"], "progress");
        assert_eq!(event["chunkLength"], 512);
        assert_eq!(event["contentLength"], 1_024);
    }
}
