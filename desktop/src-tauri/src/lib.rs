use serde::Serialize;
use std::ffi::OsString;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{mpsc, Mutex};
use std::time::{Duration, Instant};
use tauri::webview::{NewWindowResponse, WebviewWindowBuilder};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl};
use url::Url;

const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 8765;
const DASHBOARD_WINDOW: &str = "dashboard";

#[derive(Default)]
struct RuntimeState {
    child: Mutex<Option<Child>>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct EnvironmentInfo {
    platform: &'static str,
    strategy: &'static str,
    wsl_available: bool,
    staragent_available: bool,
    staragent_version: String,
    tmux_available: bool,
    tmux_version: String,
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

#[derive(Default)]
struct NativeEnvironment {
    path: Option<OsString>,
    staragent: Option<OsString>,
    tmux: Option<OsString>,
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

fn inspect_environment() -> Result<EnvironmentInfo, String> {
    let endpoint = local_endpoint(DEFAULT_PORT);
    if cfg!(target_os = "windows") {
        let wsl_available = wsl_status("true").is_some();
        let staragent_version = wsl_status("staragent version").unwrap_or_default();
        let tmux_version = wsl_status("tmux -V").unwrap_or_default();
        let install_hint = if !wsl_available {
            "wsl --install -d Ubuntu".to_string()
        } else {
            concat!(
                "wsl -- bash -lc \"sudo apt update && sudo apt install -y tmux pipx ",
                "&& pipx install git+https://github.com/SiriusNEO/StarAgent.git ",
                "&& pipx ensurepath\""
            )
            .to_string()
        };
        return Ok(EnvironmentInfo {
            platform: "windows",
            strategy: "wsl",
            wsl_available,
            staragent_available: !staragent_version.is_empty(),
            staragent_version,
            tmux_available: !tmux_version.is_empty(),
            tmux_version,
            runtime_running: local_dashboard_ready(DEFAULT_PORT),
            default_endpoint: endpoint,
            install_hint,
        });
    }

    let native = native_environment();
    let staragent_version = command_output(
        native.staragent.as_ref(),
        &["version"],
        native.path.as_ref(),
    );
    let tmux_version = command_output(native.tmux.as_ref(), &["-V"], native.path.as_ref());
    let install_hint = if cfg!(target_os = "macos") {
        concat!(
            "brew install tmux pipx && ",
            "pipx install git+https://github.com/SiriusNEO/StarAgent.git && pipx ensurepath"
        )
        .to_string()
    } else {
        concat!(
            "sudo apt install tmux pipx && ",
            "pipx install git+https://github.com/SiriusNEO/StarAgent.git && pipx ensurepath"
        )
        .to_string()
    };
    Ok(EnvironmentInfo {
        platform: if cfg!(target_os = "macos") {
            "macos"
        } else {
            "linux"
        },
        strategy: "native",
        wsl_available: false,
        staragent_available: !staragent_version.is_empty(),
        staragent_version,
        tmux_available: !tmux_version.is_empty(),
        tmux_version,
        runtime_running: local_dashboard_ready(DEFAULT_PORT),
        default_endpoint: endpoint,
        install_hint,
    })
}

fn start_runtime(app: &AppHandle, port: u16) -> Result<RuntimeStart, String> {
    if !(1024..=65535).contains(&port) {
        return Err("The local Launcher port must be between 1024 and 65535.".to_string());
    }
    let endpoint = local_endpoint(port);
    if local_dashboard_ready(port) {
        return Ok(RuntimeStart {
            endpoint,
            reused: true,
        });
    }

    clear_finished_child(app)?;
    let state = app.state::<RuntimeState>();
    if state
        .child
        .lock()
        .map_err(|_| "The runtime process lock is unavailable.".to_string())?
        .is_some()
    {
        return wait_for_runtime(port, endpoint, true);
    }

    let mut command = runtime_command(port)?;
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    suppress_console_window(&mut command);
    let child = command
        .spawn()
        .map_err(|error| format!("Could not start StarAgent: {error}"))?;
    *state
        .child
        .lock()
        .map_err(|_| "The runtime process lock is unavailable.".to_string())? = Some(child);

    let result = wait_for_runtime(port, endpoint, false);
    if result.is_err() {
        stop_managed_runtime(app);
    }
    result
}

fn wait_for_runtime(port: u16, endpoint: String, reused: bool) -> Result<RuntimeStart, String> {
    let deadline = Instant::now() + Duration::from_secs(20);
    while Instant::now() < deadline {
        if local_dashboard_ready(port) {
            return Ok(RuntimeStart { endpoint, reused });
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    Err(format!(
        "StarAgent did not become ready at {endpoint} within 20 seconds. Check that the port is free and run `staragent` in a terminal for details."
    ))
}

fn runtime_command(port: u16) -> Result<Command, String> {
    if cfg!(target_os = "windows") {
        if wsl_status("true").is_none() {
            return Err(
                "WSL2 with a Linux distribution is required for local mode on Windows.".to_string(),
            );
        }
        if wsl_status("command -v staragent").is_none() {
            return Err("StarAgent is not installed in the default WSL2 distribution.".to_string());
        }
        if wsl_status("command -v tmux").is_none() {
            return Err("tmux is not installed in the default WSL2 distribution.".to_string());
        }
        let script =
            format!("exec staragent dashboard --host {DEFAULT_HOST} --port {port} --mode launcher");
        let mut command = Command::new("wsl.exe");
        command.args(["--exec", "sh", "-lc", &script]);
        return Ok(command);
    }

    let native = native_environment();
    let staragent = native
        .staragent
        .ok_or_else(|| "StarAgent CLI was not found in your login PATH.".to_string())?;
    if native.tmux.is_none() {
        return Err("tmux was not found in your login PATH.".to_string());
    }
    let mut command = Command::new(staragent);
    command.args([
        "dashboard",
        "--host",
        DEFAULT_HOST,
        "--port",
        &port.to_string(),
        "--mode",
        "launcher",
    ]);
    if let Some(path) = native.path {
        command.env("PATH", path);
    }
    Ok(command)
}

fn native_environment() -> NativeEnvironment {
    let path = native_path();
    let staragent = std::env::var_os("STARAGENT_DESKTOP_CLI").or_else(|| {
        path.as_ref()
            .and_then(|value| find_executable("staragent", value))
    });
    let tmux = path
        .as_ref()
        .and_then(|value| find_executable("tmux", value));
    NativeEnvironment {
        path,
        staragent,
        tmux,
    }
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

fn find_executable(name: &str, path: &OsString) -> Option<OsString> {
    std::env::split_paths(path)
        .map(|directory| directory.join(name))
        .find(|candidate| Path::new(candidate).is_file())
        .map(PathBuf::into_os_string)
}

fn login_shell_output(script: &str) -> Option<String> {
    let shell = std::env::var_os("SHELL").unwrap_or_else(|| OsString::from("/bin/sh"));
    let mut command = Command::new(shell);
    command.args(["-lc", script]);
    suppress_console_window(&mut command);
    successful_output(&mut command)
}

fn wsl_status(script: &str) -> Option<String> {
    let mut command = Command::new("wsl.exe");
    command.args(["--exec", "sh", "-lc", script]);
    suppress_console_window(&mut command);
    successful_output(&mut command)
}

fn command_output(program: Option<&OsString>, args: &[&str], path: Option<&OsString>) -> String {
    let Some(program) = program else {
        return String::new();
    };
    let mut command = Command::new(program);
    command.args(args);
    if let Some(path) = path {
        command.env("PATH", path);
    }
    suppress_console_window(&mut command);
    successful_output(&mut command).unwrap_or_default()
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
        format!("GET /login HTTP/1.1\r\nHost: {DEFAULT_HOST}:{port}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = Vec::with_capacity(4096);
    if stream.take(32 * 1024).read_to_end(&mut response).is_err() {
        return false;
    }
    String::from_utf8_lossy(&response).contains("StarAgent")
}

fn clear_finished_child(app: &AppHandle) -> Result<(), String> {
    let state = app.state::<RuntimeState>();
    let mut guard = state
        .child
        .lock()
        .map_err(|_| "The runtime process lock is unavailable.".to_string())?;
    if let Some(child) = guard.as_mut() {
        match child.try_wait() {
            Ok(Some(_)) => *guard = None,
            Ok(None) => {}
            Err(_) => *guard = None,
        }
    }
    Ok(())
}

fn stop_managed_runtime(app: &AppHandle) {
    let state = app.state::<RuntimeState>();
    let Ok(mut guard) = state.child.lock() else {
        return;
    };
    if let Some(child) = guard.as_mut() {
        let _ = child.kill();
        let _ = child.wait();
    }
    *guard = None;
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
        .manage(RuntimeState::default())
        .invoke_handler(tauri::generate_handler![
            environment_info,
            start_local_runtime,
            open_dashboard
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
    use super::{normalize_endpoint, same_origin};
    use url::Url;

    #[test]
    fn endpoint_defaults_to_http_and_root() {
        let value = normalize_endpoint("staragent.internal:8080/nodes?ignored=1").unwrap();
        assert_eq!(value.as_str(), "http://staragent.internal:8080/");
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
}
