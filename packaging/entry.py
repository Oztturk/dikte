"""What the AppImage and the disk image start.

dikte/__main__.py is written for a checkout: it puts the directory above the
package on the import path, which a build has neither the need for nor a
directory to point at. What is left over is one thing a checkout never sees.
The Finder hands a double-clicked application a -psn_0_… argument naming the
process serial number, which argparse reads as a flag it has never heard of and
exits over, and no one clicking an icon would ever find out why.

The two environment lines have to run before anything starts another process,
and before is easier to be sure of here than anywhere further in.
"""

import sys

from dikte import integrate
from dikte.app import main

if __name__ == "__main__":
    integrate.restore_library_path()
    integrate.add_bundled_tools()
    sys.argv[1:] = [arg for arg in sys.argv[1:] if not arg.startswith("-psn_")]
    sys.exit(main())
