"""Complete binary ordering keys with bounded B-tree prefixes."""
from case_intelligence.modules.discovery_review.reader import sort_key, tie_key

VERSION = 2
PREFIX_BYTES = 128
_DESCENDING = bytes(reversed(range(256)))


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


class RowKeys:
    """Reuse one row's version-two encodings within its owning transaction."""

    def __init__(self, item, parent):
        self.item, self.parent = item, parent
        anchor = (parent or item) if item.family.get('root') else item
        self.tie = encode((*tie_key(anchor), anchor.version_id))
        self.groups = {}
        self.members = {}

    def order(self, chosen, descending, components=None):
        family = components is not None and self.item.family.get('root')
        value = components['group'] if family else sort_key(chosen, self.item)
        if value not in self.groups:
            self.groups[value] = self.tie if value is None else b'\x03' + encode(value) + self.tie + b'\x00'
        group = self.groups[value]
        if descending and value is not None:
            group = group.translate(_DESCENDING)
        member = b''
        if family:
            key = components['member']
            if key not in self.members:
                self.members[key] = encode(key)
            member = self.members[key]
        return (b'\x01' if value is None else b'\x00') + group + member
