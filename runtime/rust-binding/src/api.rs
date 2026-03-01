use core::slice::from_raw_parts;
use crate::bindings::{runtime_init, get_next_request, get_response_buffer, send_response};

pub struct Event<'a> {
    pub body: &'a [u8]
}

pub struct Writer {
    buffer: *mut u8,
    position: usize,
    capacity: usize,
}

impl Writer {
    pub fn write_str(&mut self, s: &str) -> () {
        let bytes = s.as_bytes();

        unsafe {
            let buffer = core::slice::from_raw_parts_mut(self.buffer, self.position + bytes.len());
            buffer[self.position..self.position + bytes.len()].copy_from_slice(bytes);
        }
        self.position += bytes.len();
    }

    pub fn buffer_ptr(&mut self) -> *mut u8 {
        self.buffer
    }

    pub fn capacity(&self) -> usize {
        self.capacity
    }

    pub fn position(&self) -> usize {
        self.position
    }

    pub fn set_position(&mut self, pos: usize) {
        self.position = pos;
    }
}

pub fn lambda_event_loop(handler_fn: impl Fn(&Event, &mut Writer) -> ()) -> ! {
    unsafe {
        core::arch::asm!(
          "and rsp, -16",
            options(nomem, nostack)
        );
        let runtime = runtime_init();
        let response_buffer = get_response_buffer(runtime);
        loop {
            let request = get_next_request(runtime);
            let event = Event {
                body: from_raw_parts((*request).body.data, (*request).body.data_len)
            };
            let mut writer = Writer {
                buffer: response_buffer,
                position: 0,
                capacity: 6 * 1048576, // MAX_RESPONSE_SIZE
            };
            handler_fn(&event, &mut writer);
            send_response(runtime, response_buffer, writer.position);
        }
    }
}
