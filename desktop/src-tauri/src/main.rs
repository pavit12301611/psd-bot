// Standard Tauri entry point. The windows_subsystem attribute is the template
// default and a no-op on Linux, where a release build has no console to hide.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    psd_ai_desktop_lib::run()
}
