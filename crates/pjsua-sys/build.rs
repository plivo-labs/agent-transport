use std::env;
use std::path::PathBuf;

fn main() {
    let out_dir = PathBuf::from(env::var("OUT_DIR").unwrap());
    let target_os = env::var("CARGO_CFG_TARGET_OS").unwrap();

    // Find PJSIP via pkg-config (works with system packages and Homebrew)
    let pjproject = pkg_config::Config::new()
        .atleast_version("2.5")
        .probe("libpjproject")
        .expect(
            "Could not find libpjproject via pkg-config.\n\
             Install it:\n\
             - macOS:        brew install pjproject\n\
             - Debian/Ubuntu: sudo apt install libpjproject-dev\n\
             - CentOS/RHEL:  sudo dnf install pjproject-devel  (enable EPEL first)\n\
             - Alpine:       apk add pjproject-dev\n\
             \n\
             Or set PKG_CONFIG_PATH to point to your libpjproject.pc"
        );

    // pkg-config emits the main PJSIP libraries but misses third-party static libs
    // (srtp, resample, webrtc, etc.) that PJSIP was built with. Link them explicitly.
    for link_path in &pjproject.link_paths {
        if let Ok(entries) = std::fs::read_dir(link_path) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.extension().map_or(false, |e| e == "a") {
                    if let Some(fname) = path.file_stem() {
                        let name = fname.to_string_lossy();
                        if let Some(lib_name) = name.strip_prefix("lib") {
                            // Check if already linked by pkg-config
                            let already_linked = pjproject.libs.iter().any(|l| lib_name.starts_with(l.as_str()));
                            if !already_linked {
                                println!("cargo:rustc-link-lib=static={}", lib_name);
                            }
                        }
                    }
                }
            }
        }
    }

    // Platform-specific system libraries that PJSIP depends on
    match target_os.as_str() {
        "macos" => {
            println!("cargo:rustc-link-lib=framework=AudioToolbox");
            println!("cargo:rustc-link-lib=framework=AudioUnit");
            println!("cargo:rustc-link-lib=framework=CoreAudio");
            println!("cargo:rustc-link-lib=framework=CoreFoundation");
            println!("cargo:rustc-link-lib=framework=CoreServices");
            println!("cargo:rustc-link-lib=framework=AVFoundation");
            println!("cargo:rustc-link-lib=framework=CoreMedia");
            println!("cargo:rustc-link-lib=framework=CoreVideo");
            println!("cargo:rustc-link-lib=framework=VideoToolbox");
            println!("cargo:rustc-link-lib=dylib=c++");

            // Homebrew OpenSSL
            for prefix in &[
                "/opt/homebrew/opt/openssl@3/lib",
                "/usr/local/opt/openssl@3/lib",
            ] {
                if std::path::Path::new(prefix).exists() {
                    println!("cargo:rustc-link-search=native={}", prefix);
                    break;
                }
            }
            println!("cargo:rustc-link-lib=dylib=ssl");
            println!("cargo:rustc-link-lib=dylib=crypto");
        }
        "linux" => {
            println!("cargo:rustc-link-lib=dylib=ssl");
            println!("cargo:rustc-link-lib=dylib=crypto");
            println!("cargo:rustc-link-lib=dylib=pthread");
            println!("cargo:rustc-link-lib=dylib=m");
            // ALSA — may not be present on all systems
            if pkg_config::probe_library("alsa").is_ok() {
                // pkg-config already emits the link flags
            }
            // uuid
            println!("cargo:rustc-link-lib=dylib=uuid");
        }
        "windows" => {
            println!("cargo:rustc-link-lib=dylib=ws2_32");
            println!("cargo:rustc-link-lib=dylib=ole32");
            println!("cargo:rustc-link-lib=dylib=winmm");
        }
        _ => {}
    }

    // Generate Rust FFI bindings from pjsua.h
    let mut builder = bindgen::Builder::default()
        .header("wrapper.h")
        .parse_callbacks(Box::new(bindgen::CargoCallbacks::new()))
        // Allow PJSUA functions and types
        .allowlist_function("pjsua_.*")
        .allowlist_function("pjsip_.*")
        .allowlist_function("pjmedia_.*")
        .allowlist_function("pj_.*")
        .allowlist_type("pjsua_.*")
        .allowlist_type("pjsip_.*")
        .allowlist_type("pjmedia_.*")
        .allowlist_type("pj_.*")
        .allowlist_var("PJ.*")
        .allowlist_var("PJSIP.*")
        .allowlist_var("PJSUA.*")
        .allowlist_var("PJMEDIA.*")
        // Derive common traits
        .derive_debug(true)
        .derive_default(true)
        .derive_copy(true)
        // Layout tests can fail on cross-compilation
        .layout_tests(false)
        // Use core types
        .use_core()
        .ctypes_prefix("::std::os::raw");

    // Add include paths from pkg-config
    for path in &pjproject.include_paths {
        builder = builder.clang_arg(format!("-I{}", path.display()));
    }

    // Add defines from pkg-config (e.g., -DPJ_AUTOCONF=1)
    for (key, val) in &pjproject.defines {
        if let Some(v) = val {
            builder = builder.clang_arg(format!("-D{}={}", key, v));
        } else {
            builder = builder.clang_arg(format!("-D{}", key));
        }
    }

    let bindings = builder
        .generate()
        .expect("Failed to generate PJSUA bindings");

    bindings
        .write_to_file(out_dir.join("bindings.rs"))
        .expect("Failed to write bindings");

    println!("cargo:rerun-if-changed=wrapper.h");
}
