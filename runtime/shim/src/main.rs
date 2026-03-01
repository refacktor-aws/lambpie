#![no_std]
#![no_main]

use aws_lambda_libc::api::{lambda_event_loop, Event, Writer};
use aws_lambda_libc::bindings::{arena_init, arena_reset, arena_freeze, static_arena, req_arena};

// Default arena sizes
const STATIC_ARENA_SIZE: usize = 64 * 1024;  // 64 KB for cold-start allocations
const REQ_ARENA_SIZE: usize = 256 * 1024;    // 256 KB for per-request allocations

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
    unsafe {
        // Initialize both arenas before handler init
        arena_init(&raw mut static_arena, STATIC_ARENA_SIZE);
        arena_init(&raw mut req_arena, REQ_ARENA_SIZE);

        lambpie_init();

        // Freeze static arena — writes after init cause SIGSEGV
        arena_freeze(&raw mut static_arena);
    }

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

        // Reset request arena after each invocation — zero-cost bulk free
        unsafe { arena_reset(&raw mut req_arena); }
    });
}
