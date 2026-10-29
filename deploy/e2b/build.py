"""Build the Checkme computer template on E2B.

    cd deploy/e2b && pip install e2b && E2B_API_KEY=... python build.py [alias]

Then set E2B_TEMPLATE=<alias> (default checkme-desktop) on the API service and redeploy. New sandboxes use it;
existing users' sandboxes keep their old template until they are recreated.
"""

import os
import sys

from e2b import Template, default_build_logger

from template import template

alias = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("E2B_TEMPLATE_ALIAS", "checkme-desktop")
Template.build(template, alias=alias, cpu_count=4, memory_mb=8192, on_build_logs=default_build_logger())
print(f"built E2B template {alias}; set E2B_TEMPLATE={alias} on the API service")
