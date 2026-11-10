# Discovery review: Production documents

Production documents is a section under **Review**. It lists every item in the
matter, opens the selected item beside the list, and moves between items from
the keyboard. Everything is read from this install; nothing is sent elsewhere.

## Reading the list

- Each row is one stored item. The standard columns lead with Begin Bates, End
  Bates, name, type, pages, custodian and date.
- Bates numbers, custodians and dates come only from a load file and are shown
  exactly as produced. An item no load file names shows **No Bates**. Nothing
  is computed or guessed.
- A standard column that no item has a value for is hidden, and the page says
  which ones. **Show anyway** brings them back.
- Click a header to sort. Bates and names sort naturally (`ABC2` before
  `ABC10`) and blanks sort last. The filter box matches the shown columns and
  the file name.

## Opening an item

Select a row or press `j`/`k`. The item opens beside the list with up to three
views:

| View | Shows | Offered when |
| --- | --- | --- |
| Document | The stored PDF or image, in the page | The item is a stored PDF, JPEG or PNG |
| Text | Text extracted by the product, one page or section at a time | Extracted text exists |
| Fields | The item's fields, grouped | Always |

Fields shows the chosen fields first, then every load-file field for the item
as produced, then the remaining fields under **More fields**. The Email group
appears only on email items. **Open source page** leads to the full source
page for notes and questions.

Keys: `j`/`↓` next, `k`/`↑` previous, `d` document, `t` text, `f` fields,
`Space` and `Shift+Space` page through text, `/` filter, `c` columns and
fields. Keys are off while typing in a box.

## Load files

Add the production's `.csv` or `.tsv` load file to the matter with its files.
A file counts as a load file when its header row has a recognised path column
or Begin Bates column. Each row attaches to one item:

1. by exact path (either slash direction, any letter case);
2. else by file name, when exactly one item has that name under that path;
3. else by Begin Bates, when exactly one file is named for it.

A row that could name two items attaches to neither. The page reports how many
rows matched, and each item shows the load file, line and how it matched.
Rows past the first for one item are counted and not used.

Concordance DAT and Opticon OPT volumes are not read here yet; that intake is
tracked separately.

## Changing what shows

- **Your own view.** Open **Columns and fields**, tick and order the columns
  and the fields shown first on each item, and save. It applies to you on this
  matter. Load-file headers found in the matter are offered as columns too.
- **The matter's starting view.** Save for everyone on the matter. Reviewers
  without their own view start from it.
- **Reset** removes the saved view and returns to the next one in line: your
  view, then the matter's view, then the standard set.
- **The standard set** for an install is
  `src/case_intelligence/config/discovery_review_fields.json`. It defines each
  field, its group, the load-file headers that feed it, the standard columns
  and the standard item fields. To have a vendor's header fill Custodian, add
  the header to that field's `load_file_headers`. To add a field, add an entry
  with a group and its headers. No code changes. A malformed file stops
  startup with the reason.

Saved views store field keys only. They are deleted with the matter and are
not part of preservation archives.
