# PDF crate coverage (issue #47, acceptance item 2)

Can Rust crates do what `zelador/pdf.py`, `compress.py`, `compress_files.py` and `lookup/fulltext.py` do with pypdf, pypdfium2 and Pillow? This note answers per operation, with a measurement on 15 PDFs from a real Zotero library. Crate versions are the latest on crates.io on 2026-11-18. Source citations in the form `crate-version/path:line` point into the unpacked crates under `~/.cargo/registry/src/`.

## Verdict per operation

**1. Per-page text extraction – covered with caveats.** Three crates extract per-page text: `pdf_oxide`, `pdf-extract` and `pdfium-render`. None reproduces pypdf exactly: on 14 readable PDFs, `pdf_oxide` and `pdfium-render` stayed within the 2% text tolerance of pypdf on 13 files, and `pdf-extract` on 11. `lopdf`'s own `extract_text`, which #47 names, is not usable: it returned under 1% of pypdf's text on two ordinary papers. The cross-implementation false alarm the issue worries about cannot happen with today's code, because `before` and `after` are always measured by the same extractor in the same run (see "Does parity matter"). What is unmeasured is whether a Rust extractor treats a Ghostscript-rewritten file the same way it treats the original.

**2. Page count – covered.** `lopdf` (`Document::get_pages`), `pdf_oxide`, `pdfium-render` and `pdf` all matched pypdf on every readable file. Do not take the page count from `pdf-extract`'s `extract_text_by_pages`, which stops silently at the first page that fails (below).

**3. Annotation count – covered.** Counting the raw `/Annots` array with `lopdf`, resolving it when it is a reference, matched pypdf on all 14 files, 3,575 annotations in total. `pdfium-render`'s `PdfPageAnnotations::len()` also matched on all 14. The `pdf` crate (pdf-rs) does not: its typed annotation parser fails on real-world entries, so it reported 0 on 11 files.

**4. Rasterise page 1 at 2× to PNG – covered.** Two routes work. `pdfium-render` uses the same engine as pypdfium2 but needs a `libpdfium` at run time, or a PDFium you build yourself for static linking. `hayro` is pure Rust: on 6 test pages its render matched pdfium's within 1.5% of pixels, including a scanned page with CCITT and JPEG images. The catch is that `hayro` 0.9 needs Rust 1.92, above this spike's MSRV of 1.88.

**5. Compression – covered, no PDF crate needed.** It is one `gs -sDEVICE=pdfwrite …` subprocess (`zelador/compress_files.py:47-68`); in Rust that is `std::process::Command` with the same arguments and output capture. Ghostscript stays an external program that the user installs, in Python and in Rust alike.

**6. MD5 – covered.** `md-5` 0.11.0 (RustCrypto, MIT OR Apache-2.0) with the same 1 MiB chunked read as `md5_of` (`compress_files.py:71-76`). The probe's digests matched Python's `hashlib.md5` on all 15 files.

## What zelador does today

| Op | Python                                              | Where                                       |
|----|-----------------------------------------------------|---------------------------------------------|
| 1  | `page.extract_text() or ""` per page                | `zelador/pdf.py:18-35`                      |
| 2  | `len(texts)`                                        | `zelador/compress_files.py:79-90`           |
| 3  | `len(page["/Annots"].get_object())`, 0 when absent  | `zelador/pdf.py:42-47`                      |
| 4  | `pypdfium2.PdfDocument(p)[0].render(scale=2.0).to_pil().save(png)` | `zelador/lookup/fulltext.py:138-148` |
| 5  | `gs -sDEVICE=pdfwrite -dPDFSETTINGS=/<preset> …`    | `zelador/compress_files.py:47-68`           |
| 6  | `hashlib.md5`, 1 MiB chunks                         | `zelador/compress_files.py:71-76`           |

Python versions measured: pypdf 6.14.2, pypdfium2 5.11.0 with PDFium build 7920.

## Candidate crates

| Crate           | Version, date      | Licence            | MSRV | Ops        | Notes |
|-----------------|--------------------|--------------------|------|------------|-------|
| `lopdf`         | 0.47.0, 2026-11-04 | MIT                | 1.88 | 2, 3 (1 broken) | Active: repo pushed 2026-11-18, frequent releases with hardening fixes (CHANGELOG v0.44–v0.45) |
| `pdf-extract`   | 0.12.1, 2026-09-16 | MIT                | none declared | 1 | Built on `lopdf` 0.42, so a tree with both pulls two `lopdf` versions |
| `pdf_oxide`     | 0.3.78, 2026-09-08 | MIT OR Apache-2.0  | 1.88 | 1, 2       | Pre-1.0, 125 open issues, 225 crates in its dependency tree (includes `office_oxide`) |
| `pdfium-render` | 0.9.5, 2026-11-01  | MIT OR Apache-2.0  | 1.61 | 1, 2, 3, 4 | Binds PDFium (BSD-3-Clause and Apache-2.0); needs the library at run time unless built statically |
| `hayro`         | 0.9.0, 2026-11-02  | Apache-2.0 OR MIT  | 1.92 | 4          | Pure Rust, `#![forbid(unsafe_code)]`; embeds fonts and CMaps by default |
| `pdf` (pdf-rs)  | 0.10.0, 2026-03-02 | MIT                | none declared | 2 | No text extraction in the crate; annotation parsing fails on real files |
| `mupdf`         | 0.8.0, 2026-06-22  | **AGPL-3.0**       | –    | 1–4        | Not tested. Excluded on licence (below) |
| `md-5`          | 0.11.0, 2026-03-27 | MIT OR Apache-2.0  | 1.85 | 6          | – |

Versions, dates, licences and MSRVs come from the crates.io API (`https://crates.io/api/v1/crates/<name>`); repo activity from the GitHub API.

### API for each operation

- **`lopdf`**: `Document::load(path)`, then `doc.get_pages()` gives a `BTreeMap<u32, ObjectId>` (`lopdf-0.47.0/src/document.rs:569`). For annotations, read the page dictionary's `Annots`; when it is `Object::Reference`, resolve it with `get_object(id)?.as_array()`, and count the array's length. The helper `get_page_annotations` (`document.rs:793-807`) skips entries that are not dictionaries, so it can undercount compared with pypdf's `len()`; the probe counted the raw array instead. `doc.extract_text(&[page])` exists (`lopdf-0.47.0/src/parser_aux.rs:66`), but its loop only handles `Tf`, `Tj`, `TJ`, `'` and `"` on the page's own content stream (`parser_aux.rs:157-200`). It never follows `Do` into form XObjects, which is the likely reason it returned almost nothing on files 6 and 7 (not verified per file).
- **`pdf-extract`**: `extract_text_by_pages(path) -> Result<Vec<String>, OutputError>` (`pdf-extract-0.12.1/src/lib.rs:2295`). It does follow `Do` (`lib.rs:1860`) and reads `ToUnicode` (`lib.rs:921`). Two hazards: its loop is `while let Ok(content) = extract_text_by_page(&doc, page_num)` (`lib.rs:2301`), so a page that errors ends the list early without an error, and `lib.rs` has 32 `panic!`/`todo!`/`unimplemented!` sites (for example `lib.rs:962`, an unsupported `ToUnicode` name), so calls need `catch_unwind`.
- **`pdf_oxide`**: `PdfDocument::open(path)`, `page_count()` (`pdf_oxide-0.3.78/src/document.rs:4484`) and `extract_text(page_index)` (`document.rs:5758`). No annotation count was tested.
- **`pdfium-render`**: `pdfium.load_pdf_from_file(path, None)`, `doc.pages().len()`, `page.annotations().len()`, which calls `FPDFPage_GetAnnotCount` (`pdfium-render-0.9.5/src/pdf/document/page/annotations.rs:74`), `page.text()?.all()` (`page/text.rs:247`), and `page.render_with_config(&PdfRenderConfig::new().scale_page_by_factor(2.0))?.as_image()` for operation 4 (`page/render_config.rs:251`, `src/pdf/bitmap.rs:329`).
- **`hayro`**: `hayro_syntax::Pdf::new(bytes)`, then `hayro::render(&pdf.pages()[0], &RenderCache::new(), &InterpreterSettings::default(), &RenderSettings::default(), &PixmapSettings { x_scale: 2.0, y_scale: 2.0, bg_color: WHITE })` and `.into_png()` (`hayro-0.9.0/src/lib.rs:278`, `hayro-0.9.0/examples/render.rs`). The default `InterpreterSettings` resolve the 14 standard fonts only when the `embed-fonts` feature is on (`hayro-interpret-0.9.0/src/interpret/mod.rs:99-117`); it is on by default in `hayro` (`hayro-0.9.0/Cargo.toml:30-36`). The README lists what it does not support: encrypted PDFs, blending and isolation, knockout groups, colour-key masking (`hayro-0.9.0/README.md`).

## Parity measurements

A throwaway Cargo project (in the session scratchpad, not in the repo) ran every candidate over 15 PDFs from the local Zotero storage: 11 picked at size quantiles and 4 picked at random from files that carry annotations. The Python side called `zelador.pdf.scan_pages` itself, through `uv run python -I`. Text length is counted in Unicode code points on both sides (Python `len`, Rust `chars().count()`). `pdfium-render` was bound to the `libpdfium.so` shipped in the pypdfium2 wheel, with the `pdfium_7881` API feature. Percentages are the Rust total against pypdf's total for the file.

| #  | Size     | Pages | Annots (pypdf) | Annots lopdf / pdfium / pdf-rs | Text (pypdf) |  lopdf | pdf-extract | pdf_oxide | pdfium-render |
|----|----------|-------|----------------|--------------------------------|--------------|--------|-------------|-----------|---------------|
| 1  | 0 B      | error |          error |          error / error / error |        error |  error |       error |     error |         error |
| 2  | 0.16 MB  |     7 |            135 |                  135 / 135 / 0 |        34618 |  +0.2% |       +0.2% |     -0.6% |         +1.1% |
| 3  | 0.29 MB  |    21 |            278 |                278 / 278 / 278 |        70491 | -10.6% |       -0.3% |     -0.4% |         +1.3% |
| 4  | 0.50 MB  |    32 |              0 |                      0 / 0 / 0 |        73708 |  +8.9% |       +5.8% |     +1.5% |         +1.7% |
| 5  | 0.75 MB  |    10 |             68 |                    68 / 68 / 0 |        54095 |  +1.8% |       +1.6% |     -0.5% |         +1.0% |
| 6  | 1.17 MB  |    27 |           1116 |                1116 / 1116 / 0 |       110044 | -99.7% |       -0.3% |     -0.4% |         +1.3% |
| 7  | 1.92 MB  |    15 |            306 |                  306 / 306 / 0 |        63259 | -99.6% |       +0.5% |     +0.3% |         +1.5% |
| 8  | 3.17 MB  |    28 |            265 |                  265 / 265 / 0 |        90030 |  +0.7% |       +0.5% |     +0.1% |         +1.2% |
| 9  | 6.87 MB  |    43 |            204 |                  204 / 204 / 0 |        98294 |  +1.3% |       +0.4% |     +0.3% |         +1.5% |
| 10 | 13.50 MB |   295 |            667 |                667 / 667 / 667 |       838247 | +22.7% |       +7.3% |    -10.5% |        -12.0% |
| 11 | 60.70 MB |    83 |            190 |                  190 / 190 / 0 |       138293 | -37.3% |       +2.9% |     +1.7% |         +0.5% |
| 12 | 0.28 MB  |     7 |            133 |                  133 / 133 / 0 |        27367 |  +0.1% |       +0.3% |     -0.7% |         +1.2% |
| 13 | 2.88 MB  |     5 |             44 |                    44 / 44 / 0 |        19417 |  -0.5% |       +0.3% |     -0.1% |         +1.4% |
| 14 | 0.19 MB  |    12 |             27 |                27 / 27 / error |        62239 | -12.5% |       -1.5% |     -1.8% |         -0.1% |
| 15 | 0.47 MB  |    13 |            142 |                  142 / 142 / 0 |        64483 |  +0.2% |       -0.2% |     -0.5% |         +1.0% |

- Page counts from every crate that opened a file matched pypdf on all 14 readable files. File 1 is a zero-byte attachment; every library, pypdf included, rejected it.
- Files within ±2% of pypdf's text: `pdf_oxide` 13/14, `pdfium-render` 13/14, `pdf-extract` 11/14, `lopdf` 7/14. `pdfium-render` runs 1–1.5% above pypdf on almost every file, a systematic offset (probably how it emits line breaks; not checked), not noise.
- `pdf` (pdf-rs) could not open file 14 and failed to parse annotations on 11 files (`Can't parse field highlighting_mode of struct Option<HighlightingMode>`), so it reported 0. Its typed `Page::annotations` is `Lazy<Vec<MaybeRef<Annot>>>` (`pdf-0.10.0/src/object/types/page.rs:131-132`), and one bad entry fails the whole page.
- Annotation subtypes were not inspected; most are probably link annotations (not verified).

**Time** for the 15 files (release build, files read from `/mnt/c` through WSL): pypdf 97.5 s in total, 77.3 s of it on the 60.7 MB file. Rust: `lopdf` 1.4 s, `pdfium-render` 4.5 s, `pdf_oxide` 6.5 s, `pdf-extract` 7.1 s, `pdf` 0.5 s (pages and annotations only). The whole probe, all crates plus MD5, ran in 21 s wall time.

### Does parity matter?

Not with the current design. `_scan_one` computes `before = pdf_facts(source)` and `after = pdf_facts(staged)` back to back in one run (`zelador/compress_files.py:179-181`), and `pdf_facts` has no other caller. Swap and restore never measure text again; they compare MD5s only (`zelador/compress.py:145-190`). So a report written by Python and swapped by Rust, or the reverse, compares like with like. Mixing extractors becomes a problem only if a future change re-measures recorded facts. Then the 2% default tolerance (`zelador/cli_compress.py:88-90`) would be crossed on 1 to 7 of 14 files, depending on the crate.

The question that does matter is not measured: does a given Rust extractor keep its text length steady between an original and its Ghostscript rewrite? A crate that loses text after `pdfwrite` rewrites fonts or moves content into form XObjects would reject good compressions, and one that gains text would hide real losses. Measuring this means running `gs` on library PDFs and writing compressed copies somewhere, so it was left for a run with an agreed scratch directory. It is the first test to run before porting `compress`.

## Test render of page 1

Both `hayro` 0.9.0 and `pdfium-render` rendered page 1 at 2× for 6 PDFs. These were picked to have no extractable text on page 1, the case `lookup fulltext --image` exists for: four full-page raster images (JPEG, Flate, and one mixing CCITT fax with JPEG, from a scanned book), one vector cover with outlined text, and one near-blank page. The two PNGs were compared after flattening to greyscale:

| Page type                       | Size     | Pixels differing by >25% | Mean abs diff (0–255) | hayro time | pdfium time |
|---------------------------------|----------|--------------------------|-----------------------|------------|-------------|
| Vector cover, outlined text     | 56.3 MB  | 0.69%                    | 3.13                  | 0.070 s    | 0.042 s     |
| Raster, JPEG                    | 3.2 MB   | 0.77%                    | 4.67                  | 0.062 s    | 0.051 s     |
| Scanned, CCITT fax + JPEG       | 25.9 MB  | 1.50%                    | 6.33                  | 0.088 s    | 0.108 s     |
| Raster, Flate                   | 4.7 MB   | 0.01%                    | 1.78                  | 0.023 s    | 0.034 s     |
| Raster, JPEG                    | 12.6 MB  | 0.01%                    | 0.02                  | 0.015 s    | 0.032 s     |
| Near-blank page                 | 11.6 MB  | 0.01%                    | 0.03                  | 0.017 s    | 0.018 s     |

Visual inspection of overlays for the scanned page and one cover showed the same image, with differences only on anti-aliased edges. Dimensions differ by at most one pixel (1190×1683 against 1191×1684). `hayro` writes RGB; pdfium writes RGBA. No page from a scanned journal article (JBIG2 is the usual codec there) was found among the library's text-less first pages, so JBIG2 is untested; `hayro` ships a decoder for it (`hayro-jbig2` 0.4.0). The rendered PNGs were deleted after comparison.

## Effect on the single-binary goal

- **`pdfium-render` does not include PDFium** (`pdfium-render-0.9.5/README.md:77-83`). By default it binds a dynamic library at run time, from next to the executable or from the system (`README.md:87-124`). For a distributed `zel` that means shipping a `libpdfium.so`/`.dll`/`.dylib` per platform, about 7.6 MB on Linux x64 (the copy in the pypdfium2 wheel), and finding it at run time.
- **Static linking is possible but you build PDFium yourself.** The `static` feature plus `PDFIUM_STATIC_LIB_PATH` links a `libpdfium.a` and enables `Pdfium::bind_to_statically_linked_library()` (`README.md:126-162`), sometimes with the `libstdc++` or `libc++` feature (`README.md:176`). `pdfium-render` will not build PDFium (`README.md:198`), and bblanchon/pdfium-binaries, the usual source of prebuilt PDFium, ships only shared libraries and "don't plan to add static builds"; its `build.sh -s` script supports a static build but is tested only on GitHub's runner OSes (`https://github.com/bblanchon/pdfium-binaries` README, "Static builds"). That is a Chromium-toolchain build per target in CI. Not attempted here.
- **`hayro` keeps the single binary** at the cost of size and MSRV. A stripped binary with `hayro` and `pdf-extract` is 8.96 MB against 2.19 MB for `pdf-extract` alone, so `hayro` adds about 6.8 MB with its embedded fonts and CMaps. For scale, today's stripped `zel` spike binary is 7.76 MB. MSRV would rise from 1.88 to 1.92 (`hayro-0.9.0/Cargo.toml:14`).
- **Ghostscript is outside the binary either way.** `compress` already requires the user to install `gs` (`compress_files.py:36-44`), so `zel` can be a single binary only for everything except `compress`.

## Licences

- The repo is MIT (`LICENSE`).
- `lopdf`, `pdf-extract`, `pdf` – MIT. `pdf_oxide`, `pdfium-render`, `hayro`, `md-5`, `image` – MIT OR Apache-2.0. All are compatible with an MIT binary; the Apache-2.0 option brings a NOTICE obligation only if chosen.
- PDFium – BSD-3-Clause with an Apache-2.0 section (`https://pdfium.googlesource.com/pdfium/+/refs/heads/main/LICENSE`), plus the third-party licences of its bundled libraries (FreeType, libjpeg-turbo, OpenJPEG, lcms, ICU, zlib and others, as listed in the pypdfium2 wheel's `BUILD_LICENSES/`). Shipping PDFium, dynamically or statically, means shipping those notices. Python zelador already ships the same library through the pypdfium2 wheel.
- **MuPDF via `mupdf` – AGPL-3.0** (crates.io licence field; `mupdf-sys` compiles MuPDF from source into the binary, `messense/mupdf-rs` `mupdf-sys/build.rs`). Linking it would make the distributed `zel` binary AGPL, which conflicts with keeping it MIT. Excluded unless Artifex's commercial licence is bought.
- Ghostscript is AGPL-3+ (Debian `ghostscript` package `copyright`; ghostscript.com sends licensing questions to Artifex). zelador runs it as a separate program and does not link it, which is the same arrangement as today.

## Recommendation for the go/no-go

PDF support does not block a port. Every operation has a working Rust equivalent on real library files:

- `lopdf` for page and annotation counts. Count the raw `/Annots` array, not `get_page_annotations`.
- `pdf_oxide` for text: closest to pypdf, MSRV 1.88, but pre-1.0 with a heavy dependency tree. `pdf-extract` is the lighter fallback; if used, wrap it in `catch_unwind` and never take the page count from it.
- `hayro` for the page-1 render, to keep a single binary. This requires accepting MSRV 1.92. Use `pdfium-render` instead only if shipping a `libpdfium` next to the binary is acceptable.
- `md-5` for the digest, and `std::process::Command` for Ghostscript.

Do not use `lopdf::Document::extract_text`, the `pdf` crate's annotation parsing, or `mupdf`.

Two conditions before porting `compress`. First, text length will not match pypdf exactly, so treat it as a per-implementation fact. Record which extractor produced each report (for example an `extractor` field next to `settings`), and never compare text lengths across implementations. Second, before trusting the 2% rule in Rust, run the unmeasured test above: the chosen Rust extractor on originals against their `gs` rewrites, compared with pypdf's verdicts on the same pairs. If that shows extra false rejections, keep `compress` in Python, as #47's step 3 already allows, and port the rest.

## Not verified

- Each Rust extractor's stability across Ghostscript rewrites (above): the main open risk for `compress`.
- Why `lopdf` loses text on files 6, 7 and 11: form XObjects are the likely cause from the source, not confirmed per file.
- The cause of `pdfium-render`'s steady +1–1.5% text offset, and of every crate's −10 to −12% or +7 to +23% on file 10.
- JBIG2 rendering in `hayro`: no JBIG2 page found among the samples.
- Static PDFium linking: not built or tested.
- `mupdf`: not compiled or measured.
- Windows and macOS: everything ran on Linux x64 (WSL2) only.
