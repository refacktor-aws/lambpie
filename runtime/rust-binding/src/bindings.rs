#[repr(C)]
pub(crate) struct Data {
    pub data: *const u8,
    pub data_len: usize,
}

#[repr(C)]
pub(crate) struct HttpBuffer {
    pub buffer: Data,
    pub aws_request_id: Data,
    pub body: Data,
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
}
