"""Complete binary ordering keys with bounded B-tree prefixes."""
from case_intelligence.modules.discovery_review.reader import sort_key, tie_key

VERSION = 2
PREFIX_BYTES = 128


def encode(value):
    if isinstance(value, tuple):
        return b'\x03' + b''.join(encode(part) for part in value) + b'\x00'
    if isinstance(value, int):
        if value < 0:
            raise ValueError('ordering numbers must be nonnegative')
        digits = str(value).encode('ascii')
        return b'\x01' + len(digits).to_bytes(4, 'big') + digits
    return b'\x02' + b''.join((ord(character) + 1).to_bytes(3, 'big') for character in value) + b'\x00\x00\x00'


def order_key(chosen, item, descending=False):
    value = sort_key(chosen, item)
    tie = (*tie_key(item), item.version_id)
    if value is None:
        return b'\x01' + encode(tie)
    complete = encode((value, tie))
    return b'\x00' + (bytes(255 - byte for byte in complete) if descending else complete)


def production_key(chosen, item, parent, components, descending=False):
    if not item.family.get('root'):
        return order_key(chosen, item, descending)
    group = components['group']
    anchor = parent or item
    tie = (*tie_key(anchor), anchor.version_id)
    group_key = encode(tie) if group is None else encode((group, tie))
    if descending and group is not None:
        group_key = bytes(255 - byte for byte in group_key)
    return (b'\x01' if group is None else b'\x00') + group_key + encode(components['member'])
