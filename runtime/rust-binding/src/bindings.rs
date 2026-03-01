/* Mirror of the C `slice` struct in runtime.h.
 * Field order and types must stay in sync with:
 *   typedef struct { char *data; size_t len; } slice;
 */
#[repr(C)]
pub(crate) struct Slice {
    pub data: *const u8,
    pub len: usize,
}

/* Mirror of `http_recv_buffer` in runtime.h. */
#[repr(C)]
pub(crate) struct HttpBuffer {
    pub buffer: Slice,
    pub aws_request_id: Slice,
    pub body: Slice,
}

#[repr(C)]
pub(crate) struct Runtime {
    _opaque: [u8; 0],
}

extern "C" {
    pub(crate) fn runtime_init() -> *const Runtime;
    pub(crate) fn get_next_request(rt: *const Runtime) -> *const HttpBuffer;
    pub(crate) fn get_response_buffer(rt: *const Runtime) -> *mut u8;
    pub(crate) fn send_response(rt: *const Runtime, response: *const u8, response_len: usize);

    // Arena allocator
    pub fn arena_init(a: *mut Arena, capacity: usize);
    pub fn arena_reset(a: *mut Arena);
    pub fn arena_freeze(a: *mut Arena);
}

#[repr(C)]
pub struct Arena {
    pub base: *mut u8,
    pub cursor: *mut u8,
    pub limit: *mut u8,
}

// Arena tags
pub const ARENA_STATIC: i32 = 0;
pub const ARENA_REQ: i32 = 1;

extern "C" {
    pub static mut static_arena: Arena;
    pub static mut req_arena: Arena;
}
