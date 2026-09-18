"""Publish crm@2, crm-advise@1 and crm-case-advise-on-demand@1 from the installed CRM wheel.

    AGENT_RUNTIME_DATABASE_URL=postgresql+asyncpg://... \
    python tools/publish_crm_advise.py --tenant-id <tenant> --actor-id <uuid> \
        --expect-digest 54f9a85048b024ca6b488abce7fc2bd3adb106a363a1c58b8ef4422e701a4095

The same entry point is installed as ``ebiz-crm-advise-publish``. It prints the
published checksum (the value to pin as ``CRM_ADVISOR_WORKFLOW_DIGEST``) and
exits non-zero unless that checksum equals ``--expect-digest``. See
``ebiz_deployment.crm_advise_publication`` for what it does and refuses.
"""

from __future__ import annotations

from ebiz_deployment.crm_advise_publication import main

if __name__ == "__main__":
    raise SystemExit(main())
