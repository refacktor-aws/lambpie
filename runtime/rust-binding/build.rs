use std::env;

fn main() {
    let profile = env::var("PROFILE").unwrap();
    let mut build = cc::Build::new();

    build.file("../src/runtime.c")
        .file("../src/arena.c")
        .file("../src/json.c")
        .file("../src/tls.c")
        .std("c17");

    if profile == "release" {
        build.define("RELEASE", None);
    }

    build.include("../src")
        .warnings(true)
        .compile("aws_lambda_libc");

    println!("cargo:rerun-if-changed=../src/runtime.c");
    println!("cargo:rerun-if-changed=../src/arena.c");
    println!("cargo:rerun-if-changed=../src/json.c");
    println!("cargo:rerun-if-changed=../src/tls.c");
    println!("cargo:rerun-if-changed=../src/tls.h");
    println!("cargo:rerun-if-changed=../src/log.h");

    // tls.c uses dlopen/dlsym — link libdl.
    // On glibc 2.34+ (AL2023) dlopen is in libc itself, but -ldl is still
    // accepted as a no-op, so this is safe across all target glibc versions.
    println!("cargo:rustc-link-lib=dylib=dl");
    println!("cargo:rustc-link-lib=dylib=c");
}
