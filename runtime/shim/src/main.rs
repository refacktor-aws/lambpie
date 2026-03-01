#![no_std]
#![no_main]

use aws_lambda_libc::api::{lambda_event_loop, Event, Writer};

extern "C" {
    fn lambpie_init();
    fn lambpie_handle(
        event_ptr: *const u8,
        event_len: usize,
        response_ptr: *mut u8,
        response_cap: usize,
    ) -> usize;
}

#[no_mangle]
#[link_section = ".text._start"]
pub extern "C" fn _start() -> ! {
    unsafe { lambpie_init(); }

    lambda_event_loop(|event: &Event, writer: &mut Writer| {
        let len = unsafe {
            lambpie_handle(
                event.body.as_ptr(),
                event.body.len(),
                writer.buffer_ptr(),
                writer.capacity(),
            )
        };
        writer.set_position(len);
    });
}
