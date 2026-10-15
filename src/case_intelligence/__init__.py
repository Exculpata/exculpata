__version__ = "0.1.0b1"

# Install before any store opens a connection. Package import is the first
# step of every application and test import.
from .sqlite_callback_guard import install_sqlite_callback_guard

install_sqlite_callback_guard()
