"""Upload-demo API service.

The HTTP layer around the existing ``tools/`` toolchain. Nothing in here knows how to
build a workbook: ``tools/`` is the single source of truth for that, and this package puts
it on ``sys.path``, feeds it an interchange file, and serves what comes out.

**One thing has to happen before anything else imports openpyxl.** openpyxl switches its
XML serialiser to lxml the moment lxml is importable, and lxml writes the same worksheet
to different bytes. ``python-docx`` depends on lxml, so adding the document parser to this
service silently changed the bytes of two of the four workbooks and broke their identity
with the set the public page already serves. The env var below pins openpyxl back to
ElementTree; ``server.runs`` asserts it took, because a byte drift that only shows up as
"the md5 moved" is exactly the failure this project keeps a byte-identity test for.
"""

from __future__ import annotations

import os

os.environ.setdefault("OPENPYXL_LXML", "False")
