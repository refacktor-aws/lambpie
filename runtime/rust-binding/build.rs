use std::env;

fn main() {
    let profile = env::var("PROFILE").unwrap();
    let mut build = cc::Build::new();

    build.file("../src/runtime.c")
        .std("c17");

    if profile == "release" {
        build.define("RELEASE", None);
    }

    build.include("../src")
        .warnings(true)
        .compile("aws_lambda_libc");

    println!("cargo:rerun-if-changed=../src/runtime.c");
    println!("cargo:rustc-link-lib=dylib=c");
}
