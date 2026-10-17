# Launch video encoding

Both one-minute launch cuts show the fictional Calder Reservoir administrative
inquiry. Their synthetic records, soundtrack rights, and Apache-2.0 license are
documented in [media rights](assets/MEDIA_RIGHTS.md).

## Sources and outputs

Downloaded from the public site on October 17, 2026. The source hashes match the
original assets in commit `47d3287`.

| Cut | Public source | Original bytes | Compressed bytes |
| --- | --- | ---: | ---: |
| Desktop | https://exculpata.com/assets/exculpata-launch-16x9.mp4 | 24,859,316 | 9,511,698 |
| Vertical | https://exculpata.com/assets/exculpata-launch-9x16.mp4 | 14,652,005 | 9,485,512 |

Those source URLs serve the compressed copies after deployment. Original SHA-256:

```text
16x9 764e069b47c7fb122f7907376868e79498594710745247f0e46416e9f59bd7ca
9x16 b82dceab0752282f7442eba2c8630f7f01e4bcd0ec46948c091aba4f8098a62d
```

Compressed SHA-256:

```text
16x9 96649404d4e396ed5281237f4a739c59c34500c897597377e6ba843a3fb1b1f8
9x16 e5d8ef83157756883bd92784d7afa5f44c2a6fd7585eec08a871363f788f4efc
```

## Encoding

FFmpeg 9.0.2 with libx264 on Apple Silicon. Two-pass average-bitrate encoding
at 1,150 kbit/s video plus 96 kbit/s AAC-LC stereo, 48 kHz. Both cuts retain
their original 1920 × 1080 / 1080 × 1920 dimensions, 30 fps and 60-second
duration. H.264 High level 4.0 with yuv420p supports ordinary browser playback;
`+faststart` places the MP4 index before the media. Metadata and chapters are
not copied. No duration truncation or file-size cutoff is used.

Run separately for each cut, with originals and pass logs outside the checkout:

```sh
ffmpeg -i original.mp4 -map 0:v:0 -c:v libx264 -preset slow \
  -b:v 1150k -pix_fmt yuv420p -profile:v high -level:v 4.0 \
  -pass 1 -passlogfile pass-log -an -f null /dev/null
ffmpeg -i original.mp4 -map 0:v:0 -map 0:a:0 \
  -map_metadata -1 -map_chapters -1 -c:v libx264 -preset slow \
  -b:v 1150k -pix_fmt yuv420p -profile:v high -level:v 4.0 \
  -pass 2 -passlogfile pass-log -c:a aac -b:a 96k -ac 2 -ar 48000 \
  -movflags +faststart compressed.mp4
```

The site and documentation asset copies must be byte-identical. GitHub README
playback uses uploaded GitHub video attachments, as the earlier
walkthroughs did; repository links remain available for direct downloads.
The website uses its existing native video controls and same-origin range worker.

## Validation

Both compressed files decode completely with `ffmpeg -v error -xerror -i FILE
-f null -`. File sizes are strictly below the decimal 10,000,000-byte limit.
Full-resolution frames and one-second OCR samples from both source cuts were
reviewed: the visible inquiry, source excerpts, transcript, and review notes are
synthetic demonstration material. The downloaded bytes match the previously
reviewed public sources. Full-resolution compressed text remains readable.

The compressed files pass the publication content and metadata scanner without
media-digest exceptions. Existing historical media dispositions remain unchanged.
Before publication, run the complete history scan and installed pre-push hook,
and inspect attachment and PR text separately. Playback acceptance includes
Chrome native controls, playback progress, seeking, end-of-file, and the rendered
GitHub attachment players; successful decoding alone is not browser acceptance.
