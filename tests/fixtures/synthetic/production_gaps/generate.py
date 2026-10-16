"""Regenerate original synthetic native-text PDFs with standard Helvetica."""
from pathlib import Path
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

ROOT = Path(__file__).parent
for name, lines in {
    'statement.pdf': ['Synthetic production fixture', 'Bates: SYN-0001 to SYN-0003', 'Person: Alex Example; 2034-04-17', 'See Exhibit 12.'],
    'follow-up.pdf': ['Synthetic production fixture', 'Bates: SYN0006', 'Organization: Amber Cooperative; 2034-04-19'],
    'duplicate.pdf': ['Synthetic production fixture', 'Bates: SYN0003', 'The label repeats a label in the synthetic statement.'],
}.items():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(('BT /F1 12 Tf 50 740 Td 18 TL\n' + '\n'.join('(' + line + ') Tj T*' for line in lines) + '\nET').encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(content)
    page.compress_content_streams()
    writer.add_metadata({'/Title': 'Synthetic production gap fixture', '/Author': 'Exculpata synthetic fixtures', '/Producer': 'pypdf'})
    with (ROOT / name).open('wb') as output:
        writer.write(output)
