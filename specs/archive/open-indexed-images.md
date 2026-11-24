# Open images named by a production index

## Kickoff

- Goal. From an OPT or LFP page record, or a DII document block, open the actual image file selected with that production. DII image names can be reviewed even when the producer supplied no DAT and the image filenames differ from the Bates label.
- Expected files. Discovery review record service, image-reference reader and presentation, the DII grammar observer where necessary, the record template, operator guide and synthetic HTTP tests.
- Acceptance. Upload two separate productions with repeated names and different original bytes. Navigate a multi-image DII block and standalone OPT/LFP records, including a missing image and TIFF. Follow each available link through the ordinary authorized source page, account for all supplied DII names across bounded windows, and verify that no page labels or implied document associations are created.
- Issue. [Volume import #279](https://github.com/Exculpata/exculpata/issues/279), following the supported image dialects in [#554](https://github.com/Exculpata/exculpata/pull/554).

## Design

The DII parser already validates each supplied image filename, but the saved
document record only reports the count. OPT and LFP retain image paths yet a
reviewer can reach a stored original directly only for certain previews.
Resolve an image reference against authorized upload inventory in the same
receipt and production root. Require one exact relative path and a current
supported image source version. Keep unselected paths inert.

For DII, browse the supplied filenames in small windows on explicit request.
Use the existing validated DII parser over the guarded load-file original.
Bound work by the configured admitted file size and keep only the requested
window in memory. Verify the completed block, its identity and the original
before returning links. A source change or incomplete import invalidates the
request. Keep these file references separate from page records and family
relationships. Link TIFF files to their source page; rendering them belongs
to the separate rendering work in #503.
