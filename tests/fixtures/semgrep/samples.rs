// Positive sample (test fixture; never executed).
pub fn bad(p: *const u8) -> u8 {
    unsafe { *p }
}
