"""Read-only installed-package diagnostic; no application imports or file writes."""
import json
import os
from pathlib import Path
import stat
import sys
import sysconfig


PROOF = {"app_user_non_root": True, "app_root_read_only": True,
         "installed_package_root_owned": True, "installed_package_not_writable": True}


class BoundaryError(RuntimeError):
    pass


def verify_app_user():
    with open('/proc/1/status', encoding='ascii') as stream:
        lines = stream.read(16384).splitlines()
    fields = {line.split(':', 1)[0]: line.split(':', 1)[1].split()
              for line in lines if line.startswith(('Uid:', 'Gid:', 'Groups:'))}
    if os.geteuid() == 0:
        raise BoundaryError('app-user-root')
    if (int(fields['Uid'][1]) != os.geteuid() or int(fields['Gid'][1]) != os.getegid()
            or {int(value) for value in fields['Groups']} != set(os.getgroups())):
        raise BoundaryError('app-user-unverified')


def installed_package():
    # -I -S -B avoids site hooks, application imports and bytecode-cache writes.
    # The production launcher uses this venv interpreter; traceback paths from
    # bytecode are never consulted.
    prefix = str(Path(sys.executable).parent.parent)
    variables = {'base': prefix, 'platbase': prefix}
    directories = {Path(sysconfig.get_path(kind, vars=variables)) / 'case_intelligence'
                   for kind in ('purelib', 'platlib')}
    present = [path for path in directories if path.is_dir()]
    if len(present) != 1 or not (present[0] / '__init__.py').is_file():
        raise BoundaryError('package-unavailable')
    return present[0]


def verify_package(package):
    def protected(path):
        metadata = path.lstat()
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise BoundaryError('package-entry-unsafe')
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise BoundaryError('package-mode-writable')
        if os.access(path, os.W_OK, effective_ids=True):
            raise BoundaryError('package-writable')
        if metadata.st_uid != 0:
            raise BoundaryError('package-not-root-owned')
        if not os.statvfs(path).f_flag & os.ST_RDONLY:
            raise BoundaryError('package-mount-writable')

    protected(package)
    for parent in package.parents:
        protected(parent)
    count = 0
    def unreadable(error):
        raise error
    for directory, directories, files in os.walk(package, followlinks=False, onerror=unreadable):
        for name in directories + files:
            count += 1
            if count > 100000:
                raise BoundaryError('package-inventory-unverified')
            protected(Path(directory) / name)


def verify():
    verify_app_user()
    if not os.statvfs('/').f_flag & os.ST_RDONLY:
        raise BoundaryError('app-root-writable')
    verify_package(installed_package())
    return dict(PROOF)


def main():
    try:
        result = verify()
    except BoundaryError as exc:
        print(json.dumps({'installed_package_verified': False, 'reason': str(exc)}))
        return 1
    except (OSError, ValueError, KeyError, IndexError, TypeError, NotImplementedError):
        print(json.dumps({'installed_package_verified': False, 'reason': 'probe-unavailable'}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
