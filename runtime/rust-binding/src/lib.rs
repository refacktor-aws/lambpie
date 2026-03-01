#![no_std]
pub mod bindings;
pub mod api;

use core::panic::PanicInfo;

#[panic_handler]
pub fn panic(_info: &PanicInfo) -> ! {
    unsafe { core::arch::asm!("int3"); }
    loop {}
}
