from uuid import NAMESPACE_URL, uuid5

import pytest

from radar.tenants import TENANTS, runtime_config, tenant_id


@pytest.mark.parametrize("slug", TENANTS)
def test_tenant_spec_is_consistent(slug):
    spec = TENANTS[slug]
    assert spec.slug == slug
    assert sum(spec.billing.values()) == pytest.approx(1.0)
    assert set(spec.source_start) == {"salesforce", "stripe", "zendesk", "product", "gmail"}
    assert tenant_id(slug) == uuid5(NAMESPACE_URL, "radar:" + slug)
    assert runtime_config(slug)["sf"]["csm_field"] == spec.csm_field
