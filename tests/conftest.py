"""Cap BLAS/OpenMP thread pools before numpy is imported anywhere in the suite.

The daemon does this in `rbvor/__main__.py` (amendment A23): OpenBLAS spawns
one worker per core and busy-waits between the tiny matrix products the DSP
issues, which on the Pi showed up as four cores at 100 % for ~4 % of real work.
pytest imports the package without going through `__main__`, so the suite ran
with the same pathology -- measured at ~380 % CPU on a four-core dev machine.
Importing the cap here, first, makes CI cheaper and its timings meaningful.
"""

import rbvor._threadcaps  # noqa: F401  -- must precede any numpy import
