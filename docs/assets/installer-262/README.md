# Installer evidence — issue 262

Synthetic captures from 2026-10-16. No password, token, hostname, or personal
account name is in these files. The dark canvas is the preview background; the
installer does not set a terminal background.

- [Password, 80 columns](password-80.png) and [50 columns](password-50.png) are
  the real password function on a PTY. The rules print before the first prompt.
  A 12-code-point entry is refused with the count, then the two entries do not
  match, then a valid pair is accepted. The values are not on screen.
  [Plain password text](password-plain.txt) is the same function with
  `NO_COLOR` and `TERM=dumb`. A PTY maps newline to CRLF; those carriage
  returns were removed. No color remains.
- [Build progress, 80](progress-80.png) and [50](progress-50.png) are selected
  lines from the CPU evaluation install console. Elapsed times, `images built`,
  and BuildKit step fractions are the lines the installer printed. Narrow
  frames clip that recorded text. [Install excerpt](install-excerpt.txt) is the
  same selection with color removed, plus the handoff from that run.
- [Up log, 80](up-progress-80.png) and [50](up-progress-50.png) are the fixed
  parser reading this install's private Compose up log. The live process had
  already started and printed `waiting for Docker output` because it did not
  yet accept Compose's aligned spacing. These frames are that log, not a second
  engine run.
- [Node online, 80](node-online-80.png) and [50](node-online-50.png) are the
  install's handoff bytes. Long lines wrap at the frame width.
- [Doctor, plain](doctor-plain.txt) is `./install doctor --root /srv/exculpata --plain`
  after the node was healthy. Exit status 0.
- [Uninstall, plain](uninstall-plain.txt) is a dry run, then
  `./install uninstall --root /srv/exculpata --plain --delete-data --yes`.
  The dry run kept volumes, images, and the root. The delete run removed the
  listed containers, networks, volumes, and namespace images, then removed the
  node contents. `rmdir` of `/srv/exculpata` returned permission denied because
  `/srv` is not writable by the service account. The directory was empty and
  was removed by an administrator. The command now reports that empty-directory
  case instead of stopping on the errno.

The install was CPU-only: `--models none`, `--auth local`,
`--enable-account-management`, `--server-name localhost`,
`--bind-address 127.0.0.1`, `--storage-profile evaluation`, and
`COMPOSE_PARALLEL_LIMIT=1`. The administrator password was supplied on stdin
and is not in the console, doctor, or uninstall output.
