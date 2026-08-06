"""Lumen service descriptor for openstacksdk."""

from openstack import service_description

from lumen_sdk.proxy import Proxy


class LumenService(service_description.ServiceDescription):
    def __init__(self):
        super().__init__("lumen", supported_versions={"1": Proxy})
