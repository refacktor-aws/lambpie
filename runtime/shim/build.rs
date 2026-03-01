use std::env;

fn main() {
    // Link the compiled handler object file
    let handler_obj = env::var("LAMBPIE_HANDLER_OBJ")
        .expect("LAMBPIE_HANDLER_OBJ must point to the compiled handler .o file");

    println!("cargo:rustc-link-arg={}", handler_obj);
    println!("cargo:rustc-link-lib=dylib=c");
    println!("cargo:rustc-link-arg=-nostartfiles");
    println!("cargo:rerun-if-env-changed=LAMBPIE_HANDLER_OBJ");
    println!("cargo:rerun-if-changed={}", handler_obj);
}
