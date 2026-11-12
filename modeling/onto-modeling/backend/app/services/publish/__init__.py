"""发布（方案 A8）。

spi 定义适配器协议，ontology_package 是知识包适配器（图模式是它的变体），
tree 按提交取只读树，versioning 定版本号，gates 是门禁链，service 串起发布流程。
"""

from . import stages  # noqa: F401
from .service import (  # noqa: F401
    export_package_zip,
    package_binding_check,
    publish_package,
    release_package,
    releases,
    resolve_commit,
    HUB_FIELDS,
    record_hub_release,
    set_object_data_source,
)
