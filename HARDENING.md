# Hardening notes for nginx 1.30.2

This branch hardens the legacy watermark extension while preserving the original
configuration interface and the design goal of keeping image watermarking in
nginx rather than in application/backend code.

## Baseline

The module was reviewed against the stock
`src/http/modules/ngx_http_image_filter_module.c` from nginx 1.30.2 and
against current nginx upstream. The non-watermark buffer allocation changes
that had drifted from stock nginx were returned to the nginx implementation.

The branch also backports nginx upstream commit
`95a24d1b9cdd89608c601748e2fdfe94fb81e7b3` ("Image filter: fixed reading
past the received data"). That upstream fix adjusts `ctx->length` to the
number of bytes actually received when an upstream response has no
Content-Length, preventing parsers/decoders from treating unused buffer space
as valid image data.

## Correctness and crash fixes

The following issues were found and fixed:

- A failed `fopen()` was followed by an unconditional
  `fclose(watermark_file)`. When the watermark did not exist this became
  `fclose(NULL)` and could SIGSEGV an nginx worker.
- The invalid-PNG path could run cleanup against an invalid image pointer.
  Image destruction is now restricted to successfully created GD images.
- Runtime evaluation of `image_filter_watermark` and
  `image_filter_watermark_position` wrote request-pool pointers back into the
  shared location configuration. Watermark path and position are now evaluated
  into request-local state only.
- Dynamic watermark path/position allocations are checked before use and are
  explicitly NUL-terminated before passing them to C library/string functions.
- `image_filter_watermark_width_from` incorrectly inherited the parent
  height threshold. It now inherits `watermark_width_from`.
- The watermark path and position complex values were not inherited correctly
  across nginx configuration scopes. They now inherit explicitly.
- Threshold checks now use the actual final GD image dimensions after
  resize/crop/rotation, rather than stale target values.
- Intermediate GD allocations used for watermark composition and transparent
  PNG/GIF handling are checked before dereference.
- Watermarks larger than the destination image are rejected safely instead of
  allowing negative/out-of-range placement calculations.
- Placement coordinates are clamped to the destination image.
- Invalid dynamic positions fall back to `bottom-right` with a warning.
- `center-random` now uses nginx's `ngx_random()` helper rather than direct
  libc `rand()`.
- Meaningless `image_filter watermark <width> <height>` syntax is rejected;
  watermark-only mode remains `image_filter watermark;`.
- `image_filter watermark;` without an
  `image_filter_watermark` directive is rejected during configuration merge
  instead of failing later per request.

## Failure policy

Watermarking is treated as an optional image decoration, not as a reason to
kill a worker or make an otherwise valid image unavailable.

For `image_filter watermark;` mode, missing, unreadable, corrupt, oversized,
or otherwise unusable watermark assets fail open: nginx returns the original
image unchanged.

For resize/crop/rotate pipelines that also specify
`image_filter_watermark`, a watermark failure leaves the transformed image
available without the watermark.

Missing/unreadable and invalid-PNG watermark files are logged at warning level
rather than as worker-fatal errors.

## Preserved interface

The branch preserves these directives:

```nginx
image_filter watermark;
image_filter_watermark /path/to/watermark.png;
image_filter_watermark_position bottom-right;
image_filter_watermark_width_from 300;
image_filter_watermark_height_from 300;
```

The watermark path and position may still contain nginx variables.

Supported positions remain:

`top-left`, `top-right`, `bottom-right`, `bottom-left`,
`right-center`, `left-center`, `bottom-center`, `top-center`,
`center-center`, and `center-random`.

## Compile validation

`.github/workflows/build-nginx-1.30.2.yml` replaces the stock nginx 1.30.2
image-filter source with this file and compiles nginx with
`--with-http_image_filter_module`. GitHub Actions may need to be explicitly
enabled on a newly-created fork before the first run appears.

## Validation matrix before production

A rebuilt nginx should be exercised with at least:

1. Valid PNG watermark.
2. Missing watermark file.
3. Unreadable watermark file.
4. Zero-byte watermark file.
5. Non-PNG content at the watermark path.
6. Watermark larger than the destination image.
7. JPEG source.
8. PNG source.
9. GIF source.
10. WebP source when GD WebP support is enabled.
11. Resize plus watermark.
12. Crop plus watermark.
13. Rotate plus watermark.
14. Watermark-only mode.
15. Static watermark path.
16. Variable watermark path.
17. Static and variable watermark positions.
18. Parent-scope watermark settings inherited by a child location.
19. Width/height threshold inheritance.
20. Concurrent repeated requests to missing and valid watermark paths.

For the missing/corrupt cases, the important regression criterion is that no
nginx worker exits with SIGSEGV and no coredump is produced.

## Known design limitations

This remains a synchronous nginx image-filter extension. Each request opens and
decodes the selected watermark PNG; there is no decoded-watermark cache. That
is a performance/design limitation rather than a correctness bug and should be
addressed separately if profiling shows it matters.

The historical PNG/GIF transparency workaround flattens the intermediate image
onto a white background. It is preserved for compatibility and should be
reviewed separately if alpha-preserving output is required.

The watermark asset itself is still PNG-only.
