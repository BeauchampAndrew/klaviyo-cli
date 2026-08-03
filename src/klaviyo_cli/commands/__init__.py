"""Import command modules so they register on the main group."""
from . import campaigns  # noqa: F401
from . import segments  # noqa: F401
from . import flows  # noqa: F401
from . import metrics  # noqa: F401
# Remaining modules are added in Task 8, e.g.:
# from . import sms, raw  # noqa: F401
