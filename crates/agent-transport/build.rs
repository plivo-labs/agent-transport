fn main() {
    // Compile vendored speexdsp resample.c — no system dependency needed.
    // This is the same C code used by FreeSWITCH, Opal, baresip, etc.
    cc::Build::new()
        .file("speexdsp/resample.c")
        .include("speexdsp")
        .define("OUTSIDE_SPEEX", None)     // Use stdlib malloc/free instead of speex internals
        .define("FLOATING_POINT", None)    // Use float arithmetic (not fixed-point)
        .define("EXPORT", Some(""))        // No DLL export decorations
        .define("RANDOM_PREFIX", Some("speex"))     // Keep standard symbol names
        .opt_level(2)                      // Optimize the resampler inner loop
        .warnings(false)                   // Suppress upstream C warnings
        .compile("speexdsp");
}
