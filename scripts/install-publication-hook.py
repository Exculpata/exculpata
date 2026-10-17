#!/usr/bin/env python3
"""Install reviewed publication code outside the branch being inspected."""
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def main():
    root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip()).resolve()
    destination = Path(sys.argv[1]).expanduser().absolute()
    if destination.is_symlink() or destination.exists():
        raise ValueError('Choose a new installation directory outside the checkout.')
    destination = destination.resolve()
    interpreter = Path(sys.executable).absolute()
    if (destination.is_relative_to(root) or interpreter.is_relative_to(root)
            or interpreter.resolve(strict=True).is_relative_to(root)):
        raise ValueError('The installation and Python environment must be outside this checkout.')
    destination.mkdir(parents=True, mode=0o700)
    source = Path(__file__).resolve().parent
    for name in ('pre-push-publication.py', 'publication-check.py',
                 'publication_home_owners.py', 'legacy_name_check.py',
                 'publication-media-reviews.json', 'publication-approvers.json'):
        shutil.copyfile(source / name, destination / name)
    hook = destination / 'pre-push'
    hook.write_text('#!/bin/sh\nset -eu\nexec ' + shlex.quote(str(interpreter)) + ' -I ' +
                    shlex.quote(str(destination / 'pre-push-publication.py')) + ' "$@"\n')
    hook.chmod(0o700)
    subprocess.run(['git', 'config', '--local', 'core.hooksPath', str(destination)], check=True)
    print('Reviewed publication hook installed. Reinstall into a new directory after reviewed updates.')


if __name__ == '__main__':
    main()
